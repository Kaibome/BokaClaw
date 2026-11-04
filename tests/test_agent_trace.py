"""Tests for the per-turn tool trace used by the /agent router."""

import json
import os
import sys
from datetime import datetime, timezone

_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from app.agent.trace import ToolTrace  # noqa: E402

FIXED_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _trace(**kwargs):
    clock = kwargs.pop("clock", None)
    if clock is None:
        ticks = [0.0]

        def clock():
            return ticks[0]

        clock.ticks = ticks
    return ToolTrace(clock=clock, now=lambda: FIXED_NOW, **kwargs)


def test_start_and_finish_upsert_one_entry_with_duration():
    trace = _trace()
    started = trace.start(
        "call_1",
        "Read",
        iteration=1,
        arguments={"path": "README.md"},
        raw_arguments='{"path": "README.md"}',
    )
    assert started["status"] == "running"
    assert started["started_at"] == FIXED_NOW.isoformat()
    assert started["arguments"] == {"path": "README.md"}

    trace._clock.ticks[0] = 0.25
    finished = trace.finish("call_1", "Read", "hello")

    assert finished["status"] == "completed"
    assert finished["content"] == "hello"
    assert finished["duration_ms"] == 250
    assert finished["finished_at"] == FIXED_NOW.isoformat()
    assert len(trace.snapshot()) == 1


def test_result_without_a_call_creates_a_result_only_entry():
    trace = _trace()

    entry = trace.finish("call_1", "Read", "hello", duration_ms=12)

    assert entry["started_at"] is None
    assert entry["finished_at"] == FIXED_NOW.isoformat()
    assert entry["duration_ms"] == 12
    assert entry["status"] == "completed"
    assert trace.snapshot()[0]["tool_call_id"] == "call_1"


def test_duplicate_start_and_finish_do_not_regress_a_terminal_entry():
    trace = _trace()
    trace.start("call_1", "Read", arguments={"path": "a"})
    trace.finish("call_1", "Read", "first")

    trace.start("call_1", "Read", arguments={"path": "b"})
    duplicate = trace.finish("call_1", "Read", "second")

    assert duplicate["status"] == "completed"
    assert duplicate["content"] == "first"
    assert duplicate["arguments"] == {"path": "a"}


def test_abort_open_settles_running_entries_and_keeps_completed_ones():
    trace = _trace()
    trace.start("call_1", "Read", arguments={"path": "a"})
    trace.start("call_2", "Write", arguments={"path": "b"})
    trace.finish("call_1", "Read", "done")
    trace._clock.ticks[0] = 1.5

    aborted = trace.abort_open("run failed: timeout")

    assert [entry["tool_call_id"] for entry in aborted] == ["call_2"]
    assert aborted[0]["status"] == "aborted"
    assert aborted[0]["is_error"] is True
    assert aborted[0]["duration_ms"] == 1500
    assert json.loads(aborted[0]["content"]) == {"error": "run failed: timeout"}

    snapshot = trace.snapshot()
    assert [entry["status"] for entry in snapshot] == ["completed", "aborted"]


def test_oversized_arguments_fall_back_to_a_truncated_raw_preview():
    trace = _trace(max_argument_bytes=128)

    entry = trace.start("call_1", "Write", arguments={"text": "a" * 2000})

    assert entry["arguments"] is None
    assert entry["truncated"] is True
    assert "truncated" in entry["raw_arguments"]
    assert len(entry["raw_arguments"].encode("utf-8")) <= 128


def test_oversized_output_is_truncated_utf8_safely():
    trace = _trace(max_bytes=128)

    trace.start("call_1", "Read")
    entry = trace.finish("call_1", "Read", "汉字" * 500)

    assert entry["truncated"] is True
    assert "truncated" in entry["content"]
    assert len(entry["content"].encode("utf-8")) <= 128
    assert "汉字" in entry["content"]


def test_snapshot_preserves_first_seen_order_and_total_budget():
    trace = _trace(max_bytes=1_000, max_argument_bytes=64, max_total_bytes=100_000)
    trace.start("call_0", "Read", arguments={"path": "a"})
    trace.finish("call_0", "Read", "x" * 400)
    first_size = len(json.dumps(trace.snapshot()[0], ensure_ascii=False).encode())
    trace.max_total_bytes = first_size

    for index in (1, 2):
        trace.start(f"call_{index}", "Read", arguments={"path": "a"})
        trace.finish(f"call_{index}", "Read", "x" * 400)

    snapshot = trace.snapshot()

    assert [entry["tool_call_id"] for entry in snapshot] == ["call_0", "call_1", "call_2"]
    assert snapshot[0]["content"] == "x" * 400
    assert snapshot[1]["truncated"] is True
    assert snapshot[1]["content"] == ""
    assert snapshot[2]["content"] == ""
    assert snapshot[2]["truncated"] is True


def test_environment_budgets_are_applied_and_clamped(monkeypatch):
    monkeypatch.setenv("AGENT_TOOL_TRACE_MAX_BYTES", "1")
    monkeypatch.setenv("AGENT_TOOL_TRACE_MAX_ARGUMENT_BYTES", "1")
    monkeypatch.setenv("AGENT_TOOL_TRACE_MAX_TOTAL_BYTES", "1")

    trace = ToolTrace()

    assert trace.max_bytes == 128
    assert trace.max_argument_bytes == 64
    assert trace.max_total_bytes == 1_024


def test_explicit_budgets_override_the_environment(monkeypatch):
    monkeypatch.setenv("AGENT_TOOL_TRACE_MAX_BYTES", "999999")

    trace = ToolTrace(max_bytes=512)

    assert trace.max_bytes == 512
