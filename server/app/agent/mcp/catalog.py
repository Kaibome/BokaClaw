"""Process-wide MCP tool catalog cache."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from app.agent.mcp.client import MCPToolInfo
from app.agent.mcp.config import MCPServerConfig

STALE_CATALOG_MAX_AGE_SECONDS = 86_400
MAX_BACKOFF_SECONDS = 300


@dataclass(frozen=True, slots=True)
class MCPCatalog:
    server: str
    tools: tuple[MCPToolInfo, ...]
    fetched_at: float
    fingerprint: str
    stale: bool = False


@dataclass
class _ServerState:
    catalog: MCPCatalog | None = None
    failures: int = 0
    next_retry_at: float = 0.0


class CatalogCache:
    def __init__(self) -> None:
        self._state: dict[str, _ServerState] = {}
        self._lock = threading.Lock()

    def get(self, server: MCPServerConfig, *, now: float | None = None) -> MCPCatalog | None:
        current = time.monotonic() if now is None else now
        with self._lock:
            state = self._state.get(server.name)
            if state is None or state.catalog is None:
                return None
            catalog = state.catalog
        if catalog.fingerprint != server.fingerprint:
            return None
        if current - catalog.fetched_at > STALE_CATALOG_MAX_AGE_SECONDS:
            return None
        return catalog

    def is_fresh(
        self,
        catalog: MCPCatalog | None,
        server: MCPServerConfig,
        *,
        now: float | None = None,
    ) -> bool:
        if catalog is None or catalog.fingerprint != server.fingerprint:
            return False
        current = time.monotonic() if now is None else now
        return current - catalog.fetched_at < server.catalog_ttl_seconds

    def put(self, server: MCPServerConfig, tools: list[MCPToolInfo]) -> MCPCatalog:
        catalog = MCPCatalog(
            server=server.name,
            tools=tuple(tools),
            fetched_at=time.monotonic(),
            fingerprint=server.fingerprint,
        )
        with self._lock:
            state = self._state.setdefault(server.name, _ServerState())
            state.catalog = catalog
            state.failures = 0
            state.next_retry_at = 0.0
        return catalog

    def mark_failure(self, server: MCPServerConfig, *, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        with self._lock:
            state = self._state.setdefault(server.name, _ServerState())
            state.failures += 1
            delay = min(MAX_BACKOFF_SECONDS, 2**state.failures)
            state.next_retry_at = current + delay
            if state.catalog is not None:
                state.catalog = MCPCatalog(
                    server=state.catalog.server,
                    tools=state.catalog.tools,
                    fetched_at=state.catalog.fetched_at,
                    fingerprint=state.catalog.fingerprint,
                    stale=True,
                )

    def can_retry(self, server: MCPServerConfig, *, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        with self._lock:
            state = self._state.get(server.name)
            return state is None or current >= state.next_retry_at

    def clear(self) -> None:
        with self._lock:
            self._state.clear()
