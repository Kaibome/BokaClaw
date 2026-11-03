"""In-process executor for the ``bokaclaw-agent`` ReAct runtime.

The server stays the system of record for conversations: the runtime is
created without a session store and receives the conversation history
explicitly on every turn. This keeps the dashboard's ``conversations`` tables
authoritative instead of duplicating the thread into the SDK's Postgres tables.

The SDK is optional at import time so the server can still start (and use
``AGENT_LOOP_URL`` or mock mode) when ``agent-loop/`` is not installed.
"""

import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from agent_dev_tools import build_dev_tools
from agent_events import AgentCompleted, AgentDelta, AgentEvent, AgentLoopError

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


def _current_llm_config() -> Dict[str, Any]:
    """Return mem0's active LLM config, so the agent follows /configure."""
    try:
        from server_state import get_current_config

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
        from server_state import get_memory_instance

        bounded = max(1, min(int(limit), 20))
        return get_memory_instance().search(query=query, filters={"user_id": user_id}, top_k=bounded)

    def save_memory(content: str) -> Dict[str, Any]:
        """Save a durable fact about the user to long-term memory.

        Args:
            content: The fact to remember, written as a complete sentence.
        """
        from server_state import get_memory_instance

        get_memory_instance().add(
            messages=[{"role": "user", "content": content}],
            user_id=user_id,
        )
        return {"status": "saved"}

    return [sdk.tool(search_memories), sdk.tool(save_memory)]


def build_runtime(sdk, config: LocalAgentConfig, payload: Dict[str, Any]):
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
    )
    tools = [*_memory_tools(sdk, payload.get("user_id")), *build_dev_tools(sdk)]
    return sdk.AgentRuntime(agent_config, tools=tools)


async def stream_local_agent(payload: Dict[str, Any]) -> AsyncIterator[AgentEvent]:
    """Run one ReAct turn in a worker thread and emit its final answer.

    The SDK does not stream tokens yet, so a completed turn produces one
    ``message.delta`` followed by ``message.completed`` metadata.
    """
    config = load_local_config()
    if config is None:
        raise AgentLoopError(
            "agent_not_configured",
            "Agent execution is not configured. Set AGENT_MODEL and AGENT_API_KEY (or OPENAI_API_KEY).",
        )

    sdk = _load_sdk()
    messages = build_messages(payload)
    runtime = build_runtime(sdk, config, payload)
    conversation_id = payload.get("conversation_id")

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(
                runtime.run,
                messages,
                session_id=conversation_id,
                user_id=payload.get("user_id"),
            ),
            timeout=config.max_seconds,
        )
    except asyncio.TimeoutError as exc:
        raise AgentLoopError("agent_timeout", "Agent turn timed out.") from exc
    except sdk.MaxIterationsExceeded as exc:
        raise AgentLoopError("agent_upstream_error", "Agent exceeded the maximum number of steps.") from exc
    except sdk.ConfigurationError as exc:
        raise AgentLoopError("agent_not_configured", str(exc)) from exc
    except Exception as exc:
        logger.exception("Agent runtime failed for conversation %s", conversation_id)
        raise AgentLoopError("agent_upstream_error", f"Agent runtime failed: {exc}") from exc
    finally:
        try:
            runtime.close()
        except Exception:  # pragma: no cover - defensive
            logger.warning("Failed to close the agent runtime", exc_info=True)

    content = (result.content or "").strip()
    if not content:
        raise AgentLoopError("agent_protocol_error", "Agent produced no content.")

    yield AgentDelta(content)

    metadata: Dict[str, Any] = {}
    if result.usage:
        metadata["usage"] = result.usage
    if result.stop_reason:
        metadata["finish_reason"] = result.stop_reason
    if payload.get("run_id"):
        metadata["run_id"] = payload["run_id"]
    yield AgentCompleted(metadata=metadata)
