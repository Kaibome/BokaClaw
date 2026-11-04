"""Capture agent execution spans for offline evaluation.

Spans are a neutral structure (agent / llm / tool) persisted per background
run. They are written from the runner thread, never from the event loop, and a
write failure is logged instead of failing the run. The SDK stays untouched:
``runtime._bridge_event`` only forwards the events the SDK already emits.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.agent.events import (
    AgentContextCompressed,
    AgentError,
    AgentModelRequest,
    AgentModelResponse,
    AgentRunEnded,
    AgentRunStarted,
    AgentToolCall,
    AgentToolResult,
)
from app.agent.repo import RunRepository, utcnow

logger = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 16_384
TERMINAL_SPAN_STATUSES = ("completed", "failed", "aborted", "interrupted")
_RUN_TO_SPAN_STATUS = {
    "completed": "completed",
    "failed": "failed",
    "cancelled": "aborted",
    "interrupted": "interrupted",
}
# Deliberately narrow: enough to keep credentials out of stored tool payloads
# without rewriting ordinary prose that happens to mention "token".
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    re.compile(r"api[_-]?key\s*[\"']?\s*[:=]\s*[\"']?[^\"'\s,}]{4,}", re.IGNORECASE),
)
_SECRET_KEYS = {"api_key", "apikey", "authorization", "password", "secret", "token", "access_token"}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class SpanOptions:
    capture: bool = True
    max_bytes: int = DEFAULT_MAX_BYTES
    sample_rate: float = 1.0
    tool_content: bool = True

    @classmethod
    def from_env(cls) -> SpanOptions:
        return cls(
            capture=_env_bool("AGENT_SPAN_CAPTURE", True),
            max_bytes=_env_int("AGENT_SPAN_MAX_BYTES", DEFAULT_MAX_BYTES),
            sample_rate=min(1.0, max(0.0, _env_float("AGENT_SPAN_SAMPLE_RATE", 1.0))),
            tool_content=_env_bool("AGENT_SPAN_TOOL_CONTENT", True),
        )


def _sampled(run_id: Any, rate: float) -> bool:
    """Deterministic sampling so a run's capture decision is reproducible."""
    if rate >= 1.0:
        return True
    if rate <= 0.0:
        return False
    digest = hashlib.sha256(str(run_id).encode("utf-8")).hexdigest()[:8]
    return int(digest, 16) / 0xFFFFFFFF < rate


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        redacted = value
        for pattern in _SECRET_PATTERNS:
            redacted = pattern.sub("[REDACTED]", redacted)
        return redacted
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items() if str(key).lower() not in _SECRET_KEYS}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


def _bounded(value: Any, max_bytes: int) -> Any:
    """Return ``value`` unchanged when small, otherwise a truncated preview."""
    text = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    preview = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return {"truncated": True, "preview": preview, "original_bytes": len(encoded)}


def _tool_names(tools: Any) -> list[str]:
    if not isinstance(tools, list):
        return []
    names: list[str] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            names.append(function["name"])
        elif isinstance(tool.get("name"), str):
            names.append(tool["name"])
    return names


def _tool_span_id(tool_call_id: str) -> str:
    if len(tool_call_id) <= 100:
        return f"tool:{tool_call_id}"
    digest = hashlib.sha256(tool_call_id.encode("utf-8")).hexdigest()[:32]
    return f"tool:{digest}"


class SpanRecorder:
    """Build and persist the span tree for one claimed run."""

    def __init__(
        self,
        repo: RunRepository,
        run_id: Any,
        *,
        options: SpanOptions | None = None,
    ) -> None:
        self.repo = repo
        self.run_id = run_id
        self.options = options or SpanOptions.from_env()
        self.enabled = self.options.capture and _sampled(run_id, self.options.sample_rate)
        self.root_attributes: dict[str, Any] = {
            "executor": "remote" if os.environ.get("AGENT_LOOP_URL") else "local",
        }
        self.root_output: dict[str, Any] = {}
        self.root_error: str | None = None
        self.root_started_at: datetime | None = None
        self.root_input: dict[str, Any] = {}
        self.pending_compression: Any = None
        self.pending_llm: dict[int, deque[str]] = defaultdict(deque)
        self.llm_spans: dict[str, dict[str, Any]] = {}
        self.tool_spans: dict[str, dict[str, Any]] = {}
        self._llm_fallback = 0

    # ------------------------------------------------------------- lifecycle

    def start(self, run: Any) -> None:
        if not self.enabled:
            return
        payload = dict(getattr(run, "request_payload", None) or {})
        metadata = payload.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        message = _redact(payload.get("message"))
        self.root_started_at = getattr(run, "started_at", None) or utcnow()
        self.root_input = {
            "message": _bounded(message, self.options.max_bytes),
            "conversation_id": str(payload.get("conversation_id") or getattr(run, "conversation_id", "")),
            "client_run_id": payload.get("client_run_id") or getattr(run, "client_run_id", None),
            "eval_case_id": metadata.get("eval_case_id"),
        }
        self._write(
            span_id="agent",
            span_type="agent",
            parent_span_id=None,
            name="agent.turn",
            status="running",
            started_at=self.root_started_at,
            input=self.root_input,
            attributes=dict(self.root_attributes),
        )

    def handle(self, event: Any) -> None:
        if not self.enabled:
            return
        try:
            self._handle(event)
        except Exception:  # pragma: no cover - capture must never fail a run
            logger.warning("Failed to record agent span for run %s", self.run_id, exc_info=True)

    def finalize(
        self,
        *,
        status: str,
        content: str,
        metadata: dict[str, Any],
        error_detail: str | None,
    ) -> None:
        if not self.enabled:
            return
        span_status = _RUN_TO_SPAN_STATUS.get(status, "failed")
        try:
            now = utcnow()
            for span in self.llm_spans.values():
                if span["status"] == "running":
                    self._close_span(span, span_status, now)
            for span in self.tool_spans.values():
                if span["status"] == "running":
                    self._close_span(span, span_status, now)

            usage = metadata.get("usage")
            if isinstance(usage, dict):
                self.root_attributes["usage"] = _bounded(_redact(usage), self.options.max_bytes)
            stop_reason = metadata.get("stop_reason")
            if isinstance(stop_reason, str):
                self.root_attributes["stop_reason"] = stop_reason
            self.root_attributes["error_code"] = metadata.get("error_code")
            self.root_output = {
                "content": _bounded(_redact(content), self.options.max_bytes),
            }
            duration_ms = None
            if self.root_started_at is not None:
                duration_ms = max(0, int((now - self.root_started_at).total_seconds() * 1000))
            self._write(
                span_id="agent",
                span_type="agent",
                parent_span_id=None,
                name="agent.turn",
                status=span_status,
                started_at=None,
                ended_at=now,
                duration_ms=duration_ms,
                input=None,
                output=self.root_output,
                attributes=dict(self.root_attributes),
                error=error_detail,
            )
            # Safety net for spans whose settling event was dropped (cancel,
            # timeout, worker crash). Never downgrades a terminal status.
            self.repo.settle_open_spans(self.run_id, span_status, ended_at=now)
        except Exception:  # pragma: no cover - capture must never fail a run
            logger.warning("Failed to finalize agent spans for run %s", self.run_id, exc_info=True)

    def settle(self, status: str) -> None:
        """Close every still-open span without needing the in-memory tree."""
        if not self.enabled:
            return
        span_status = _RUN_TO_SPAN_STATUS.get(status, status)
        try:
            self.repo.settle_open_spans(self.run_id, span_status)
        except Exception:  # pragma: no cover - capture must never fail a run
            logger.warning("Failed to settle agent spans for run %s", self.run_id, exc_info=True)

    # -------------------------------------------------------------- handlers

    def _handle(self, event: Any) -> None:
        if isinstance(event, AgentModelRequest):
            self._on_model_request(event)
        elif isinstance(event, AgentModelResponse):
            self._on_model_response(event)
        elif isinstance(event, AgentContextCompressed):
            self.pending_compression = _bounded(
                _redact(dict(event.stats or {})),
                self.options.max_bytes,
            )
        elif isinstance(event, (AgentRunStarted, AgentRunEnded)):
            self._on_run_event(event)
        elif isinstance(event, AgentError):
            self.root_error = event.error
            self.root_attributes["sdk_error"] = _bounded(
                {"error": _redact(event.error), "type": event.type},
                self.options.max_bytes,
            )
        elif isinstance(event, AgentToolCall):
            self._on_tool_call(event)
        elif isinstance(event, AgentToolResult):
            self._on_tool_result(event)

    def _on_run_event(self, event: Any) -> None:
        if isinstance(event, AgentRunEnded) and isinstance(event.stop_reason, str):
            self.root_attributes["stop_reason"] = event.stop_reason

    def _on_model_request(self, event: AgentModelRequest) -> None:
        span_id = self._llm_span_id(event.seq, event.iteration)
        attributes: dict[str, Any] = {
            "model": event.model,
            "prompt_mode": "full" if event.prompt is not None else "summary",
            "message_count": event.message_count,
            "roles": event.roles,
            "last_message_hash": event.last_message_hash,
            "tool_names": _tool_names(event.tools),
        }
        if self.pending_compression is not None:
            attributes["context_compressed"] = self.pending_compression
            self.pending_compression = None
        if event.prompt is not None:
            input_payload = {"messages": _bounded(_redact(event.prompt), self.options.max_bytes)}
        else:
            input_payload = {
                "summary": {
                    "message_count": event.message_count,
                    "roles": event.roles,
                    "last_message_hash": event.last_message_hash,
                }
            }
        span = {
            "span_id": span_id,
            "span_type": "llm",
            "name": f"llm.turn.{event.iteration}",
            "status": "running",
            "iteration": event.iteration,
            "started_at": utcnow(),
            "ended_at": None,
            "duration_ms": None,
            "input": input_payload,
            "output": {},
            "attributes": attributes,
            "error": None,
        }
        self.llm_spans[span_id] = span
        self.pending_llm[event.iteration].append(span_id)
        self._write(**span)

    def _on_model_response(self, event: AgentModelResponse) -> None:
        span_id = None
        pending = self.pending_llm.get(event.iteration)
        if pending:
            span_id = pending.popleft()
        if span_id is None or span_id not in self.llm_spans:
            span_id = self._llm_span_id(event.seq, event.iteration)
            span = {
                "span_id": span_id,
                "span_type": "llm",
                "name": f"llm.turn.{event.iteration}",
                "status": "running",
                "iteration": event.iteration,
                "started_at": utcnow(),
                "ended_at": None,
                "duration_ms": None,
                "input": {},
                "output": {},
                "attributes": {},
                "error": None,
            }
            self.llm_spans[span_id] = span
        span = self.llm_spans[span_id]
        span["output"] = {
            "content": _bounded(_redact(event.content), self.options.max_bytes),
            "tool_calls": _bounded(_redact(event.tool_calls), self.options.max_bytes),
            "finish_reason": event.finish_reason,
        }
        span["attributes"]["finish_reason"] = event.finish_reason
        self._close_span(span, "completed", utcnow())
        self._write(**span)

    def _on_tool_call(self, event: AgentToolCall) -> None:
        span_id = _tool_span_id(event.tool_call_id)
        if self.options.tool_content:
            input_payload = {
                "arguments": _bounded(_redact(event.arguments), self.options.max_bytes),
                "raw_arguments": _bounded(_redact(event.raw_arguments), self.options.max_bytes),
                "arguments_error": event.arguments_error,
            }
        else:
            input_payload = {"arguments_hash": hashlib.sha256(repr(event.arguments).encode()).hexdigest()[:16]}
        span = {
            "span_id": span_id,
            "span_type": "tool",
            "name": event.name,
            "status": "running",
            "iteration": event.iteration,
            "started_at": utcnow(),
            "ended_at": None,
            "duration_ms": None,
            "input": input_payload,
            "output": {},
            "attributes": {"tool_call_id": event.tool_call_id},
            "error": None,
        }
        existing = self.tool_spans.get(span_id)
        if existing is not None:
            span["started_at"] = existing["started_at"]
        self.tool_spans[span_id] = span
        self._write(**span)

    def _on_tool_result(self, event: AgentToolResult) -> None:
        span_id = _tool_span_id(event.tool_call_id)
        span = self.tool_spans.get(span_id)
        if span is None:
            span = {
                "span_id": span_id,
                "span_type": "tool",
                "name": event.name,
                "status": "running",
                "iteration": event.iteration,
                "started_at": utcnow(),
                "ended_at": None,
                "duration_ms": None,
                "input": {},
                "output": {},
                "attributes": {"tool_call_id": event.tool_call_id},
                "error": None,
            }
            self.tool_spans[span_id] = span
        if self.options.tool_content:
            span["output"] = {
                "content": _bounded(_redact(event.content), self.options.max_bytes),
                "is_error": event.is_error,
            }
        else:
            span["output"] = {"content_chars": len(event.content or ""), "is_error": event.is_error}
        span["status"] = (
            event.status
            if event.status in {"completed", "failed", "aborted"}
            else ("failed" if event.is_error else "completed")
        )
        if event.duration_ms is not None:
            span["duration_ms"] = event.duration_ms
        self._close_span(span, span["status"], utcnow())
        if event.is_error:
            span["error"] = _bounded(event.content, self.options.max_bytes)
            if not isinstance(span["error"], str):
                span["error"] = json.dumps(span["error"], ensure_ascii=False, default=str)
        self._write(**span)

    # --------------------------------------------------------------- helpers

    def _llm_span_id(self, seq: int | None, iteration: int) -> str:
        if seq is not None:
            return f"llm:{seq}"
        self._llm_fallback += 1
        return f"llm:{iteration}:{self._llm_fallback}"

    @staticmethod
    def _close_span(span: dict[str, Any], status: str, ended_at: datetime) -> None:
        span["status"] = status
        span["ended_at"] = ended_at
        started_at = span.get("started_at")
        if isinstance(started_at, datetime) and span.get("duration_ms") is None:
            span["duration_ms"] = max(0, int((ended_at - started_at).total_seconds() * 1000))

    def _write(
        self,
        *,
        span_id: str,
        span_type: str,
        name: str,
        status: str,
        started_at: datetime | None,
        ended_at: datetime | None = None,
        duration_ms: int | None = None,
        input: dict[str, Any] | None = None,
        output: dict[str, Any] | None = None,
        attributes: dict[str, Any] | None = None,
        error: str | None = None,
        iteration: int | None = None,
        parent_span_id: str | None = "agent",
    ) -> None:
        self.repo.upsert_span(
            self.run_id,
            span_id=span_id,
            span_type=span_type,
            parent_span_id=parent_span_id,
            name=name,
            status=status,
            iteration=iteration,
            started_at=started_at,
            ended_at=ended_at,
            duration_ms=duration_ms,
            input=input,
            output=output,
            attributes=attributes,
            error=error,
        )
