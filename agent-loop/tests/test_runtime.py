from __future__ import annotations

import json

import pytest
from conftest import FakeProvider

from bokaclaw_agent.agent.runtime import AgentRuntime
from bokaclaw_agent.core.config import AgentConfig
from bokaclaw_agent.core.exceptions import MaxIterationsExceeded, ModelError, NoProgressError, ToolExecutionError
from bokaclaw_agent.core.types import EventType, ModelResponse, ToolCall
from bokaclaw_agent.sessions.memory import InMemorySessionStore
from bokaclaw_agent.tools import tool


@tool
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@tool
def boom() -> str:
    """Always raise."""
    raise ValueError("kaboom")


@tool
def big() -> str:
    """Return a large result."""
    return "z" * 5000


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


def test_step_limit_final_answer_uses_one_tool_free_call():
    responses = [
        ModelResponse(
            tool_calls=[ToolCall(id=f"call_{index}", name="add", arguments={"a": index, "b": 1})],
            finish_reason="tool_calls",
        )
        for index in range(2)
    ]
    responses.append(ModelResponse(content="best effort answer", finish_reason="stop"))
    provider = FakeProvider(responses)
    store = InMemorySessionStore()
    runtime = AgentRuntime(AgentConfig(max_iterations=2), provider=provider, tools=[add], store=store)

    result = runtime.run("stuck", session_id="s1")

    assert result.content == "best effort answer"
    assert result.stop_reason == "max_iterations"
    assert result.iterations == 2
    assert store.get_session("s1").status == "completed"
    assert store.load_messages("s1")[-1] == {"role": "assistant", "content": "best effort answer"}

    final_call = provider.calls[-1]
    assert final_call["tools"] is None
    assert final_call["tool_choice"] is None
    assert final_call["messages"][-1]["role"] == "user"
    assert "maximum number of steps" in final_call["messages"][-1]["content"]
    # The notice is part of the request view only, never the conversation.
    assert all("maximum number of steps" not in str(message.get("content")) for message in store.load_messages("s1"))


def test_step_limit_without_a_final_answer_still_fails():
    responses = [
        ModelResponse(
            tool_calls=[ToolCall(id=f"call_{index}", name="add", arguments={"a": index, "b": 1})],
            finish_reason="tool_calls",
        )
        for index in range(2)
    ]
    responses.append(ModelResponse(content=None, finish_reason="stop"))
    store = InMemorySessionStore()
    runtime = AgentRuntime(
        AgentConfig(max_iterations=2),
        provider=FakeProvider(responses),
        tools=[add],
        store=store,
    )

    with pytest.raises(MaxIterationsExceeded) as exc_info:
        runtime.run("stuck", session_id="s1")

    assert exc_info.value.result.stop_reason == "max_iterations"
    assert store.get_session("s1").status == "failed"


def test_step_limit_final_answer_can_be_disabled():
    responses = [
        ModelResponse(
            tool_calls=[ToolCall(id=f"call_{index}", name="add", arguments={"a": index, "b": 1})],
            finish_reason="tool_calls",
        )
        for index in range(2)
    ]
    provider = FakeProvider(responses)
    runtime = AgentRuntime(
        AgentConfig(max_iterations=2, final_answer_on_max_iterations=False),
        provider=provider,
        tools=[add],
    )

    with pytest.raises(MaxIterationsExceeded):
        runtime.run("stuck", session_id="s1")

    assert len(provider.calls) == 2


def test_step_limit_drops_tool_calls_from_the_final_response():
    tool_call = ModelResponse(
        tool_calls=[ToolCall(id="call_1", name="add", arguments={"a": 1, "b": 2})],
        finish_reason="tool_calls",
    )
    provider = FakeProvider(
        [
            tool_call,
            tool_call,
            ModelResponse(
                content="answer anyway",
                finish_reason="stop",
                tool_calls=[ToolCall(id="call_x", name="add", arguments={"a": 3, "b": 4})],
            ),
        ]
    )
    store = InMemorySessionStore()
    runtime = AgentRuntime(AgentConfig(max_iterations=2), provider=provider, tools=[add], store=store)

    result = runtime.run("stuck", session_id="s1")

    assert result.content == "answer anyway"
    assert result.stop_reason == "max_iterations"
    assert "tool_calls" not in result.new_messages[-1]
    assert result.new_messages[-1] == {"role": "assistant", "content": "answer anyway"}


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


def test_tool_result_truncation_is_view_only():
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=[ToolCall(id="call_1", name="big", arguments={}, raw_arguments="{}")],
                finish_reason="tool_calls",
            ),
            ModelResponse(content="done", finish_reason="stop"),
        ]
    )
    store = InMemorySessionStore()
    events = []
    runtime = AgentRuntime(
        AgentConfig(tool_result_max_chars=1000),
        provider=provider,
        tools=[big],
        store=store,
        on_event=events.append,
    )

    result = runtime.run("go", session_id="s1")

    tool_event = next(event for event in events if event.type == EventType.TOOL_RESULT)
    assert tool_event.data["truncated"] is True
    assert tool_event.data["original_chars"] == 5000
    assert len(tool_event.data["content"]) == 1000

    sent_tool = next(message for message in provider.calls[1]["messages"] if message["role"] == "tool")
    assert len(sent_tool["content"]) == 1000

    assert result.new_messages[-2]["content"] == "z" * 5000
    assert result.tool_results[0].content == "z" * 5000
    stored_tool = next(message for message in store.load_messages("s1") if message["role"] == "tool")
    assert stored_tool["content"] == "z" * 5000


def test_tool_result_truncation_is_off_by_default():
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=[ToolCall(id="call_1", name="big", arguments={}, raw_arguments="{}")],
                finish_reason="tool_calls",
            ),
            ModelResponse(content="done", finish_reason="stop"),
        ]
    )
    events = []
    runtime = AgentRuntime(AgentConfig(), provider=provider, tools=[big], on_event=events.append)

    runtime.run("go", session_id="s1")

    tool_event = next(event for event in events if event.type == EventType.TOOL_RESULT)
    assert tool_event.data["truncated"] is False
    assert tool_event.data["content"] == "z" * 5000


def test_model_response_event_carries_usage_for_each_call():
    events = []
    provider = FakeProvider(
        [
            ModelResponse(
                content="ok",
                finish_reason="stop",
                usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            )
        ]
    )
    runtime = AgentRuntime(AgentConfig(), provider=provider, on_event=events.append)

    result = runtime.run("hi", session_id="s1")

    response = next(event for event in events if event.type == EventType.MODEL_RESPONSE)
    assert response.data["usage"] == {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}
    assert result.usage == {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}


def test_model_response_event_reports_empty_usage_when_unavailable():
    events = []
    provider = FakeProvider([ModelResponse(content="ok", finish_reason="stop")])
    runtime = AgentRuntime(AgentConfig(), provider=provider, on_event=events.append)

    runtime.run("hi", session_id="s1")

    response = next(event for event in events if event.type == EventType.MODEL_RESPONSE)
    assert response.data["usage"] == {}


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


# ---- fact ledger and step budget notice -----------------------------------------


def test_step_budget_notice_is_appended_to_the_view_only():
    provider = FakeProvider([ModelResponse(content="done", finish_reason="stop")])
    store = InMemorySessionStore()
    runtime = AgentRuntime(
        AgentConfig(max_iterations=5, step_budget_notice=True),
        provider=provider,
        store=store,
    )

    result = runtime.run("hi", session_id="s1")

    request = provider.calls[0]["messages"]
    assert request[:-1] == [{"role": "user", "content": "hi"}]
    assert request[-1]["role"] == "user"
    assert request[-1]["content"].startswith("[Runtime context, not a new user request]")
    assert "Step 1 of 5" in request[-1]["content"]
    # The notice never reaches the conversation or the store.
    assert result.new_messages == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "done"},
    ]
    assert store.load_messages("s1") == result.new_messages


def test_step_budget_notice_escalates_near_the_budget():
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=[ToolCall(id=f"call_{index}", name="add", arguments={"a": index, "b": 1})],
                finish_reason="tool_calls",
            )
            for index in range(2)
        ]
        + [ModelResponse(content="ok", finish_reason="stop")]
    )
    runtime = AgentRuntime(
        AgentConfig(max_iterations=3, step_budget_notice=True),
        provider=provider,
        tools=[add],
    )

    runtime.run("count", session_id="s1")

    assert "Step 1 of 3" in provider.calls[0]["messages"][-1]["content"]
    assert "the last step" in provider.calls[1]["messages"][-1]["content"]
    assert "the final step" in provider.calls[2]["messages"][-1]["content"]


def test_step_budget_notice_is_off_by_default():
    provider = FakeProvider([ModelResponse(content="done", finish_reason="stop")])
    runtime = AgentRuntime(AgentConfig(), provider=provider)

    runtime.run("hi", session_id="s1")

    assert provider.calls[0]["messages"] == [{"role": "user", "content": "hi"}]


def test_fact_ledger_lists_earlier_tool_results_in_the_next_request():
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=[ToolCall(id="call_1", name="add", arguments={"a": 1, "b": 2})],
                finish_reason="tool_calls",
            ),
            ModelResponse(content="3", finish_reason="stop"),
        ]
    )
    store = InMemorySessionStore()
    runtime = AgentRuntime(AgentConfig(fact_ledger=True), provider=provider, tools=[add], store=store)

    result = runtime.run("what is 1 + 2?", session_id="s1")

    second_request = provider.calls[1]["messages"]
    assert "## Research state (this run)" in second_request[-1]["content"]
    assert "add" in second_request[-1]["content"]
    assert "-> ok: 3" in second_request[-1]["content"]
    # The ledger is a view, not a persisted message.
    assert all("## Research state" not in str(message.get("content")) for message in store.load_messages("s1"))
    assert result.content == "3"


def test_fact_ledger_is_injected_after_context_compression():
    events = []
    provider = FakeProvider(
        [
            ModelResponse(tool_calls=[ToolCall(id="call_1", name="big")], finish_reason="tool_calls"),
            ModelResponse(tool_calls=[ToolCall(id="call_2", name="big")], finish_reason="tool_calls"),
            ModelResponse(content="done", finish_reason="stop"),
        ]
    )
    runtime = AgentRuntime(
        AgentConfig(fact_ledger=True, context_max_tokens=1024, context_compression="truncate"),
        provider=provider,
        tools=[big],
        on_event=events.append,
    )

    runtime.run("read it", session_id="s1")

    assert any(event.type == EventType.CONTEXT_COMPRESSED for event in events)
    compressed_request = provider.calls[2]["messages"]
    assert compressed_request[0]["role"] == "system"
    assert "earlier messages were omitted" in compressed_request[0]["content"]
    # The ledger is appended after compression, so it is never summarized away.
    assert compressed_request[-1]["content"].startswith("[Runtime context, not a new user request]")
    assert "## Research state (this run)" in compressed_request[-1]["content"]
    assert "(x2)" in compressed_request[-1]["content"]
    assert "-> ok:" in compressed_request[-1]["content"]


def test_fact_ledger_reaches_the_forced_final_answer():
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=[ToolCall(id="call_1", name="add", arguments={"a": 1, "b": 2})],
                finish_reason="tool_calls",
            ),
            ModelResponse(content="best effort", finish_reason="stop"),
        ]
    )
    runtime = AgentRuntime(
        AgentConfig(max_iterations=1, fact_ledger=True),
        provider=provider,
        tools=[add],
    )

    result = runtime.run("stuck", session_id="s1")

    assert result.stop_reason == "max_iterations"
    final_messages = provider.calls[1]["messages"]
    assert "## Research state (this run)" in final_messages[-1]["content"]
    assert "-> ok: 3" in final_messages[-1]["content"]
    assert "maximum number of steps" in final_messages[-1]["content"]


def test_progress_guard_nudges_then_forces_a_final_answer():
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=[ToolCall(id=f"call_{index}", name="add", arguments={"a": 1, "b": 2})],
                finish_reason="tool_calls",
            )
            for index in range(1, 6)
        ]
        + [ModelResponse(content="final from what I have", finish_reason="stop")]
    )
    store = InMemorySessionStore()
    runtime = AgentRuntime(
        AgentConfig(
            progress_guard=True,
            progress_nudge_after=2,
            progress_stop_after=4,
            max_iterations=8,
        ),
        provider=provider,
        tools=[add],
        store=store,
    )

    result = runtime.run("stuck", session_id="s1")

    assert result.stop_reason == "no_progress"
    assert result.content == "final from what I have"
    assert result.iterations == 5
    # The first call is new evidence; the nudge follows the second repeat and
    # stays in view while the model keeps repeating.
    assert "Progress check" not in provider.calls[2]["messages"][-1]["content"]
    assert "Progress check" in provider.calls[3]["messages"][-1]["content"]
    assert "Progress check" in provider.calls[4]["messages"][-1]["content"]
    # The forced answer runs without tools and carries the stop notice.
    assert provider.calls[5]["tools"] is None
    assert "No new information" in provider.calls[5]["messages"][-1]["content"]
    # Both notices are views, never persisted.
    assert all("Progress check" not in str(message.get("content")) for message in store.load_messages("s1"))
    assert all("No new information" not in str(message.get("content")) for message in store.load_messages("s1"))


def test_progress_guard_stays_quiet_while_new_evidence_arrives():
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=[ToolCall(id="call_1", name="add", arguments={"a": 1, "b": 2})],
                finish_reason="tool_calls",
            ),
            ModelResponse(
                tool_calls=[ToolCall(id="call_2", name="add", arguments={"a": 1, "b": 2})],
                finish_reason="tool_calls",
            ),
            ModelResponse(
                tool_calls=[ToolCall(id="call_3", name="add", arguments={"a": 2, "b": 3})],
                finish_reason="tool_calls",
            ),
            ModelResponse(content="5", finish_reason="stop"),
        ]
    )
    runtime = AgentRuntime(
        AgentConfig(progress_guard=True, progress_nudge_after=2, progress_stop_after=3),
        provider=provider,
        tools=[add],
    )

    result = runtime.run("add", session_id="s1")

    assert result.stop_reason == "final"
    assert all("Progress check" not in call["messages"][-1]["content"] for call in provider.calls)


def test_progress_guard_is_off_by_default():
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=[ToolCall(id="call_1", name="add", arguments={"a": 1, "b": 2})],
                finish_reason="tool_calls",
            ),
            ModelResponse(
                tool_calls=[ToolCall(id="call_2", name="add", arguments={"a": 1, "b": 2})],
                finish_reason="tool_calls",
            ),
            ModelResponse(
                tool_calls=[ToolCall(id="call_3", name="add", arguments={"a": 1, "b": 2})],
                finish_reason="tool_calls",
            ),
            ModelResponse(content="done", finish_reason="stop"),
        ]
    )
    runtime = AgentRuntime(AgentConfig(max_iterations=6), provider=provider, tools=[add])

    result = runtime.run("add", session_id="s1")

    assert result.stop_reason == "final"
    assert all("Progress check" not in call["messages"][-1]["content"] for call in provider.calls)


def test_progress_guard_raises_when_the_forced_answer_is_empty():
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=[ToolCall(id=f"call_{index}", name="add", arguments={"a": 1, "b": 2})],
                finish_reason="tool_calls",
            )
            for index in range(1, 5)
        ]
        + [ModelResponse(content=None, finish_reason="stop")]
    )
    runtime = AgentRuntime(
        AgentConfig(progress_guard=True, progress_nudge_after=2, progress_stop_after=3, max_iterations=8),
        provider=provider,
        tools=[add],
    )

    with pytest.raises(NoProgressError) as excinfo:
        runtime.run("stuck", session_id="s1")

    assert excinfo.value.result is not None
    assert excinfo.value.result.stop_reason == "no_progress"
