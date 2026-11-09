"""MCP manager discovery cache, backoff, and failure isolation."""

from __future__ import annotations

import asyncio
import os
import sys
import threading

import pytest

_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import app.agent.mcp.manager as manager_module  # noqa: E402
from app.agent.mcp.client import MCPError  # noqa: E402
from app.agent.mcp.config import MCPConfig  # noqa: E402
from app.agent.mcp.manager import MCPManager  # noqa: E402

from .mcp_fakes import FakeClient, make_server, make_tool  # noqa: E402


@pytest.fixture(autouse=True)
def _skip_dns(monkeypatch):
    monkeypatch.setattr(manager_module, "check_public_url", lambda url, *, allow_private: None)


def _config(servers) -> MCPConfig:
    return MCPConfig(
        servers=tuple(servers),
        allow_insecure_http=False,
        allow_private=True,
        discovery_timeout_seconds=1.0,
        max_tools_per_server=100,
        max_schema_bytes=65_536,
    )


class FakeSDK:
    class Tool:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class ToolResult:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)


def test_catalog_cache_hits_within_ttl():
    server = make_server(catalog_ttl_seconds=300)
    client = FakeClient(tools=[make_tool(read_only=True)])
    manager = MCPManager(client=client)
    config = _config([server])

    first = asyncio.run(manager.prepare_tools(config, FakeSDK))
    second = asyncio.run(manager.prepare_tools(config, FakeSDK))

    assert [tool.name for tool in first] == ["mcp__demo__get_item"]
    assert [tool.name for tool in second] == ["mcp__demo__get_item"]
    assert client.list_calls == 1


def test_catalog_refreshes_when_ttl_is_zero():
    server = make_server(catalog_ttl_seconds=0)
    client = FakeClient(tools=[make_tool(read_only=True)])
    manager = MCPManager(client=client)
    config = _config([server])

    asyncio.run(manager.prepare_tools(config, FakeSDK))
    asyncio.run(manager.prepare_tools(config, FakeSDK))

    assert client.list_calls == 2


def test_failed_refresh_keeps_the_stale_catalog():
    server = make_server(catalog_ttl_seconds=0)
    client = FakeClient(tools=[make_tool(read_only=True)])
    manager = MCPManager(client=client)
    config = _config([server])

    first = asyncio.run(manager.prepare_tools(config, FakeSDK))
    client.list_error = RuntimeError("down")
    second = asyncio.run(manager.prepare_tools(config, FakeSDK))

    assert [tool.name for tool in first] == ["mcp__demo__get_item"]
    assert [tool.name for tool in second] == ["mcp__demo__get_item"]
    catalog = manager.cache.get(server)
    assert catalog is not None and catalog.stale is True


def test_backoff_suppresses_repeated_discovery():
    server = make_server(catalog_ttl_seconds=0)
    client = FakeClient(list_error=RuntimeError("down"))
    manager = MCPManager(client=client)
    config = _config([server])

    asyncio.run(manager.prepare_tools(config, FakeSDK))
    asyncio.run(manager.prepare_tools(config, FakeSDK))

    assert client.list_calls == 1
    assert not manager.cache.can_retry(server)


def test_one_broken_server_does_not_hide_the_other():
    good = make_server("good")
    bad = make_server("bad")

    class SplitClient(FakeClient):
        async def list_tools(self, server, *, timeout):
            if server.name == "bad":
                raise RuntimeError("down")
            return [make_tool(server="good", read_only=True)]

    manager = MCPManager(client=SplitClient())
    tools = asyncio.run(manager.prepare_tools(_config([good, bad]), FakeSDK))

    assert [tool.name for tool in tools] == ["mcp__good__get_item"]


def test_call_timeout_is_reported():
    server = make_server(timeout_seconds=0.05)
    client = FakeClient(delay=1.0)
    manager = MCPManager(client=client)

    with pytest.raises(MCPError) as excinfo:
        asyncio.run(
            manager.call_tool(
                server,
                "get_item",
                {},
                allow_private=True,
                timeout=0.05,
                cancel_event=None,
            )
        )

    assert "timed out" in str(excinfo.value)


def test_pre_cancelled_call_is_aborted():
    server = make_server()
    client = FakeClient()
    manager = MCPManager(client=client)
    cancel_event = threading.Event()
    cancel_event.set()

    with pytest.raises(MCPError) as excinfo:
        asyncio.run(
            manager.call_tool(
                server,
                "get_item",
                {},
                allow_private=True,
                timeout=5,
                cancel_event=cancel_event,
            )
        )

    assert excinfo.value.aborted is True
    assert client.call_calls == []


def test_oversized_arguments_are_rejected_before_connecting():
    server = make_server()
    client = FakeClient()
    manager = MCPManager(client=client)

    with pytest.raises(MCPError) as excinfo:
        asyncio.run(
            manager.call_tool(
                server,
                "get_item",
                {"blob": "x" * (300 * 1024)},
                allow_private=True,
                timeout=5,
                cancel_event=None,
            )
        )

    assert "too large" in str(excinfo.value)
    assert client.call_calls == []
