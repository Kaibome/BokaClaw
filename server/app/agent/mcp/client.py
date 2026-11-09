"""Streamable HTTP MCP client adapter.

This is the only module that imports the ``mcp`` SDK, and those imports happen
inside ``_default_session`` so the rest of the server (and its tests) can run
without the optional package installed.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, AsyncIterator, Protocol

from app.agent.mcp.config import MCPServerConfig


class MCPError(Exception):
    """A sanitized, model-visible MCP failure."""

    def __init__(self, message: str, *, aborted: bool = False) -> None:
        super().__init__(message)
        self.aborted = aborted


@dataclass(frozen=True, slots=True)
class MCPToolInfo:
    server: str
    name: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool = False
    destructive: bool = False
    idempotent: bool = False
    open_world: bool | None = None


@dataclass(frozen=True, slots=True)
class MCPContent:
    kind: str
    text: str | None = None
    uri: str | None = None
    mime_type: str | None = None
    data_chars: int = 0


@dataclass(frozen=True, slots=True)
class MCPCallOutcome:
    contents: tuple[MCPContent, ...]
    structured: Any | None = None
    is_error: bool = False


class MCPClient(Protocol):
    async def list_tools(self, server: MCPServerConfig, *, timeout: float) -> list[MCPToolInfo]: ...

    async def call_tool(
        self,
        server: MCPServerConfig,
        name: str,
        arguments: dict[str, Any],
        *,
        timeout: float,
    ) -> MCPCallOutcome: ...


def _normalize_content(block: Any) -> MCPContent:
    kind = str(getattr(block, "type", "") or "")
    if kind == "text":
        return MCPContent(kind="text", text=str(getattr(block, "text", "") or ""))
    if kind in {"image", "audio"}:
        data = getattr(block, "data", "") or ""
        return MCPContent(
            kind=kind,
            mime_type=str(getattr(block, "mimeType", "") or ""),
            data_chars=len(data) if isinstance(data, str) else 0,
        )
    if kind == "resource":
        resource = getattr(block, "resource", None)
        uri = str(getattr(resource, "uri", "") or "")
        mime_type = str(getattr(resource, "mimeType", "") or "")
        text = getattr(resource, "text", None)
        if text is not None:
            return MCPContent(kind="resource_text", text=str(text), uri=uri, mime_type=mime_type)
        blob = getattr(resource, "blob", "") or ""
        return MCPContent(
            kind="resource_blob",
            uri=uri,
            mime_type=mime_type,
            data_chars=len(blob) if isinstance(blob, str) else 0,
        )
    if kind == "resource_link":
        return MCPContent(
            kind="resource_link",
            uri=str(getattr(block, "uri", "") or ""),
            mime_type=str(getattr(block, "mimeType", "") or ""),
        )
    return MCPContent(kind=kind or "unknown", text=str(block)[:500])


def _http_client_factory(*, headers: Any = None, timeout: Any = None, auth: Any = None) -> Any:
    """Create an httpx client that never follows redirects (SSRF hardening)."""

    import httpx

    return httpx.AsyncClient(
        headers=headers,
        timeout=timeout,
        auth=auth,
        follow_redirects=False,
    )


class StreamableHTTPClient:
    """Short-lived Streamable HTTP sessions.

    A session is opened per discovery or call. This avoids cross-run state and
    thread-affinity problems in the SDK's async runtime; a persistent pool is a
    later optimization.
    """

    def __init__(self, session_factory: Any | None = None) -> None:
        self._session_factory = session_factory

    @asynccontextmanager
    async def _session(self, server: MCPServerConfig, timeout: float) -> AsyncIterator[Any]:
        if self._session_factory is not None:
            async with self._session_factory(server, timeout) as session:
                yield session
            return

        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(
            server.url,
            headers=dict(server.headers),
            timeout=timeout,
            sse_read_timeout=timeout,
            httpx_client_factory=_http_client_factory,
        ) as (read_stream, write_stream, _):
            async with ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=timedelta(seconds=timeout),
            ) as session:
                await session.initialize()
                yield session

    async def list_tools(self, server: MCPServerConfig, *, timeout: float) -> list[MCPToolInfo]:
        async with self._session(server, timeout) as session:
            result = await session.list_tools()

        tools: list[MCPToolInfo] = []
        for tool in getattr(result, "tools", ()) or ():
            schema = getattr(tool, "inputSchema", None)
            if not isinstance(schema, dict):
                schema = {}
            annotations = getattr(tool, "annotations", None)
            tools.append(
                MCPToolInfo(
                    server=server.name,
                    name=str(getattr(tool, "name", "") or ""),
                    description=str(getattr(tool, "description", "") or ""),
                    input_schema=schema,
                    read_only=bool(getattr(annotations, "readOnlyHint", False)),
                    destructive=bool(getattr(annotations, "destructiveHint", False)),
                    idempotent=bool(getattr(annotations, "idempotentHint", False)),
                    open_world=getattr(annotations, "openWorldHint", None),
                )
            )
        return tools

    async def call_tool(
        self,
        server: MCPServerConfig,
        name: str,
        arguments: dict[str, Any],
        *,
        timeout: float,
    ) -> MCPCallOutcome:
        async with self._session(server, timeout) as session:
            result = await session.call_tool(
                name,
                arguments,
                read_timeout_seconds=timedelta(seconds=timeout),
            )

        contents = tuple(
            _normalize_content(block) for block in (getattr(result, "content", ()) or ())
        )
        return MCPCallOutcome(
            contents=contents,
            structured=getattr(result, "structuredContent", None),
            is_error=bool(getattr(result, "isError", False)),
        )


async def _discard(task: asyncio.Task[Any]) -> None:
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


async def run_with_cancel(awaitable: Any, *, timeout: float, cancel_event: Any | None) -> Any:
    """Bound one MCP operation by timeout and cooperative cancellation."""

    task = asyncio.ensure_future(awaitable)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                await _discard(task)
                raise MCPError("MCP tool call was cancelled.", aborted=True)
            remaining = deadline - loop.time()
            if remaining <= 0:
                await _discard(task)
                raise MCPError(f"MCP tool call timed out after {timeout:.0f}s.")
            done, _ = await asyncio.wait({task}, timeout=min(0.2, remaining))
            if done:
                return task.result()
    finally:
        if not task.done():
            await _discard(task)
