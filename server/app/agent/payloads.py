"""Event payload builders shared by the legacy and background paths."""

from __future__ import annotations

import uuid
from typing import Any

from app.agent.trace import ToolTrace


def tool_call_payload(conversation_id: uuid.UUID, assistant_id: uuid.UUID, entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "conversation_id": str(conversation_id),
        "message_id": str(assistant_id),
        "tool_call_id": entry["tool_call_id"],
        "name": entry["name"],
        "iteration": entry["iteration"],
        "status": entry["status"],
        "arguments": entry["arguments"],
        "raw_arguments": entry["raw_arguments"],
        "arguments_error": entry["arguments_error"],
        "started_at": entry["started_at"],
        "truncated": entry["truncated"],
    }


def tool_result_payload(conversation_id: uuid.UUID, assistant_id: uuid.UUID, entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "conversation_id": str(conversation_id),
        "message_id": str(assistant_id),
        "tool_call_id": entry["tool_call_id"],
        "name": entry["name"],
        "iteration": entry["iteration"],
        "status": entry["status"],
        "content": entry["content"],
        "is_error": entry["is_error"],
        "duration_ms": entry["duration_ms"],
        "finished_at": entry["finished_at"],
        "truncated": entry["truncated"],
    }


def attach_trace(trace: ToolTrace, metadata: dict[str, Any]) -> None:
    entries = trace.snapshot()
    if entries:
        metadata["tool_trace"] = entries


def should_persist_reply(content: str, trace: ToolTrace) -> bool:
    return bool(content) or trace.has_entries()
