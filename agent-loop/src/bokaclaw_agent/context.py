"""In-memory conversation context for one ReAct run."""

from __future__ import annotations

import json
from typing import Any

from .types import ModelResponse, ToolResult


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

    def __init__(self, system_prompt: str | None = None, max_history_messages: int | None = None) -> None:
        self.system_prompt = system_prompt
        self.max_history_messages = max_history_messages
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
        if self.system_prompt:
            return [{"role": "system", "content": self.system_prompt}, *messages]
        return messages

    def _truncate(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        limit = self.max_history_messages
        if limit is None or len(messages) <= limit:
            return list(messages)

        start = len(messages) - limit

        # Prefer starting at a user turn so the model sees a complete exchange.
        for index in range(start, len(messages)):
            if messages[index].get("role") == "user":
                return messages[index:]
        # Otherwise only start at an assistant message with no pending tools.
        for index in range(start, len(messages)):
            message = messages[index]
            if message.get("role") == "assistant" and not message.get("tool_calls"):
                return messages[index:]
        return []
