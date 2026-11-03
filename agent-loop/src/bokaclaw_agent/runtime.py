"""The ReAct agent runtime."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Iterable
from typing import Any
from uuid import uuid4

from .config import AgentConfig
from .context import ContextManager, assistant_message, normalize_input, tool_message
from .exceptions import AgentError, ConfigurationError, MaxIterationsExceeded, ModelError
from .providers.base import ModelProvider
from .providers.openai_compatible import OpenAICompatibleProvider
from .sessions.base import SessionRecord, SessionStore
from .tools import Tool, ToolRegistry
from .types import AgentEvent, EventType, ModelResponse, RunResult, ToolCall, ToolResult
from .utils import resolve_awaitable

logger = logging.getLogger(__name__)


def _map_stop_reason(finish_reason: str | None) -> str:
    if finish_reason == "length":
        return "length"
    if finish_reason == "content_filter":
        return "content_filter"
    return "final"


class AgentRuntime:
    """Execute a ReAct loop and persist each run to a session store.

    Example::

        @tool
        def get_weather(city: str) -> str:
            \"\"\"Return the weather for a city.\"\"\"
            return f"{city}: sunny"

        runtime = AgentRuntime(
            AgentConfig(model=ModelConfig(model="gpt-4o-mini")),
            tools=[get_weather],
            store=InMemorySessionStore(),
        )
        result = runtime.run("What's the weather in Beijing?")

    The same ``session_id`` resumes a conversation; messages are loaded from
    the store before the new turn and written back once at the end of the run.
    """

    def __init__(
        self,
        config: AgentConfig,
        *,
        tools: Iterable[Tool | Callable[..., Any]] = (),
        provider: ModelProvider | None = None,
        store: SessionStore | None = None,
        on_event: Callable[[AgentEvent], Any] | None = None,
    ) -> None:
        self.config = config
        if provider is None:
            if config.model is None:
                raise ConfigurationError("Pass provider=... or set AgentConfig.model when constructing AgentRuntime")
            provider = OpenAICompatibleProvider(config.model)
        self.provider = provider
        self.store = store
        self.on_event = on_event
        self.tools = ToolRegistry(tools)

    def register_tool(self, item: Tool | Callable[..., Any]) -> Tool:
        """Register a tool after construction and return it."""

        return self.tools.register(item)

    def run(
        self,
        input: str | list[dict[str, Any]],
        *,
        session_id: str | None = None,
        user_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        **model_kwargs: Any,
    ) -> RunResult:
        """Run the ReAct loop once and return the final assistant turn.

        Args:
            input: A user string or a list of OpenAI wire-format messages.
            session_id: Existing session to resume, or a new id. Generated
                when omitted.
            user_id: Optional owner recorded on the session row.
            metadata: Session metadata merged into the existing JSONB object.
            **model_kwargs: Forwarded to the provider's ``complete``.
        """

        user_messages = normalize_input(input)
        session_id = session_id or uuid4().hex
        context = ContextManager(self.config.system_prompt, self.config.max_history_messages)
        new_messages: list[dict[str, Any]] = []
        tool_results: list[ToolResult] = []
        pending_calls: list[ToolCall] = []
        usage: dict[str, int] = {}
        iterations = 0
        stop_reason = "final"
        content: str | None = None

        self._emit(
            EventType.RUN_START,
            session_id,
            0,
            {"input": input if isinstance(input, str) else "[messages]"},
        )

        try:
            history = self.store.load_messages(session_id) if self.store is not None else []
            context.load_history(history)

            for message in user_messages:
                context.append(message)
                new_messages.append(message)

            for iteration in range(1, self.config.max_iterations + 1):
                iterations = iteration
                messages = context.build()
                tool_specs = self.tools.specs()
                self._emit(
                    EventType.MODEL_REQUEST,
                    session_id,
                    iteration,
                    {"messages": messages, "tools": tool_specs},
                )
                response = self._complete(messages, **model_kwargs)
                self._merge_usage(usage, response.usage)
                self._emit(
                    EventType.MODEL_RESPONSE,
                    session_id,
                    iteration,
                    {
                        "content": response.content,
                        "tool_calls": [call.model_dump() for call in response.tool_calls],
                        "finish_reason": response.finish_reason,
                    },
                )

                assistant = assistant_message(response)
                context.append(assistant)
                new_messages.append(assistant)
                content = response.content

                if not response.tool_calls:
                    stop_reason = _map_stop_reason(response.finish_reason)
                    break

                for index, call in enumerate(response.tool_calls):
                    pending_calls = response.tool_calls[index:]
                    self._emit(
                        EventType.TOOL_CALL,
                        session_id,
                        iteration,
                        {
                            "tool_call_id": call.id,
                            "name": call.name,
                            "arguments": call.arguments,
                        },
                    )
                    result = self.tools.invoke(call, error_mode=self.config.tool_error_mode)
                    tool_results.append(result)
                    self._emit(
                        EventType.TOOL_RESULT,
                        session_id,
                        iteration,
                        {
                            "tool_call_id": result.tool_call_id,
                            "name": result.name,
                            "content": result.content,
                            "is_error": result.is_error,
                        },
                    )
                    message = tool_message(result)
                    context.append(message)
                    new_messages.append(message)
                    pending_calls = response.tool_calls[index + 1 :]
                pending_calls = []
            else:
                result = self._build_result(
                    session_id,
                    content,
                    "max_iterations",
                    new_messages,
                    tool_results,
                    iterations,
                    usage,
                )
                self._persist_best_effort(
                    session_id,
                    new_messages,
                    user_id,
                    metadata,
                    status="failed",
                    error="max_iterations exceeded",
                )
                self._emit(EventType.RUN_END, session_id, iterations, {"stop_reason": "max_iterations"})
                raise MaxIterationsExceeded(session_id, result)

            result = self._build_result(
                session_id,
                content,
                stop_reason,
                new_messages,
                tool_results,
                iterations,
                usage,
            )
            self._persist(session_id, new_messages, user_id, metadata, status="completed")
            self._emit(EventType.RUN_END, session_id, iterations, {"stop_reason": stop_reason})
            return result

        except MaxIterationsExceeded:
            raise
        except Exception as exc:
            self._handle_failure(
                session_id,
                new_messages,
                context,
                pending_calls,
                tool_results,
                user_id,
                metadata,
                exc,
            )
            self._emit(
                EventType.ERROR,
                session_id,
                iterations,
                {"error": str(exc), "type": type(exc).__name__},
            )
            raise

    async def arun(
        self,
        input: str | list[dict[str, Any]],
        *,
        session_id: str | None = None,
        user_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        **model_kwargs: Any,
    ) -> RunResult:
        """Async wrapper around :meth:`run`; safe for async tools and frameworks."""

        return await asyncio.to_thread(
            self.run,
            input,
            session_id=session_id,
            user_id=user_id,
            metadata=metadata,
            **model_kwargs,
        )

    def get_session(self, session_id: str) -> SessionRecord | None:
        if self.store is None:
            return None
        return self.store.get_session(session_id)

    def list_sessions(
        self,
        *,
        user_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SessionRecord]:
        if self.store is None:
            return []
        return self.store.list_sessions(user_id=user_id, limit=limit, offset=offset)

    def delete_session(self, session_id: str) -> None:
        if self.store is not None:
            self.store.delete_session(session_id)

    def close(self) -> None:
        if self.store is not None:
            self.store.close()

    async def aclose(self) -> None:
        await asyncio.to_thread(self.close)

    def _complete(self, messages: list[dict[str, Any]], **model_kwargs: Any) -> ModelResponse:
        try:
            response = self.provider.complete(
                messages,
                tools=self.tools.specs(),
                tool_choice=self.config.tool_choice,
                **model_kwargs,
            )
            response = resolve_awaitable(response)
        except AgentError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize provider errors
            raise ModelError(f"Model request failed: {exc}") from exc

        if isinstance(response, dict):
            try:
                response = ModelResponse.model_validate(response)
            except Exception as exc:  # noqa: BLE001
                raise ModelError(f"Provider returned an invalid response: {exc}") from exc
        if not isinstance(response, ModelResponse):
            raise ModelError(f"Provider returned {type(response).__name__}, expected ModelResponse")
        return response

    @staticmethod
    def _merge_usage(total: dict[str, int], usage: dict[str, int] | None) -> None:
        for key, value in (usage or {}).items():
            total[key] = total.get(key, 0) + int(value or 0)

    @staticmethod
    def _build_result(
        session_id: str,
        content: str | None,
        stop_reason: str,
        new_messages: list[dict[str, Any]],
        tool_results: list[ToolResult],
        iterations: int,
        usage: dict[str, int],
    ) -> RunResult:
        return RunResult(
            session_id=session_id,
            content=content,
            stop_reason=stop_reason,
            new_messages=list(new_messages),
            tool_results=list(tool_results),
            iterations=iterations,
            usage=dict(usage),
        )

    def _persist(
        self,
        session_id: str,
        new_messages: list[dict[str, Any]],
        user_id: str | None,
        metadata: dict[str, Any] | None,
        *,
        status: str,
        error: str | None = None,
    ) -> None:
        if self.store is None:
            return
        self.store.persist_run(
            session_id,
            new_messages,
            agent_name=self.config.name,
            user_id=user_id,
            metadata=metadata,
            status=status,
            error=error,
        )

    def _persist_best_effort(
        self,
        session_id: str,
        new_messages: list[dict[str, Any]],
        user_id: str | None,
        metadata: dict[str, Any] | None,
        *,
        status: str,
        error: str | None = None,
    ) -> None:
        try:
            self._persist(session_id, new_messages, user_id, metadata, status=status, error=error)
        except Exception:  # noqa: BLE001 - never mask the original failure
            logger.exception("Could not persist failed agent session %s", session_id)

    def _handle_failure(
        self,
        session_id: str,
        new_messages: list[dict[str, Any]],
        context: ContextManager,
        pending_calls: list[ToolCall],
        tool_results: list[ToolResult],
        user_id: str | None,
        metadata: dict[str, Any] | None,
        exc: Exception,
    ) -> None:
        # Keep the persisted conversation valid: every assistant tool call must
        # be followed by a tool message, even when the run aborted mid-loop.
        for call in pending_calls:
            result = ToolResult(
                tool_call_id=call.id,
                name=call.name,
                content=json.dumps({"error": f"run aborted: {exc}"}, ensure_ascii=False),
                is_error=True,
            )
            tool_results.append(result)
            message = tool_message(result)
            context.append(message)
            new_messages.append(message)

        self._persist_best_effort(
            session_id,
            new_messages,
            user_id,
            metadata,
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
        )

    def _emit(
        self,
        event_type: EventType,
        session_id: str,
        iteration: int,
        data: dict[str, Any],
    ) -> None:
        if self.on_event is None:
            return
        event = AgentEvent(
            type=event_type,
            session_id=session_id,
            iteration=iteration,
            data=data,
        )
        try:
            resolve_awaitable(self.on_event(event))
        except Exception:  # noqa: BLE001 - observer failures must not stop a run
            logger.exception("Agent event callback failed for %s", event_type.value)
