"""BokaClaw AgentRuntime: a minimal ReAct loop with Postgres session persistence."""

from importlib.metadata import PackageNotFoundError, version

from .config import AgentConfig, ModelConfig
from .exceptions import (
    AgentError,
    ConfigurationError,
    MaxIterationsExceeded,
    ModelError,
    PersistenceError,
    SessionNotFoundError,
    SessionStoreError,
    ToolArgumentError,
    ToolError,
    ToolExecutionError,
    ToolNotFoundError,
    ToolSchemaError,
)
from .providers.base import ModelProvider
from .providers.openai_compatible import OpenAICompatibleProvider
from .runtime import AgentRuntime
from .sessions.base import SessionRecord, SessionStore
from .sessions.memory import InMemorySessionStore
from .sessions.postgres import PostgresConfig, PostgresSessionStore
from .tools import Tool, ToolRegistry, tool
from .types import AgentEvent, EventType, ModelResponse, RunResult, ToolCall, ToolResult

try:
    __version__ = version("bokaclaw-agent")
except PackageNotFoundError:  # pragma: no cover - source checkout
    __version__ = "0.1.0"

__all__ = [
    "AgentConfig",
    "AgentError",
    "AgentEvent",
    "AgentRuntime",
    "ConfigurationError",
    "EventType",
    "InMemorySessionStore",
    "MaxIterationsExceeded",
    "ModelConfig",
    "ModelError",
    "ModelProvider",
    "ModelResponse",
    "OpenAICompatibleProvider",
    "PersistenceError",
    "PostgresConfig",
    "PostgresSessionStore",
    "RunResult",
    "SessionNotFoundError",
    "SessionRecord",
    "SessionStore",
    "SessionStoreError",
    "Tool",
    "ToolArgumentError",
    "ToolCall",
    "ToolError",
    "ToolExecutionError",
    "ToolNotFoundError",
    "ToolRegistry",
    "ToolResult",
    "ToolSchemaError",
    "tool",
]
