"""Accumulate one ReAct turn's tool calls for SSE and message metadata.

The trace is keyed by ``tool_call_id`` so a call and its result collapse into
one row even when events arrive out of order or are duplicated. It owns the
timing (wall clock for ``started_at`` / ``finished_at``, monotonic clock for
``duration_ms``) and enforces the size budgets applied to both live SSE frames
and the persisted ``metadata.tool_trace``.
"""

import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

DEFAULT_MAX_BYTES = 16_384
DEFAULT_MAX_ARGUMENT_BYTES = 4_096
DEFAULT_MAX_TOTAL_BYTES = 65_536
TERMINAL_STATUSES = {"completed", "failed", "aborted"}


def _env_int(name: str, default: int, minimum: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def _cap_text(text: str, max_bytes: int) -> tuple[str, bool]:
    """Keep the head and tail of an oversized UTF-8 string."""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text, False

    marker = "\n... [truncated] ...\n"
    budget = max(0, max_bytes - len(marker.encode()))
    head = encoded[: budget // 2].decode("utf-8", errors="ignore")
    tail = encoded[-(budget - budget // 2) :].decode("utf-8", errors="ignore")
    return f"{head}{marker}{tail}", True


def _json_safe(value: Any) -> Any:
    if value is None:
        return None
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


class ToolTrace:
    """Ordered, id-addressed record of the tool calls in one assistant turn."""

    def __init__(
        self,
        *,
        max_bytes: Optional[int] = None,
        max_argument_bytes: Optional[int] = None,
        max_total_bytes: Optional[int] = None,
        clock: Callable[[], float] = time.monotonic,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.max_bytes = (
            max_bytes
            if max_bytes is not None
            else _env_int("AGENT_TOOL_TRACE_MAX_BYTES", DEFAULT_MAX_BYTES, 128)
        )
        self.max_argument_bytes = (
            max_argument_bytes
            if max_argument_bytes is not None
            else _env_int("AGENT_TOOL_TRACE_MAX_ARGUMENT_BYTES", DEFAULT_MAX_ARGUMENT_BYTES, 64)
        )
        self.max_total_bytes = (
            max_total_bytes
            if max_total_bytes is not None
            else _env_int("AGENT_TOOL_TRACE_MAX_TOTAL_BYTES", DEFAULT_MAX_TOTAL_BYTES, 1_024)
        )
        self._clock = clock
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._entries: Dict[str, Dict[str, Any]] = {}
        self._order: List[str] = []
        self._started: Dict[str, float] = {}

    def has_entries(self) -> bool:
        return bool(self._order)

    def start(
        self,
        tool_call_id: str,
        name: str,
        *,
        iteration: int = 0,
        arguments: Optional[Dict[str, Any]] = None,
        raw_arguments: Optional[str] = None,
        arguments_error: Optional[str] = None,
    ) -> Dict[str, Any]:
        entry = self._entries.get(tool_call_id)
        if entry is not None and entry["status"] != "running":
            # A late duplicate call must not reopen a settled row.
            return dict(entry)

        if entry is None:
            entry = {
                "tool_call_id": tool_call_id,
                "name": name,
                "iteration": iteration,
                "status": "running",
                "arguments": None,
                "raw_arguments": None,
                "arguments_error": None,
                "content": "",
                "is_error": False,
                "started_at": self._now().isoformat(),
                "finished_at": None,
                "duration_ms": None,
                "truncated": False,
            }
            self._entries[tool_call_id] = entry
            self._order.append(tool_call_id)
            self._started[tool_call_id] = self._clock()

        entry["name"] = name or entry["name"]
        entry["iteration"] = iteration
        entry["arguments_error"] = arguments_error or entry["arguments_error"]
        self._store_arguments(entry, arguments, raw_arguments)
        return dict(entry)

    def finish(
        self,
        tool_call_id: str,
        name: str,
        content: str,
        *,
        is_error: bool = False,
        iteration: int = 0,
        status: Optional[str] = None,
        duration_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        entry = self._entries.get(tool_call_id)
        if entry is None:
            # A remote executor may send a result without a preceding call.
            entry = {
                "tool_call_id": tool_call_id,
                "name": name,
                "iteration": iteration,
                "status": "running",
                "arguments": None,
                "raw_arguments": None,
                "arguments_error": None,
                "content": "",
                "is_error": False,
                "started_at": None,
                "finished_at": None,
                "duration_ms": None,
                "truncated": False,
            }
            self._entries[tool_call_id] = entry
            self._order.append(tool_call_id)

        if entry["status"] in TERMINAL_STATUSES:
            return dict(entry)

        entry["name"] = name or entry["name"]
        entry["iteration"] = iteration
        entry["is_error"] = bool(is_error)
        entry["status"] = status if status in TERMINAL_STATUSES else ("failed" if is_error else "completed")
        entry["finished_at"] = self._now().isoformat()

        capped, truncated = _cap_text(content or "", self.max_bytes)
        entry["content"] = capped
        if truncated:
            entry["truncated"] = True

        if duration_ms is None:
            started = self._started.pop(tool_call_id, None)
            if started is not None:
                duration_ms = max(0, int(round((self._clock() - started) * 1000)))
        entry["duration_ms"] = duration_ms
        return dict(entry)

    def abort_open(self, reason: str) -> List[Dict[str, Any]]:
        """Settle every still-running row as ``aborted`` and return the changes."""
        aborted: List[Dict[str, Any]] = []
        for tool_call_id in self._order:
            entry = self._entries[tool_call_id]
            if entry["status"] != "running":
                continue

            entry["status"] = "aborted"
            entry["is_error"] = True
            entry["finished_at"] = self._now().isoformat()
            if not entry["content"]:
                entry["content"] = json.dumps({"error": reason}, ensure_ascii=False)

            started = self._started.pop(tool_call_id, None)
            if entry["duration_ms"] is None and started is not None:
                entry["duration_ms"] = max(0, int(round((self._clock() - started) * 1000)))
            aborted.append(dict(entry))
        return aborted

    def snapshot(self) -> List[Dict[str, Any]]:
        """Return the ordered trace, bounded by the per-message total budget."""
        remaining = self.max_total_bytes
        result: List[Dict[str, Any]] = []
        for tool_call_id in self._order:
            entry = dict(self._entries[tool_call_id])
            encoded = json.dumps(entry, ensure_ascii=False, default=str).encode("utf-8")
            if len(encoded) > remaining:
                if remaining <= 0:
                    entry["content"] = ""
                else:
                    entry["content"] = _cap_text(entry["content"], remaining)[0]
                entry["arguments"] = None
                entry["raw_arguments"] = None
                entry["truncated"] = True
                remaining = 0
            else:
                remaining -= len(encoded)
            result.append(entry)
        return result

    def _store_arguments(
        self,
        entry: Dict[str, Any],
        arguments: Optional[Dict[str, Any]],
        raw_arguments: Optional[str],
    ) -> None:
        arguments = _json_safe(arguments) if isinstance(arguments, dict) else None
        serialized = json.dumps(arguments, ensure_ascii=False, default=str) if arguments is not None else raw_arguments
        if entry.get("arguments_error") and isinstance(raw_arguments, str):
            # The provider's JSON did not parse; show the raw text, not ``{}``.
            serialized = raw_arguments
            arguments = None
        serialized = serialized if isinstance(serialized, str) else None

        if serialized is not None:
            capped, truncated = _cap_text(serialized, self.max_argument_bytes)
            if truncated:
                entry["arguments"] = None
                entry["raw_arguments"] = capped
                entry["truncated"] = True
                return

        entry["arguments"] = arguments
        entry["raw_arguments"] = serialized
