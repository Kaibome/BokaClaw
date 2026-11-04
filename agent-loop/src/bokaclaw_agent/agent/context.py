"""In-memory conversation context for one ReAct run."""

from __future__ import annotations

import json
import math
from typing import Any

from ..core.types import ModelResponse, ToolResult

TRUNCATION_MARKER = "\n... [truncated] ...\n"


def estimate_tokens(text: str | None) -> int:
    """Conservatively estimate tokens without a tokenizer dependency.

    ASCII text is roughly 0.4 tokens per character; non-ASCII (for example CJK)
    is counted as one token per character.
    """

    if not text:
        return 0
    ascii_chars = sum(1 for char in text if ord(char) < 128)
    return math.ceil(ascii_chars * 0.4 + (len(text) - ascii_chars))


def estimate_message_tokens(message: dict[str, Any]) -> int:
    """Estimate one wire-format message, including tool calls and role overhead."""

    tokens = 4
    content = message.get("content")
    if isinstance(content, str):
        tokens += estimate_tokens(content)
    elif content is not None:
        tokens += estimate_tokens(_json_text(content))
    tool_calls = message.get("tool_calls")
    if tool_calls:
        tokens += estimate_tokens(_json_text(tool_calls))
    return tokens


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(estimate_message_tokens(message) for message in messages)


def truncate_text(
    text: str,
    max_chars: int | None,
    *,
    head_ratio: float = 0.5,
) -> tuple[str, bool]:
    """Keep the head and tail of ``text`` within ``max_chars`` characters.

    The marker mirrors the server's tool-trace convention. Python strings are
    sliced by code point, so multi-byte characters are never split.
    """

    if max_chars is None or len(text) <= max_chars:
        return text, False
    if max_chars <= len(TRUNCATION_MARKER):
        return TRUNCATION_MARKER[:max_chars], True

    preserved = max_chars - len(TRUNCATION_MARKER)
    ratio = min(max(head_ratio, 0.0), 1.0)
    head_len = int(preserved * ratio)
    tail_len = preserved - head_len
    tail = text[-tail_len:] if tail_len else ""
    return f"{text[:head_len]}{TRUNCATION_MARKER}{tail}", True


def safe_start_index(
    messages: list[dict[str, Any]],
    start: int,
    *,
    allow_tool_call_blocks: bool = False,
) -> int:
    """Move ``start`` forward to a message that can begin a valid request view.

    A view must not begin with an orphaned ``tool`` message. With
    ``allow_tool_call_blocks`` an assistant that carries tool calls is also a
    valid start, because its results immediately follow it; otherwise only a
    completed assistant turn counts. Returns ``len(messages)`` when no safe
    boundary exists at or after ``start``.
    """

    start = max(0, start)
    for index in range(start, len(messages)):
        if messages[index].get("role") == "user":
            return index
    for index in range(start, len(messages)):
        message = messages[index]
        if message.get("role") == "assistant" and (allow_tool_call_blocks or not message.get("tool_calls")):
            return index
    return len(messages)


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def normalize_input(content: str | list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turn ``run(input=...)`` into OpenAI wire-format messages."""

    if isinstance(content, str):
        return [{"role": "user", "content": content}]
    if isinstance(content, list):
        messages: list[dict[str, Any]] = []
        for item in content:
            if not isinstance(item, dict) or "role" not in item:
                raise ValueError("Each input message must be a dict with a 'role' key")
            messages.append(dict(item))
        return messages
    raise TypeError("input must be a string or a list of message dicts")


def assistant_message(response: ModelResponse) -> dict[str, Any]:
    """Build the wire-format assistant message, preserving tool-call ids."""

    message: dict[str, Any] = {"role": "assistant", "content": response.content}
    if response.tool_calls:
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": call.raw_arguments
                    if call.raw_arguments is not None
                    else json.dumps(call.arguments, ensure_ascii=False),
                },
            }
            for call in response.tool_calls
        ]
    return message


def tool_message(result: ToolResult) -> dict[str, Any]:
    """Build a tool result message.

    ``name`` is intentionally omitted: OpenAI's tool-message schema does not
    define it and stricter compatible servers reject unknown fields. The name
    remains available on :class:`~bokaclaw_agent.types.ToolResult`.
    """

    return {
        "role": "tool",
        "tool_call_id": result.tool_call_id,
        "content": result.content,
    }


class ContextManager:
    """Holds the full message history for one run.

    The system prompt is reconstructed on every call and never persisted, so
    resuming a session does not duplicate it and prompt changes take effect.
    ``max_history_messages`` only affects previously stored history and the
    view sent to the model; the current run's messages and the persisted
    history always stay complete.
    """

    def __init__(
        self,
        system_prompt: str | None = None,
        max_history_messages: int | None = None,
        *,
        tool_result_max_chars: int | None = None,
    ) -> None:
        self.system_prompt = system_prompt
        self.max_history_messages = max_history_messages
        self.tool_result_max_chars = tool_result_max_chars
        self._history: list[dict[str, Any]] = []
        self._current: list[dict[str, Any]] = []

    @property
    def messages(self) -> list[dict[str, Any]]:
        return [dict(message) for message in (*self._history, *self._current)]

    def load_history(self, messages: list[dict[str, Any]]) -> None:
        self._history = [dict(message) for message in messages]

    def append(self, message: dict[str, Any]) -> None:
        self._current.append(dict(message))

    def build(self) -> list[dict[str, Any]]:
        """Return the message view to send to the model."""

        messages = [*self._truncate(self._history), *self._current]
        if self.tool_result_max_chars is not None:
            messages = [self._cap_tool_message(message) for message in messages]
        if self.system_prompt:
            return [{"role": "system", "content": self.system_prompt}, *messages]
        return messages

    def _cap_tool_message(self, message: dict[str, Any]) -> dict[str, Any]:
        if message.get("role") != "tool":
            return message
        content = message.get("content")
        if not isinstance(content, str):
            return message
        capped, truncated = truncate_text(content, self.tool_result_max_chars)
        if not truncated:
            return message
        return {**message, "content": capped}

    def _truncate(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        limit = self.max_history_messages
        if limit is None or len(messages) <= limit:
            return list(messages)

        start = safe_start_index(messages, len(messages) - limit)
        return messages[start:] if start < len(messages) else []
