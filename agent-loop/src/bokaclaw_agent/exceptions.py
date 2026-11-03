"""Exception hierarchy for the BokaClaw agent runtime."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .types import RunResult


class AgentError(Exception):
    """Base class for every error raised by the runtime."""


class ConfigurationError(AgentError):
    """Raised when the runtime or a dependency is misconfigured."""


class ModelError(AgentError):
    """Raised when the model provider fails to return a usable response."""


class ToolError(AgentError):
    """Base class for tool related failures."""


class ToolSchemaError(ToolError):
    """Raised when a tool cannot be described as a JSON Schema."""


class ToolNotFoundError(ToolError):
    """Raised in ``raise`` mode when the model calls an unknown tool."""


class ToolArgumentError(ToolError):
    """Raised in ``raise`` mode when tool arguments fail validation."""


class ToolExecutionError(ToolError):
    """Raised in ``raise`` mode when a tool raises."""


class MaxIterationsExceeded(AgentError):
    """Raised when the ReAct loop reaches ``AgentConfig.max_iterations``.

    The partial run is persisted with status ``failed`` and attached to the
    exception as ``result`` so callers can inspect what happened.
    """

    def __init__(self, session_id: str, result: "RunResult | None" = None) -> None:
        self.session_id = session_id
        self.result = result
        detail = f" after {result.iterations} iteration(s)" if result is not None else ""
        super().__init__(f"Agent session {session_id!r} exceeded max_iterations{detail}")


class SessionStoreError(AgentError):
    """Raised when the session store cannot be used."""


class PersistenceError(SessionStoreError):
    """Raised when session messages cannot be written to or read from Postgres."""


# Kept for symmetry with the public error list; callers may surface it when a
# session is expected but absent. The runtime itself treats unknown ids as new
# sessions.
class SessionNotFoundError(SessionStoreError):
    """Raised when a caller requires an existing session."""

    def __init__(self, session_id: str, details: dict[str, Any] | None = None) -> None:
        self.session_id = session_id
        self.details = details or {}
        super().__init__(f"Session {session_id!r} was not found")
