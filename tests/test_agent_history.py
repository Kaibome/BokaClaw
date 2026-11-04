"""Tests for rebuilding the model's history from persisted agent messages.

`build_history` must turn each run's stored tool trace back into OpenAI
wire-format tool calls and results, bounded so a long thread stays inside the
prompt budget.
"""

import os
import sys
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from app.agent.store import (  # noqa: E402
    DEFAULT_HISTORY_TOOL_ARGUMENT_MAX_CHARS,
    DEFAULT_HISTORY_TOOL_RESULT_MAX_CHARS,
    HISTORY_TRUNCATION_MARKER,
    build_history,
)


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _Session:
    """Stands in for the SQLAlchemy session; rows arrive newest first."""

    def __init__(self, rows):
        self.rows = rows

    def scalars(self, statement):
        return _Rows(self.rows)


def _message(role, content, *, status="completed", trace=None):
    metadata = {"tool_trace": trace} if trace is not None else {}
    return SimpleNamespace(
        id=uuid.uuid4(),
        role=role,
        content=content,
        status=status,
        meta=metadata,
        created_at=datetime.now(timezone.utc),
    )


def _entry(**overrides):
    entry = {
        "tool_call_id": "call_1",
        "name": "WebFetch",
        "iteration": 1,
        "status": "completed",
        "arguments": {"url": "https://example.com"},
        "raw_arguments": '{"url": "https://example.com"}',
        "arguments_error": None,
        "content": '{"url": "https://example.com", "content": "page"}',
        "is_error": False,
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:00:01+00:00",
        "duration_ms": 1000,
        "truncated": False,
    }
    entry.update(overrides)
    return entry


def _build(rows, **kwargs):
    return build_history(_Session(rows), uuid.uuid4(), limit=40, **kwargs)


def test_build_history_reconstructs_tool_calls_and_results():
    rows = [
        _message("assistant", "answer", trace=[_entry()]),
        _message("user", "compare qwen models"),
    ]

    history = _build(rows)

    assert history == [
        {"role": "user", "content": "compare qwen models"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "WebFetch", "arguments": '{"url": "https://example.com"}'},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": '{"url": "https://example.com", "content": "page"}',
        },
        {"role": "assistant", "content": "answer"},
    ]


def test_build_history_caps_tool_results_and_arguments():
    long_url = "https://example.com/" + "a" * 500
    rows = [
        _message(
            "assistant",
            "answer",
            trace=[
                _entry(
                    raw_arguments='{"url": "' + long_url + '"}',
                    arguments={"url": long_url},
                    content="x" * 500,
                )
            ],
        ),
    ]

    history = _build(rows, tool_result_max_chars=100, tool_argument_max_chars=60)

    tool_call = history[0]["tool_calls"][0]["function"]
    assert len(tool_call["arguments"]) <= 60
    assert HISTORY_TRUNCATION_MARKER.strip() in tool_call["arguments"]
    assert len(history[1]["content"]) <= 100
    assert HISTORY_TRUNCATION_MARKER.strip() in history[1]["content"]


def test_build_history_defaults_are_bounded():
    rows = [
        _message(
            "assistant",
            "answer",
            trace=[_entry(content="x" * (DEFAULT_HISTORY_TOOL_RESULT_MAX_CHARS + 100))],
        ),
    ]

    history = _build(rows)

    assert len(history[1]["content"]) == DEFAULT_HISTORY_TOOL_RESULT_MAX_CHARS
    assert len(history[0]["tool_calls"][0]["function"]["arguments"]) <= DEFAULT_HISTORY_TOOL_ARGUMENT_MAX_CHARS


def test_build_history_can_disable_tool_trace(monkeypatch):
    monkeypatch.setenv("AGENT_LOOP_HISTORY_TOOL_TRACE", "0")
    rows = [_message("assistant", "answer", trace=[_entry()])]

    history = _build(rows)

    assert history == [{"role": "assistant", "content": "answer"}]


def test_build_history_keeps_trace_from_a_failed_run_without_partial_text():
    rows = [_message("assistant", "partial answer", status="failed", trace=[_entry()])]

    history = _build(rows)

    assert [message["role"] for message in history] == ["assistant", "tool"]
    assert history[0]["content"] == ""


def test_build_history_skips_empty_replies_without_a_trace():
    rows = [
        _message("assistant", "answer"),
        _message("assistant", "", status="failed"),
        _message("user", "question"),
    ]

    history = _build(rows)

    assert history == [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ]


def test_build_history_drops_whole_blocks_to_stay_inside_the_budget():
    rows = [
        _message("assistant", "b" * 60),
        _message("user", "second"),
        _message("assistant", "first answer", trace=[_entry(content="p" * 200)]),
        _message("user", "first"),
    ]

    history = _build(rows, character_budget=90)

    assert history == [
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "b" * 60},
    ]


def test_build_history_does_not_emit_a_whole_trace_when_it_does_not_fit():
    rows = [
        _message("assistant", "", trace=[_entry(content="p" * 500)]),
        _message("user", "question"),
    ]

    history = _build(rows, character_budget=200)

    # The newest block is kept even when oversized, but it stays internally paired.
    roles = [message["role"] for message in history]
    assert roles == ["user", "assistant", "tool"]
    assert history[2]["tool_call_id"] == history[1]["tool_calls"][0]["id"]
