"""MCP tool mapping, filtering, and proxy invocation."""

from __future__ import annotations

import asyncio
import json
import os
import sys

_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from app.agent.mcp.catalog import MCPCatalog  # noqa: E402
from app.agent.mcp.client import MCPCallOutcome, MCPContent, MCPError  # noqa: E402
from app.agent.mcp.config import MCPConfig  # noqa: E402
from app.agent.mcp.tools import build_mcp_tools, is_exposed, namespaced_name  # noqa: E402

from .mcp_fakes import FakeInteraction, FakeManager, FakeSDK, make_server, make_tool  # noqa: E402


def _config(**overrides) -> MCPConfig:
    values = {
        "servers": (),
        "allow_insecure_http": False,
        "allow_private": True,
        "discovery_timeout_seconds": 5.0,
        "max_tools_per_server": 100,
        "max_schema_bytes": 65_536,
    }
    values.update(overrides)
    return MCPConfig(**values)


def _catalog(server, tools) -> MCPCatalog:
    return MCPCatalog(
        server=server.name,
        tools=tuple(tools),
        fetched_at=0.0,
        fingerprint=server.fingerprint,
    )


def _build(server, tools, manager=None, *, interaction=None, cancel_event=None, **config):
    manager = manager or FakeManager()
    cfg = _config(servers=(server,), **config)
    built = build_mcp_tools(
        FakeSDK,
        cfg,
        [(server, _catalog(server, tools))],
        manager,
        interaction=interaction,
        cancel_event=cancel_event,
    )
    return built, manager


def test_namespacing_and_sanitization():
    assert namespaced_name("demo", "get_item", set()) == "mcp__demo__get_item"
    assert namespaced_name("demo", "a.b:c/d e", set()) == "mcp__demo__a_b_c_d_e"
    assert namespaced_name("demo", "___", set()) == "mcp__demo__tool"


def test_overlong_names_are_truncated_and_unique():
    first = namespaced_name("demo", "x" * 100, set())
    second = namespaced_name("demo", "y" * 100, set())

    assert len(first) <= 64
    assert len(second) <= 64
    assert first != second


def test_collisions_get_a_deterministic_suffix():
    taken = {"mcp__demo__get_item"}
    collided = namespaced_name("demo", "get_item", taken)

    assert collided != "mcp__demo__get_item"
    assert collided.startswith("mcp__demo__get_item-")
    assert len(collided) <= 64


def test_allow_deny_deny_wins():
    server = make_server(allow_tools=("get_*", "delete_*"), deny_tools=("delete_*",))

    assert is_exposed(server, "get_item")
    assert not is_exposed(server, "delete_item")
    assert not is_exposed(server, "other")


def test_schema_description_and_effect_mapping():
    server = make_server()
    schema = {"type": "object", "properties": {"id": {"type": "string"}}}
    tools, _ = _build(
        server,
        [
            make_tool("get_item", read_only=True, schema=schema),
            make_tool("write_item", destructive=True),
            make_tool("unannotated"),
        ],
    )

    by_name = {tool.name: tool for tool in tools}

    assert by_name["mcp__demo__get_item"].effect == "read"
    assert by_name["mcp__demo__get_item"].concurrency_safe is True
    assert by_name["mcp__demo__get_item"].parameters == schema
    assert "untrusted data" in by_name["mcp__demo__get_item"].description
    assert by_name["mcp__demo__write_item"].effect == "write"
    assert by_name["mcp__demo__write_item"].concurrency_safe is False
    assert by_name["mcp__demo__unannotated"].effect == "write"


def test_invalid_and_oversized_schemas_are_skipped():
    server = make_server()
    tools, _ = _build(
        server,
        [
            make_tool("array_schema", schema={"type": "array"}),
            make_tool("huge", schema={"type": "object", "properties": {"x": {"description": "y" * 500}}}),
            make_tool("ok"),
        ],
        max_schema_bytes=256,
    )

    assert [tool.name for tool in tools] == ["mcp__demo__ok"]


def test_tool_cap_is_applied_after_filtering():
    server = make_server(deny_tools=("skip_*",))
    tools, _ = _build(
        server,
        [make_tool("skip_one"), make_tool("keep_one"), make_tool("keep_two")],
        max_tools_per_server=2,
    )

    assert [tool.name for tool in tools] == ["mcp__demo__keep_one", "mcp__demo__keep_two"]


def test_read_only_tool_invokes_without_approval():
    server = make_server()
    manager = FakeManager(MCPCallOutcome(contents=(MCPContent("text", text="hello"),)))
    tools, _ = _build(server, [make_tool(read_only=True)], manager)
    interaction = FakeInteraction()

    result = asyncio.run(tools[0].func(id="1"))

    assert manager.calls == [("get_item", {"id": "1"})]
    assert not interaction.asked
    assert result.is_error is False
    assert "BEGIN UNTRUSTED MCP CONTENT" in result.content
    assert "hello" in result.content
    assert result.metadata["mcp_server"] == "demo"


def test_destructive_tool_denied_never_calls_the_server():
    server = make_server()
    manager = FakeManager()

    tools, _ = _build(
        server,
        [make_tool(destructive=True)],
        manager,
        interaction=FakeInteraction(answer="Deny"),
    )

    result = asyncio.run(tools[0].func())

    assert manager.calls == []
    assert result.is_error is True
    assert "not approved" in result.content


def test_destructive_tool_without_interaction_fails_closed():
    server = make_server()
    manager = FakeManager()
    tools, _ = _build(server, [make_tool(destructive=True)], manager)

    result = asyncio.run(tools[0].func())

    assert manager.calls == []
    assert result.is_error is True
    assert "no interactive approval channel" in result.content


def test_approval_always_prompts_even_for_read_only():
    server = make_server(approval="always")
    manager = FakeManager()
    interaction = FakeInteraction(answer="Allow")
    tools, _ = _build(server, [make_tool(read_only=True)], manager, interaction=interaction)

    result = asyncio.run(tools[0].func())

    assert len(interaction.asked) == 1
    assert manager.calls == [("get_item", {})]
    assert result.is_error is False


def test_mcp_error_is_returned_as_a_tool_error():
    server = make_server()
    manager = FakeManager(error=MCPError("MCP server 'demo' is unavailable (ConnectError)."))
    tools, _ = _build(server, [make_tool(read_only=True)], manager)

    result = asyncio.run(tools[0].func())

    assert result.is_error is True
    assert json.loads(result.content)["error"].startswith("MCP server 'demo' is unavailable")


def test_cancelled_error_is_marked_aborted():
    server = make_server()
    manager = FakeManager(error=MCPError("MCP tool call was cancelled.", aborted=True))
    tools, _ = _build(server, [make_tool(read_only=True)], manager)

    result = asyncio.run(tools[0].func())

    assert result.is_error is True
    assert result.metadata.get("aborted") is True
