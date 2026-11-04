"""Provider protocol implemented by model backends."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Protocol, runtime_checkable

from ..core.types import ModelResponse, ModelStreamChunk


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


@runtime_checkable
class StreamingModelProvider(Protocol):
    """Optional provider capability used by :meth:`AgentRuntime.stream`.

    A provider that does not implement this is still usable: the runtime falls
    back to :meth:`ModelProvider.complete` and emits the answer as one chunk.
    """

    def stream_complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = "auto",
        **kwargs: Any,
    ) -> Iterator[ModelStreamChunk]:
        """Yield incremental chunks for the next assistant turn."""
        ...
