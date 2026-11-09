"""Streamable HTTP adapter mapping (fake session, no network)."""

from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace

_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from app.agent.mcp.client import StreamableHTTPClient  # noqa: E402

from .mcp_fakes import make_server  # noqa: E402


@dataclass
class FakeSession:
    tool_result: object
    call_result: object

    async def list_tools(self):
        return self.tool_result

    async def call_tool(self, name, arguments, *, read_timeout_seconds=None):
        return self.call_result


def _client(session: FakeSession) -> StreamableHTTPClient:
    @asynccontextmanager
    async def factory(server, timeout):
        yield session

    return StreamableHTTPClient(session_factory=factory)


def test_list_tools_maps_schema_and_annotations():
    tools = [
        SimpleNamespace(
            name="get_item",
            description="Get it",
            inputSchema={"type": "object", "properties": {"id": {"type": "string"}}},
            annotations=SimpleNamespace(
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            ),
        ),
        SimpleNamespace(
            name="write_item",
            description="Write it",
            inputSchema={"type": "object"},
            annotations=None,
        ),
    ]
    session = FakeSession(tool_result=SimpleNamespace(tools=tools), call_result=None)

    result = asyncio_run(_client(session).list_tools(make_server(), timeout=5))

    assert result[0].name == "get_item"
    assert result[0].read_only is True
    assert result[0].destructive is False
    assert result[0].idempotent is True
    assert result[0].open_world is False
    assert result[0].input_schema["properties"]["id"]["type"] == "string"
    assert result[1].read_only is False


def test_call_tool_normalizes_content_blocks_and_errors():
    content = [
        SimpleNamespace(type="text", text="hello"),
        SimpleNamespace(type="image", mimeType="image/png", data="abcd"),
        SimpleNamespace(
            type="resource",
            resource=SimpleNamespace(
                uri="file:///a.txt", mimeType="text/plain", text="body", blob=None
            ),
        ),
        SimpleNamespace(
            type="resource",
            resource=SimpleNamespace(
                uri="file:///b.bin", mimeType="application/octet-stream", text=None, blob="xx"
            ),
        ),
        SimpleNamespace(type="resource_link", uri="file:///c.txt", mimeType="text/plain"),
    ]
    session = FakeSession(
        tool_result=None,
        call_result=SimpleNamespace(
            content=content,
            structuredContent={"n": 1},
            isError=True,
        ),
    )

    result = asyncio_run(
        _client(session).call_tool(make_server(), "get_item", {"id": "1"}, timeout=5)
    )

    assert [item.kind for item in result.contents] == [
        "text",
        "image",
        "resource_text",
        "resource_blob",
        "resource_link",
    ]
    assert result.contents[1].data_chars == 4
    assert result.contents[2].text == "body"
    assert result.contents[3].data_chars == 2
    assert result.structured == {"n": 1}
    assert result.is_error is True


def asyncio_run(awaitable):
    import asyncio

    return asyncio.run(awaitable)
