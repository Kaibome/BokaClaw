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


AgentEvent = AgentDelta | AgentCompleted
