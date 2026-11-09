"""MCP server discovery, catalog caching, and tool invocation."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any

from app.agent.mcp.catalog import CatalogCache, MCPCatalog
from app.agent.mcp.client import MCPCallOutcome, MCPClient, MCPError, StreamableHTTPClient, run_with_cancel
from app.agent.mcp.config import MCPConfig, MCPServerConfig
from app.agent.mcp.security import check_public_url

logger = logging.getLogger(__name__)

MAX_ARGUMENT_BYTES = 256 * 1024


class MCPManager:
    def __init__(
        self,
        client: MCPClient | None = None,
        cache: CatalogCache | None = None,
    ) -> None:
        self.client = client or StreamableHTTPClient()
        self.cache = cache or CatalogCache()

    async def get_catalog(
        self,
        server: MCPServerConfig,
        *,
        allow_private: bool,
        discovery_timeout: float,
    ) -> MCPCatalog | None:
        cached = self.cache.get(server)
        if cached is not None and self.cache.is_fresh(cached, server):
            return cached
        if not self.cache.can_retry(server):
            return cached

        try:
            await asyncio.to_thread(check_public_url, server.url, allow_private=allow_private)
            tools = await asyncio.wait_for(
                self.client.list_tools(server, timeout=discovery_timeout),
                timeout=discovery_timeout + 1.0,
            )
        except Exception as exc:  # noqa: BLE001 - one server must never fail the run
            self.cache.mark_failure(server)
            logger.warning(
                "MCP discovery failed server=%s error=%s", server.name, type(exc).__name__
            )
            return cached

        catalog = self.cache.put(server, tools)
        logger.info("MCP catalog loaded server=%s tools=%d", server.name, len(tools))
        return catalog

    async def call_tool(
        self,
        server: MCPServerConfig,
        name: str,
        arguments: dict[str, Any],
        *,
        allow_private: bool,
        timeout: float,
        cancel_event: threading.Event | None,
    ) -> MCPCallOutcome:
        try:
            encoded = json.dumps(arguments, ensure_ascii=False, default=str).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise MCPError("MCP tool arguments are not JSON-serializable.") from exc
        if len(encoded) > MAX_ARGUMENT_BYTES:
            raise MCPError("MCP tool arguments are too large.")

        await asyncio.to_thread(check_public_url, server.url, allow_private=allow_private)
        try:
            return await run_with_cancel(
                self.client.call_tool(server, name, arguments, timeout=timeout),
                timeout=timeout,
                cancel_event=cancel_event,
            )
        except MCPError:
            raise
        except asyncio.TimeoutError as exc:
            raise MCPError(f"MCP tool call timed out after {timeout:.0f}s.") from exc
        except Exception as exc:  # noqa: BLE001 - keep URLs and headers out of the message
            raise MCPError(
                f"MCP server '{server.name}' is unavailable ({type(exc).__name__})."
            ) from exc

    async def prepare_tools(
        self,
        config: MCPConfig,
        sdk: Any,
        *,
        interaction: Any = None,
        cancel_event: threading.Event | None = None,
    ) -> list[Any]:
        if not config.servers:
            return []

        results = await asyncio.gather(
            *(
                self.get_catalog(
                    server,
                    allow_private=config.allow_private,
                    discovery_timeout=config.discovery_timeout_seconds,
                )
                for server in config.servers
            ),
            return_exceptions=True,
        )
        catalogs = [
            (server, result)
            for server, result in zip(config.servers, results, strict=False)
            if isinstance(result, MCPCatalog)
        ]
        if not catalogs:
            return []

        from app.agent.mcp.tools import build_mcp_tools

        return build_mcp_tools(
            sdk,
            config,
            catalogs,
            self,
            interaction=interaction,
            cancel_event=cancel_event,
        )


_manager: MCPManager | None = None
_manager_lock = threading.Lock()


def get_mcp_manager() -> MCPManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = MCPManager()
        return _manager


def reset_mcp_manager(manager: MCPManager | None = None) -> None:
    """Replace the singleton; tests use this to inject fakes."""

    global _manager
    with _manager_lock:
        _manager = manager


async def prepare_mcp_tools(
    config: MCPConfig,
    sdk: Any,
    *,
    interaction: Any = None,
    cancel_event: threading.Event | None = None,
) -> list[Any]:
    return await get_mcp_manager().prepare_tools(
        config,
        sdk,
        interaction=interaction,
        cancel_event=cancel_event,
    )
