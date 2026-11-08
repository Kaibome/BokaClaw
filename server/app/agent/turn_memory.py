"""Combine episodic capture and research-conclusion saving for one turn."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

from app.agent.research_memory import save_research

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
) -> TurnMemorySave:
    """Best-effort: append an episode and persist a research conclusion."""

    report = TurnMemorySave(question=str(payload.get("message") or "")[:200])
    trace = list(entries or [])
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    try:
        from app.core.server_state import get_memory_instance

        service = get_memory_instance()
        capture = service.capture_turn(
            str(payload.get("user_id") or ""),
            str(payload.get("message") or ""),
            content or "",
            tool_entries=trace,
            metadata=metadata,
            conversation_id=payload.get("conversation_id"),
            run_id=payload.get("run_id"),
            trigger=metadata.get("trigger"),
            is_subagent=bool(metadata.get("is_subagent") or metadata.get("subagent")),
            used_memory_tool=_used_memory_tool(trace),
        )
    except Exception as exc:
        logger.warning("Episodic capture failed for run %s", payload.get("run_id"), exc_info=True)
        report.error = f"{type(exc).__name__}: {exc}"
        capture = None

    research = save_research(payload, content, trace)

    report.sources = research.sources
    report.chars = (capture.chars if capture is not None else 0) + research.chars
    report.memory_id = (capture.memory_id if capture is not None else None) or research.memory_id
    report.path = capture.path if capture is not None else None
    report.error = report.error or research.error
    if capture is not None and capture.action == "appended":
        report.action = "appended"
        report.kind = "episode"
        report.captured = 1
        report.entries += 1
    elif research.action in {"added", "updated"}:
        report.action = research.action
        report.kind = "research_conclusion"
        report.entries += 1
    elif capture is not None:
        report.action = capture.action
        report.reason = capture.reason or research.reason
    else:
        report.action = "failed"
        report.reason = research.reason
    return report
