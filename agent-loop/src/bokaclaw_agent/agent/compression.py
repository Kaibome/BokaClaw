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
    content_text,
    count_message_tokens,
    count_messages_tokens,
    safe_start_index,
    truncate_text,
)
from .tokenization import TokenCounter, get_token_counter

logger = logging.getLogger(__name__)

# Caps are sized so one summary call fits a 128K-token summarizer even in the
# worst case: ~64K chunk tokens + ~12K previous-summary chars + 1.5K output.
# Large folds are split into chunks of at most SUMMARY_CHUNK_TOKENS rendered
# tokens and merged into one rolling summary; SUMMARY_INPUT_MAX_CHARS is a
# per-chunk safety cap.
SUMMARY_MAX_TOKENS = 1_536
SUMMARY_MAX_CHARS = 12_000
SUMMARY_CHUNK_TOKENS = 64_000
SUMMARY_MAX_CHUNKS = 8
SUMMARY_INPUT_MAX_CHARS = 256_000
SUMMARY_MESSAGE_MAX_CHARS = 8_000
SUMMARY_MIN_FOLD_TOKENS = 256
SUMMARY_PREVIEW_CHARS = 4_000

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

    def __init__(
        self,
        config: AgentConfig,
        provider: ModelProvider,
        usage: dict[str, int],
        *,
        token_counter: TokenCounter | None = None,
    ) -> None:
        self.max_tokens = config.context_max_tokens
        self.trigger_ratio = config.context_trigger_ratio
        self.keep_recent_tokens = config.context_keep_recent_tokens
        self.mode = config.context_compression
        self.image_token_estimate = config.image_token_estimate
        self.provider = provider
        self.usage = usage
        self.token_counter = token_counter or get_token_counter()
        self._summary: str | None = None
        self._folded_until = 0
        self._summary_error: str | None = None
        self._call_usage: dict[str, int] = {}
        self._call_count = 0
        self._call_unknown = 0
        self._chunk_count = 0
        self._chunk_failures = 0

    def compress(self, messages: list[dict[str, Any]], *, tools_tokens: int = 0) -> CompressionResult:
        self._call_usage = {}
        self._call_count = 0
        self._call_unknown = 0
        self._chunk_count = 0
        self._chunk_failures = 0
        if self.max_tokens is None:
            return CompressionResult(messages)

        trigger_tokens = int(self.max_tokens * self.trigger_ratio)
        budget = trigger_tokens - max(0, tools_tokens)
        before = count_messages_tokens(
            messages,
            counter=self.token_counter,
            image_token_estimate=self.image_token_estimate,
        )
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
                    if (
                        count_messages_tokens(
                            folded,
                            counter=self.token_counter,
                            image_token_estimate=self.image_token_estimate,
                        )
                        >= SUMMARY_MIN_FOLD_TOKENS
                    ):
                        summary = self._summarize_chunks(self._summary_chunks(folded))
                        if summary is not None:
                            self._summary = summary
                            if self._chunk_failures == 0:
                                self._summary_error = None
                            summarized = True
                            fallback = self._chunk_failures > 0
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
        after = count_messages_tokens(
            view,
            counter=self.token_counter,
            image_token_estimate=self.image_token_estimate,
        )
        stats: dict[str, Any] = {
            "before_tokens": before,
            "after_tokens": after,
            "trigger_tokens": trigger_tokens,
            "trigger_ratio": self.trigger_ratio,
            "dropped_messages": dropped_count,
            "summarized": summarized,
            "fallback": fallback,
            # Descriptors for observability spans. The message lists themselves
            # are not attached: they can be very large and are exactly what
            # compression removes. Only counts and a bounded summary preview
            # leave the compressor.
            "input": {
                "message_count": len(messages),
                "roles": _role_counts(messages),
                "chars": _messages_chars(messages),
                "tokens": before,
            },
            "output": {
                "message_count": len(view),
                "roles": _role_counts(view),
                "chars": _messages_chars(view),
                "tokens": after,
                "dropped_messages": dropped_count,
                "summarized": summarized,
                "fallback": fallback,
            },
        }
        if self._summary:
            preview, _ = truncate_text(self._summary, SUMMARY_PREVIEW_CHARS)
            stats["output"]["summary"] = preview
            stats["output"]["summary_chars"] = len(self._summary)
        if self._call_count:
            if self._call_usage:
                stats["usage"] = dict(self._call_usage)
            stats["model_calls"] = self._call_count
            stats["usage_unknown_calls"] = self._call_unknown
            stats["summary_chunks"] = self._chunk_count
            stats["summary_chunks_failed"] = self._chunk_failures
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
            total += count_message_tokens(
                messages[index],
                counter=self.token_counter,
                image_token_estimate=self.image_token_estimate,
            )
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

    def _summary_chunks(self, messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        """Split a fold into chunks that each fit one summarizer request.

        Chunks are packed by the token count of their rendered transcript, so
        per-message caps are already applied. A tool-call block (the assistant
        with ``tool_calls`` plus its following tool messages) is never split.
        """

        chunks: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        current_tokens = 0
        current_chars = 0

        for unit in _message_units(messages):
            transcript = _format_transcript(unit)
            tokens = self.token_counter.count(transcript)
            chars = len(transcript) + 1
            if current and (
                current_tokens + tokens > SUMMARY_CHUNK_TOKENS or current_chars + chars > SUMMARY_INPUT_MAX_CHARS
            ):
                chunks.append(current)
                current = []
                current_tokens = 0
                current_chars = 0
            current.extend(unit)
            current_tokens += tokens
            current_chars += chars

        if current:
            chunks.append(current)
        return chunks

    def _summarize_chunks(self, chunks: list[list[dict[str, Any]]]) -> str | None:
        """Roll the summary through chunks oldest to newest.

        A failed chunk is skipped and marks the result as partial; later chunks
        still use the last successful summary. The hard call cap is advisory:
        the current 256K window produces at most a handful of chunks, and
        dropping folded content to honor the cap would defeat the purpose.
        """

        if len(chunks) > SUMMARY_MAX_CHUNKS:
            logger.warning(
                "Context fold needs %d summary chunks (cap %d); summarizing all to keep coverage",
                len(chunks),
                SUMMARY_MAX_CHUNKS,
            )

        summary = self._summary
        succeeded = False
        failures = 0
        self._chunk_count = len(chunks)
        for chunk in chunks:
            result = self._summarize(chunk, summary)
            if result is None:
                failures += 1
                continue
            summary = result
            succeeded = True
        self._chunk_failures = failures
        return summary if succeeded else None

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

    def _count(self, messages: list[dict[str, Any]]) -> int:
        return count_messages_tokens(
            messages,
            counter=self.token_counter,
            image_token_estimate=self.image_token_estimate,
        )

    @staticmethod
    def _strip_images(content: Any) -> list[dict[str, Any]] | None:
        if not isinstance(content, list):
            return None
        changed = False
        parts: list[dict[str, Any]] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                parts.append({"type": "text", "text": "[image omitted]"})
                changed = True
            else:
                parts.append(part)
        return parts if changed else None

    def _fit_budget(self, messages: list[dict[str, Any]], budget: int) -> list[dict[str, Any]]:
        """Shrink the view until it fits, dropping images before text.

        Structure is never changed here, so tool-call pairs stay intact. Images
        from older messages go first; the newest user turn keeps its images
        unless even that is not enough.
        """

        current = self._count(messages)
        if current <= budget:
            return messages

        user_indices = [index for index, message in enumerate(messages) if message.get("role") == "user"]
        newest_user = max(user_indices) if user_indices else len(messages)

        # Pass 1: replace images in messages older than the newest user turn.
        for index, message in enumerate(messages):
            if index >= newest_user:
                continue
            stripped = self._strip_images(message.get("content"))
            if stripped is None:
                continue
            messages[index] = {**message, "content": stripped}
            current = self._count(messages)
            if current <= budget:
                return messages

        # Pass 2: the newest turn's images are the last resort.
        if current > budget:
            for index in range(len(messages) - 1, -1, -1):
                message = messages[index]
                stripped = self._strip_images(message.get("content"))
                if stripped is None:
                    continue
                messages[index] = {**message, "content": stripped}
                current = self._count(messages)
                if current <= budget:
                    return messages

        # Pass 3: flatten remaining content-part lists so text truncation can work.
        for index, message in enumerate(messages):
            content = message.get("content")
            if isinstance(content, list):
                messages[index] = {**message, "content": content_text(content)}
        current = self._count(messages)

        for _ in range(64):
            if current <= budget:
                break

            candidates = [
                (count_message_tokens(message, counter=self.token_counter), index)
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
            current = self._count(messages)

        return messages


def _role_counts(messages: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for message in messages:
        role = str(message.get("role") or "unknown")
        counts[role] = counts.get(role, 0) + 1
    return counts


def _messages_chars(messages: list[dict[str, Any]]) -> int:
    total = 0
    for message in messages:
        total += len(content_text(message.get("content")))
    return total


def _message_units(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group an assistant tool-call block with its tool results.

    Chunking must never separate a ``tool_calls`` assistant from the tool
    messages that answer it; each other message is its own unit.
    """

    units: list[list[dict[str, Any]]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        unit = [message]
        index += 1
        if message.get("role") == "assistant" and message.get("tool_calls"):
            while index < len(messages) and messages[index].get("role") == "tool":
                unit.append(messages[index])
                index += 1
        units.append(unit)
    return units


def _format_message(message: dict[str, Any]) -> str:
    role = str(message.get("role") or "unknown")
    text = content_text(message.get("content"))

    tool_calls = message.get("tool_calls")
    if tool_calls:
        text = (text + "\n" if text else "") + "tool_calls: " + _json_text(tool_calls)

    text = truncate_text(text, SUMMARY_MESSAGE_MAX_CHARS)[0]
    if role == "tool" and message.get("tool_call_id"):
        return f"tool {message['tool_call_id']}: {text}"
    return f"{role}: {text}"


def _format_transcript(messages: list[dict[str, Any]]) -> str:
    transcript = "\n".join(_format_message(message) for message in messages)
    if len(transcript) > SUMMARY_INPUT_MAX_CHARS:
        transcript = transcript[-SUMMARY_INPUT_MAX_CHARS:]
    return transcript


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)
