"""The ReAct agent runtime."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable, Generator, Iterable, Iterator
from typing import Any, NoReturn
from uuid import uuid4

from ..core.config import AgentConfig
from ..core.exceptions import AgentError, ConfigurationError, MaxIterationsExceeded, ModelError, RunCancelled
from ..core.types import (
    AgentEvent,
    EventType,
    ModelResponse,
    ModelStreamChunk,
    RunResult,
    ToolCall,
    ToolCallDelta,
    ToolResult,
)
from ..core.utils import parse_tool_arguments, resolve_awaitable
from ..providers.base import ModelProvider
from ..providers.openai_compatible import OpenAICompatibleProvider
from ..sessions.base import SessionRecord, SessionStore
from ..tools import Tool, ToolRegistry
from .compression import ContextCompressor
from .context import (
    ContextManager,
    assistant_message,
    estimate_tokens,
    normalize_input,
    tool_message,
    truncate_text,
)

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
        self._event_seq = 0

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
        cancel_event: threading.Event | None = None,
        **model_kwargs: Any,
    ) -> RunResult:
        """Run the ReAct loop once and return the final assistant turn.

        Args:
            input: A user string or a list of OpenAI wire-format messages.
            session_id: Existing session to resume, or a new id. Generated
                when omitted.
            user_id: Optional owner recorded on the session row.
            metadata: Session metadata merged into the existing JSONB object.
            cancel_event: Cooperative cancellation signal checked at safe
                boundaries. When set, the run stops and raises
                :class:`~bokaclaw_agent.exceptions.RunCancelled`.
            **model_kwargs: Forwarded to the provider's ``complete``.

        Raises:
            RunCancelled: ``cancel_event`` was set at a safe boundary.
        """

        turn = self._execute(
            input,
            session_id=session_id,
            user_id=user_id,
            metadata=metadata,
            stream=False,
            model_kwargs=model_kwargs,
            cancel_event=cancel_event,
        )
        while True:
            try:
                next(turn)
            except StopIteration as stop:
                return stop.value

    def stream(
        self,
        input: str | list[dict[str, Any]],
        *,
        session_id: str | None = None,
        user_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        cancel_event: threading.Event | None = None,
        **model_kwargs: Any,
    ) -> Generator[str, None, RunResult]:
        """Run the ReAct loop, yielding assistant text deltas as they arrive.

        The final :class:`RunResult` is returned when the generator is
        exhausted. Providers that do not implement ``stream_complete`` are
        supported through a one-chunk fallback. Closing the generator before
        exhaustion abandons the turn without persisting it; that is not
        cancellation.

        Args:
            input: A user string or a list of OpenAI wire-format messages.
            session_id: Existing session to resume, or a new id. Generated
                when omitted.
            user_id: Optional owner recorded on the session row.
            metadata: Session metadata merged into the existing JSONB object.
            cancel_event: Cooperative cancellation signal checked between the
                streaming chunks and the tool calls, never inside the chunk
                loop. When set, the run stops and raises
                :class:`~bokaclaw_agent.exceptions.RunCancelled`.
            **model_kwargs: Forwarded to the provider's ``stream_complete``.

        Raises:
            RunCancelled: ``cancel_event`` was set at a safe boundary.
        """

        turn = self._execute(
            input,
            session_id=session_id,
            user_id=user_id,
            metadata=metadata,
            stream=True,
            model_kwargs=model_kwargs,
            cancel_event=cancel_event,
        )
        while True:
            try:
                delta = next(turn)
            except StopIteration as stop:
                return stop.value
            yield delta

    def _execute(
        self,
        input: str | list[dict[str, Any]],
        *,
        session_id: str | None,
        user_id: str | None,
        metadata: dict[str, Any] | None,
        stream: bool,
        model_kwargs: dict[str, Any],
        cancel_event: threading.Event | None = None,
    ) -> Generator[str, None, RunResult]:
        """Shared ReAct loop. Yields text deltas only when ``stream`` is true."""

        user_messages = normalize_input(input)
        session_id = session_id or uuid4().hex
        context = ContextManager(
            self.config.system_prompt,
            self.config.max_history_messages,
            tool_result_max_chars=self.config.tool_result_max_chars,
        )
        compressor = None
        new_messages: list[dict[str, Any]] = []
        tool_results: list[ToolResult] = []
        pending_calls: list[ToolCall] = []
        usage: dict[str, int] = {}
        if self.config.context_max_tokens is not None:
            compressor = ContextCompressor(self.config, self.provider, usage)
        iterations = 0
        stop_reason = "final"
        content: str | None = None

        # Event sequence numbers are scoped to one run and restart at 1.
        self._event_seq = 0
        self._emit(
            EventType.RUN_START,
            session_id,
            0,
            {"input": input if isinstance(input, str) else "[messages]"},
        )

        def check_cancelled(iteration: int) -> None:
            if cancel_event is not None and cancel_event.is_set():
                self._cancel_run(
                    session_id=session_id,
                    new_messages=new_messages,
                    context=context,
                    pending_calls=pending_calls,
                    tool_results=tool_results,
                    user_id=user_id,
                    metadata=metadata,
                    iteration=iteration,
                    content=content,
                    usage=usage,
                )

        try:
            history = self.store.load_messages(session_id) if self.store is not None else []
            context.load_history(history)

            for message in user_messages:
                context.append(message)
                new_messages.append(message)

            for iteration in range(1, self.config.max_iterations + 1):
                iterations = iteration
                # Cooperative cancellation: before each ReAct iteration.
                check_cancelled(iteration)
                messages = context.build()
                tool_specs = self.tools.specs()
                if compressor is not None:
                    # Tool schemas are part of the request; reserve their
                    # estimated cost so compression applies to the messages
                    # the model receives.
                    tools_tokens = estimate_tokens(json.dumps(tool_specs, ensure_ascii=False, default=str))
                    compressed = compressor.compress(messages, tools_tokens=tools_tokens)
                    if compressed.changed:
                        messages = compressed.messages
                        self._emit(
                            EventType.CONTEXT_COMPRESSED,
                            session_id,
                            iteration,
                            compressed.stats,
                        )
                self._emit(
                    EventType.MODEL_REQUEST,
                    session_id,
                    iteration,
                    {"messages": messages, "tools": tool_specs},
                )
                if stream:
                    response = yield from self._stream_response(messages, usage, model_kwargs)
                else:
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

                # Cooperative cancellation: after the model response and
                # before this response's tool calls execute. Never inside the
                # streaming chunk loop, so the assistant message is complete.
                pending_calls = list(response.tool_calls)
                check_cancelled(iteration)

                if not response.tool_calls:
                    stop_reason = _map_stop_reason(response.finish_reason)
                    break

                for index, call in enumerate(response.tool_calls):
                    pending_calls = response.tool_calls[index:]
                    # Cooperative cancellation: before each tool call.
                    check_cancelled(iteration)
                    self._emit(
                        EventType.TOOL_CALL,
                        session_id,
                        iteration,
                        {
                            "tool_call_id": call.id,
                            "name": call.name,
                            "arguments": call.arguments,
                            "raw_arguments": call.raw_arguments,
                            "arguments_error": call.arguments_error,
                            "status": "running",
                        },
                    )
                    result = self.tools.invoke(call, error_mode=self.config.tool_error_mode)
                    tool_results.append(result)
                    # Events and the model view carry the capped copy; the
                    # persisted message and RunResult keep the full result.
                    view_content, truncated = truncate_text(
                        result.content,
                        self.config.tool_result_max_chars,
                    )
                    self._emit(
                        EventType.TOOL_RESULT,
                        session_id,
                        iteration,
                        {
                            "tool_call_id": result.tool_call_id,
                            "name": result.name,
                            "content": view_content,
                            "truncated": truncated,
                            "original_chars": len(result.content),
                            "is_error": result.is_error,
                            "status": "failed" if result.is_error else "completed",
                        },
                    )
                    message = tool_message(result)
                    context.append(message)
                    new_messages.append(message)
                    pending_calls = response.tool_calls[index + 1 :]
                    # Cooperative cancellation: after each tool call, before
                    # the next call or the next ReAct iteration.
                    check_cancelled(iteration)
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
        except RunCancelled:
            # Already filled aborted tool messages, persisted as cancelled,
            # and emitted run_ended inside _cancel_run.
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
                iterations,
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
        cancel_event: threading.Event | None = None,
        **model_kwargs: Any,
    ) -> RunResult:
        """Async wrapper around :meth:`run`; safe for async tools and frameworks."""

        return await asyncio.to_thread(
            self.run,
            input,
            session_id=session_id,
            user_id=user_id,
            metadata=metadata,
            cancel_event=cancel_event,
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

    def _stream_response(
        self,
        messages: list[dict[str, Any]],
        usage: dict[str, int],
        model_kwargs: dict[str, Any],
    ) -> Generator[str, None, ModelResponse]:
        """Consume provider chunks, forwarding text and reassembling tool calls."""

        content_parts: list[str] = []
        call_parts: dict[int, dict[str, str]] = {}
        finish_reason: str | None = None

        for chunk in self._stream_complete(messages, **model_kwargs):
            if chunk.content:
                content_parts.append(chunk.content)
                yield chunk.content
            for delta in chunk.tool_calls:
                entry = call_parts.setdefault(delta.index, {"id": "", "name": "", "arguments": ""})
                if delta.id:
                    entry["id"] = delta.id
                if delta.name:
                    entry["name"] += delta.name
                entry["arguments"] += delta.arguments
            if chunk.finish_reason:
                finish_reason = chunk.finish_reason
            self._merge_usage(usage, chunk.usage)

        return ModelResponse(
            content="".join(content_parts) or None,
            tool_calls=self._tool_calls_from_parts(call_parts),
            finish_reason=finish_reason,
        )

    def _stream_complete(self, messages: list[dict[str, Any]], **model_kwargs: Any) -> Iterator[ModelStreamChunk]:
        stream_method = getattr(self.provider, "stream_complete", None)
        if stream_method is None:
            response = self._complete(messages, **model_kwargs)
            tool_calls = [
                ToolCallDelta(
                    index=index,
                    id=call.id,
                    name=call.name,
                    arguments=call.raw_arguments
                    if call.raw_arguments is not None
                    else json.dumps(call.arguments, ensure_ascii=False),
                )
                for index, call in enumerate(response.tool_calls)
            ]
            yield ModelStreamChunk(
                content=response.content,
                tool_calls=tool_calls,
                finish_reason=response.finish_reason,
                usage=response.usage,
            )
            return

        try:
            chunks = stream_method(
                messages,
                tools=self.tools.specs(),
                tool_choice=self.config.tool_choice,
                **model_kwargs,
            )
            for chunk in chunks:
                if isinstance(chunk, dict):
                    chunk = ModelStreamChunk.model_validate(chunk)
                if not isinstance(chunk, ModelStreamChunk):
                    raise ModelError(f"Provider streamed {type(chunk).__name__}, expected ModelStreamChunk")
                yield chunk
        except AgentError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize provider errors
            raise ModelError(f"Model request failed: {exc}") from exc

    @staticmethod
    def _tool_calls_from_parts(parts: dict[int, dict[str, str]]) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for index in sorted(parts):
            entry = parts[index]
            if not entry["name"]:
                continue
            arguments, raw, error = parse_tool_arguments(entry["arguments"])
            calls.append(
                ToolCall(
                    id=entry["id"] or f"call_{uuid4().hex}",
                    name=entry["name"],
                    arguments=arguments,
                    raw_arguments=raw,
                    arguments_error=error,
                )
            )
        return calls

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
        iteration: int,
        exc: Exception,
    ) -> None:
        self._abort_pending_tool_calls(
            session_id,
            new_messages,
            context,
            pending_calls,
            tool_results,
            iteration,
            reason=f"run aborted: {exc}",
        )
        self._persist_best_effort(
            session_id,
            new_messages,
            user_id,
            metadata,
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
        )

    def _cancel_run(
        self,
        *,
        session_id: str,
        new_messages: list[dict[str, Any]],
        context: ContextManager,
        pending_calls: list[ToolCall],
        tool_results: list[ToolResult],
        user_id: str | None,
        metadata: dict[str, Any] | None,
        iteration: int,
        content: str | None,
        usage: dict[str, int],
    ) -> NoReturn:
        """Finish a cancelled run: abort pending tools, persist, then raise."""

        cancelled = RunCancelled(session_id, None, iteration)
        self._abort_pending_tool_calls(
            session_id,
            new_messages,
            context,
            pending_calls,
            tool_results,
            iteration,
            reason=f"run aborted: {cancelled}",
        )
        result = self._build_result(
            session_id,
            content,
            "cancelled",
            new_messages,
            tool_results,
            iteration,
            usage,
        )
        cancelled.result = result
        self._persist_best_effort(
            session_id,
            new_messages,
            user_id,
            metadata,
            status="cancelled",
            error=f"{type(cancelled).__name__}: {cancelled}",
        )
        self._emit(EventType.RUN_END, session_id, iteration, {"stop_reason": "cancelled"})
        raise cancelled

    def _abort_pending_tool_calls(
        self,
        session_id: str,
        new_messages: list[dict[str, Any]],
        context: ContextManager,
        pending_calls: list[ToolCall],
        tool_results: list[ToolResult],
        iteration: int,
        *,
        reason: str,
    ) -> None:
        # Keep the persisted conversation valid: every assistant tool call must
        # be followed by a tool message, even when the run aborted mid-loop.
        for call in pending_calls:
            result = ToolResult(
                tool_call_id=call.id,
                name=call.name,
                content=json.dumps({"error": reason}, ensure_ascii=False),
                is_error=True,
            )
            tool_results.append(result)
            self._emit(
                EventType.TOOL_RESULT,
                session_id,
                iteration,
                {
                    "tool_call_id": result.tool_call_id,
                    "name": result.name,
                    "content": result.content,
                    "is_error": True,
                    "status": "aborted",
                },
            )
            message = tool_message(result)
            context.append(message)
            new_messages.append(message)

    def _emit(
        self,
        event_type: EventType,
        session_id: str,
        iteration: int,
        data: dict[str, Any],
    ) -> None:
        # Sequence numbers are allocated for every event, even without a
        # callback, and restart at 1 for each run.
        self._event_seq += 1
        if self.on_event is None:
            return
        event = AgentEvent(
            type=event_type,
            session_id=session_id,
            iteration=iteration,
            seq=self._event_seq,
            data=data,
        )
        try:
            resolve_awaitable(self.on_event(event))
        except Exception:  # noqa: BLE001 - observer failures must not stop a run
            logger.exception("Agent event callback failed for %s", event_type.value)
