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


@dataclass
class AgentRunStarted:
    """The SDK entered one ReAct run."""

    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentModelRequest:
    """One model call, captured before the prompt is mutated by later turns."""

    iteration: int = 0
    message_count: int = 0
    roles: list[str] = field(default_factory=list)
    last_message_hash: str | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    prompt: list[dict[str, Any]] | None = None
    model: str | None = None
    seq: int | None = None


@dataclass
class AgentModelResponse:
    iteration: int = 0
    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, int] | None = None
    seq: int | None = None


@dataclass
class AgentContextCompressed:
    iteration: int = 0
    stats: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, int] | None = None
    model_calls: int = 0
    usage_unknown_calls: int = 0


@dataclass
class AgentUsageUpdate:
    """One metered model call, emitted independently of span capture.

    A ``phase="request"`` event marks a call that has started but not produced
    usage yet; it becomes an unknown call if no response is ever seen.
    """

    source: str = "agent_loop"
    phase: str = "response"
    iteration: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    calls: int = 1
    unknown_calls: int = 0
    seq: int | None = None


@dataclass
class AgentMemoryRecall:
    """The pre-run lookup of saved research conclusions.

    Emitted once per turn, before the first model request, whether or not it
    found anything: without it a trace cannot tell a miss from a lookup that
    never ran.
    """

    question: str = ""
    hits: list[dict[str, Any]] = field(default_factory=list)
    floor: float = 0.5
    ttl_days: int = 30
    candidates: int = 0
    skipped_low_score: int = 0
    skipped_stale: int = 0
    skipped_invalid: int = 0
    skipped_reason: str | None = None
    error: str | None = None
    chars: int = 0
    duration_ms: int | None = None
    profile_chars: int = 0
    query_hits: int = 0
    budget_tokens: int = 1500
    categories: list[str] = field(default_factory=list)
    research_hits: int = 0


@dataclass
class AgentMemorySave:
    """The post-turn write of one research conclusion."""

    action: str = "skipped"
    question: str = ""
    memory_id: str | None = None
    sources: int = 0
    chars: int = 0
    reason: str | None = None
    error: str | None = None
    duration_ms: int | None = None
    kind: str | None = None
    path: str | None = None
    captured: int = 0
    entries: int = 0


@dataclass
class AgentSubagentStarted:
    """A delegated child run started; its span nests under the parent tool span."""

    subagent_id: str
    subagent_type: str = ""
    label: str = ""
    depth: int = 0
    goal_chars: int = 0
    parent_tool_call_id: str = ""
    resume: bool = False
    model: str | None = None
    tool_names: list[str] = field(default_factory=list)


@dataclass
class AgentSubagentEvent:
    """One child model call or tool call, forwarded for the nested span tree."""

    subagent_id: str
    phase: str = ""
    iteration: int = 0
    message_count: int = 0
    tool_count: int = 0
    content_chars: int = 0
    finish_reason: str | None = None
    usage: dict[str, int] | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    arguments: dict[str, Any] | None = None
    is_error: bool = False
    status: str | None = None
    original_chars: int | None = None
    content: str | None = None
    error: str | None = None


@dataclass
class AgentSubagentFinished:
    """A delegated child run settled, successfully or not."""

    subagent_id: str
    subagent_type: str = ""
    status: str = "failed"
    turns: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    isolated_chars: int = 0
    result_chars: int = 0
    duration_ms: int | None = None
    resumable: bool = False
    error: str | None = None


@dataclass
class AgentRunEnded:
    stop_reason: str | None = None


@dataclass
class AgentError:
    """The SDK-reported failure, before the bridge converts it to AgentLoopError."""

    error: str
    type: str | None = None


AgentEvent = (
    AgentDelta
    | AgentCompleted
    | AgentToolCall
    | AgentToolResult
    | AgentRunStarted
    | AgentModelRequest
    | AgentModelResponse
    | AgentContextCompressed
    | AgentUsageUpdate
    | AgentMemoryRecall
    | AgentMemorySave
    | AgentSubagentStarted
    | AgentSubagentEvent
    | AgentSubagentFinished
    | AgentRunEnded
    | AgentError
)
