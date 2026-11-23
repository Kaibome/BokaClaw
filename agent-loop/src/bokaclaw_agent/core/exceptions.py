"""Exception hierarchy for the BokaClaw agent runtime."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from .types import RunResult


class AgentError(Exception):
    """Base class for every error raised by the runtime."""


class ConfigurationError(AgentError):
    """Raised when the runtime or a dependency is misconfigured."""


class ModelError(AgentError):
    """Raised when the model provider fails to return a usable response."""


class ModelProtocolError(ModelError):
    """Raised when a model response violates the runtime's message protocol.

    Example: a provider returns a tool call as raw DSML text instead of the
    structured ``tool_calls`` field and leaves no usable final answer.
    """


class ToolError(AgentError):
    """Base class for tool related failures."""


class ToolSchemaError(ToolError):
    """Raised when a tool cannot be described as a JSON Schema."""


class ToolNotFoundError(ToolError):
    """Raised in ``raise`` mode when the model calls an unknown tool."""


class ToolArgumentError(ToolError):
    """Raised in ``raise`` mode when tool arguments fail validation."""


class TransientToolError(ToolError):
    """Raised by a tool when the failure is likely to succeed on a retry.

    Tools should raise this instead of a plain ``RuntimeError`` for timeouts,
    connection failures, HTTP 429/5xx and similar transient conditions. The
    runtime treats it as retryable only when the tool's retry policy allows it.

    ``retry_after`` carries a server-provided delay (for example an HTTP
    ``Retry-After`` header); the runtime uses it as a lower bound for backoff.
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        self.retry_after = retry_after
        super().__init__(message)


class ToolExecutionError(ToolError):
    """Raised in ``raise`` mode when a tool raises.

    ``kind`` classifies the failure so dispatch can decide whether a retry can
    help: ``transient`` may succeed on a retry, ``permanent`` will not, and
    ``unknown`` (the default) is treated as non-retryable. ``cause_type`` names
    the original exception class. ``retry_after`` is a tool-supplied minimum
    delay in seconds.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: Literal["transient", "permanent", "unknown"] = "unknown",
        cause_type: str = "",
        retry_after: float | None = None,
    ) -> None:
        self.kind = kind
        self.cause_type = cause_type
        self.retry_after = retry_after
        super().__init__(message)


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


class NoProgressError(AgentError):
    """Raised when consecutive tool calls added no new evidence.

    After ``AgentConfig.progress_stop_after`` such calls the runtime asks for a
    tool-free final answer. The run only fails when that answer is empty; the
    partial run is then persisted with status ``failed`` and attached as
    ``result``.
    """

    def __init__(self, session_id: str, result: "RunResult | None" = None) -> None:
        self.session_id = session_id
        self.result = result
        detail = f" after {result.iterations} iteration(s)" if result is not None else ""
        super().__init__(f"Agent session {session_id!r} made no progress{detail}")


class RunCancelled(AgentError):
    """Raised when ``cancel_event`` was set at a safe boundary.

    The partial run is persisted with status ``cancelled`` and attached to the
    exception as ``result`` so callers can inspect the messages and tool
    results produced before the run stopped.
    """

    def __init__(
        self,
        session_id: str,
        result: "RunResult | None" = None,
        iteration: int = 0,
    ) -> None:
        self.session_id = session_id
        self.result = result
        self.iteration = iteration
        detail = f" during iteration {iteration}" if iteration else ""
        super().__init__(f"Agent session {session_id!r} was cancelled{detail}")


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
