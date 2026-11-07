"""Tool registration, JSON Schema derivation, and dispatch."""

from __future__ import annotations

import inspect
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, wait
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, get_type_hints

from pydantic import BaseModel, ValidationError, create_model

from ..core.exceptions import (
    ToolArgumentError,
    ToolExecutionError,
    ToolNotFoundError,
    ToolSchemaError,
)
from ..core.types import ToolCall, ToolResult
from ..core.utils import resolve_awaitable

# Names that Pydantic reserves on model classes. Function parameters with these
# names cannot be turned into a generated argument model.
_RESERVED_PARAMETER_NAMES = {
    "model_config",
    "model_fields",
    "model_computed_fields",
    "model_extra",
    "model_fields_set",
}

_SECTION_HEADER = re.compile(r"^\s*([A-Z][A-Za-z ]*)\s*:\s*$")
_ARGS_HEADERS = {"args", "arguments", "parameters"}
_GOOGLE_ARG_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:\([^)]*\))?\s*:\s*(.*)$")

# Set while a tool runs so nested work (for example a subagent tool) can record
# which parent tool call opened it. Context-local, so concurrent runs do not
# leak into each other.
CURRENT_TOOL_CALL_ID: ContextVar[str] = ContextVar("bokaclaw_tool_call_id", default="")


def _parse_docstring(func: Callable[..., Any]) -> tuple[str | None, dict[str, str]]:
    """Return the tool description and per-parameter descriptions."""

    doc = inspect.getdoc(func) or ""
    description_lines: list[str] = []
    parameter_descriptions: dict[str, str] = {}
    in_args = False
    current: str | None = None

    for line in doc.splitlines():
        header = _SECTION_HEADER.match(line)
        if header:
            if header.group(1).strip().lower() in _ARGS_HEADERS:
                in_args = True
                current = None
                continue
            if in_args or description_lines:
                break

        if in_args:
            arg_match = _GOOGLE_ARG_LINE.match(line)
            if arg_match:
                current = arg_match.group(1)
                parameter_descriptions[current] = arg_match.group(2).strip()
            elif current and line.strip():
                parameter_descriptions[current] = f"{parameter_descriptions[current]} {line.strip()}".strip()
        else:
            description_lines.append(line)

    description = " ".join(part.strip() for part in description_lines if part.strip())
    return description or None, parameter_descriptions


def _caller_locals(depth: int = 1) -> dict[str, Any] | None:
    """Snapshot locals from ``depth`` frames above this helper.

    With ``from __future__ import annotations`` every annotation is a string
    that ``typing.get_type_hints`` resolves against module globals. Types that
    are defined inside a function (tests, notebooks, factory functions) only
    exist in the caller's locals, so capture them while we are still on the
    decorating frame.
    """

    frame = inspect.currentframe()
    try:
        for _ in range(depth):
            if frame is None:
                return None
            frame = frame.f_back
        return dict(frame.f_locals) if frame is not None else None
    finally:
        del frame


def _build_arguments_model(
    func: Callable[..., Any],
    tool_name: str,
    localns: dict[str, Any] | None = None,
) -> type[BaseModel]:
    try:
        hints = get_type_hints(func, include_extras=True, localns=localns)
    except Exception:  # noqa: BLE001 - fall back to raw annotations below
        hints = {}

    try:
        signature = inspect.signature(func, eval_str=True, locals=localns)
    except (TypeError, ValueError, NameError):
        signature = inspect.signature(func)

    fields: dict[str, tuple[Any, Any]] = {}
    for parameter_name, parameter in signature.parameters.items():
        if parameter.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            raise ToolSchemaError(f"Tool {tool_name!r} cannot use *args or **kwargs")
        if parameter_name in _RESERVED_PARAMETER_NAMES or parameter_name.startswith("_"):
            raise ToolSchemaError(f"Tool {tool_name!r} has unsupported parameter name {parameter_name!r}")

        annotation = hints.get(parameter_name, parameter.annotation)
        if annotation is inspect.Parameter.empty:
            annotation = str
        if isinstance(annotation, str):
            raise ToolSchemaError(
                f"Tool {tool_name!r} parameter {parameter_name!r} has an unresolved annotation {annotation!r}"
            )

        default = ... if parameter.default is inspect.Parameter.empty else parameter.default
        fields[parameter_name] = (annotation, default)

    try:
        return create_model(f"{tool_name}_arguments", **fields)
    except Exception as exc:  # noqa: BLE001 - Pydantic raises several exception types
        raise ToolSchemaError(f"Could not build a JSON Schema for tool {tool_name!r}: {exc}") from exc


def serialize_tool_output(value: Any) -> str:
    """Convert a tool return value into the string sent back to the model."""

    if isinstance(value, str):
        return value
    if isinstance(value, BaseModel):
        return value.model_dump_json()
    if value is None:
        return "null"
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return repr(value)
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


@dataclass(slots=True)
class Tool:
    """A callable exposed to the model as an OpenAI function tool."""

    name: str
    description: str
    parameters: dict[str, Any]
    func: Callable[..., Any]
    is_async: bool = False
    args_model: type[BaseModel] | None = field(default=None, repr=False)
    effect: Literal["read", "write", "external"] = "write"
    concurrency_safe: bool = False

    @classmethod
    def from_callable(
        cls,
        func: Callable[..., Any],
        *,
        name: str | None = None,
        description: str | None = None,
        localns: dict[str, Any] | None = None,
        effect: Literal["read", "write", "external"] = "write",
        concurrency_safe: bool | None = None,
    ) -> "Tool":
        if not callable(func):
            raise ToolSchemaError("Tool must be callable")

        tool_name = name or getattr(func, "__name__", None)
        if not tool_name:
            raise ToolSchemaError("Tool name could not be determined; pass name=...")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tool_name):
            raise ToolSchemaError(f"Invalid tool name {tool_name!r}; use letters, digits, '_' or '-' (max 64 chars)")

        doc_description, parameter_descriptions = _parse_docstring(func)
        args_model = _build_arguments_model(func, tool_name, localns)

        parameters = args_model.model_json_schema()
        parameters.pop("title", None)
        parameters.setdefault("type", "object")
        parameters.setdefault("properties", {})
        for parameter_name, parameter_schema in parameters.get("properties", {}).items():
            description_text = parameter_descriptions.get(parameter_name)
            if description_text and "description" not in parameter_schema:
                parameter_schema["description"] = description_text

        call_method = inspect.getattr_static(type(func), "__call__", None)
        is_async = inspect.iscoroutinefunction(func) or inspect.iscoroutinefunction(call_method)
        # Read-only tools are safe to run together by default. Anything that
        # writes or has unknown side effects must opt in explicitly.
        safe = (effect == "read") if concurrency_safe is None else concurrency_safe

        return cls(
            name=tool_name,
            description=description or doc_description or f"Call the {tool_name} tool.",
            parameters=parameters,
            func=func,
            is_async=is_async,
            args_model=args_model,
            effect=effect,
            concurrency_safe=safe,
        )

    def to_openai_spec(self) -> dict[str, Any]:
        """Return the OpenAI ``tools`` entry for this tool."""

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def invoke(self, arguments: dict[str, Any], *, tool_call_id: str = "") -> ToolResult:
        """Validate ``arguments`` and call the function.

        Raises:
            ToolArgumentError: arguments do not match the generated schema.
            ToolExecutionError: the function raised.
        """

        try:
            if self.args_model is not None:
                validated = self.args_model(**arguments)
                kwargs = {name: getattr(validated, name) for name in type(validated).model_fields}
            else:
                kwargs = dict(arguments)
        except ValidationError as exc:
            raise ToolArgumentError(f"Invalid arguments for tool {self.name!r}: {exc}") from exc
        except TypeError as exc:
            raise ToolArgumentError(f"Invalid arguments for tool {self.name!r}: {exc}") from exc

        token = CURRENT_TOOL_CALL_ID.set(tool_call_id)
        try:
            value = resolve_awaitable(self.func(**kwargs))
        except Exception as exc:  # noqa: BLE001 - all tool failures are normalized
            raise ToolExecutionError(f"Tool {self.name!r} failed: {exc}") from exc
        finally:
            CURRENT_TOOL_CALL_ID.reset(token)

        # Tools that need to attach usage or observer metadata (subagents, for
        # example) return a ToolResult directly. Fill the identity fields the
        # caller supplied when the tool left them blank.
        if isinstance(value, ToolResult):
            update: dict[str, Any] = {}
            if not value.tool_call_id:
                update["tool_call_id"] = tool_call_id
            if not value.name:
                update["name"] = self.name
            return value.model_copy(update=update) if update else value

        return ToolResult(
            tool_call_id=tool_call_id,
            name=self.name,
            content=serialize_tool_output(value),
        )


class ToolRegistry:
    """Name-indexed collection of tools."""

    def __init__(self, tools: Any = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for item in tools:
            self.register(item)

    def register(self, item: Tool | Callable[..., Any]) -> Tool:
        tool = item if isinstance(item, Tool) else Tool.from_callable(item, localns=_caller_locals(2))
        if tool.name in self._tools:
            raise ToolSchemaError(f"Tool {tool.name!r} is already registered")
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    @property
    def tools(self) -> tuple[Tool, ...]:
        """Return the registered tools in registration order."""

        return tuple(self._tools.values())

    def specs(self) -> list[dict[str, Any]]:
        return [tool.to_openai_spec() for tool in self._tools.values()]

    def invoke(self, call: ToolCall, *, error_mode: str = "return") -> ToolResult:
        if call.arguments_error:
            return self._error(
                call,
                f"Invalid JSON arguments: {call.arguments_error}",
                error_mode,
                ToolArgumentError,
            )

        tool = self._tools.get(call.name)
        if tool is None:
            available = sorted(self._tools)
            return self._error(
                call,
                f"Unknown tool {call.name!r}. Available tools: {available}",
                error_mode,
                ToolNotFoundError,
            )

        try:
            return tool.invoke(call.arguments, tool_call_id=call.id)
        except (ToolArgumentError, ToolExecutionError) as exc:
            if error_mode == "raise":
                raise
            return ToolResult(
                tool_call_id=call.id,
                name=call.name,
                content=json.dumps({"error": str(exc)}, ensure_ascii=False),
                is_error=True,
            )

    def plan_batches(self, calls: list[ToolCall]) -> list[list[int]]:
        """Split ``calls`` into execution batches, preserving declaration order.

        Consecutive concurrency-safe calls share a batch; every other call is a
        single-call batch and a barrier.
        """

        batches: list[list[int]] = []
        index = 0
        while index < len(calls):
            batch = self._safe_batch(calls, index)
            batches.append(batch)
            index = batch[-1] + 1
        return batches

    def invoke_batch(
        self,
        calls: list[ToolCall],
        batch: list[int],
        *,
        error_mode: str = "return",
        max_workers: int = 1,
        cancel_event: threading.Event | None = None,
    ) -> list[ToolResult]:
        """Execute one planned batch and return results in batch order.

        Calls that did not produce a result (cancelled before or during the
        batch) are returned as ``aborted`` results, so the caller can always
        pair every declared call with a tool message.
        """

        if cancel_event is not None and cancel_event.is_set():
            return [self._aborted(calls[index], "run aborted: cancelled") for index in batch]

        parallel_ok = error_mode == "return" and max_workers > 1 and len(batch) > 1
        results: dict[int, ToolResult] = {}
        if parallel_ok:
            self._invoke_parallel(calls, batch, results, error_mode, max_workers, cancel_event)
        else:
            for index in batch:
                results[index] = self.invoke(calls[index], error_mode=error_mode)

        return [
            results[index] if index in results else self._aborted(calls[index], "run aborted: cancelled")
            for index in batch
        ]

    def invoke_many(
        self,
        calls: list[ToolCall],
        *,
        error_mode: str = "return",
        max_workers: int = 1,
        cancel_event: threading.Event | None = None,
    ) -> list[ToolResult]:
        """Invoke every call, parallelising each planned batch.

        This is the batch-level convenience wrapper; ``AgentRuntime`` walks
        ``plan_batches`` itself so it can replay events between batches. The
        returned list always matches ``calls`` order, regardless of completion
        order. Cancellation after a batch stops later batches and fills their
        slots with aborted results.
        """

        if not calls:
            return []
        results: list[ToolResult | None] = [None] * len(calls)
        for batch in self.plan_batches(calls):
            if cancel_event is not None and cancel_event.is_set():
                break
            batch_results = self.invoke_batch(
                calls,
                batch,
                error_mode=error_mode,
                max_workers=max_workers,
                cancel_event=cancel_event,
            )
            for index, result in zip(batch, batch_results, strict=True):
                results[index] = result

        final: list[ToolResult] = []
        for position, result in enumerate(results):
            if result is None:
                result = self._aborted(calls[position], "run aborted: cancelled")
            final.append(result)
        return final

    def _safe_batch(self, calls: list[ToolCall], start: int) -> list[int]:
        """Return the contiguous run of concurrency-safe calls starting at ``start``."""

        tool = self._tools.get(calls[start].name)
        if tool is None or not tool.concurrency_safe or calls[start].arguments_error:
            return [start]

        batch = [start]
        index = start + 1
        while index < len(calls):
            call = calls[index]
            candidate = self._tools.get(call.name)
            if candidate is None or not candidate.concurrency_safe or call.arguments_error:
                break
            batch.append(index)
            index += 1
        return batch

    def _invoke_parallel(
        self,
        calls: list[ToolCall],
        batch: list[int],
        results: dict[int, ToolResult],
        error_mode: str,
        max_workers: int,
        cancel_event: threading.Event | None,
    ) -> None:
        executor = ThreadPoolExecutor(
            max_workers=min(max_workers, len(batch)),
            thread_name_prefix="bokaclaw-tool",
        )
        futures = {executor.submit(self.invoke, calls[position], error_mode=error_mode): position for position in batch}
        pending = set(futures)
        cancelled = False
        try:
            while pending:
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    break
                done, pending = wait(pending, timeout=0.2)
                for future in done:
                    results[futures[future]] = future.result()
            if cancelled:
                # Keep results that finished before the cancellation and cancel
                # the rest. Running tools cannot be interrupted; their results
                # are discarded and their slots become aborted results.
                done, pending = wait(pending, timeout=0)
                for future in done:
                    results[futures[future]] = future.result()
                for future in pending:
                    future.cancel()
        finally:
            executor.shutdown(wait=not cancelled, cancel_futures=True)

    @staticmethod
    def _aborted(call: ToolCall, reason: str) -> ToolResult:
        return ToolResult(
            tool_call_id=call.id,
            name=call.name,
            content=json.dumps({"error": reason}, ensure_ascii=False),
            is_error=True,
            metadata={"aborted": True},
        )

    @staticmethod
    def _error(
        call: ToolCall,
        message: str,
        error_mode: str,
        error_type: type[ToolArgumentError] | type[ToolNotFoundError],
    ) -> ToolResult:
        if error_mode == "raise":
            raise error_type(message)
        return ToolResult(
            tool_call_id=call.id,
            name=call.name,
            content=json.dumps({"error": message}, ensure_ascii=False),
            is_error=True,
        )


def tool(
    func: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    effect: Literal["read", "write", "external"] = "write",
    concurrency_safe: bool | None = None,
) -> Tool | Callable[[Callable[..., Any]], Tool]:
    """Decorate a function as a :class:`Tool`.

    Usable as ``@tool`` or ``@tool(name="...", description="...")``.
    Read-only tools (``effect="read"``) are concurrency-safe by default; pass
    ``concurrency_safe=...`` to override.
    """

    def decorator(callable_obj: Callable[..., Any]) -> Tool:
        return Tool.from_callable(
            callable_obj,
            name=name,
            description=description,
            localns=_caller_locals(2),
            effect=effect,
            concurrency_safe=concurrency_safe,
        )

    if func is None:
        return decorator
    return decorator(func)
