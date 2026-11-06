"""BokaClaw AgentRuntime: a minimal ReAct loop with Postgres session persistence."""

from importlib.metadata import PackageNotFoundError, version

from .agent.runtime import AgentRuntime
from .agent.subagent import (
    Fact,
    SubagentOptions,
    SubagentResult,
    SubagentSpec,
    SubagentTask,
    SubagentTool,
)
from .core.config import AgentConfig, ModelConfig
from .core.exceptions import (
    AgentError,
    ConfigurationError,
    MaxIterationsExceeded,
    ModelError,
    NoProgressError,
    PersistenceError,
    RunCancelled,
    SessionNotFoundError,
    SessionStoreError,
    ToolArgumentError,
    ToolError,
    ToolExecutionError,
    ToolNotFoundError,
    ToolSchemaError,
)
from .core.types import (
    AgentEvent,
    EventType,
    ModelResponse,
    ModelStreamChunk,
    RunResult,
    ToolCall,
    ToolCallDelta,
    ToolResult,
)
from .providers.base import ModelProvider, StreamingModelProvider
from .providers.openai_compatible import OpenAICompatibleProvider
from .sessions.base import SessionRecord, SessionStore
from .sessions.memory import InMemorySessionStore
from .sessions.postgres import PostgresConfig, PostgresSessionStore
from .tools import Tool, ToolRegistry, tool

try:
    __version__ = version("bokaclaw-agent")
except PackageNotFoundError:  # pragma: no cover - source checkout
    __version__ = "0.2.0"

__all__ = [
    "AgentConfig",
    "AgentError",
    "AgentEvent",
    "AgentRuntime",
    "ConfigurationError",
    "EventType",
    "Fact",
    "InMemorySessionStore",
    "MaxIterationsExceeded",
    "ModelConfig",
    "ModelError",
    "NoProgressError",
    "ModelProvider",
    "ModelResponse",
    "ModelStreamChunk",
    "OpenAICompatibleProvider",
    "PersistenceError",
    "PostgresConfig",
    "PostgresSessionStore",
    "RunCancelled",
    "RunResult",
    "SessionNotFoundError",
    "SessionRecord",
    "SessionStore",
    "SessionStoreError",
    "StreamingModelProvider",
    "SubagentOptions",
    "SubagentResult",
    "SubagentSpec",
    "SubagentTask",
    "SubagentTool",
    "Tool",
    "ToolArgumentError",
    "ToolCall",
    "ToolCallDelta",
    "ToolError",
    "ToolExecutionError",
    "ToolNotFoundError",
    "ToolRegistry",
    "ToolResult",
    "ToolSchemaError",
    "tool",
]
