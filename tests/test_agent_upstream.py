"""Tests for the remote executor's tool event parser."""

import json
import os
import sys

import pytest

_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import app.agent.loop as agent_loop  # noqa: E402
from app.agent.events import AgentCompleted, AgentToolCall, AgentToolResult  # noqa: E402


def _parse(event_name, payload):
    return agent_loop._handle_upstream_event(event_name, json.dumps(payload))


def test_tool_call_is_parsed_into_an_event():
    event = _parse(
        "tool.call",
        {
            "tool_call_id": "call_1",
            "name": "Read",
            "iteration": 2,
            "arguments": {"path": "README.md"},
            "raw_arguments": '{"path": "README.md"}',
        },
    )

    assert isinstance(event, AgentToolCall)
    assert event.tool_call_id == "call_1"
    assert event.name == "Read"
    assert event.iteration == 2
    assert event.arguments == {"path": "README.md"}
    assert event.raw_arguments == '{"path": "README.md"}'
    assert event.arguments_error is None


def test_tool_call_accepts_json_string_arguments_and_snake_case_alias():
    event = _parse(
        "tool_call",
        {
            "tool_call_id": "call_1",
            "name": "Read",
            "arguments": '{"path": "README.md"}',
        },
    )

    assert isinstance(event, AgentToolCall)
    assert event.arguments == {"path": "README.md"}


def test_tool_call_with_missing_fields_is_ignored():
    assert _parse("tool.call", {"name": "Read"}) is None
    assert _parse("tool.call", {"tool_call_id": "call_1"}) is None


def test_invalid_tool_json_raises_a_protocol_error():
    with pytest.raises(agent_loop.AgentLoopError) as excinfo:
        agent_loop._handle_upstream_event("tool.call", "{not json")

    assert excinfo.value.code == "agent_protocol_error"


def test_tool_result_is_parsed_into_an_event():
    event = _parse(
        "tool.result",
        {
            "tool_call_id": "call_1",
            "name": "Read",
            "iteration": 1,
            "status": "completed",
            "content": "hello",
            "is_error": False,
            "duration_ms": 42,
        },
    )

    assert isinstance(event, AgentToolResult)
    assert event.tool_call_id == "call_1"
    assert event.content == "hello"
    assert event.is_error is False
    assert event.status == "completed"
    assert event.duration_ms == 42


def test_tool_result_serializes_non_string_content_and_derives_status():
    event = _parse(
        "tool_result",
        {
            "tool_call_id": "call_1",
            "name": "Read",
            "content": {"value": 1},
            "is_error": True,
        },
    )

    assert isinstance(event, AgentToolResult)
    assert json.loads(event.content) == {"value": 1}
    assert event.status == "failed"
    assert event.is_error is True


def test_unknown_events_are_still_ignored():
    assert _parse("model.thinking", {"text": "..."}) is None


def test_message_completed_passes_context_from_top_level():
    context = {"used_tokens": 1234, "window_tokens": 131_072, "budget_tokens": 32_000}
    event = _parse(
        "message.completed",
        {"finish_reason": "stop", "run_id": "run-1", "context": context},
    )

    assert isinstance(event, AgentCompleted)
    assert event.metadata["context"] == context


def test_message_completed_passes_context_from_nested_metadata():
    context = {"used_tokens": 1234, "window_tokens": 131_072, "budget_tokens": 32_000}
    event = _parse(
        "message.completed",
        {"metadata": {"finish_reason": "stop", "context": context}},
    )

    assert isinstance(event, AgentCompleted)
    assert event.metadata["context"] == context
