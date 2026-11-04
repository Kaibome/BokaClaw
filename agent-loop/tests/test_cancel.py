from __future__ import annotations

import json
import threading
from typing import Any

import pytest
from conftest import FakeProvider
from test_streaming import StreamingProvider

from bokaclaw_agent.config import AgentConfig
from bokaclaw_agent.exceptions import RunCancelled
from bokaclaw_agent.runtime import AgentRuntime
from bokaclaw_agent.sessions.memory import InMemorySessionStore
from bokaclaw_agent.tools import tool
from bokaclaw_agent.types import EventType, ModelResponse, ModelStreamChunk, ToolCall


def make_recording_tool(calls: list[str]):
    """Build a tool whose invocations are recorded in ``calls``."""

    @tool
    def record(value: str) -> str:
        """Record a value."""
        calls.append(value)
        return value

    return record


def assert_tool_calls_answered(messages: list[dict[str, Any]]) -> None:
    """Every assistant tool call must be followed by a matching tool message."""

    for index, message in enumerate(messages):
        if message["role"] != "assistant" or not message.get("tool_calls"):
            continue
        expected = [call["id"] for call in message["tool_calls"]]
        actual = []
        for following in messages[index + 1 :]:
            if following["role"] != "tool":
                break
            actual.append(following["tool_call_id"])
        assert actual == expected


def test_cancel_before_first_iteration_persists_a_cancelled_session():
    events = []
    store = InMemorySessionStore()
    cancel = threading.Event()
    cancel.set()
    provider = FakeProvider([ModelResponse(content="never requested", finish_reason="stop")])
    runtime = AgentRuntime(AgentConfig(), provider=provider, store=store, on_event=events.append)

    with pytest.raises(RunCancelled) as exc_info:
        runtime.run("hi", session_id="s1", cancel_event=cancel)

    cancelled = exc_info.value
    assert cancelled.session_id == "s1"
    assert cancelled.iteration == 1
    assert cancelled.result is not None
    assert cancelled.result.stop_reason == "cancelled"
    assert provider.calls == []
    assert store.load_messages("s1") == [{"role": "user", "content": "hi"}]
    record = store.get_session("s1")
    assert record is not None
    assert record.status == "cancelled"
    assert record.error.startswith("RunCancelled: ")

    assert events[0].type == EventType.RUN_START
    assert events[0].seq == 1
    assert [event.seq for event in events] == list(range(1, len(events) + 1))
    assert events[-1].type == EventType.RUN_END
    assert events[-1].data == {"stop_reason": "cancelled"}


def test_cancel_after_model_response_aborts_all_requested_tools():
    calls: list[str] = []
    record = make_recording_tool(calls)
    store = InMemorySessionStore()
    cancel = threading.Event()

    def on_event(event):
        if event.type == EventType.MODEL_RESPONSE:
            cancel.set()

    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=[ToolCall(id="call_1", name="record", arguments={"value": "a"})],
                finish_reason="tool_calls",
            )
        ]
    )
    runtime = AgentRuntime(AgentConfig(), provider=provider, tools=[record], store=store, on_event=on_event)

    with pytest.raises(RunCancelled) as exc_info:
        runtime.run("go", session_id="s1", cancel_event=cancel)

    assert calls == []
    result = exc_info.value.result
    assert result is not None
    assert result.stop_reason == "cancelled"
    assert [item.tool_call_id for item in result.tool_results] == ["call_1"]
    assert result.tool_results[0].is_error is True

    messages = store.load_messages("s1")
    assert [message["role"] for message in messages] == ["user", "assistant", "tool"]
    assert messages[1]["tool_calls"][0]["id"] == "call_1"
    assert messages[2]["tool_call_id"] == "call_1"
    assert "run aborted" in json.loads(messages[2]["content"])["error"]
    assert_tool_calls_answered(messages)
    assert store.get_session("s1").status == "cancelled"


def test_cancel_before_a_later_tool_call_keeps_earlier_result_and_aborts_the_rest():
    calls: list[str] = []
    record = make_recording_tool(calls)
    store = InMemorySessionStore()
    cancel = threading.Event()
    tool_events = []

    def on_event(event):
        if event.type in {EventType.TOOL_CALL, EventType.TOOL_RESULT}:
            tool_events.append(event)
        if event.type == EventType.TOOL_RESULT and event.data["tool_call_id"] == "call_1":
            cancel.set()

    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(id="call_1", name="record", arguments={"value": "first"}),
                    ToolCall(id="call_2", name="record", arguments={"value": "second"}),
                ],
                finish_reason="tool_calls",
            )
        ]
    )
    runtime = AgentRuntime(AgentConfig(), provider=provider, tools=[record], store=store, on_event=on_event)

    with pytest.raises(RunCancelled) as exc_info:
        runtime.run("go", session_id="s1", cancel_event=cancel)

    assert calls == ["first"]
    # The second model round was never requested.
    assert len(provider.calls) == 1

    result = exc_info.value.result
    assert result is not None
    assert result.stop_reason == "cancelled"
    assert [item.tool_call_id for item in result.tool_results] == ["call_1", "call_2"]
    assert result.tool_results[0].content == "first"
    assert result.tool_results[0].is_error is False
    assert result.tool_results[1].is_error is True

    messages = store.load_messages("s1")
    tool_messages = [message for message in messages if message["role"] == "tool"]
    assert [message["tool_call_id"] for message in tool_messages] == ["call_1", "call_2"]
    assert tool_messages[0]["content"] == "first"
    assert "run aborted" in json.loads(tool_messages[1]["content"])["error"]
    assert_tool_calls_answered(messages)

    statuses = [(event.data["tool_call_id"], event.data["status"]) for event in tool_events]
    assert statuses == [
        ("call_1", "running"),
        ("call_1", "completed"),
        ("call_2", "aborted"),
    ]
    assert store.get_session("s1").status == "cancelled"


def test_stream_cancel_waits_until_the_model_response_is_complete():
    provider = StreamingProvider(
        [
            [
                ModelStreamChunk(content="Hel"),
                ModelStreamChunk(content="lo"),
                ModelStreamChunk(finish_reason="stop"),
            ]
        ]
    )
    store = InMemorySessionStore()
    cancel = threading.Event()
    runtime = AgentRuntime(AgentConfig(), provider=provider, store=store)

    turn = runtime.stream("hi", session_id="s1", cancel_event=cancel)
    assert next(turn) == "Hel"
    cancel.set()

    # The remaining chunks are still consumed; cancellation is deferred until
    # the assistant message has been assembled.
    with pytest.raises(RunCancelled) as exc_info:
        while True:
            next(turn)

    result = exc_info.value.result
    assert result is not None
    assert result.content == "Hello"
    assert result.stop_reason == "cancelled"
    assert store.load_messages("s1") == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "Hello"},
    ]
    assert store.get_session("s1").status == "cancelled"


def test_run_without_cancel_event_preserves_the_existing_behavior():
    store = InMemorySessionStore()
    provider = FakeProvider([ModelResponse(content="ok", finish_reason="stop")])
    runtime = AgentRuntime(AgentConfig(), provider=provider, store=store)

    result = runtime.run("hi", session_id="s1", cancel_event=None)

    assert result.stop_reason == "final"
    assert result.content == "ok"
    assert store.get_session("s1").status == "completed"


@pytest.mark.asyncio
async def test_arun_forwards_cancel_event():
    cancel = threading.Event()
    cancel.set()
    runtime = AgentRuntime(
        AgentConfig(),
        provider=FakeProvider([ModelResponse(content="never requested", finish_reason="stop")]),
    )

    with pytest.raises(RunCancelled):
        await runtime.arun("hi", session_id="s1", cancel_event=cancel)
