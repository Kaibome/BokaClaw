"""Event and error types shared by the agent executors.

Both the in-process runtime and the optional remote executor emit the same
events, so the router does not need to know which one produced a turn.
"""

from dataclasses import dataclass, field
from typing import Any


class AgentLoopError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass
class AgentDelta:
    delta: str


@dataclass
class AgentCompleted:
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentToolCall:
    """One tool invocation requested by the model, before its result exists."""

    tool_call_id: str
    name: str
    iteration: int = 0
    arguments: dict[str, Any] = field(default_factory=dict)
    raw_arguments: str | None = None
    arguments_error: str | None = None


@dataclass
class AgentToolResult:
    """The settled outcome of one tool invocation."""

    tool_call_id: str
    name: str
    content: str
    is_error: bool = False
    iteration: int = 0
    status: str = "completed"
    duration_ms: int | None = None


AgentEvent = AgentDelta | AgentCompleted | AgentToolCall | AgentToolResult
