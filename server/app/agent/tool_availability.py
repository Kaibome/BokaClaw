"""Probe tools before they reach the model, with a process-wide 30s cache.

A run assembles its tool list from memory, dev, office, sheet, MCP and skill
providers. Some of those depend on the workspace, the shell executor, or an
outbound network path. Registering a broken tool wastes context and invites
calls that cannot succeed, so the run probes those dependencies first and
drops unavailable tools from the assembled list.

State machine, per tool::

    cache valid (<30s)             -> return the cached value, do not probe
    no cache / cache expired       -> probe
      probe True                   -> last_good = now, cache True for 30s
      probe False, recent success  -> return True (jitter), do not cache
      probe False, no success <60s -> cache False for 30s, return False

Probes that raise, or exceed their timeout, count as a False result. The whole
batch is bounded by ``PROBE_HARD_TIMEOUT_SECONDS`` so a hanging dependency can
never stall a run.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable, Mapping, Optional

import httpx

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 30.0
JITTER_GRACE_SECONDS = 60.0
LOCAL_PROBE_TIMEOUT_SECONDS = 2.0
EXTERNAL_PROBE_TIMEOUT_SECONDS = 5.0
PROBE_HARD_TIMEOUT_SECONDS = 10.0
MAX_PROBE_WORKERS = 32

# Stay below the probe deadline so a slow request fails on its own terms
# instead of being reported as a bare timeout.
_EXTERNAL_REQUEST_MARGIN_SECONDS = 1.0
PROBE_FETCH_URL = "https://example.com"

_MEMORY_TOOL_NAMES = frozenset(
    {"search_memories", "get_memory", "save_memory", "update_memory", "delete_memory"}
)
_READ_TOOL_NAMES = frozenset({"Read", "Glob", "Grep"})
_WRITE_TOOL_NAMES = frozenset({"Write", "Edit", "NotebookEdit"})
_SHELL_TOOL_NAMES = frozenset({"Bash", "PowerShell", "Monitor"})
_OFFICE_TOOL_NAMES = frozenset({"PptxBuild", "PptxUpdateSlide", "PptxAddSlide", "PptxDeleteSlide"})
_SHEET_READ_TOOL_NAMES = frozenset({"SheetInspect", "SheetQuery"})
_SHEET_WRITE_TOOL_NAMES = frozenset({"SheetCreate", "SheetTransform", "SheetPivot"})


@dataclass(frozen=True, slots=True)
class ToolProbe:
    """One dependency check and the deadline that applies to it."""

    check: Callable[[], bool]
    timeout: float = LOCAL_PROBE_TIMEOUT_SECONDS
    external: bool = False


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    available: bool
    expires_at: float


def _safe_probe(name: str, check: Callable[[], bool]) -> bool:
    try:
        return bool(check())
    except Exception:  # noqa: BLE001 - a probe failure means "unavailable"
        logger.debug("Tool probe failed name=%s", name, exc_info=True)
        return False


def _probe_result(future: Future[bool]) -> bool:
    try:
        return bool(future.result())
    except Exception:  # noqa: BLE001 - cancelled or failed probes are unavailable
        return False


class ToolAvailabilityTracker:
    """Process-wide availability state shared by every run."""

    def __init__(
        self,
        *,
        ttl: float = CACHE_TTL_SECONDS,
        grace: float = JITTER_GRACE_SECONDS,
        hard_timeout: float = PROBE_HARD_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = max(0.0, float(ttl))
        self._grace = max(0.0, float(grace))
        self._hard_timeout = max(0.0, float(hard_timeout))
        self._clock = clock
        self._cache: dict[str, _CacheEntry] = {}
        self._last_good: dict[str, float] = {}
        self._lock = threading.Lock()

    def reset(self) -> None:
        """Drop all cached state; tests and maintenance commands use this."""

        with self._lock:
            self._cache.clear()
            self._last_good.clear()

    def check(self, probes: Mapping[str, ToolProbe]) -> dict[str, bool]:
        """Return availability for ``probes``, probing only cache misses."""

        if not probes:
            return {}

        now = self._clock()
        decisions: dict[str, bool] = {}
        pending: dict[str, ToolProbe] = {}
        with self._lock:
            for name, probe in probes.items():
                entry = self._cache.get(name)
                if entry is not None and now < entry.expires_at:
                    decisions[name] = entry.available
                else:
                    pending[name] = probe

        fresh_unavailable: list[str] = []
        if pending:
            results = self._probe_all(pending)
            decided_at = self._clock()
            with self._lock:
                for name, available in results.items():
                    if available:
                        self._last_good[name] = decided_at
                        self._cache[name] = _CacheEntry(True, decided_at + self._ttl)
                        decisions[name] = True
                        continue
                    last_good = self._last_good.get(name)
                    if last_good is not None and decided_at - last_good < self._grace:
                        # A single failure inside the grace window is treated as
                        # jitter: report available and leave the cache empty so
                        # the next check probes again.
                        decisions[name] = True
                    else:
                        self._cache[name] = _CacheEntry(False, decided_at + self._ttl)
                        decisions[name] = False
                        fresh_unavailable.append(name)

        if fresh_unavailable:
            logger.warning("Tools unavailable; excluded: %s", ", ".join(sorted(fresh_unavailable)))
        return decisions

    def _probe_all(self, pending: Mapping[str, ToolProbe]) -> dict[str, bool]:
        """Run every pending probe concurrently, honoring per-tool deadlines.

        Tools may share one probe object (a workspace check, one MCP server), so
        group by probe first: a shared dependency is checked once per batch.
        """

        grouped: dict[ToolProbe, list[str]] = {}
        for name, probe in pending.items():
            grouped.setdefault(probe, []).append(name)

        started = time.monotonic()
        hard_deadline = started + self._hard_timeout
        pool = ThreadPoolExecutor(
            max_workers=min(len(grouped), MAX_PROBE_WORKERS),
            thread_name_prefix="tool-probe",
        )
        try:
            remaining: dict[Future[bool], tuple[list[str], float]] = {}
            for probe, names in grouped.items():
                future = pool.submit(_safe_probe, names[0], probe.check)
                remaining[future] = (names, started + max(0.05, probe.timeout))

            results: dict[str, bool] = {}

            def record(future: Future[bool], available: bool) -> None:
                names, _ = remaining.pop(future)
                for name in names:
                    results[name] = available

            while remaining:
                now = time.monotonic()
                for future in list(remaining):
                    _, deadline = remaining[future]
                    if future.done():
                        record(future, _probe_result(future))
                    elif now >= deadline:
                        future.cancel()
                        record(future, False)
                if not remaining:
                    break
                now = time.monotonic()
                if now >= hard_deadline:
                    for future in list(remaining):
                        future.cancel()
                        record(future, False)
                    break
                wake_at = min(min(deadline for _, deadline in remaining.values()), hard_deadline)
                done, _ = wait(
                    tuple(remaining),
                    timeout=max(0.001, wake_at - now),
                    return_when=FIRST_COMPLETED,
                )
                for future in done:
                    record(future, _probe_result(future))
            return results
        finally:
            # Never wait for a hung dependency at the end of the batch.
            pool.shutdown(wait=False, cancel_futures=True)


TOOL_AVAILABILITY = ToolAvailabilityTracker()


def probes_enabled() -> bool:
    """Return whether runs should probe tools (``AGENT_TOOL_PROBES=false`` opts out)."""

    raw = (os.environ.get("AGENT_TOOL_PROBES") or "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def filter_tools(
    tools: Iterable[Any],
    probes: Mapping[str, ToolProbe],
    *,
    tracker: Optional[ToolAvailabilityTracker] = None,
) -> list[Any]:
    """Drop tools whose probe reports them unavailable; unprobed tools stay."""

    items = list(tools)
    active = tracker if tracker is not None else TOOL_AVAILABILITY
    wanted = {
        name: probes[name]
        for name in (getattr(tool, "name", "") for tool in items)
        if isinstance(name, str) and name in probes
    }
    decisions = active.check(wanted)
    return [tool for tool in items if decisions.get(getattr(tool, "name", ""), True)]


def filter_tool_mapping(
    items: Mapping[str, Any],
    probes: Mapping[str, ToolProbe],
    *,
    tracker: Optional[ToolAvailabilityTracker] = None,
) -> dict[str, Any]:
    """Re-check a deferred catalog before ToolSearch offers its tools."""

    active = tracker if tracker is not None else TOOL_AVAILABILITY
    wanted = {name: probes[name] for name in items if name in probes}
    decisions = active.check(wanted)
    return {name: item for name, item in items.items() if decisions.get(name, True)}


def build_tool_probes(
    tools: Iterable[Any],
    *,
    dev_config: Any = None,
    mcp_config: Any = None,
) -> dict[str, ToolProbe]:
    """Map the tools in ``tools`` to their dependency probes.

    Tools without an entry are always assumed available: they are registered
    only when their enablement flag and dependencies already hold.
    """

    names = {getattr(tool, "name", "") for tool in tools}
    names.discard("")
    probes: dict[str, ToolProbe] = {}

    memory_names = names & _MEMORY_TOOL_NAMES
    if memory_names:
        probe = ToolProbe(_memory_service_check)
        probes.update({name: probe for name in memory_names})

    if dev_config is not None:
        _add_dev_probes(probes, names, dev_config)
    _add_mcp_probes(probes, names, mcp_config)
    return probes


def _add_dev_probes(probes: dict[str, ToolProbe], names: set[str], config: Any) -> None:
    read_probe = ToolProbe(lambda: _workspace_check(config, writable=False))
    write_probe = ToolProbe(lambda: _workspace_check(config, writable=True))
    for name in names & _READ_TOOL_NAMES:
        probes[name] = read_probe
    for name in names & _WRITE_TOOL_NAMES:
        probes[name] = write_probe

    executor = getattr(config, "executor", None)
    if executor is not None:
        executor_probe = ToolProbe(lambda: _executor_check(executor), EXTERNAL_PROBE_TIMEOUT_SECONDS, True)
        for name in names & _SHELL_TOOL_NAMES:
            probes[name] = executor_probe
    else:
        for name in names & _SHELL_TOOL_NAMES:
            probes[name] = ToolProbe(lambda name=name: _local_shell_check(name))

    if "WebFetch" in names:
        probes["WebFetch"] = ToolProbe(lambda: _web_fetch_check(config), EXTERNAL_PROBE_TIMEOUT_SECONDS, True)
    if "WebSearch" in names:
        probes["WebSearch"] = ToolProbe(lambda: _web_search_check(config), EXTERNAL_PROBE_TIMEOUT_SECONDS, True)

    office_names = names & _OFFICE_TOOL_NAMES
    if office_names:
        office_probe = ToolProbe(lambda: _office_check(config))
        probes.update({name: office_probe for name in office_names})

    sheet_read_names = names & _SHEET_READ_TOOL_NAMES
    if sheet_read_names:
        sheet_read_probe = ToolProbe(lambda: _sheet_check(config, writable=False))
        probes.update({name: sheet_read_probe for name in sheet_read_names})
    sheet_write_names = names & _SHEET_WRITE_TOOL_NAMES
    if sheet_write_names:
        sheet_write_probe = ToolProbe(lambda: _sheet_check(config, writable=True))
        probes.update({name: sheet_write_probe for name in sheet_write_names})


def _add_mcp_probes(probes: dict[str, ToolProbe], names: set[str], config: Any) -> None:
    if config is None:
        return
    for server in getattr(config, "servers", ()):
        prefix = f"mcp__{_mcp_component(getattr(server, 'name', ''))}__"
        matched = [name for name in names if name.startswith(prefix)]
        if not matched:
            continue
        probe = ToolProbe(lambda server=server: _mcp_server_check(server, config), EXTERNAL_PROBE_TIMEOUT_SECONDS, True)
        for name in matched:
            probes[name] = probe


def _mcp_component(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_-]", "_", str(value or "")).strip("_")
    return sanitized or "tool"


def _memory_service_check() -> bool:
    from app.core.server_state import get_memory_instance

    return get_memory_instance() is not None


def _workspace_check(config: Any, *, writable: bool) -> bool:
    from app.agent.dev_tools import _workspace_root

    root = _workspace_root(config)
    if not root.is_dir():
        return False
    return os.access(root, os.W_OK if writable else os.R_OK)


def _office_check(config: Any) -> bool:
    from app.agent.office_tools import PPTX_AVAILABLE

    return bool(PPTX_AVAILABLE) and _workspace_check(config, writable=True)


def _sheet_check(config: Any, *, writable: bool) -> bool:
    from app.agent.sheet_tools import SHEET_TOOLS_AVAILABLE

    return bool(SHEET_TOOLS_AVAILABLE) and _workspace_check(config, writable=writable)


def _web_fetch_check(config: Any) -> bool:
    from app.agent.dev_tools import _fetch_url_uncached

    timeout = _external_request_timeout(getattr(config, "web_timeout", EXTERNAL_PROBE_TIMEOUT_SECONDS))
    result = _fetch_url_uncached(replace(config, web_timeout=timeout), PROBE_FETCH_URL)
    return int(result.get("status") or 0) == 200


def _web_search_check(config: Any) -> bool:
    from app.agent.dev_tools import _search_web

    timeout = _external_request_timeout(getattr(config, "web_timeout", EXTERNAL_PROBE_TIMEOUT_SECONDS))
    result = _search_web(replace(config, web_timeout=timeout), "bokaclaw tool availability probe", 1)
    return isinstance(result.get("results"), list)


def _executor_check(executor: Any) -> bool:
    timeout = _external_request_timeout(getattr(executor, "timeout", EXTERNAL_PROBE_TIMEOUT_SECONDS))
    headers = {"Accept": "application/json"}
    token = getattr(executor, "token", None)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with httpx.Client(timeout=timeout) as client:
        response = client.get(f"{str(executor.url).rstrip('/')}/healthz", headers=headers)
    return response.status_code == 200


def _local_shell_check(name: str) -> bool:
    if name == "PowerShell":
        return bool(shutil.which("pwsh") or shutil.which("powershell"))
    return bool(shutil.which("bash") or shutil.which("sh"))


def _mcp_server_check(server: Any, config: Any) -> bool:
    from app.agent.mcp.manager import get_mcp_manager

    configured = float(getattr(config, "discovery_timeout_seconds", EXTERNAL_PROBE_TIMEOUT_SECONDS))
    timeout = max(0.5, min(configured, EXTERNAL_PROBE_TIMEOUT_SECONDS - _EXTERNAL_REQUEST_MARGIN_SECONDS))
    allow_private = bool(getattr(config, "allow_private", False))

    async def run() -> bool:
        catalog = await get_mcp_manager().get_catalog(
            server,
            allow_private=allow_private,
            discovery_timeout=timeout,
        )
        return catalog is not None

    return asyncio.run(run())


def _external_request_timeout(configured: float) -> float:
    limit = EXTERNAL_PROBE_TIMEOUT_SECONDS - _EXTERNAL_REQUEST_MARGIN_SECONDS
    try:
        value = float(configured)
    except (TypeError, ValueError):
        value = limit
    return max(1.0, min(value, limit))
