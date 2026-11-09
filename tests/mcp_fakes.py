"""Shared fakes for MCP tests (no network, no ``mcp`` package)."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any

_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from app.agent.mcp.client import MCPCallOutcome, MCPContent, MCPToolInfo  # noqa: E402
from app.agent.mcp.config import MCPServerConfig  # noqa: E402


def make_server(name: str = "demo", **overrides: Any) -> MCPServerConfig:
    values: dict[str, Any] = {
        "name": name,
        "url": f"https://{name}.example.com/mcp",
        "headers": (),
        "allow_tools": (),
        "deny_tools": (),
        "approval": "auto",
        "timeout_seconds": 5.0,
        "catalog_ttl_seconds": 300,
        "max_result_chars": 4_000,
        "allowed_hosts": (f"{name}.example.com",),
        "fingerprint": f"fp-{name}",
    }
    values.update(overrides)
    return MCPServerConfig(**values)


def make_tool(
    name: str = "get_item",
    *,
    server: str = "demo",
    read_only: bool = False,
    destructive: bool = False,
    description: str = "Do a thing.",
    schema: dict[str, Any] | None = None,
) -> MCPToolInfo:
    return MCPToolInfo(
        server=server,
        name=name,
        description=description,
        input_schema=schema if schema is not None else {"type": "object", "properties": {}},
        read_only=read_only,
        destructive=destructive,
    )


@dataclass
class FakeToolResult:
    tool_call_id: str
    name: str
    content: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, int] = field(default_factory=dict)


class FakeTool:
    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


class FakeSDK:
    Tool = FakeTool
    ToolResult = FakeToolResult


class FakeClient:
    def __init__(
        self,
        tools: list[MCPToolInfo] | None = None,
        outcome: MCPCallOutcome | None = None,
        *,
        list_error: Exception | None = None,
        call_error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self.tools = tools or []
        self.outcome = outcome or MCPCallOutcome(
            contents=(MCPContent("text", text="ok"),)
        )
        self.list_error = list_error
        self.call_error = call_error
        self.delay = delay
        self.list_calls = 0
        self.call_calls: list[tuple[str, dict[str, Any]]] = []

    async def list_tools(self, server: MCPServerConfig, *, timeout: float) -> list[MCPToolInfo]:
        import asyncio

        self.list_calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.list_error is not None:
            raise self.list_error
        return list(self.tools)

    async def call_tool(
        self,
        server: MCPServerConfig,
        name: str,
        arguments: dict[str, Any],
        *,
        timeout: float,
    ) -> MCPCallOutcome:
        import asyncio

        self.call_calls.append((name, arguments))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.call_error is not None:
            raise self.call_error
        return self.outcome


class FakeManager:
    def __init__(self, outcome: MCPCallOutcome | None = None, error: Exception | None = None) -> None:
        self.outcome = outcome or MCPCallOutcome(contents=(MCPContent("text", text="ok"),))
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, server, name, arguments, **kwargs):  # noqa: ANN001, ANN003
        self.calls.append((name, arguments))
        if self.error is not None:
            raise self.error
        return self.outcome


class FakeInteraction:
    def __init__(self, answer: str = "Allow", *, error: str | None = None, closed: bool = False) -> None:
        self._answer = answer
        self._error = error
        self._closed = closed
        self.asked: list[dict[str, Any]] = []

    @property
    def closed(self) -> bool:
        return self._closed

    def ask(self, **kwargs: Any) -> dict[str, Any]:
        self.asked.append(kwargs)
        if self._error is not None:
            return {"error": self._error}
        return {"question_id": "q1", "answer": self._answer}
