"""Run-scoped ledger of tool calls and findings for the model view.

Context compression can fold early tool results into a summary, which makes an
agent forget facts it already gathered and search or fetch them again. The
ledger keeps a compact, deduplicated record of what each call returned and is
injected into the request *after* compression, so the facts survive while the
full tool messages stay free to be compressed. Nothing here is persisted.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from ..core.types import ToolCall, ToolResult

DEFAULT_LEDGER_MAX_CHARS = 2_500
MAX_ENTRIES = 24
SUMMARY_CHARS = 240
ARGS_CHARS = 120

_WHITESPACE = re.compile(r"\s+")


@dataclass(slots=True)
class _Entry:
    key: str
    name: str
    args: str
    summary: str
    ok: bool
    repeats: int = 1


class ToolLedger:
    """Compact record of tool calls and findings for one run."""

    def __init__(self, max_entries: int = MAX_ENTRIES) -> None:
        self._entries: list[_Entry] = []
        self._by_key: dict[str, _Entry] = {}
        self._max_entries = max(1, max_entries)

    def record(self, call: ToolCall, result: ToolResult) -> None:
        """Add or refresh one call. Repeated identical calls are counted, not duplicated."""

        key = _call_key(call)
        summary = _summarize(result)
        existing = self._by_key.get(key)
        if existing is not None:
            existing.repeats += 1
            existing.summary = summary
            existing.ok = not result.is_error
            return

        entry = _Entry(
            key=key,
            name=call.name,
            args=_format_args(call),
            summary=summary,
            ok=not result.is_error,
        )
        self._entries.append(entry)
        self._by_key[key] = entry
        if len(self._entries) > self._max_entries:
            dropped = self._entries.pop(0)
            self._by_key.pop(dropped.key, None)

    def render(self, max_chars: int = DEFAULT_LEDGER_MAX_CHARS) -> str | None:
        """Return the model-view block, or ``None`` when nothing was recorded."""

        if not self._entries:
            return None

        header = [
            "## Research state (this run)",
            "Facts already gathered. Do not repeat these calls; reuse these results and sources.",
        ]
        lines = list(header)
        for entry in self._entries:
            marker = "ok" if entry.ok else "ERROR"
            repeat = f" (x{entry.repeats})" if entry.repeats > 1 else ""
            lines.append(f"- {entry.name}{repeat} {entry.args} -> {marker}: {entry.summary}")

        omitted = 0
        while len(lines) > len(header) + 1 and _length(lines) > max_chars:
            lines.pop(len(header))
            omitted += 1
        if omitted:
            lines.append(f"- ({omitted} earlier calls omitted)")
        text = "\n".join(lines)
        if len(text) > max_chars:
            # A single entry can still exceed a very small budget. Keep the
            # header and cut the entry instead of dropping the context.
            head = "\n".join(header)
            keep = max_chars - len(head) - 1
            if keep > 0 and len(lines) > len(header):
                text = head + "\n" + lines[len(header)][:keep]
            else:
                text = text[:max_chars]
        return text


def _call_key(call: ToolCall) -> str:
    try:
        args = json.dumps(call.arguments or {}, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        args = str(call.arguments)
    return f"{call.name}:{args}"


def _format_args(call: ToolCall) -> str:
    args = call.arguments if isinstance(call.arguments, dict) else {}
    for key in ("query", "url", "file_path", "path", "pattern", "command"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            text = _one_line(value)[:ARGS_CHARS]
            return f'"{text}"' if key in {"query", "command", "pattern"} else text
    try:
        compact = json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        compact = str(args)
    return _one_line(compact)[:ARGS_CHARS]


def _summarize(result: ToolResult) -> str:
    text = result.content or ""
    payload = _load_json(text)
    if isinstance(payload, dict):
        error = payload.get("error")
        if error:
            return _cap(f"error: {error}")
        results = payload.get("results")
        if isinstance(results, list):
            if not results:
                return "no results"
            parts: list[str] = []
            for item in results[:3]:
                if not isinstance(item, dict) or not item.get("url"):
                    continue
                title = _one_line(str(item.get("title") or ""))[:60]
                url = _one_line(str(item["url"]))
                parts.append(f"{title} <{url}>" if title else f"<{url}>")
            summary = f"{len(results)} results"
            if parts:
                summary += ": " + "; ".join(parts)
            return _cap(summary)
        content = payload.get("content")
        url = payload.get("url")
        if isinstance(content, str) and url:
            summary = f"{len(content)} chars"
            if payload.get("cached"):
                summary += " (already fetched)"
            if payload.get("truncated"):
                summary += " (truncated)"
            head = _one_line(content)[:SUMMARY_CHARS]
            return _cap(f"{summary} from {_one_line(str(url))}: {head}")
        if "stdout" in payload:
            head = _one_line(str(payload.get("stdout") or ""))
            return _cap(head or f"exit {payload.get('exit_code')}")
    return _cap(_one_line(text))


def _load_json(text: str) -> Any:
    stripped = text.strip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        return json.loads(stripped)
    except (TypeError, ValueError):
        return None


def _one_line(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def _cap(text: str) -> str:
    if len(text) <= SUMMARY_CHARS:
        return text
    return text[: SUMMARY_CHARS - 3] + "..."


def _length(lines: list[str]) -> int:
    return sum(len(line) + 1 for line in lines)
