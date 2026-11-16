"""Retry, breaker and idempotency plumbing in the server tool layer."""

import os
import stat
import sys
import threading

import pytest

_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import httpx  # noqa: E402

from app.agent import executor as agent_executor  # noqa: E402
from app.agent import executor_app  # noqa: E402
from app.agent.dev_tools import _atomic_write_text  # noqa: E402
from app.agent.executor_app import IdempotencyCache  # noqa: E402
from bokaclaw_agent import TransientToolError  # noqa: E402


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        if self.error is not None:
            raise self.error
        return self.response


def _install_client(monkeypatch, client):
    monkeypatch.setattr(agent_executor.httpx, "Client", lambda **kwargs: client)


def test_run_command_forwards_the_idempotency_key(monkeypatch):
    client = FakeClient(FakeResponse(200, {"stdout": "ok"}))
    _install_client(monkeypatch, client)
    config = agent_executor.ExecutorConfig(url="http://executor", token="t", timeout=5)

    result = agent_executor.run_command(config, "echo hi", idempotency_key="run:call")

    assert result == {"stdout": "ok"}
    assert client.calls[0]["json"]["idempotency_key"] == "run:call"


def test_run_command_omits_the_key_when_absent(monkeypatch):
    client = FakeClient(FakeResponse(200, {"stdout": "ok"}))
    _install_client(monkeypatch, client)
    config = agent_executor.ExecutorConfig(url="http://executor", timeout=5)

    agent_executor.run_command(config, "echo hi")

    assert "idempotency_key" not in client.calls[0]["json"]


def test_monitor_start_forwards_the_idempotency_key(monkeypatch):
    client = FakeClient(FakeResponse(200, {"monitor_id": "m1"}))
    _install_client(monkeypatch, client)
    config = agent_executor.ExecutorConfig(url="http://executor", timeout=5)

    agent_executor.monitor_action(config, "start", "sleep 1", idempotency_key="run:call")

    assert client.calls[0]["json"]["idempotency_key"] == "run:call"


def test_executor_gateway_errors_are_transient(monkeypatch):
    client = FakeClient(FakeResponse(503, {"detail": "restarting"}, headers={"retry-after": "3"}))
    _install_client(monkeypatch, client)
    config = agent_executor.ExecutorConfig(url="http://executor", timeout=5)

    with pytest.raises(TransientToolError) as info:
        agent_executor.run_command(config, "echo hi")

    assert info.value.retry_after == 3.0


def test_executor_client_errors_stay_permanent(monkeypatch):
    client = FakeClient(FakeResponse(400, {"detail": "bad command"}))
    _install_client(monkeypatch, client)
    config = agent_executor.ExecutorConfig(url="http://executor", timeout=5)

    with pytest.raises(RuntimeError) as info:
        agent_executor.run_command(config, "echo hi")

    assert not isinstance(info.value, TransientToolError)


def test_executor_transport_errors_are_transient(monkeypatch):
    client = FakeClient(error=httpx.ConnectError("refused"))
    _install_client(monkeypatch, client)
    config = agent_executor.ExecutorConfig(url="http://executor", timeout=5)

    with pytest.raises(TransientToolError):
        agent_executor.run_command(config, "echo hi")


def test_executor_timeouts_are_transient(monkeypatch):
    client = FakeClient(error=httpx.ReadTimeout("slow"))
    _install_client(monkeypatch, client)
    config = agent_executor.ExecutorConfig(url="http://executor", timeout=5)

    with pytest.raises(TransientToolError):
        agent_executor.run_command(config, "echo hi")


def test_idempotency_cache_runs_once_per_key():
    cache = IdempotencyCache(ttl_seconds=60, max_entries=8)
    calls = []

    def run():
        calls.append(1)
        return {"n": len(calls)}

    assert cache.run("key", run) == {"n": 1}
    assert cache.run("key", run) == {"n": 1}
    assert calls == [1]


def test_idempotency_cache_does_not_store_failures():
    cache = IdempotencyCache(ttl_seconds=60, max_entries=8)
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("transport failure")

    with pytest.raises(RuntimeError):
        cache.run("key", boom)

    assert cache.run("key", lambda: {"ok": True}) == {"ok": True}
    assert calls == [1]


def test_idempotency_cache_without_key_or_ttl_always_runs():
    calls = []

    def run():
        calls.append(1)
        return {}

    cache = IdempotencyCache(ttl_seconds=60, max_entries=8)
    cache.run(None, run)
    cache.run(None, run)
    assert len(calls) == 2

    disabled = IdempotencyCache(ttl_seconds=0, max_entries=8)
    disabled.run("key", run)
    disabled.run("key", run)
    assert len(calls) == 4


def test_idempotency_cache_expires_and_evicts(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(executor_app.time, "monotonic", lambda: now[0])
    cache = IdempotencyCache(ttl_seconds=10, max_entries=2)
    calls = []

    def run(label):
        calls.append(label)
        return {"label": label}

    assert cache.run("a", lambda: run("a")) == {"label": "a"}
    now[0] = 11.0
    assert cache.run("a", lambda: run("a")) == {"label": "a"}
    assert len(calls) == 2

    cache.run("a", lambda: run("a"))
    cache.run("b", lambda: run("b"))
    cache.run("c", lambda: run("c"))
    # "a" was evicted by the two-entry cap; the next call runs again.
    assert cache.run("a", lambda: run("a")) == {"label": "a"}
    assert calls.count("a") == 3


def test_idempotency_cache_waits_for_an_inflight_duplicate():
    cache = IdempotencyCache(ttl_seconds=60, max_entries=8)
    started = threading.Event()
    release = threading.Event()
    calls = []

    def slow():
        calls.append(1)
        started.set()
        assert release.wait(2)
        return {"n": 1}

    thread = threading.Thread(target=lambda: cache.run("key", slow))
    thread.start()
    try:
        assert started.wait(2)
        timer = threading.Timer(0.05, release.set)
        timer.start()
        try:
            assert cache.run("key", lambda: {"n": 2}) == {"n": 1}
        finally:
            timer.cancel()
    finally:
        release.set()
        thread.join(timeout=2)

    assert calls == [1]


def test_atomic_write_keeps_the_original_on_failure(tmp_path, monkeypatch):
    target = tmp_path / "note.txt"
    target.write_text("old", encoding="utf-8")

    def fail_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(OSError):
        _atomic_write_text(target, "new")

    assert target.read_text(encoding="utf-8") == "old"
    assert [path.name for path in tmp_path.iterdir()] == ["note.txt"]


def test_atomic_write_preserves_mode_and_creates_regular_files(tmp_path):
    existing = tmp_path / "existing.txt"
    existing.write_text("old", encoding="utf-8")
    os.chmod(existing, 0o640)

    _atomic_write_text(existing, "new")
    assert existing.read_text(encoding="utf-8") == "new"
    assert stat.S_IMODE(existing.stat().st_mode) == 0o640

    created = tmp_path / "created.txt"
    _atomic_write_text(created, "fresh")
    assert created.read_text(encoding="utf-8") == "fresh"
    assert stat.S_IMODE(created.stat().st_mode) == 0o644
