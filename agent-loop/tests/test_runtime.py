from __future__ import annotations

import json

import pytest
from conftest import FakeProvider

from bokaclaw_agent.config import AgentConfig
from bokaclaw_agent.exceptions import MaxIterationsExceeded, ModelError, ToolExecutionError
from bokaclaw_agent.runtime import AgentRuntime
from bokaclaw_agent.sessions.memory import InMemorySessionStore
from bokaclaw_agent.tools import tool
from bokaclaw_agent.types import EventType, ModelResponse, ToolCall


@tool
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@tool
def boom() -> str:
    """Always raise."""
    raise ValueError("kaboom")


def test_run_without_tools_returns_final_answer():
    provider = FakeProvider([ModelResponse(content="hello", finish_reason="stop")])
    runtime = AgentRuntime(AgentConfig(), provider=provider, store=InMemorySessionStore())

    result = runtime.run("hi", session_id="s1")

    assert result.session_id == "s1"
    assert result.content == "hello"
    assert result.stop_reason == "final"
    assert result.iterations == 1
    assert result.new_messages == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    assert provider.calls[0]["messages"] == [{"role": "user", "content": "hi"}]


def test_tool_call_round_trip_preserves_ids_and_raw_arguments():
    provider = FakeProvider(
        [
            ModelResponse(
                content=None,
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="add",
                        arguments={"a": 1, "b": 2},
                        raw_arguments='{"a": 1, "b": 2}',
                    )
                ],
            ),
            ModelResponse(content="The sum is 3", finish_reason="stop"),
        ]
    )
    runtime = AgentRuntime(AgentConfig(), provider=provider, tools=[add], store=InMemorySessionStore())

    result = runtime.run("what is 1 + 2?", session_id="s1")

    assert result.content == "The sum is 3"
    assert result.iterations == 2
    assert result.tool_results[0].tool_call_id == "call_1"
    assert result.tool_results[0].content == "3"

    second_request = provider.calls[1]["messages"]
    assert second_request[-2]["role"] == "assistant"
    assert second_request[-2]["content"] is None
    assert second_request[-2]["tool_calls"][0]["id"] == "call_1"
    assert second_request[-2]["tool_calls"][0]["function"]["arguments"] == '{"a": 1, "b": 2}'
    assert second_request[-1] == {"role": "tool", "tool_call_id": "call_1", "content": "3"}


def test_multiple_tool_calls_execute_in_order():
    calls = [
        ToolCall(id="call_1", name="add", arguments={"a": 1, "b": 1}),
        ToolCall(id="call_2", name="add", arguments={"a": 2, "b": 2}),
    ]
    provider = FakeProvider(
        [
            ModelResponse(tool_calls=calls, finish_reason="tool_calls"),
            ModelResponse(content="done", finish_reason="stop"),
        ]
    )
    runtime = AgentRuntime(AgentConfig(), provider=provider, tools=[add], store=InMemorySessionStore())

    result = runtime.run("add twice", session_id="s1")

    assert [item.tool_call_id for item in result.tool_results] == ["call_1", "call_2"]
    tool_messages = [message for message in provider.calls[1]["messages"] if message["role"] == "tool"]
    assert [message["tool_call_id"] for message in tool_messages] == ["call_1", "call_2"]
    assert [message["content"] for message in tool_messages] == ["2", "4"]


def test_tool_error_is_returned_to_the_model_by_default():
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=[ToolCall(id="call_1", name="boom", arguments={})],
                finish_reason="tool_calls",
            ),
            ModelResponse(content="recovered", finish_reason="stop"),
        ]
    )
    runtime = AgentRuntime(AgentConfig(), provider=provider, tools=[boom], store=InMemorySessionStore())

    result = runtime.run("go", session_id="s1")

    assert result.content == "recovered"
    assert result.tool_results[0].is_error is True
    tool_message = provider.calls[1]["messages"][-1]
    assert tool_message["role"] == "tool"
    assert "kaboom" in json.loads(tool_message["content"])["error"]


def test_max_iterations_persists_failed_session_and_raises():
    responses = [
        ModelResponse(
            tool_calls=[ToolCall(id=f"call_{index}", name="add", arguments={"a": index, "b": 1})],
            finish_reason="tool_calls",
        )
        for index in range(3)
    ]
    store = InMemorySessionStore()
    runtime = AgentRuntime(AgentConfig(max_iterations=2), provider=FakeProvider(responses), tools=[add], store=store)

    with pytest.raises(MaxIterationsExceeded) as exc_info:
        runtime.run("loop forever", session_id="s1")

    assert exc_info.value.result is not None
    assert exc_info.value.result.stop_reason == "max_iterations"
    assert store.get_session("s1").status == "failed"
    assert len(store.load_messages("s1")) == 5  # user + 2 * (assistant + tool)


def test_provider_error_persists_failed_session_and_reraises():
    store = InMemorySessionStore()
    runtime = AgentRuntime(AgentConfig(), provider=FakeProvider([RuntimeError("offline")]), store=store)

    with pytest.raises(ModelError, match="offline"):
        runtime.run("hi", session_id="s1")

    record = store.get_session("s1")
    assert record is not None
    assert record.status == "failed"
    assert "ModelError" in record.error
    assert store.load_messages("s1") == [{"role": "user", "content": "hi"}]


def test_raise_tool_mode_appends_synthetic_tool_message_before_persisting():
    store = InMemorySessionStore()
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=[ToolCall(id="call_1", name="boom", arguments={})],
                finish_reason="tool_calls",
            )
        ]
    )
    runtime = AgentRuntime(
        AgentConfig(tool_error_mode="raise"),
        provider=provider,
        tools=[boom],
        store=store,
    )

    with pytest.raises(ToolExecutionError):
        runtime.run("go", session_id="s1")

    messages = store.load_messages("s1")
    assert messages[-1]["role"] == "tool"
    assert messages[-1]["tool_call_id"] == "call_1"
    assert "run aborted" in messages[-1]["content"]
    assert store.get_session("s1").status == "failed"


def test_session_resume_loads_history_and_appends_new_turn():
    provider = FakeProvider(
        [
            ModelResponse(content="first answer", finish_reason="stop"),
            ModelResponse(content="second answer", finish_reason="stop"),
        ]
    )
    runtime = AgentRuntime(AgentConfig(), provider=provider, store=InMemorySessionStore())

    runtime.run("hello", session_id="s1", user_id="user-1", metadata={"source": "test"})
    result = runtime.run("again", session_id="s1")

    assert result.content == "second answer"
    assert provider.calls[1]["messages"] == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "again"},
    ]
    session = runtime.get_session("s1")
    assert session is not None
    assert session.user_id == "user-1"
    assert session.metadata == {"source": "test"}
    assert session.title == "hello"


def test_system_prompt_is_sent_but_not_persisted():
    provider = FakeProvider([ModelResponse(content="ok", finish_reason="stop")])
    store = InMemorySessionStore()
    runtime = AgentRuntime(
        AgentConfig(system_prompt="You are terse."),
        provider=provider,
        store=store,
    )

    runtime.run("hi", session_id="s1")

    assert provider.calls[0]["messages"][0] == {"role": "system", "content": "You are terse."}
    assert store.load_messages("s1") == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "ok"},
    ]


def test_events_are_emitted_in_order():
    events = []
    provider = FakeProvider([ModelResponse(content="ok", finish_reason="stop")])
    runtime = AgentRuntime(AgentConfig(), provider=provider, on_event=events.append)

    runtime.run("hi", session_id="s1")

    assert [event.type for event in events] == [
        EventType.RUN_START,
        EventType.MODEL_REQUEST,
        EventType.MODEL_RESPONSE,
        EventType.RUN_END,
    ]
    assert all(event.session_id == "s1" for event in events)


def test_event_seq_starts_at_one_and_restarts_for_each_run():
    events = []
    provider = FakeProvider(
        [
            ModelResponse(content="first", finish_reason="stop"),
            ModelResponse(content="second", finish_reason="stop"),
        ]
    )
    runtime = AgentRuntime(AgentConfig(), provider=provider, on_event=events.append)

    runtime.run("hello", session_id="s1")
    first_run_count = len(events)
    runtime.run("again", session_id="s1")

    first_run, second_run = events[:first_run_count], events[first_run_count:]
    assert [event.seq for event in first_run] == list(range(1, len(first_run) + 1))
    assert first_run[0].type == EventType.RUN_START
    assert first_run[-1].type == EventType.RUN_END
    assert [event.seq for event in second_run] == list(range(1, len(second_run) + 1))
    assert second_run[0].type == EventType.RUN_START
    assert second_run[-1].type == EventType.RUN_END


def test_event_seq_is_monotonic_across_tool_calls_within_a_run():
    events = []
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(id="call_1", name="add", arguments={"a": 1, "b": 1}),
                    ToolCall(id="call_2", name="add", arguments={"a": 2, "b": 2}),
                ],
                finish_reason="tool_calls",
            ),
            ModelResponse(content="done", finish_reason="stop"),
        ]
    )
    runtime = AgentRuntime(AgentConfig(), provider=provider, tools=[add], on_event=events.append)

    runtime.run("add twice", session_id="s1")

    assert [event.seq for event in events] == list(range(1, len(events) + 1))
    assert events[0].type == EventType.RUN_START
    assert events[-1].type == EventType.RUN_END


def test_tool_events_carry_lifecycle_metadata():
    events = []
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="add",
                        arguments={"a": 1, "b": 2},
                        raw_arguments='{"a": 1, "b": 2}',
                    )
                ],
                finish_reason="tool_calls",
            ),
            ModelResponse(content="3", finish_reason="stop"),
        ]
    )
    runtime = AgentRuntime(
        AgentConfig(),
        provider=provider,
        tools=[add],
        store=InMemorySessionStore(),
        on_event=events.append,
    )

    runtime.run("1 + 2?", session_id="s1")

    tool_events = [event for event in events if event.type in {EventType.TOOL_CALL, EventType.TOOL_RESULT}]
    assert [event.type for event in tool_events] == [EventType.TOOL_CALL, EventType.TOOL_RESULT]

    call, result = tool_events
    assert call.iteration == 1
    assert call.data == {
        "tool_call_id": "call_1",
        "name": "add",
        "arguments": {"a": 1, "b": 2},
        "raw_arguments": '{"a": 1, "b": 2}',
        "arguments_error": None,
        "status": "running",
    }
    assert result.iteration == 1
    assert result.data["tool_call_id"] == "call_1"
    assert result.data["content"] == "3"
    assert result.data["is_error"] is False
    assert result.data["status"] == "completed"


def test_tool_failure_emits_an_aborted_result_event():
    events = []
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=[ToolCall(id="call_1", name="boom", arguments={})],
                finish_reason="tool_calls",
            )
        ]
    )
    runtime = AgentRuntime(
        AgentConfig(tool_error_mode="raise"),
        provider=provider,
        tools=[boom],
        store=InMemorySessionStore(),
        on_event=events.append,
    )

    with pytest.raises(ToolExecutionError):
        runtime.run("go", session_id="s1")

    tool_events = [event for event in events if event.type in {EventType.TOOL_CALL, EventType.TOOL_RESULT}]
    assert [event.type for event in tool_events] == [EventType.TOOL_CALL, EventType.TOOL_RESULT]
    assert tool_events[0].data["status"] == "running"
    assert tool_events[1].data["tool_call_id"] == "call_1"
    assert tool_events[1].data["is_error"] is True
    assert tool_events[1].data["status"] == "aborted"
    assert "run aborted" in tool_events[1].data["content"]


def test_event_callback_errors_do_not_break_the_run():
    def failing_callback(_event):
        raise RuntimeError("observer failed")

    runtime = AgentRuntime(
        AgentConfig(),
        provider=FakeProvider([ModelResponse(content="ok", finish_reason="stop")]),
        on_event=failing_callback,
    )

    result = runtime.run("hi")
    assert result.content == "ok"


def test_register_tool_after_construction():
    runtime = AgentRuntime(
        AgentConfig(),
        provider=FakeProvider(
            [
                ModelResponse(
                    tool_calls=[ToolCall(id="call_1", name="add", arguments={"a": 3, "b": 4})],
                    finish_reason="tool_calls",
                ),
                ModelResponse(content="7", finish_reason="stop"),
            ]
        ),
    )
    runtime.register_tool(add)

    result = runtime.run("3 + 4")
    assert result.content == "7"


def test_no_store_means_in_memory_only():
    runtime = AgentRuntime(
        AgentConfig(),
        provider=FakeProvider([ModelResponse(content="ok", finish_reason="stop")]),
    )

    result = runtime.run("hi", session_id="s1")

    assert result.session_id == "s1"
    assert runtime.get_session("s1") is None
    assert runtime.list_sessions() == []
