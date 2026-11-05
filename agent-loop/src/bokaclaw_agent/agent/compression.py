"""Token-budget compression for the model-visible conversation view.

The compressed view is built on every request and never written back: session
stores, ``new_messages``, and ``RunResult.tool_results`` keep full content.
Only the messages sent to the model are summarized or dropped.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from ..core.config import AgentConfig
from ..core.types import ModelResponse
from ..core.utils import resolve_awaitable
from ..providers.base import ModelProvider
from .context import (
    estimate_message_tokens,
    estimate_messages_tokens,
    safe_start_index,
    truncate_text,
)

logger = logging.getLogger(__name__)

SUMMARY_MAX_TOKENS = 1024
SUMMARY_MAX_CHARS = 8_000
SUMMARY_INPUT_MAX_CHARS = 24_000
SUMMARY_MESSAGE_MAX_CHARS = 2_000
SUMMARY_MIN_FOLD_TOKENS = 256

SUMMARY_SYSTEM_PROMPT = (
    "You compress earlier conversation history for a tool-using AI agent so it can continue "
    "the task. Preserve the user's goals and constraints, decisions already made, concrete "
    "facts and tool findings (paths, IDs, numbers, errors), and unresolved next steps. Do not "
    "invent facts. Reply with a compact plain-text summary in the language of the conversation."
)


@dataclass(slots=True)
class CompressionResult:
    """The model view plus what changed while building it."""

    messages: list[dict[str, Any]]
    changed: bool = False
    stats: dict[str, Any] = field(default_factory=dict)


class ContextCompressor:
    """Keeps a model request inside an approximate token budget.

    Per-run state (the rolling summary) lives here; it is never persisted.
    """

    def __init__(self, config: AgentConfig, provider: ModelProvider, usage: dict[str, int]) -> None:
        self.max_tokens = config.context_max_tokens
        self.keep_recent_tokens = config.context_keep_recent_tokens
        self.mode = config.context_compression
        self.provider = provider
        self.usage = usage
        self._summary: str | None = None
        self._folded_until = 0
        self._summary_error: str | None = None
        self._call_usage: dict[str, int] = {}
        self._call_count = 0
        self._call_unknown = 0

    def compress(self, messages: list[dict[str, Any]], *, tools_tokens: int = 0) -> CompressionResult:
        self._call_usage = {}
        self._call_count = 0
        self._call_unknown = 0
        if self.max_tokens is None:
            return CompressionResult(messages)

        budget = self.max_tokens - max(0, tools_tokens)
        before = estimate_messages_tokens(messages)
        if before <= budget:
            return CompressionResult(messages)

        system_end = 1 if messages and messages[0].get("role") == "system" else 0
        keep_recent = self.keep_recent_tokens
        if keep_recent is None:
            keep_recent = max(1024, budget // 2)
        keep_recent = max(0, min(keep_recent, budget - 256))

        cut = max(self._find_cut(messages, system_end, keep_recent), self._folded_until)
        cut = min(cut, len(messages))

        summarized = False
        fallback = False
        dropped_count = 0

        if cut <= system_end:
            # No message can be dropped at a valid boundary; only shrink content.
            view = list(messages)
        else:
            last_user = self._last_user_index(messages, system_end, cut)
            dropped_count = max(0, cut - system_end - (1 if last_user is not None else 0))
            # Everything before the cut is covered by the summary, including the
            # pinned user turn: it is both summarized and kept, so a later turn
            # can drop it without losing the instruction entirely.
            new_indices = [index for index in range(system_end, cut) if index >= self._folded_until]

            if new_indices:
                if self.mode == "summary":
                    folded = [messages[index] for index in new_indices]
                    if estimate_messages_tokens(folded) >= SUMMARY_MIN_FOLD_TOKENS:
                        summary = self._summarize(folded, self._summary)
                        if summary is not None:
                            self._summary = summary
                            self._summary_error = None
                            summarized = True
                        else:
                            fallback = True
                    else:
                        # Too little to be worth a model call; drop without a summary.
                        fallback = True
                else:
                    fallback = True
                self._folded_until = cut

            tail: list[dict[str, Any]] = list(messages[cut:])
            if last_user is not None:
                tail = [messages[last_user], *tail]

            notes: list[str] = []
            if self._summary:
                notes.append("## Summary of earlier conversation\n" + self._summary)
            if fallback and dropped_count:
                notes.append(f"[Note: {dropped_count} earlier messages were omitted to fit the context window.]")
            if notes:
                system_content = messages[0]["content"] if system_end else ""
                prefix = f"{system_content}\n\n" if isinstance(system_content, str) and system_content else ""
                view = [{"role": "system", "content": prefix + "\n\n".join(notes)}, *tail]
            else:
                view = [*messages[:system_end], *tail]

        view = self._fit_budget(view, budget)
        after = estimate_messages_tokens(view)
        stats: dict[str, Any] = {
            "before_tokens": before,
            "after_tokens": after,
            "dropped_messages": dropped_count,
            "summarized": summarized,
            "fallback": fallback,
        }
        if self._call_count:
            if self._call_usage:
                stats["usage"] = dict(self._call_usage)
            stats["model_calls"] = self._call_count
            stats["usage_unknown_calls"] = self._call_unknown
        self._call_usage = {}
        self._call_count = 0
        self._call_unknown = 0
        return CompressionResult(
            messages=view,
            changed=view != messages,
            stats=stats,
        )

    def _find_cut(self, messages: list[dict[str, Any]], system_end: int, keep_recent: int) -> int:
        total = 0
        start = len(messages)
        for index in range(len(messages) - 1, system_end - 1, -1):
            total += estimate_message_tokens(messages[index])
            start = index
            if total >= keep_recent:
                break

        safe = safe_start_index(messages, start, allow_tool_call_blocks=True)
        if safe < len(messages):
            return safe
        # No boundary at or after the candidate: walk back to the newest one.
        for index in range(min(start, len(messages) - 1), system_end - 1, -1):
            message = messages[index]
            if message.get("role") in {"user", "assistant"}:
                return index
        return system_end

    @staticmethod
    def _last_user_index(messages: list[dict[str, Any]], start: int, end: int) -> int | None:
        for index in range(end - 1, start - 1, -1):
            if messages[index].get("role") == "user":
                return index
        return None

    def _summarize(self, messages: list[dict[str, Any]], previous: str | None) -> str | None:
        transcript = _format_transcript(messages)
        if not transcript:
            return None

        parts: list[str] = []
        if previous:
            parts.append("Previous summary:\n" + previous)
        parts.append("New messages to fold in:\n" + transcript)
        request = [
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": "\n\n".join(parts)},
        ]

        self._call_count += 1
        try:
            response = self.provider.complete(
                request,
                tools=None,
                tool_choice=None,
                temperature=0,
                max_tokens=SUMMARY_MAX_TOKENS,
            )
            response = resolve_awaitable(response)
        except Exception as exc:  # noqa: BLE001 - summarization is best effort
            self._call_unknown += 1
            self._summary_error = f"{type(exc).__name__}: {exc}"
            logger.warning("Context summarization failed; falling back to truncation: %s", exc)
            return None

        if isinstance(response, dict):
            try:
                response = ModelResponse.model_validate(response)
            except Exception as exc:  # noqa: BLE001
                self._call_unknown += 1
                self._summary_error = f"invalid summarizer response: {exc}"
                logger.warning("Context summarization returned an invalid response: %s", exc)
                return None
        if not isinstance(response, ModelResponse):
            self._call_unknown += 1
            self._summary_error = f"provider returned {type(response).__name__}"
            logger.warning("Context summarization returned %s, expected ModelResponse", type(response).__name__)
            return None

        usage = response.usage or {}
        if not usage:
            self._call_unknown += 1
        self._merge_usage(usage)
        self._merge_into(self._call_usage, usage)
        content = (response.content or "").strip()
        if not content:
            self._summary_error = "empty summary"
            return None

        capped, _ = truncate_text(content, SUMMARY_MAX_CHARS)
        return capped

    def _merge_usage(self, usage: dict[str, int] | None) -> None:
        self._merge_into(self.usage, usage)

    @staticmethod
    def _merge_into(total: dict[str, int], usage: dict[str, int] | None) -> None:
        for key, value in (usage or {}).items():
            if isinstance(value, int):
                total[key] = total.get(key, 0) + value

    @staticmethod
    def _fit_budget(messages: list[dict[str, Any]], budget: int) -> list[dict[str, Any]]:
        """Shrink the largest message contents until the view fits.

        Structure is never changed here, so tool-call pairs stay intact.
        """

        for _ in range(64):
            current = estimate_messages_tokens(messages)
            if current <= budget:
                break

            candidates = [
                (estimate_message_tokens(message), index)
                for index, message in enumerate(messages)
                if message.get("role") != "system"
                and isinstance(message.get("content"), str)
                and len(message["content"]) > 32
            ]
            if not candidates:
                break

            _, index = max(candidates, key=lambda item: item[0])
            content = messages[index]["content"]
            excess = max(64, current - budget)
            target = max(64, len(content) - excess)
            if target >= len(content):
                target = max(64, len(content) // 2)
            capped, _ = truncate_text(content, target)
            messages[index] = {**messages[index], "content": capped}

        return messages


def _format_transcript(messages: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for message in messages:
        role = str(message.get("role") or "unknown")
        content = message.get("content")
        text = content if isinstance(content, str) else _json_text(content)

        tool_calls = message.get("tool_calls")
        if tool_calls:
            text = (text + "\n" if text else "") + "tool_calls: " + _json_text(tool_calls)

        text = truncate_text(text, SUMMARY_MESSAGE_MAX_CHARS)[0]
        if role == "tool" and message.get("tool_call_id"):
            lines.append(f"tool {message['tool_call_id']}: {text}")
        else:
            lines.append(f"{role}: {text}")

    transcript = "\n".join(lines)
    if len(transcript) > SUMMARY_INPUT_MAX_CHARS:
        transcript = transcript[-SUMMARY_INPUT_MAX_CHARS:]
    return transcript


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)
