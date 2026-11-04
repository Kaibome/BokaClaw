"""In-process executor for the ``bokaclaw-agent`` ReAct runtime.

The server stays the system of record for conversations: the runtime is
created without a session store and receives the conversation history
explicitly on every turn. This keeps the dashboard's ``conversations`` tables
authoritative instead of duplicating the thread into the SDK's Postgres tables.

The SDK is optional at import time so the server can still start (and use
``AGENT_LOOP_URL`` or mock mode) when ``agent-loop/`` is not installed.
"""

import asyncio
import json
import logging
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple

from app.agent.dev_tools import build_dev_tools
from app.agent.events import (
    AgentCompleted,
    AgentDelta,
    AgentEvent,
    AgentLoopError,
    AgentToolCall,
    AgentToolResult,
)

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = (
    "You are BokaClaw's assistant. Answer in the user's language.\n"
    "Use the provided file, search, command, and web tools when a task requires them. "
    "File paths are relative to the workspace root; never assume a path outside it is accessible. "
    "Do not run destructive commands or overwrite files unless the user explicitly asks.\n"
    "Use search_memories before answering questions about the user's preferences, plans, or "
    "anything they previously asked you to remember. Use save_memory only when the user explicitly "
    "asks you to remember something. Keep answers concise and never invent memories."
)

_SUPPORTED_PROVIDERS = {"openai", "deepseek"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_optional_float(name: str, default: Optional[float] = None) -> Optional[float]:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_optional_int(name: str) -> Optional[int]:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _env_budget(name: str, default: Optional[int]) -> Optional[int]:
    """Read a positive integer budget; ``0`` / ``off`` disables it."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    text = raw.strip().lower()
    if text in {"0", "off", "none", "disabled", "false"}:
        return None
    try:
        value = int(text)
    except ValueError:
        return default
    return value if value > 0 else None


def _env_compression_mode(name: str, default: str = "summary") -> str:
    value = (os.environ.get(name) or default).strip().lower()
    return value if value in {"summary", "truncate"} else default


@dataclass(frozen=True)
class LocalAgentConfig:
    """Model and loop settings for the in-process runtime."""

    model: str
    api_key: str
    base_url: Optional[str]
    temperature: Optional[float]
    max_tokens: Optional[int]
    request_timeout: float
    max_retries: int
    system_prompt: str
    max_iterations: int
    max_seconds: float
    context_max_tokens: Optional[int] = None
    context_keep_recent_tokens: Optional[int] = None
    context_compression: str = "summary"
    tool_result_max_chars: Optional[int] = None


def _current_llm_config() -> Dict[str, Any]:
    """Return mem0's active LLM config, so the agent follows /configure."""
    try:
        from app.core.server_state import get_current_config

        return get_current_config().get("llm") or {}
    except Exception:
        return {}


def _provider_credentials(provider: str, config: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    if provider == "openai":
        api_key = config.get("api_key") or os.getenv("OPENAI_API_KEY")
        base_url = config.get("openai_base_url") or os.getenv("OPENAI_BASE_URL")
        return api_key, base_url
    if provider == "deepseek":
        api_key = config.get("api_key") or os.getenv("DEEPSEEK_API_KEY")
        base_url = config.get("deepseek_base_url") or os.getenv("DEEPSEEK_API_BASE")
        if api_key and not base_url:
            base_url = "https://api.deepseek.com"
        return api_key, base_url
    return None, None


def load_local_config() -> Optional[LocalAgentConfig]:
    """Resolve the in-process runtime settings, or ``None`` when unavailable.

    Explicit ``AGENT_*`` variables win. Otherwise the agent reuses the LLM
    configured for mem0 (provider, model, key, base URL), which means a server
    with a working OpenAI or DeepSeek configuration needs no extra setup.
    """
    llm = _current_llm_config()
    provider = str(llm.get("provider") or "openai")
    llm_config = llm.get("config") or {}

    explicit_model = (os.getenv("AGENT_MODEL") or "").strip() or None
    if provider not in _SUPPORTED_PROVIDERS and not explicit_model:
        return None

    provider_key, provider_base = _provider_credentials(provider, llm_config)
    model = explicit_model or llm_config.get("model")
    api_key = (os.getenv("AGENT_API_KEY") or "").strip() or provider_key
    base_url = (os.getenv("AGENT_BASE_URL") or "").strip() or provider_base

    if not model:
        return None
    if not api_key and not base_url:
        return None
    if base_url and not api_key:
        # Local OpenAI-compatible servers (Ollama, vLLM) often take any key.
        api_key = "local"

    context_max_tokens = _env_budget("AGENT_CONTEXT_MAX_TOKENS", 32_000)
    context_keep_recent_tokens = _env_optional_int("AGENT_CONTEXT_KEEP_RECENT_TOKENS")
    if context_keep_recent_tokens is not None:
        # Keep the SDK config valid even when the environment is misconfigured:
        # the keep window must leave room for the part that gets compressed.
        context_keep_recent_tokens = max(0, context_keep_recent_tokens)
        if context_max_tokens is not None:
            context_keep_recent_tokens = min(context_keep_recent_tokens, max(0, context_max_tokens - 256))

    return LocalAgentConfig(
        model=str(model),
        api_key=str(api_key),
        base_url=base_url,
        temperature=_env_optional_float("AGENT_TEMPERATURE", 0.2),
        max_tokens=_env_optional_int("AGENT_MAX_TOKENS"),
        request_timeout=max(1.0, _env_float("AGENT_REQUEST_TIMEOUT_SECONDS", 60.0)),
        max_retries=max(0, _env_int("AGENT_MAX_RETRIES", 2)),
        system_prompt=(os.getenv("AGENT_SYSTEM_PROMPT") or "").strip() or DEFAULT_SYSTEM_PROMPT,
        max_iterations=max(1, _env_int("AGENT_MAX_ITERATIONS", 10)),
        max_seconds=max(1.0, _env_float("AGENT_LOOP_MAX_SECONDS", 300.0)),
        context_max_tokens=context_max_tokens,
        context_keep_recent_tokens=context_keep_recent_tokens,
        context_compression=_env_compression_mode("AGENT_CONTEXT_COMPRESSION"),
        tool_result_max_chars=_env_budget("AGENT_TOOL_RESULT_MAX_CHARS", 16_000),
    )


def _load_sdk():
    """Import the SDK, falling back to the repository checkout in development."""
    try:
        import bokaclaw_agent

        return bokaclaw_agent
    except ImportError as exc:
        source = Path(__file__).resolve().parent.parent / "agent-loop" / "src"
        if source.is_dir() and str(source) not in sys.path:
            sys.path.insert(0, str(source))
            try:
                import bokaclaw_agent

                return bokaclaw_agent
            except ImportError:
                pass
        raise AgentLoopError(
            "agent_not_configured",
            "The bokaclaw-agent runtime is not installed. Install agent-loop/ or set AGENT_LOOP_URL.",
        ) from exc


def is_locally_configured() -> bool:
    if load_local_config() is None:
        return False
    try:
        _load_sdk()
    except AgentLoopError:
        return False
    return True


def build_messages(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Turn a server payload into the message list sent to the model."""
    messages: List[Dict[str, Any]] = []
    for item in payload.get("history") or []:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role in {"user", "assistant", "system"} and isinstance(content, str):
            messages.append({"role": role, "content": content})

    message = payload.get("message")
    if not isinstance(message, str) or not message.strip():
        raise AgentLoopError("agent_protocol_error", "Agent request is missing a message.")
    messages.append({"role": "user", "content": message})
    return messages


def _memory_tools(sdk, user_id: Optional[str]):
    """Register read/write access to the signed-in user's mem0 memories."""
    if not user_id:
        return []

    def search_memories(query: str, limit: int = 5) -> Dict[str, Any]:
        """Search the user's saved long-term memories.

        Args:
            query: What to look for, in natural language.
            limit: Maximum number of memories to return (1-20).
        """
        from app.core.server_state import get_memory_instance

        bounded = max(1, min(int(limit), 20))
        return get_memory_instance().search(query=query, filters={"user_id": user_id}, top_k=bounded)

    def save_memory(content: str) -> Dict[str, Any]:
        """Save a durable fact about the user to long-term memory.

        Args:
            content: The fact to remember, written as a complete sentence.
        """
        from app.core.server_state import get_memory_instance

        get_memory_instance().add(
            messages=[{"role": "user", "content": content}],
            user_id=user_id,
        )
        return {"status": "saved"}

    return [sdk.tool(search_memories), sdk.tool(save_memory)]


_TOOL_TERMINAL_STATUSES = {"completed", "failed", "aborted"}


def _bridge_tool_event(event: Any, put: Callable[[str, Any], None]) -> None:
    """Map one SDK event to an ``AgentToolCall`` / ``AgentToolResult``.

    Runs on the SDK worker thread, so it only calls ``put``; the queue write is
    hopped back to the event loop by ``stream_local_agent``.
    """
    event_type = getattr(getattr(event, "type", None), "value", getattr(event, "type", None))
    if event_type not in {"tool_call", "tool_result"}:
        return

    data = getattr(event, "data", None) or {}
    if not isinstance(data, dict):
        return
    tool_call_id = data.get("tool_call_id")
    name = data.get("name")
    if not isinstance(tool_call_id, str) or not tool_call_id:
        return
    if not isinstance(name, str) or not name:
        return

    iteration = data.get("iteration") or getattr(event, "iteration", 0)
    try:
        iteration = int(iteration)
    except (TypeError, ValueError):
        iteration = 0

    if event_type == "tool_call":
        arguments = data.get("arguments")
        raw_arguments = data.get("raw_arguments")
        arguments_error = data.get("arguments_error")
        put(
            "tool_call",
            AgentToolCall(
                tool_call_id=tool_call_id,
                name=name,
                iteration=iteration,
                arguments=arguments if isinstance(arguments, dict) else {},
                raw_arguments=raw_arguments if isinstance(raw_arguments, str) else None,
                arguments_error=arguments_error if isinstance(arguments_error, str) else None,
            ),
        )
        return

    content = data.get("content")
    if content is None:
        content = ""
    elif not isinstance(content, str):
        try:
            content = json.dumps(content, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            content = str(content)

    status = data.get("status")
    if status not in _TOOL_TERMINAL_STATUSES:
        status = "failed" if bool(data.get("is_error")) else "completed"

    duration_ms = data.get("duration_ms")
    try:
        duration_ms = int(duration_ms) if duration_ms is not None else None
    except (TypeError, ValueError):
        duration_ms = None

    put(
        "tool_result",
        AgentToolResult(
            tool_call_id=tool_call_id,
            name=name,
            content=content,
            is_error=bool(data.get("is_error")),
            iteration=iteration,
            status=status,
            duration_ms=duration_ms,
        ),
    )


def build_runtime(
    sdk,
    config: LocalAgentConfig,
    payload: Dict[str, Any],
    *,
    on_event: Optional[Callable[[Any], None]] = None,
):
    model_config = sdk.ModelConfig(
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
        timeout=config.request_timeout,
        max_retries=config.max_retries,
    )
    agent_config = sdk.AgentConfig(
        name="bokaclaw-agent",
        system_prompt=config.system_prompt,
        max_iterations=config.max_iterations,
        model=model_config,
        context_max_tokens=config.context_max_tokens,
        context_keep_recent_tokens=config.context_keep_recent_tokens,
        context_compression=config.context_compression,
        tool_result_max_chars=config.tool_result_max_chars,
    )
    tools = [*_memory_tools(sdk, payload.get("user_id")), *build_dev_tools(sdk)]
    return sdk.AgentRuntime(agent_config, tools=tools, on_event=on_event)


def _completion_metadata(payload: Dict[str, Any], result: Any) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {}
    usage = getattr(result, "usage", None)
    if usage:
        metadata["usage"] = usage
    stop_reason = getattr(result, "stop_reason", None)
    if stop_reason:
        metadata["finish_reason"] = stop_reason
    if payload.get("run_id"):
        metadata["run_id"] = payload["run_id"]
    return metadata


async def stream_local_agent(
    payload: Dict[str, Any],
    *,
    cancel_event: threading.Event | None = None,
    max_seconds: float | None = None,
) -> AsyncIterator[AgentEvent]:
    """Run one ReAct turn, streaming the SDK's text deltas as they arrive.

    The SDK is synchronous, so the turn runs in a worker thread and pushes
    deltas into an asyncio queue. Tool-call rounds produce no text; the final
    answer is forwarded chunk by chunk. ``cancel_event`` lets a background run
    stop at the next iteration boundary without waiting for the turn to end.
    """
    config = load_local_config()
    if config is None:
        raise AgentLoopError(
            "agent_not_configured",
            "Agent execution is not configured. Set AGENT_MODEL and AGENT_API_KEY (or OPENAI_API_KEY).",
        )

    sdk = _load_sdk()
    messages = build_messages(payload)
    conversation_id = payload.get("conversation_id")

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Tuple[str, Any]] = asyncio.Queue()
    stop = threading.Event()

    def put(kind: str, value: Any = None) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, (kind, value))
        except RuntimeError:  # pragma: no cover - the loop is already closed
            pass

    def on_event(event: Any) -> None:
        _bridge_tool_event(event, put)

    runtime = build_runtime(sdk, config, payload, on_event=on_event)

    def worker() -> None:
        try:
            kwargs: Dict[str, Any] = {
                "session_id": conversation_id,
                "user_id": payload.get("user_id"),
            }
            if cancel_event is not None:
                kwargs["cancel_event"] = cancel_event
            turn = runtime.stream(messages, **kwargs)
            while not stop.is_set():
                if cancel_event is not None and cancel_event.is_set():
                    break
                try:
                    delta = next(turn)
                except StopIteration as finished:
                    result = finished.value
                    if stop.is_set() or (cancel_event is not None and cancel_event.is_set()):
                        return
                    if not (getattr(result, "content", None) or "").strip():
                        put("error", AgentLoopError("agent_protocol_error", "Agent produced no content."))
                        return
                    put("completed", result)
                    return
                if stop.is_set() or (cancel_event is not None and cancel_event.is_set()):
                    break
                put("delta", delta)
            turn.close()
            if cancel_event is not None and cancel_event.is_set():
                put("cancelled")
        except sdk.MaxIterationsExceeded:
            put("error", AgentLoopError("agent_upstream_error", "Agent exceeded the maximum number of steps."))
        except sdk.ConfigurationError as exc:
            put("error", AgentLoopError("agent_not_configured", str(exc)))
        except Exception as exc:
            if type(exc).__name__ == "RunCancelled":
                put("cancelled")
                return
            logger.exception("Agent runtime failed for conversation %s", conversation_id)
            put("error", AgentLoopError("agent_upstream_error", f"Agent runtime failed: {exc}"))
        finally:
            try:
                runtime.close()
            except Exception:  # pragma: no cover - defensive
                logger.warning("Failed to close the agent runtime", exc_info=True)
            put("end")

    thread = threading.Thread(
        target=worker,
        name=f"agent-{conversation_id or 'turn'}",
        daemon=True,
    )
    thread.start()

    deadline = loop.time() + (max_seconds if max_seconds is not None else config.max_seconds)
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise AgentLoopError("agent_cancelled", "Agent run was cancelled.")
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise AgentLoopError("agent_timeout", "Agent turn timed out.")
            timeout = min(remaining, 0.5) if cancel_event is not None else remaining
            try:
                kind, value = await asyncio.wait_for(queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                if cancel_event is not None and cancel_event.is_set():
                    raise AgentLoopError("agent_cancelled", "Agent run was cancelled.") from None
                if loop.time() >= deadline:
                    raise AgentLoopError("agent_timeout", "Agent turn timed out.") from None
                continue

            if kind == "delta":
                yield AgentDelta(value)
            elif kind == "completed":
                yield AgentCompleted(metadata=_completion_metadata(payload, value))
            elif kind == "tool_call":
                yield value
            elif kind == "tool_result":
                yield value
            elif kind == "error":
                raise value
            elif kind == "cancelled":
                raise AgentLoopError("agent_cancelled", "Agent run was cancelled.")
            elif kind == "end":
                return
    finally:
        stop.set()
        if cancel_event is not None:
            cancel_event.set()
        try:
            runtime.close()
        except Exception:  # pragma: no cover - defensive
            pass
