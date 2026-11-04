"""Run-scoped no-progress detection for the ReAct loop.

A model can burn the whole step budget on calls that return nothing new:
the same query, the same URL, the same page under a different address, or an
error it keeps retrying. The guard fingerprints the *evidence* each tool call
returns and counts consecutive calls that add no new evidence. After a few it
asks the model to change course; after a few more it reports ``stop`` so the
runtime can force a final answer.

Evidence is deliberately content-based, not call-based: fetching a second URL
that returns an identical body (a fallback page, a paywall) is no progress,
while reading a different page or a different range of a file is.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from ..core.types import ToolResult

ProgressAction = Literal["ok", "nudge", "stop"]


class ProgressGuard:
    """Count consecutive tool calls that return no new evidence."""

    def __init__(self, nudge_after: int = 3, stop_after: int = 5) -> None:
        self._nudge_after = max(2, nudge_after)
        self._stop_after = max(self._nudge_after, stop_after)
        self._no_progress = 0
        self._nudged = False
        self._evidence: set[str] = set()
        self._last_was_progress = False

    @property
    def stalled_calls(self) -> int:
        """Consecutive calls without new evidence, for observability and tests."""

        return self._no_progress

    @property
    def last_was_progress(self) -> bool:
        """Whether the most recent call added evidence this run had not seen."""

        return self._last_was_progress

    def record(self, result: ToolResult) -> ProgressAction:
        """Account for one tool call and return the action the runtime should take.

        The same call counts as progress only if it produced evidence this run
        has not seen before; errors and empty results never do.
        """

        signature = _evidence_signature(result)
        self._last_was_progress = False
        if signature is None or signature in self._evidence:
            self._no_progress += 1
        else:
            self._evidence.add(signature)
            self._no_progress = 0
            self._nudged = False
            self._last_was_progress = True

        if self._no_progress >= self._stop_after:
            return "stop"
        if self._no_progress >= self._nudge_after and not self._nudged:
            self._nudged = True
            return "nudge"
        return "ok"


def _evidence_signature(result: ToolResult) -> str | None:
    """Hash the new information in a result, or ``None`` when it carries none."""

    if result.is_error:
        return None
    text = (result.content or "").strip()
    if not text:
        return None

    payload = _load_json(text)
    if isinstance(payload, dict):
        if payload.get("error"):
            return None
        if payload.get("cached"):
            # A cache hit is by definition a result this run already has, even
            # when the server returns a shortened digest of it.
            return None
        results = payload.get("results")
        if isinstance(results, list):
            urls = sorted(
                {
                    _normalize_url(str(item.get("url") or ""))
                    for item in results
                    if isinstance(item, dict) and str(item.get("url") or "").strip()
                }
            )
            return f"search:{_digest(chr(10).join(urls))}" if urls else None
        content = payload.get("content")
        if isinstance(content, str):
            return f"content:{_digest(content)}" if content.strip() else None
        if "stdout" in payload:
            stdout = str(payload.get("stdout") or "")
            return f"stdout:{_digest(stdout)}" if stdout.strip() else None
    if text in {"{}", "[]"}:
        return None
    return f"text:{_digest(text)}"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize_url(url: str) -> str:
    # Paths are case-sensitive, so only trim the decorations search engines add.
    return url.strip().rstrip("/")


def _load_json(text: str) -> Any:
    if text[0] not in "[{":
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None
