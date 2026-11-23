"""Append one filtered episode at the end of a completed turn."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

logger = logging.getLogger(__name__)

_MEMORY_WRITE_TOOLS = {"save_memory", "update_memory", "delete_memory", "memory_add", "memory_update", "memory_delete"}


@dataclass
class TurnMemorySave:
    action: str = "skipped"
    question: str = ""
    memory_id: Optional[str] = None
    sources: int = 0
    chars: int = 0
    reason: Optional[str] = None
    error: Optional[str] = None
    kind: Optional[str] = None
    path: Optional[str] = None
    captured: int = 0
    entries: int = 0
    duration_ms: Optional[int] = None


def _attachment_names(payload: Dict[str, Any]) -> list[str]:
    names: list[str] = []
    descriptors = payload.get("attachments")
    if isinstance(descriptors, list):
        for descriptor in descriptors:
            if isinstance(descriptor, dict) and descriptor.get("name"):
                names.append(str(descriptor["name"])[:80])
    if not names:
        for attachment_id in payload.get("attachment_ids") or []:
            if isinstance(attachment_id, str):
                names.append(attachment_id[:80])
    return names


def _memory_question(payload: Dict[str, Any]) -> str:
    message = str(payload.get("message") or "").strip()
    if message:
        return message
    names = _attachment_names(payload)
    if names:
        return f"用户上传了图片：{', '.join(names[:2])}"
    return ""


def _used_memory_tool(entries: Iterable[Dict[str, Any]]) -> bool:
    for entry in entries or ():
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").replace("-", "_").lower()
        if name in _MEMORY_WRITE_TOOLS:
            return True
    return False


def save_turn_memory(
    payload: Dict[str, Any],
    content: str,
    entries: Iterable[Dict[str, Any]],
    *,
    memory_injected: bool = False,
) -> TurnMemorySave:
    """Best-effort: append one filtered episode for a completed turn.

    Research conclusions are no longer persisted: the web tools are cheap to
    call again, and keeping their answers in memory added review and cleanup
    cost without improving the next turn.
    """

    question = _memory_question(payload)
    report = TurnMemorySave(question=question[:200])
    trace = list(entries or [])
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    if metadata.get("benchmark"):
        report.reason = "benchmark"
        return report
    try:
        from app.core.server_state import get_memory_instance

        service = get_memory_instance()
        capture = service.capture_turn(
            str(payload.get("user_id") or ""),
            question,
            content or "",
            tool_entries=trace,
            metadata=metadata,
            conversation_id=payload.get("conversation_id"),
            run_id=payload.get("run_id"),
            trigger=metadata.get("trigger"),
            is_subagent=bool(metadata.get("is_subagent") or metadata.get("subagent")),
            used_memory_tool=_used_memory_tool(trace),
            injected_memory=memory_injected,
            has_attachments=bool(_attachment_names(payload)) or bool(payload.get("attachment_ids")),
        )
    except Exception as exc:
        logger.warning("Episodic capture failed for run %s", payload.get("run_id"), exc_info=True)
        report.error = f"{type(exc).__name__}: {exc}"
        report.action = "failed"
        return report

    report.chars = capture.chars
    report.memory_id = capture.memory_id
    report.path = capture.path
    if capture.action == "appended":
        report.action = "appended"
        report.kind = "episode"
        report.captured = 1
        report.entries = 1
    else:
        report.action = capture.action
        report.reason = capture.reason
    return report
