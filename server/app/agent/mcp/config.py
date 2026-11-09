"""Parse ``AGENT_MCP_*`` configuration into immutable settings.

Configuration is best-effort: one broken server is skipped and recorded in
``MCPConfig.errors`` so the rest of the run can proceed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from app.agent.mcp.security import MCPConfigError, interpolate_env, validate_mcp_url

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,15}$")
_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9-]+$")
_APPROVAL_MODES = {"auto", "always", "never"}

DEFAULT_DISCOVERY_TIMEOUT_SECONDS = 10.0
DEFAULT_CATALOG_TTL_SECONDS = 300
DEFAULT_CALL_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_SERVERS = 10
DEFAULT_MAX_TOOLS_PER_SERVER = 100
DEFAULT_MAX_SCHEMA_BYTES = 65_536
DEFAULT_MAX_RESULT_CHARS = 16_000


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name) or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(_env(name) or default))
    except ValueError:
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    url: str
    headers: tuple[tuple[str, str], ...]
    allow_tools: tuple[str, ...]
    deny_tools: tuple[str, ...]
    approval: Literal["auto", "always", "never"]
    timeout_seconds: float
    catalog_ttl_seconds: int
    max_result_chars: int
    allowed_hosts: tuple[str, ...]
    fingerprint: str


@dataclass(frozen=True)
class MCPConfig:
    servers: tuple[MCPServerConfig, ...]
    allow_insecure_http: bool
    allow_private: bool
    discovery_timeout_seconds: float
    max_tools_per_server: int
    max_schema_bytes: int
    errors: tuple[str, ...] = ()


def _string_tuple(value: Any) -> tuple[str, ...] | None:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return tuple(item for item in value if item.strip())


def _parse_headers(raw: Any) -> tuple[tuple[str, str], ...] | None:
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        return None
    resolved: list[tuple[str, str]] = []
    for key, value in raw.items():
        header = str(key or "").strip()
        if not header or not _HEADER_NAME_RE.match(header):
            raise MCPConfigError(f"invalid header name {header!r}")
        text = interpolate_env(str(value))
        if "\r" in text or "\n" in text:
            raise MCPConfigError(f"header {header!r} contains a newline")
        resolved.append((header, text))
    return tuple(resolved)


def _fingerprint(
    *,
    name: str,
    url: str,
    headers: tuple[tuple[str, str], ...],
    allow_tools: tuple[str, ...],
    deny_tools: tuple[str, ...],
    approval: str,
    timeout_seconds: float,
    catalog_ttl_seconds: int,
    max_result_chars: int,
    allowed_hosts: tuple[str, ...],
) -> str:
    payload = {
        "name": name,
        "url": url,
        "headers": {
            key: hashlib.sha256(value.encode("utf-8")).hexdigest() for key, value in headers
        },
        "allow_tools": allow_tools,
        "deny_tools": deny_tools,
        "approval": approval,
        "timeout_seconds": timeout_seconds,
        "catalog_ttl_seconds": catalog_ttl_seconds,
        "max_result_chars": max_result_chars,
        "allowed_hosts": allowed_hosts,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _parse_server(
    raw: Any,
    *,
    allow_insecure_http: bool,
    timeout_default: float,
    ttl_default: int,
    result_chars_default: int,
    max_result_chars: int,
) -> MCPServerConfig:
    if not isinstance(raw, dict):
        raise MCPConfigError("server entry must be an object")

    name = str(raw.get("name") or "").strip()
    if not _NAME_RE.match(name):
        raise MCPConfigError(
            f"invalid server name {name!r}; expected [a-z0-9][a-z0-9_-]{{0,15}}"
        )

    url = str(raw.get("url") or "").strip()
    if not url:
        raise MCPConfigError(f"server {name!r} is missing url")

    allowed_hosts = _string_tuple(raw.get("allowed_hosts"))
    if allowed_hosts is None:
        raise MCPConfigError(f"server {name!r} has invalid allowed_hosts")
    validate_mcp_url(
        url,
        allow_insecure_http=allow_insecure_http,
        allowed_hosts=allowed_hosts,
    )
    parsed_host = (urlparse(url).hostname or "").lower()
    hosts = allowed_hosts or (parsed_host,)

    headers = _parse_headers(raw.get("headers"))
    if headers is None:
        raise MCPConfigError(f"server {name!r} has invalid headers")

    allow_tools = _string_tuple(raw.get("allow_tools"))
    deny_tools = _string_tuple(raw.get("deny_tools"))
    if allow_tools is None or deny_tools is None:
        raise MCPConfigError(f"server {name!r} has invalid allow_tools/deny_tools")

    approval = str(raw.get("approval") or "auto").strip().lower()
    if approval not in _APPROVAL_MODES:
        raise MCPConfigError(f"server {name!r} has invalid approval mode {approval!r}")

    timeout_seconds = _clamp(
        float(raw.get("timeout_seconds") or timeout_default), 1.0, 300.0
    )
    catalog_ttl_seconds = int(_clamp(float(raw.get("catalog_ttl_seconds", ttl_default)), 0, 3600))
    server_result_chars = int(
        _clamp(float(raw.get("max_result_chars") or result_chars_default), 256, max_result_chars)
    )

    return MCPServerConfig(
        name=name,
        url=url,
        headers=headers,
        allow_tools=allow_tools,
        deny_tools=deny_tools,
        approval=approval,  # type: ignore[arg-type]
        timeout_seconds=timeout_seconds,
        catalog_ttl_seconds=catalog_ttl_seconds,
        max_result_chars=server_result_chars,
        allowed_hosts=tuple(host.lower() for host in hosts),
        fingerprint=_fingerprint(
            name=name,
            url=url,
            headers=headers,
            allow_tools=allow_tools,
            deny_tools=deny_tools,
            approval=approval,
            timeout_seconds=timeout_seconds,
            catalog_ttl_seconds=catalog_ttl_seconds,
            max_result_chars=server_result_chars,
            allowed_hosts=tuple(host.lower() for host in hosts),
        ),
    )


def _read_payload(errors: list[str]) -> Any:
    file_path = _env("AGENT_MCP_CONFIG_FILE")
    inline = _env("AGENT_MCP_SERVERS_JSON")
    if file_path and inline:
        logger.warning("Both AGENT_MCP_CONFIG_FILE and AGENT_MCP_SERVERS_JSON are set; using the file")
    if file_path:
        try:
            return json.loads(Path(file_path).read_text(encoding="utf-8"))
        except OSError as exc:
            errors.append(f"cannot read MCP config file: {type(exc).__name__}")
            return None
        except ValueError:
            errors.append("MCP config file is not valid JSON")
            return None
    if inline:
        try:
            return json.loads(inline)
        except ValueError:
            errors.append("AGENT_MCP_SERVERS_JSON is not valid JSON")
            return None
    errors.append("no MCP servers configured")
    return None


def load_mcp_config() -> MCPConfig | None:
    """Return parsed MCP settings, or ``None`` when the feature is off."""

    if not _env_bool("AGENT_MCP_ENABLED", False):
        return None

    allow_insecure_http = _env_bool("AGENT_MCP_ALLOW_INSECURE_HTTP", False)
    allow_private = _env_bool("AGENT_MCP_ALLOW_PRIVATE", False)
    discovery_timeout = _clamp(
        _env_float("AGENT_MCP_DISCOVERY_TIMEOUT_SECONDS", DEFAULT_DISCOVERY_TIMEOUT_SECONDS),
        1.0,
        120.0,
    )
    ttl_default = int(
        _clamp(_env_float("AGENT_MCP_CATALOG_TTL_SECONDS", DEFAULT_CATALOG_TTL_SECONDS), 0, 3600)
    )
    timeout_default = _clamp(
        _env_float("AGENT_MCP_TIMEOUT_SECONDS", DEFAULT_CALL_TIMEOUT_SECONDS), 1.0, 300.0
    )
    max_servers = int(_clamp(_env_float("AGENT_MCP_MAX_SERVERS", DEFAULT_MAX_SERVERS), 1, 100))
    max_tools_per_server = int(
        _clamp(_env_float("AGENT_MCP_MAX_TOOLS_PER_SERVER", DEFAULT_MAX_TOOLS_PER_SERVER), 1, 1000)
    )
    max_schema_bytes = int(
        _clamp(_env_float("AGENT_MCP_MAX_SCHEMA_BYTES", DEFAULT_MAX_SCHEMA_BYTES), 1024, 1_000_000)
    )
    result_chars = int(
        _clamp(
            _env_float(
                "AGENT_MCP_RESULT_MAX_CHARS",
                _env_float("AGENT_TOOL_RESULT_MAX_CHARS", DEFAULT_MAX_RESULT_CHARS),
            ),
            256,
            200_000,
        )
    )

    errors: list[str] = []
    payload = _read_payload(errors)
    raw_servers: Any = payload.get("servers") if isinstance(payload, dict) else payload
    if raw_servers is not None and not isinstance(raw_servers, list):
        errors.append("MCP config must contain a servers array")
        raw_servers = []

    servers: list[MCPServerConfig] = []
    seen: set[str] = set()
    for raw in raw_servers or []:
        enabled = raw.get("enabled", True) if isinstance(raw, dict) else True
        if enabled is False or (
            isinstance(enabled, str) and enabled.strip().lower() in {"false", "0", "no", "off"}
        ):
            continue
        if len(servers) >= max_servers:
            errors.append(f"AGENT_MCP_MAX_SERVERS={max_servers} reached; extra servers skipped")
            break
        try:
            server = _parse_server(
                raw,
                allow_insecure_http=allow_insecure_http,
                timeout_default=timeout_default,
                ttl_default=ttl_default,
                result_chars_default=result_chars,
                max_result_chars=result_chars,
            )
        except (MCPConfigError, TypeError, ValueError) as exc:
            errors.append(str(exc))
            continue
        if server.name in seen:
            errors.append(f"duplicate MCP server name {server.name!r}; later entry skipped")
            continue
        seen.add(server.name)
        servers.append(server)

    for error in errors:
        logger.warning("MCP config: %s", error)

    return MCPConfig(
        servers=tuple(servers),
        allow_insecure_http=allow_insecure_http,
        allow_private=allow_private,
        discovery_timeout_seconds=discovery_timeout,
        max_tools_per_server=max_tools_per_server,
        max_schema_bytes=max_schema_bytes,
        errors=tuple(errors),
    )
