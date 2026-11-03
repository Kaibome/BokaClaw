"""Provider protocol implemented by model backends."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..types import ModelResponse


@runtime_checkable
class ModelProvider(Protocol):
    """A synchronous model backend.

    Implementations receive OpenAI wire-format messages and return a
    normalized :class:`~bokaclaw_agent.types.ModelResponse`. A provider may
    return an awaitable instead; the runtime resolves it safely.
    """

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = "auto",
        **kwargs: Any,
    ) -> ModelResponse:
        """Return the next assistant turn for ``messages``."""
        ...
