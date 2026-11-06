"""Subagents as ordinary tools.

A subagent is a second :class:`~bokaclaw_agent.agent.runtime.AgentRuntime` with
its own context, a hard tool allowlist, and a structured result envelope. The
parent sees only one tool call and one JSON tool result; every intermediate
model turn and tool output stays inside the child. Nested subagents are not
enabled in this version.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ..core.config import AgentConfig, ModelConfig
from ..core.exceptions import (
    ConfigurationError,
    MaxIterationsExceeded,
    NoProgressError,
    RunCancelled,
)
from ..core.types import AgentEvent, EventType, ToolResult
from ..providers.base import ModelProvider
from ..providers.openai_compatible import OpenAICompatibleProvider
from ..tools.registry import CURRENT_TOOL_CALL_ID, Tool, ToolRegistry
from .context import truncate_text

# Conservative defaults for the two read-oriented profiles. A server can add
# its own names with the "custom" policy; the SDK never assumes a tool exists.
READ_ONLY_TOOL_NAMES = frozenset(
    {
        "Read",
        "ViewImage",
        "Glob",
        "Grep",
        "WebFetch",
        "WebSearch",
        "search_memories",
    }
)
RESEARCH_TOOL_NAMES = READ_ONLY_TOOL_NAMES | frozenset({"save_memory"})

_TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
_WHITESPACE = re.compile(r"\s+")
_MAX_FACTS = 20
_MAX_SOURCES = 20
_PREVIEW_CHARS = 1_000

_PHASE_BY_EVENT = {
    EventType.MODEL_REQUEST: "model_request",
    EventType.MODEL_RESPONSE: "model_response",
    EventType.TOOL_CALL: "tool_call",
    EventType.TOOL_RESULT: "tool_result",
    EventType.CONTEXT_COMPRESSED: "context_compressed",
    EventType.ERROR: "error",
}


class Fact(BaseModel):
    """One observed fact and the evidence that supports it."""

    model_config = ConfigDict(extra="ignore")

    claim: str
    evidence: str = ""
    confidence: Literal["high", "medium", "low"] = "medium"


class SubagentSpec(BaseModel):
    """Type-level definition of a subagent."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Tool name exposed to the parent, for example ``explore``.")
    description: str = Field(
        min_length=1,
        description="When to use and when not to use this subagent; the parent routes on this text.",
    )
    system_prompt: str = Field(min_length=1, description="Role and tool rules for the child agent.")
    tool_policy: Literal["read_only", "research", "code", "custom"] = "read_only"
    tool_names: tuple[str, ...] | None = Field(
        default=None,
        description="Explicit allowlist used only when tool_policy is 'custom'.",
    )
    model: ModelConfig | None = Field(
        default=None,
        description="Optional child model. Defaults to the parent's provider.",
    )
    default_max_turns: int = Field(default=8, ge=1)
    default_timeout_seconds: float = Field(default=120.0, gt=0)
    max_result_chars: int = Field(default=8_000, ge=256)
    output_schema: dict[str, Any] | None = Field(
        default=None,
        description="When set, the child is asked to answer with a JSON object matching this schema.",
    )
    allow_nested: bool = Field(
        default=False,
        description="Reserved for a later phase; nested subagents are not registered in this version.",
    )
    resumable: bool = Field(default=True, description="Keep the child transcript for resume within this run.")

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not _TOOL_NAME.fullmatch(value):
            raise ValueError("name must contain only letters, digits, '_' or '-' (max 64 chars)")
        return value

    @model_validator(mode="after")
    def _validate_custom_policy(self) -> "SubagentSpec":
        if self.tool_policy == "custom" and not self.tool_names:
            raise ValueError("tool_policy='custom' requires tool_names")
        return self


class SubagentTask(BaseModel):
    """One self-contained delegation request."""

    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1, description="One-sentence objective.")
    label: str = Field(default="", max_length=80, description="Short label for traces and the UI.")
    scope: list[str] = Field(
        default_factory=list,
        description="Absolute paths, URLs, or data sources the child may inspect.",
    )
    background: str = Field(
        default="",
        description="Everything the child needs; it cannot see the parent conversation.",
    )
    questions: list[str] = Field(default_factory=list, description="Numbered questions that define completion.")
    output_format: str = Field(default="", description="Required result shape: table, bullets, JSON, ...")
    constraints: list[str] = Field(default_factory=list, description="Extra rules such as read-only.")
    max_turns: int | None = Field(default=None, ge=1)
    timeout_seconds: float | None = Field(default=None, gt=0)
    run_in_background: bool = Field(
        default=False,
        description="Reserved; background subagents are rejected in this version.",
    )
    resume: str | None = Field(default=None, description="A subagent_id returned by an earlier call in this run.")


class SubagentResult(BaseModel):
    """Structured envelope returned to the parent as one tool result."""

    model_config = ConfigDict(extra="ignore")

    subagent_id: str
    subagent_type: str
    status: Literal["completed", "partial", "failed", "timeout", "cancelled"]
    report: str | None = None
    data: dict[str, Any] | None = None
    facts: list[Fact] = Field(default_factory=list)
    inferences: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    usage: dict[str, int] = Field(default_factory=dict)
    turns: int = 0
    isolated_chars: int = 0
    stop_reason: str | None = None
    duration_ms: int = 0
    resumable: bool = False
    truncated: bool = False
    error: str | None = None


class SubagentOptions(BaseModel):
    """Runtime-wide limits for subagent delegation."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    max_depth: int = Field(default=2, ge=0)
    max_calls_per_run: int = Field(default=4, ge=1)
    max_result_chars: int = Field(default=8_000, ge=256)
    default_timeout_seconds: float = Field(default=120.0, gt=0)
    token_budget: int | None = Field(default=None, ge=1)
    verification_policy: Literal["none", "spot_check", "strict"] = "spot_check"


@dataclass(slots=True)
class _ChildSession:
    spec_name: str
    messages: list[dict[str, Any]]


@dataclass(slots=True)
class _DelegationState:
    """Run-scoped state shared by every subagent tool of one parent runtime."""

    sessions: dict[str, _ChildSession] = field(default_factory=dict)
    calls: int = 0
    spent_tokens: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def reset(self) -> None:
        with self.lock:
            self.sessions.clear()
            self.calls = 0
            self.spent_tokens = 0


@dataclass(slots=True)
class _Delegation:
    label: str
    goal: str
    status: str
    summary: str
    repeats: int = 1


class DelegationLedger:
    """Compact record of delegated tasks, injected into the parent model view."""

    def __init__(
        self,
        max_entries: int = 8,
        max_chars: int = 1_500,
        *,
        verification_policy: str = "spot_check",
    ) -> None:
        self._entries: list[_Delegation] = []
        self._by_key: dict[str, _Delegation] = {}
        self._max_entries = max(1, max_entries)
        self._max_chars = max(256, max_chars)
        self._verification_policy = verification_policy

    def clear(self) -> None:
        self._entries.clear()
        self._by_key.clear()

    def record(self, task: SubagentTask, result: SubagentResult) -> None:
        label = task.label.strip() or _label_from_goal(task.goal)
        key = f"{label}\0{task.goal}"
        summary = _result_summary(result)
        existing = self._by_key.get(key)
        if existing is not None:
            existing.repeats += 1
            existing.status = result.status
            existing.summary = summary
            return
        entry = _Delegation(label=label, goal=_one_line(task.goal), status=result.status, summary=summary)
        self._entries.append(entry)
        self._by_key[key] = entry
        if len(self._entries) > self._max_entries:
            dropped = self._entries.pop(0)
            self._by_key.pop(f"{dropped.label}\0{dropped.goal}", None)

    def render(self, max_chars: int | None = None) -> str | None:
        if not self._entries:
            return None
        budget = max_chars or self._max_chars
        header = [
            "## Subagent delegations (this run)",
            "Tasks already delegated. Do not repeat them; reuse their conclusions.",
        ]
        if self._verification_policy == "strict":
            header.append(
                "Before relying on any high-impact subagent fact, verify it against its evidence "
                "with a direct tool call; say so if verification fails."
            )
        elif self._verification_policy == "spot_check":
            header.append(
                "Before relying on a high-impact subagent fact, spot-check at least one fact "
                "against its evidence with a direct tool call."
            )
        lines = list(header)
        for entry in self._entries:
            repeat = f" (x{entry.repeats})" if entry.repeats > 1 else ""
            goal = entry.goal[:120]
            lines.append(f"- {entry.label}{repeat} -> {entry.status}: {goal} => {entry.summary}")
        omitted = 0
        while len(lines) > len(header) + 1 and _line_length(lines) > budget:
            lines.pop(len(header))
            omitted += 1
        if omitted:
            lines.append(f"- ({omitted} earlier delegations omitted)")
        text = "\n".join(lines)
        return text if len(text) <= budget else text[:budget]


class SubagentTool:
    """Wrap a :class:`SubagentSpec` as a parent-facing ``Tool``."""

    def __init__(
        self,
        spec: SubagentSpec,
        *,
        parent_config: AgentConfig,
        parent_tools: ToolRegistry,
        depth: int = 0,
        options: SubagentOptions | None = None,
        emit: Callable[[EventType, dict[str, Any]], None] | None = None,
        cancel_event_provider: Callable[[], threading.Event | None] | None = None,
        parent_provider: ModelProvider | None = None,
        provider_factory: Callable[[ModelConfig], ModelProvider] | None = None,
        exclude_tool_names: Iterable[str] = (),
        ledger: DelegationLedger | None = None,
        state: _DelegationState | None = None,
        task_validator: Callable[[SubagentTask], str | None] | None = None,
    ) -> None:
        self.spec = spec
        self.depth = depth
        self.options = options or SubagentOptions()
        self._parent_config = parent_config
        self._parent_tools = parent_tools
        self._emit = emit
        self._cancel_event_provider = cancel_event_provider
        self._parent_provider = parent_provider
        self._provider_factory = provider_factory
        self._exclude_tool_names = frozenset(exclude_tool_names)
        self._ledger = ledger
        self._state = state or _DelegationState()
        self._task_validator = task_validator
        self._tool: Tool | None = None

    def as_tool(self) -> Tool:
        """Return the parent-facing tool, building its schema once."""

        if self._tool is None:
            self._tool = Tool.from_callable(
                self._invoke,
                name=self.spec.name,
                description=self.spec.description,
            )
        return self._tool

    def _invoke(
        self,
        goal: str,
        label: str = "",
        scope: list[str] | None = None,
        background: str = "",
        questions: list[str] | None = None,
        output_format: str = "",
        constraints: list[str] | None = None,
        max_turns: int | None = None,
        timeout_seconds: float | None = None,
        run_in_background: bool = False,
        resume: str | None = None,
    ) -> ToolResult:
        """Delegate a self-contained task to an isolated subagent.

        Args:
            goal: One-sentence objective for the subagent.
            label: Short label shown in traces and the dashboard.
            scope: Absolute paths, URLs, or data sources the subagent may inspect.
            background: Everything the subagent needs. It cannot see the parent conversation.
            questions: Numbered questions that define when the task is complete.
            output_format: Required shape of the result: table, bullets, JSON, and so on.
            constraints: Extra rules such as read-only or no recommendations.
            max_turns: Optional ReAct step budget for this call.
            timeout_seconds: Optional wall-clock timeout for this call.
            run_in_background: Reserved; background subagents are rejected in this version.
            resume: A subagent_id returned by an earlier call in this run.

        Returns:
            A JSON SubagentResult envelope with facts, inferences, unknowns, and sources.
        """

        try:
            task = SubagentTask(
                goal=goal,
                label=label,
                scope=list(scope or ()),
                background=background,
                questions=list(questions or ()),
                output_format=output_format,
                constraints=list(constraints or ()),
                max_turns=max_turns,
                timeout_seconds=timeout_seconds,
                run_in_background=run_in_background,
                resume=resume,
            )
        except ValidationError as exc:
            return self._failure_tool_result(
                subagent_id=resume or "",
                error=f"Invalid subagent task: {exc}",
            )
        return self._delegate(task)

    def _delegate(self, task: SubagentTask) -> ToolResult:
        with self._state.lock:
            if self.depth >= self.options.max_depth:
                return self._failure_tool_result(
                    subagent_id=task.resume or "",
                    error=f"Subagent depth limit ({self.options.max_depth}) reached.",
                )
            if task.run_in_background:
                return self._failure_tool_result(
                    subagent_id=task.resume or "",
                    error="Background subagents are not enabled; call again with run_in_background=false.",
                )
            if self._task_validator is not None:
                try:
                    rejection = self._task_validator(task)
                except Exception as exc:  # noqa: BLE001 - a validator bug must not crash the parent run
                    rejection = f"Task validation failed: {exc}"
                if rejection:
                    return self._failure_tool_result(
                        subagent_id=task.resume or "",
                        error=rejection,
                    )
            if self._state.calls >= self.options.max_calls_per_run:
                return self._failure_tool_result(
                    subagent_id=task.resume or "",
                    error=f"Subagent call budget exhausted ({self.options.max_calls_per_run} per run).",
                )
            if self.options.token_budget is not None and self._state.spent_tokens >= self.options.token_budget:
                return self._failure_tool_result(
                    subagent_id=task.resume or "",
                    error=f"Subagent token budget exhausted ({self.options.token_budget}).",
                )
            session = self._state.sessions.get(task.resume) if task.resume else None
            if task.resume and session is None:
                return self._failure_tool_result(
                    subagent_id=task.resume,
                    error=f"Unknown or expired subagent_id {task.resume!r}.",
                )
            if session is not None and session.spec_name != self.spec.name:
                return self._failure_tool_result(
                    subagent_id=task.resume or "",
                    error=f"subagent_id {task.resume!r} belongs to {session.spec_name!r}, not {self.spec.name!r}.",
                )
            self._state.calls += 1

        subagent_id = task.resume or f"sub_{uuid4().hex}"
        child_depth = self.depth + 1
        prompt = render_subagent_prompt(self.spec, task, continuation=session is not None)
        history = list(session.messages) if session is not None else []
        input_payload: str | list[dict[str, Any]] = (
            [*history, {"role": "user", "content": prompt}] if history else prompt
        )
        label = task.label.strip() or _label_from_goal(task.goal)

        self._emit_event(
            EventType.SUBAGENT_STARTED,
            {
                "subagent_id": subagent_id,
                "subagent_type": self.spec.name,
                "label": label,
                "goal": task.goal,
                "depth": child_depth,
                "resume": session is not None,
                "parent_tool_call_id": CURRENT_TOOL_CALL_ID.get(),
                "model": self.spec.model.model if self.spec.model is not None else None,
                "tool_names": [tool.name for tool in self._select_tools()],
            },
        )

        child = self._build_child_runtime(subagent_id, max_turns=task.max_turns)
        timeout = task.timeout_seconds or self.spec.default_timeout_seconds
        parent_cancel = self._cancel_event_provider() if self._cancel_event_provider is not None else None
        child_cancel = threading.Event()
        timed_out = threading.Event()
        stop_watch = threading.Event()
        watcher: threading.Thread | None = None
        timer: threading.Timer | None = None

        if timeout and timeout > 0:
            timer = threading.Timer(timeout, _set_event, args=(timed_out, child_cancel))
            timer.daemon = True
            timer.start()
        if parent_cancel is not None:
            if parent_cancel.is_set():
                child_cancel.set()
            else:
                watcher = threading.Thread(
                    target=_watch_parent_cancel,
                    args=(parent_cancel, child_cancel, stop_watch),
                    daemon=True,
                )
                watcher.start()

        started = time.monotonic()
        run_result = None
        status = "failed"
        error: str | None = None
        try:
            run_result = child.run(input_payload, session_id=subagent_id, cancel_event=child_cancel)
            status = _status_from_run(run_result)
        except RunCancelled as exc:
            run_result = exc.result
            if parent_cancel is not None and parent_cancel.is_set():
                status = "cancelled"
            elif timed_out.is_set():
                status = "timeout"
            else:
                status = "cancelled"
        except (MaxIterationsExceeded, NoProgressError) as exc:
            run_result = exc.result
            status = "partial" if run_result is not None and run_result.content else "failed"
            if status == "failed":
                error = str(exc)
        except Exception as exc:  # noqa: BLE001 - a child failure must not fail the parent
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"
        finally:
            if timer is not None:
                timer.cancel()
            stop_watch.set()
            if watcher is not None:
                watcher.join(timeout=0.2)
            duration_ms = int((time.monotonic() - started) * 1_000)

        result = self._normalize_result(
            subagent_id=subagent_id,
            run_result=run_result,
            status=status,
            error=error,
            duration_ms=duration_ms,
        )

        if result.resumable and run_result is not None:
            with self._state.lock:
                self._state.sessions[subagent_id] = _ChildSession(
                    spec_name=self.spec.name,
                    messages=list(run_result.new_messages),
                )
        with self._state.lock:
            self._state.spent_tokens += _total_tokens(result.usage)

        if self._ledger is not None:
            self._ledger.record(task, result)

        tool_result = self._tool_result(result)
        self._emit_event(
            EventType.SUBAGENT_FINISHED,
            {
                "subagent_id": result.subagent_id,
                "subagent_type": result.subagent_type,
                "status": result.status,
                "turns": result.turns,
                "usage": result.usage,
                "isolated_chars": result.isolated_chars,
                "result_chars": len(tool_result.content),
                "duration_ms": result.duration_ms,
                "resumable": result.resumable,
                "error": result.error,
            },
        )
        return tool_result

    def _build_child_runtime(self, subagent_id: str, *, max_turns: int | None = None):
        # Imported lazily so this module can be imported by runtime.py without a
        # circular import at module load time.
        from .runtime import AgentRuntime

        max_turns = max_turns or self.spec.default_max_turns
        child_config = AgentConfig(
            name=f"{self._parent_config.name}:{self.spec.name}",
            system_prompt=self.spec.system_prompt,
            max_iterations=max_turns,
            tool_error_mode="return",
            context_max_tokens=self._parent_config.context_max_tokens,
            context_keep_recent_tokens=self._parent_config.context_keep_recent_tokens,
            context_compression=self._parent_config.context_compression,
            tool_result_max_chars=self._parent_config.tool_result_max_chars,
            fact_ledger=self._parent_config.fact_ledger,
            fact_ledger_max_chars=self._parent_config.fact_ledger_max_chars,
            step_budget_notice=True,
            compression_model=self._parent_config.compression_model,
        )
        provider = self._resolve_provider()
        child_tools = self._select_tools()

        def on_child_event(event: AgentEvent) -> None:
            phase = _PHASE_BY_EVENT.get(event.type)
            if phase is None:
                return
            data: dict[str, Any] = {
                "subagent_id": subagent_id,
                "subagent_type": self.spec.name,
                "phase": phase,
                "iteration": event.iteration,
                "child_session_id": event.session_id,
            }
            if event.type == EventType.MODEL_REQUEST:
                data["message_count"] = len(event.data.get("messages") or ())
                data["tool_count"] = len(event.data.get("tools") or ())
            elif event.type == EventType.MODEL_RESPONSE:
                content = event.data.get("content")
                data["content_chars"] = len(content) if isinstance(content, str) else 0
                data["finish_reason"] = event.data.get("finish_reason")
                data["usage"] = event.data.get("usage") or {}
                data["tool_calls"] = [
                    {"id": call.get("id"), "name": call.get("name")}
                    for call in event.data.get("tool_calls") or ()
                    if isinstance(call, dict)
                ]
            elif event.type == EventType.TOOL_CALL:
                data["tool_call_id"] = event.data.get("tool_call_id")
                data["name"] = event.data.get("name")
                data["arguments"] = event.data.get("arguments")
                data["status"] = event.data.get("status")
            elif event.type == EventType.TOOL_RESULT:
                content = event.data.get("content")
                data["tool_call_id"] = event.data.get("tool_call_id")
                data["name"] = event.data.get("name")
                data["is_error"] = event.data.get("is_error")
                data["status"] = event.data.get("status")
                data["original_chars"] = event.data.get("original_chars")
                data["content"] = content[:_PREVIEW_CHARS] if isinstance(content, str) else ""
            elif event.type == EventType.CONTEXT_COMPRESSED:
                data["stats"] = event.data
            elif event.type == EventType.ERROR:
                data["error"] = event.data.get("error")
                data["error_type"] = event.data.get("type")
            self._emit_event(EventType.SUBAGENT_EVENT, data)

        return AgentRuntime(
            child_config,
            tools=child_tools,
            provider=provider,
            on_event=on_child_event,
            subagent_depth=self.depth + 1,
            subagent_options=SubagentOptions(enabled=False),
        )

    def _resolve_provider(self) -> ModelProvider:
        if self.spec.model is not None:
            if self._provider_factory is not None:
                return self._provider_factory(self.spec.model)
            return OpenAICompatibleProvider(self.spec.model)
        if self._parent_provider is None:
            raise ConfigurationError(f"Subagent {self.spec.name!r} needs a parent provider or an explicit spec.model")
        return self._parent_provider

    def _select_tools(self) -> list[Tool]:
        selected: list[Tool] = []
        for tool in self._parent_tools.tools:
            if tool.name in self._exclude_tool_names:
                continue
            if self.spec.tool_policy == "read_only":
                allowed = tool.name in READ_ONLY_TOOL_NAMES
            elif self.spec.tool_policy == "research":
                allowed = tool.name in RESEARCH_TOOL_NAMES
            elif self.spec.tool_policy == "code":
                allowed = True
            else:
                allowed = tool.name in (self.spec.tool_names or ())
            if allowed:
                selected.append(tool)
        return selected

    def _normalize_result(
        self,
        *,
        subagent_id: str,
        run_result: Any,
        status: str,
        error: str | None,
        duration_ms: int,
    ) -> SubagentResult:
        content = (run_result.content or "") if run_result is not None else ""
        usage = dict(run_result.usage) if run_result is not None else {}
        turns = run_result.iterations if run_result is not None else 0
        stop_reason = run_result.stop_reason if run_result is not None else None
        isolated_chars = (
            sum(len(item.content or "") for item in run_result.tool_results) if run_result is not None else 0
        )

        data = _extract_json_object(content) if content else None
        if self.spec.output_schema is not None and content and data is None:
            if status == "completed":
                status = "partial"
            error = error or "output_schema was provided but the final message was not a JSON object"

        report: str | None = None
        facts: list[Fact] = []
        inferences: list[str] = []
        unknowns: list[str] = []
        sources: list[str] = []
        if isinstance(data, dict):
            report = _first_str(data, ("report", "summary", "conclusion"))
            facts = _parse_facts(data)
            inferences = _str_list(data, ("inferences", "inference"))
            unknowns = _str_list(data, ("unknowns", "open_questions", "gaps"))
            sources = _str_list(data, ("sources", "references"))
            if report is None:
                report = content or None
        else:
            report = content or None

        max_chars = min(self.spec.max_result_chars, self.options.max_result_chars)
        truncated = False
        if report is not None:
            report, truncated = truncate_text(report, max_chars)

        facts = facts[:_MAX_FACTS]
        sources = _dedupe(sources)[:_MAX_SOURCES]
        if status == "completed" and not report and not facts and data is None:
            status = "failed"
            error = error or "Subagent returned no content."

        resumable = bool(
            self.spec.resumable
            and run_result is not None
            and status in {"completed", "partial"}
            and _can_resume(run_result)
        )
        return SubagentResult(
            subagent_id=subagent_id,
            subagent_type=self.spec.name,
            status=status,
            report=report,
            data=data,
            facts=facts,
            inferences=inferences,
            unknowns=unknowns,
            sources=sources,
            usage=usage,
            turns=turns,
            isolated_chars=isolated_chars,
            stop_reason=stop_reason,
            duration_ms=duration_ms,
            resumable=resumable,
            truncated=truncated,
            error=error,
        )

    def _failure_tool_result(self, *, subagent_id: str, error: str) -> ToolResult:
        result = SubagentResult(
            subagent_id=subagent_id or f"sub_{uuid4().hex}",
            subagent_type=self.spec.name,
            status="failed",
            error=error,
        )
        return self._tool_result(result)

    def _tool_result(self, result: SubagentResult) -> ToolResult:
        payload = result.model_dump_json()
        return ToolResult(
            tool_call_id="",
            name=self.spec.name,
            content=payload,
            is_error=result.status in {"failed", "timeout", "cancelled"},
            usage=dict(result.usage),
            metadata={
                "subagent": {
                    "id": result.subagent_id,
                    "type": result.subagent_type,
                    "status": result.status,
                    "turns": result.turns,
                    "isolated_chars": result.isolated_chars,
                    "result_chars": len(payload),
                    "stop_reason": result.stop_reason,
                    "resumable": result.resumable,
                }
            },
        )

    def _emit_event(self, event_type: EventType, data: dict[str, Any]) -> None:
        if self._emit is not None:
            self._emit(event_type, data)


def render_subagent_prompt(
    spec: SubagentSpec,
    task: SubagentTask,
    *,
    continuation: bool = False,
) -> str:
    """Render a self-contained task packet in the five-part shape.

    The child never receives the parent conversation, so the prompt states that
    explicitly and carries the goal, scope, background, completion questions,
    and output format.
    """

    scope = [item.strip() for item in task.scope if item.strip()]
    questions = [item.strip() for item in task.questions if item.strip()]
    constraints = [item.strip() for item in task.constraints if item.strip()]
    if spec.tool_policy == "read_only" and not any(
        "只读" in item or "read-only" in item.lower() for item in constraints
    ):
        constraints.insert(0, "只读：不要修改、创建或删除任何文件。")

    parts: list[str] = []
    if continuation:
        parts.append("这是同一个子任务的续接。你之前的对话历史已在上文，继续完成下面的新要求。")
    parts.append(f"目标：{_one_line(task.goal)}")
    if scope:
        parts.append("范围（绝对路径 / URL / 数据源）：\n" + "\n".join(f"- {item}" for item in scope))
    else:
        parts.append("范围：未显式指定；只使用你可用的工具，不要越界。")

    background = task.background.strip() or "（无额外背景。）"
    parts.append("背景（你看不到任何对话历史，以下是全部信息）：\n" + background)

    if questions:
        parts.append("请回答以下问题：\n" + "\n".join(f"{index}. {item}" for index, item in enumerate(questions, 1)))
    else:
        parts.append("完成标准：给出可验证的结论。")

    output_format = task.output_format.strip() or "先给结论，再列事实与证据；不要输出过程性思考。"
    parts.append("返回格式：\n" + output_format)

    if spec.output_schema is not None:
        parts.append(
            "只返回一个 JSON 对象，不要加 Markdown 代码围栏或额外说明。JSON 必须符合以下 Schema：\n"
            + json.dumps(spec.output_schema, ensure_ascii=False, indent=2)
        )

    parts.append(
        "要求：\n"
        "- 事实与推断分开。每条事实附证据（文件路径:行号 / URL / 命令输出节选）。\n"
        "- 无法证实的内容写入 unknowns，不要编造。\n"
        "- 不要给建议，只报告事实。"
    )
    if constraints:
        parts.append("额外约束：\n" + "\n".join(f"- {item}" for item in constraints))
    return "\n\n".join(parts)


def _status_from_run(run_result: Any) -> str:
    stop_reason = run_result.stop_reason
    if stop_reason == "final":
        return "completed"
    if stop_reason in {"length", "content_filter"}:
        return "partial"
    if stop_reason in {"max_iterations", "no_progress"}:
        return "partial" if run_result.content else "failed"
    if stop_reason == "cancelled":
        return "cancelled"
    return "completed"


def _can_resume(run_result: Any) -> bool:
    messages = run_result.new_messages or []
    return run_result.stop_reason != "cancelled" and bool(messages) and messages[-1].get("role") == "assistant"


def _set_event(*events: threading.Event) -> None:
    for event in events:
        event.set()


def _watch_parent_cancel(parent: threading.Event, child: threading.Event, stop: threading.Event) -> None:
    while not stop.wait(0.05):
        if parent.is_set():
            child.set()
            return


def _total_tokens(usage: dict[str, int]) -> int:
    total = usage.get("total_tokens")
    if total:
        return int(total)
    return int(usage.get("prompt_tokens", 0)) + int(usage.get("completion_tokens", 0))


def _extract_json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    if not stripped.startswith("{"):
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            return None
        stripped = stripped[start : end + 1]
    try:
        value = json.loads(stripped)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _parse_facts(data: dict[str, Any]) -> list[Fact]:
    raw = data.get("facts") or data.get("findings") or []
    if not isinstance(raw, list):
        return []
    facts: list[Fact] = []
    for item in raw:
        if isinstance(item, dict):
            claim = _first_str(item, ("claim", "fact", "text", "statement"))
            if not claim:
                continue
            evidence = _first_str(item, ("evidence", "source", "citation")) or ""
            confidence = item.get("confidence")
            if confidence not in {"high", "medium", "low"}:
                confidence = "medium"
            facts.append(Fact(claim=claim, evidence=evidence, confidence=confidence))
        elif isinstance(item, str) and item.strip():
            facts.append(Fact(claim=item.strip(), evidence="", confidence="low"))
    return facts


def _first_str(data: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _str_list(data: dict[str, Any], keys: tuple[str, ...]) -> list[str]:
    for key in keys:
        value = data.get(key)
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
    return []


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _result_summary(result: SubagentResult) -> str:
    if result.report:
        text = result.report
    elif result.facts:
        text = result.facts[0].claim
    elif result.error:
        text = result.error
    else:
        text = result.status
    return _one_line(text)[:180]


def _label_from_goal(goal: str) -> str:
    return _one_line(goal)[:60] or "subagent task"


def _one_line(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def _line_length(lines: list[str]) -> int:
    return sum(len(line) + 1 for line in lines)


__all__ = [
    "DelegationLedger",
    "Fact",
    "READ_ONLY_TOOL_NAMES",
    "RESEARCH_TOOL_NAMES",
    "SubagentOptions",
    "SubagentResult",
    "SubagentSpec",
    "SubagentTask",
    "SubagentTool",
    "render_subagent_prompt",
]
