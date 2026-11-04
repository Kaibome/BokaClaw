"""Data structures shared by providers, tools, the runtime, and stores."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ToolCall(BaseModel):
    """A single tool invocation requested by the model."""

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    raw_arguments: str | None = None
    arguments_error: str | None = Field(
        default=None,
        description="Set when the provider could not parse the raw arguments as a JSON object.",
    )


class ModelResponse(BaseModel):
    """Normalized model output."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, int] = Field(default_factory=dict)
    raw: Any = Field(default=None, exclude=True)


class ToolCallDelta(BaseModel):
    """One incremental fragment of a streamed tool call.

    The provider sends ``id`` and ``name`` once, then appends JSON fragments to
    ``arguments`` across chunks; the runtime reassembles them by ``index``.
    """

    model_config = ConfigDict(extra="ignore")

    index: int = 0
    id: str | None = None
    name: str | None = None
    arguments: str = ""


class ModelStreamChunk(BaseModel):
    """One incremental chunk from a streaming provider."""

    model_config = ConfigDict(extra="ignore")

    content: str | None = None
    tool_calls: list[ToolCallDelta] = Field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, int] = Field(default_factory=dict)


class ToolResult(BaseModel):
    """The string result that is fed back to the model as a ``tool`` message."""

    tool_call_id: str
    name: str
    content: str
    is_error: bool = False


class RunResult(BaseModel):
    """Outcome of one :meth:`AgentRuntime.run` call."""

    session_id: str
    content: str | None = None
    stop_reason: Literal["final", "length", "content_filter", "max_iterations", "cancelled"]
    new_messages: list[dict[str, Any]] = Field(default_factory=list)
    tool_results: list[ToolResult] = Field(default_factory=list)
    iterations: int = 0
    usage: dict[str, int] = Field(default_factory=dict)


class EventType(str, Enum):
    """Lifecycle events emitted to the optional ``on_event`` callback."""

    RUN_START = "run_started"
    MODEL_REQUEST = "model_request"
    MODEL_RESPONSE = "model_response"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    RUN_END = "run_ended"
    ERROR = "error"


class AgentEvent(BaseModel):
    """One lifecycle event. Callback exceptions never interrupt a run."""

    type: EventType
    session_id: str
    iteration: int = 0
    seq: int = 0
    data: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
