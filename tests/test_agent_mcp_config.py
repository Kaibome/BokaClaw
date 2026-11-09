"""MCP configuration parsing and validation."""

from __future__ import annotations

import json
import os
import sys

import pytest

_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from app.agent.mcp.config import load_mcp_config  # noqa: E402

_KEYS = (
    "AGENT_MCP_ENABLED",
    "AGENT_MCP_CONFIG_FILE",
    "AGENT_MCP_SERVERS_JSON",
    "AGENT_MCP_ALLOW_INSECURE_HTTP",
    "AGENT_MCP_ALLOW_PRIVATE",
    "AGENT_MCP_DISCOVERY_TIMEOUT_SECONDS",
    "AGENT_MCP_CATALOG_TTL_SECONDS",
    "AGENT_MCP_TIMEOUT_SECONDS",
    "AGENT_MCP_MAX_SERVERS",
    "AGENT_MCP_MAX_TOOLS_PER_SERVER",
    "AGENT_MCP_MAX_SCHEMA_BYTES",
    "AGENT_MCP_RESULT_MAX_CHARS",
    "AGENT_TOOL_RESULT_MAX_CHARS",
    "DEMO_TOKEN",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in _KEYS:
        monkeypatch.delenv(key, raising=False)


def _enable(monkeypatch, servers, *, extra_env=None):
    monkeypatch.setenv("AGENT_MCP_ENABLED", "true")
    monkeypatch.setenv("AGENT_MCP_SERVERS_JSON", json.dumps(servers))
    for key, value in (extra_env or {}).items():
        monkeypatch.setenv(key, value)


def test_disabled_by_default(monkeypatch):
    assert load_mcp_config() is None


def test_disabled_ignores_broken_json(monkeypatch):
    monkeypatch.setenv("AGENT_MCP_ENABLED", "false")
    monkeypatch.setenv("AGENT_MCP_SERVERS_JSON", "{not json")

    assert load_mcp_config() is None


def test_inline_server_parses_and_interpolates_headers(monkeypatch):
    _enable(
        monkeypatch,
        [
            {
                "name": "demo",
                "url": "https://mcp.example.com/mcp",
                "headers": {"Authorization": "Bearer ${DEMO_TOKEN}"},
                "allow_tools": ["get_*"],
                "deny_tools": ["delete_*"],
                "approval": "always",
                "timeout_seconds": 12,
                "catalog_ttl_seconds": 60,
            }
        ],
        extra_env={"DEMO_TOKEN": "secret-value"},
    )

    config = load_mcp_config()

    assert config is not None
    assert not config.errors
    server = config.servers[0]
    assert server.name == "demo"
    assert server.headers == (("Authorization", "Bearer secret-value"),)
    assert server.allow_tools == ("get_*",)
    assert server.deny_tools == ("delete_*",)
    assert server.approval == "always"
    assert server.timeout_seconds == 12
    assert server.catalog_ttl_seconds == 60
    assert "secret-value" not in server.fingerprint


def test_missing_env_skips_only_that_server(monkeypatch):
    _enable(
        monkeypatch,
        [
            {"name": "broken", "url": "https://a.example.com/mcp", "headers": {"X": "${MISSING}"}},
            {"name": "ok", "url": "https://b.example.com/mcp"},
        ],
    )

    config = load_mcp_config()

    assert config is not None
    assert [server.name for server in config.servers] == ["ok"]
    assert any("MISSING" in error for error in config.errors)


def test_duplicate_names_keep_first(monkeypatch):
    _enable(
        monkeypatch,
        [
            {"name": "demo", "url": "https://a.example.com/mcp"},
            {"name": "demo", "url": "https://b.example.com/mcp"},
        ],
    )

    config = load_mcp_config()

    assert config is not None
    assert [server.url for server in config.servers] == ["https://a.example.com/mcp"]
    assert any("duplicate" in error for error in config.errors)


def test_http_requires_explicit_flag(monkeypatch):
    _enable(monkeypatch, [{"name": "demo", "url": "http://mcp.example.com/mcp"}])

    rejected = load_mcp_config()
    assert rejected is not None
    assert rejected.servers == ()
    assert any("http" in error for error in rejected.errors)

    monkeypatch.setenv("AGENT_MCP_ALLOW_INSECURE_HTTP", "true")
    accepted = load_mcp_config()
    assert accepted is not None
    assert [server.name for server in accepted.servers] == ["demo"]


def test_allowed_hosts_must_contain_url_host(monkeypatch):
    _enable(
        monkeypatch,
        [
            {
                "name": "demo",
                "url": "https://mcp.example.com/mcp",
                "allowed_hosts": ["other.example.com"],
            }
        ],
    )

    config = load_mcp_config()

    assert config is not None
    assert config.servers == ()
    assert any("allowed_hosts" in error for error in config.errors)


def test_invalid_json_does_not_raise(monkeypatch):
    monkeypatch.setenv("AGENT_MCP_ENABLED", "true")
    monkeypatch.setenv("AGENT_MCP_SERVERS_JSON", "{broken")

    config = load_mcp_config()

    assert config is not None
    assert config.servers == ()
    assert any("JSON" in error for error in config.errors)


def test_enabled_false_server_is_skipped(monkeypatch):
    _enable(
        monkeypatch,
        [
            {"name": "demo", "url": "https://mcp.example.com/mcp", "enabled": False},
            {"name": "other", "url": "https://other.example.com/mcp"},
        ],
    )

    config = load_mcp_config()

    assert config is not None
    assert [server.name for server in config.servers] == ["other"]


def test_max_servers_is_enforced(monkeypatch):
    _enable(
        monkeypatch,
        [
            {"name": "one", "url": "https://one.example.com/mcp"},
            {"name": "two", "url": "https://two.example.com/mcp"},
        ],
    )
    monkeypatch.setenv("AGENT_MCP_MAX_SERVERS", "1")

    config = load_mcp_config()

    assert config is not None
    assert [server.name for server in config.servers] == ["one"]
    assert any("MAX_SERVERS" in error for error in config.errors)
