"""Turn-end memory writes append one episode and nothing else."""

import os
import sys
import types

_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from app.agent.turn_memory import save_turn_memory  # noqa: E402


def test_save_turn_memory_persists_only_the_episode(monkeypatch):
    captured = {}

    class FakeService:
        def capture_turn(self, user_id, message, content, **kwargs):
            captured.update(
                {"user_id": user_id, "message": message, "content": content, **kwargs}
            )
            return types.SimpleNamespace(action="appended", chars=120, memory_id="m-1", path="/tmp/day.md", reason=None)

    monkeypatch.setitem(sys.modules, "app.core.server_state", types.SimpleNamespace(get_memory_instance=FakeService))
    trace = [{"name": "WebSearch", "status": "completed", "content": '{"results": []}'}]

    report = save_turn_memory(
        {"user_id": "user-1", "message": "which model?", "run_id": "run-1"},
        "answer",
        trace,
    )

    assert report.action == "appended"
    assert report.kind == "episode"
    assert report.captured == 1
    assert report.entries == 1
    assert report.sources == 0
    assert report.path == "/tmp/day.md"
    assert captured["user_id"] == "user-1"
    assert captured["tool_entries"] == trace


def test_save_turn_memory_passes_the_injected_flag(monkeypatch):
    captured = {}

    class FakeService:
        def capture_turn(self, user_id, message, content, **kwargs):
            captured.update(kwargs)
            return types.SimpleNamespace(action="skipped", chars=0, memory_id=None, path=None, reason="injected_memory")

    monkeypatch.setitem(sys.modules, "app.core.server_state", types.SimpleNamespace(get_memory_instance=FakeService))

    report = save_turn_memory(
        {"user_id": "user-1", "message": "hello", "run_id": "run-1"},
        "answer",
        [],
        memory_injected=True,
    )

    assert captured["injected_memory"] is True
    assert report.action == "skipped"
    assert report.reason == "injected_memory"
