from __future__ import annotations

import pytest
from pydantic import ValidationError

from bokaclaw_agent.config import AgentConfig, ModelConfig
from bokaclaw_agent.sessions.postgres import PostgresConfig


def test_agent_config_defaults():
    config = AgentConfig()
    assert config.name == "agent"
    assert config.max_iterations == 10
    assert config.tool_error_mode == "return"
    assert config.model is None


def test_agent_config_rejects_unknown_tool_choice():
    with pytest.raises(ValidationError):
        AgentConfig(tool_choice="sometimes")


def test_agent_config_accepts_dict_tool_choice():
    config = AgentConfig(tool_choice={"type": "function", "function": {"name": "add"}})
    assert config.tool_choice["function"]["name"] == "add"


def test_model_config_defaults():
    config = ModelConfig(model="gpt-4o-mini")
    assert config.timeout == 60.0
    assert config.max_retries == 2


def test_postgres_config_from_env(monkeypatch):
    monkeypatch.setenv("POSTGRES_HOST", "db.local")
    monkeypatch.setenv("POSTGRES_PORT", "6543")
    monkeypatch.setenv("POSTGRES_DB", "mem0_app")
    monkeypatch.setenv("POSTGRES_USER", "alice")
    monkeypatch.setenv("POSTGRES_PASSWORD", "secret")
    monkeypatch.delenv("BOKACLAW_AGENT_POSTGRES_DSN", raising=False)
    monkeypatch.delenv("POSTGRES_DSN", raising=False)

    config = PostgresConfig.from_env()

    assert config.host == "db.local"
    assert config.port == 6543
    assert config.dbname == "mem0_app"
    assert config.user == "alice"
    assert config.password == "secret"
    assert config.dsn is None


def test_postgres_config_dsn_env(monkeypatch):
    monkeypatch.setenv("BOKACLAW_AGENT_POSTGRES_DSN", "postgresql://u:p@localhost/db")
    config = PostgresConfig.from_env()
    assert config.dsn == "postgresql://u:p@localhost/db"


def test_postgres_config_rejects_invalid_prefix():
    with pytest.raises(ValidationError):
        PostgresConfig(table_prefix="bad-prefix")


def test_postgres_config_rejects_inverted_pool_sizes():
    with pytest.raises(ValidationError):
        PostgresConfig(minconn=5, maxconn=2)
