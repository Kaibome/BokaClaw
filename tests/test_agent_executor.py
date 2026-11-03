"""Tests for the sandboxed shell executor and its server-side client."""

import os
import sys
from pathlib import Path
from typing import Optional

from fastapi.testclient import TestClient

_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import app.agent.dev_tools as agent_dev_tools  # noqa: E402
import app.agent.executor as agent_executor  # noqa: E402
import app.agent.executor_app as executor_app  # noqa: E402
from app.agent.dev_tools import DevToolConfig, execute_command, load_dev_tool_config  # noqa: E402


def _config(workspace: Path, **overrides) -> DevToolConfig:
    values = {
        "workspace": workspace.resolve(),
        "allow_shell": True,
        "command_timeout": 5.0,
        "max_output_bytes": 100_000,
        "max_file_bytes": 100_000,
        "web_timeout": 5.0,
        "web_max_bytes": 100_000,
        "web_allow_private": False,
    }
    values.update(overrides)
    return DevToolConfig(**values)


def _client(tmp_path: Path, token: Optional[str] = "secret") -> TestClient:
    client = TestClient(executor_app.create_app(_config(tmp_path), token=token))
    if token:
        client.headers["Authorization"] = f"Bearer {token}"
    return client


# ---- configuration ------------------------------------------------------------


def test_executor_is_optional(monkeypatch):
    monkeypatch.delenv("AGENT_EXECUTOR_URL", raising=False)

    assert agent_executor.load_executor_config() is None


def test_executor_config_from_env(monkeypatch):
    monkeypatch.setenv("AGENT_EXECUTOR_URL", "http://bokaclaw-executor:8099/")
    monkeypatch.setenv("AGENT_EXECUTOR_TOKEN", "tok")
    monkeypatch.setenv("AGENT_EXECUTOR_TIMEOUT_SECONDS", "30")

    config = agent_executor.load_executor_config()

    assert config is not None
    assert config.url == "http://bokaclaw-executor:8099"
    assert config.token == "tok"
    assert config.timeout == 30.0


def test_dev_tool_config_carries_executor(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENT_EXECUTOR_URL", "http://executor:8099")
    monkeypatch.delenv("AGENT_EXECUTOR_TOKEN", raising=False)

    config = load_dev_tool_config()

    assert config is not None
    assert config.executor is not None
    assert config.executor.url == "http://executor:8099"
    assert config.executor.token is None


# ---- executor service ---------------------------------------------------------


def test_healthz_reports_workspace(tmp_path):
    response = _client(tmp_path).get("/healthz")

    assert response.status_code == 200
    assert response.json()["workspace"] == str(tmp_path.resolve())


def test_requests_require_the_token(tmp_path):
    client = TestClient(executor_app.create_app(_config(tmp_path), token="secret"))

    assert client.post("/exec", json={"command": "true"}).status_code == 401
    assert client.post("/exec", json={"command": "true"}, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.post("/exec", json={"command": "true"}, headers={"Authorization": "Bearer secret"}).status_code == 200


def test_exec_runs_in_the_workspace(tmp_path):
    result = _client(tmp_path).post("/exec", json={"command": "pwd", "description": "cwd"}).json()

    assert result["exit_code"] == 0
    assert result["stdout"].strip() == str(tmp_path.resolve())
    assert result["timed_out"] is False


def test_exec_reports_failures_and_timeouts(tmp_path):
    client = _client(tmp_path)

    failed = client.post("/exec", json={"command": "echo oops >&2; exit 3"}).json()
    assert failed["exit_code"] == 3
    assert "oops" in failed["stderr"]

    timed_out = client.post("/exec", json={"command": "sleep 2", "timeout_seconds": 1}).json()
    assert timed_out["timed_out"] is True


def test_exec_does_not_leak_the_server_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOULD_NOT_LEAK", "1")

    result = _client(tmp_path).post("/exec", json={"command": "printenv SHOULD_NOT_LEAK || true"}).json()

    assert result["stdout"].strip() == ""


def test_monitor_round_trip(tmp_path):
    client = _client(tmp_path)

    started = client.post("/monitor", json={"action": "start", "command": "printf 'ready\\n'; sleep 0.2"}).json()
    assert started["output"].strip() == "ready"

    stopped = client.post("/monitor", json={"action": "stop", "monitor_id": started["monitor_id"]}).json()
    assert stopped["running"] is False


# ---- delegation ---------------------------------------------------------------


def test_commands_are_delegated_when_executor_is_configured(tmp_path, monkeypatch):
    calls = []

    def fake_run(config, command, description="", timeout_seconds=120, *, shell="bash"):
        calls.append((config.url, command, description, timeout_seconds, shell))
        return {"exit_code": 0, "stdout": "remote", "stderr": "", "timed_out": False, "truncated": False}

    monkeypatch.setattr(agent_executor, "run_command", fake_run)
    config = _config(tmp_path, executor=agent_executor.ExecutorConfig(url="http://executor:8099"))

    result = execute_command(config, "echo hi", "desc", 30, shell="bash")

    assert result["stdout"] == "remote"
    assert calls == [("http://executor:8099", "echo hi", "desc", 30, "bash")]


def test_commands_stay_local_without_executor(tmp_path):
    config = _config(tmp_path)

    result = execute_command(config, "printf hi", "", 5, shell="bash")

    assert result["stdout"] == "hi"
    assert agent_dev_tools.command_env(config)["HOME"] == str(tmp_path.resolve())
