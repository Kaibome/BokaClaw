from __future__ import annotations

import json
import threading
import time

import pytest
from conftest import FakeProvider

from bokaclaw_agent.agent.runtime import AgentRuntime
from bokaclaw_agent.core.config import AgentConfig
from bokaclaw_agent.core.exceptions import RunCancelled, ToolExecutionError
from bokaclaw_agent.core.types import EventType, ModelResponse, ToolCall
from bokaclaw_agent.sessions.memory import InMemorySessionStore
from bokaclaw_agent.tools import Tool, ToolRegistry, tool


def _call(call_id: str, tool_name: str, **arguments: object) -> ToolCall:
    return ToolCall(
        id=call_id,
        name=tool_name,
        arguments=dict(arguments),
        raw_arguments=json.dumps(arguments, ensure_ascii=False),
    )


def test_default_tool_is_not_concurrency_safe():
    @tool
    def store(value: str) -> str:
        """Store a value."""
        return value

    assert store.effect == "write"
    assert store.concurrency_safe is False


def test_read_effect_marks_tool_safe_by_default():
    @tool(effect="read")
    def load(value: str) -> str:
        """Load a value."""
        return value

    assert load.concurrency_safe is True


def test_external_tool_can_opt_in():
    @tool(effect="external", concurrency_safe=True)
    def fetch(url: str) -> str:
        """Fetch a URL."""
        return url

    assert fetch.concurrency_safe is True


def test_external_tool_defaults_to_serial():
    @tool(effect="external")
    def fetch(url: str) -> str:
        """Fetch a URL."""
        return url

    assert fetch.concurrency_safe is False


def test_consecutive_safe_calls_run_in_parallel():
    barrier = threading.Barrier(2, timeout=3)

    def wait_for_peer(name: str) -> str:
        barrier.wait()
        return name

    registry = ToolRegistry(
        [
            Tool.from_callable(wait_for_peer, name="a", effect="read"),
            Tool.from_callable(wait_for_peer, name="b", effect="read"),
        ]
    )

    results = registry.invoke_many(
        [_call("call_1", "a", name="a"), _call("call_2", "b", name="b")],
        max_workers=2,
    )

    assert [result.content for result in results] == ["a", "b"]
    assert not any(result.is_error for result in results)


def test_unsafe_tool_is_a_barrier_between_batches():
    events: list[tuple[str, str]] = []
    lock = threading.Lock()

    def record(name: str, phase: str) -> None:
        with lock:
            events.append((name, phase))

    def read_tool(name: str) -> str:
        record(name, "enter")
        time.sleep(0.05)
        record(name, "exit")
        return name

    def write_tool(name: str) -> str:
        record(name, "enter")
        time.sleep(0.05)
        record(name, "exit")
        return name

    registry = ToolRegistry(
        [
            Tool.from_callable(read_tool, name="read_a", effect="read"),
            Tool.from_callable(read_tool, name="read_b", effect="read"),
            Tool.from_callable(write_tool, name="write_c"),
            Tool.from_callable(read_tool, name="read_d", effect="read"),
        ]
    )

    registry.invoke_many(
        [
            _call("1", "read_a", name="read_a"),
            _call("2", "read_b", name="read_b"),
            _call("3", "write_c", name="write_c"),
            _call("4", "read_d", name="read_d"),
        ],
        max_workers=2,
    )

    enters = [name for name, phase in events if phase == "enter"]
    write_enter = enters.index("write_c")
    assert enters.index("read_a") < write_enter
    assert enters.index("read_b") < write_enter
    assert enters.index("read_d") > write_enter

    # The write must finish before the next batch starts.
    write_exit = events.index(("write_c", "exit"))
    read_d_enter = events.index(("read_d", "enter"))
    assert write_exit < read_d_enter


def test_results_follow_declaration_order():
    def slow(name: str, delay: float) -> str:
        time.sleep(delay)
        return name

    registry = ToolRegistry(
        [
            Tool.from_callable(slow, name="slow", effect="read"),
            Tool.from_callable(slow, name="fast", effect="read"),
        ]
    )

    results = registry.invoke_many(
        [
            _call("call_slow", "slow", name="slow", delay=0.15),
            _call("call_fast", "fast", name="fast", delay=0.01),
        ],
        max_workers=2,
    )

    assert [result.tool_call_id for result in results] == ["call_slow", "call_fast"]
    assert [result.content for result in results] == ["slow", "fast"]


def test_cancelled_before_start_returns_aborted_results():
    calls: list[str] = []

    def read_tool(name: str) -> str:
        calls.append(name)
        return name

    registry = ToolRegistry([Tool.from_callable(read_tool, name="read", effect="read")])
    cancel_event = threading.Event()
    cancel_event.set()

    results = registry.invoke_many(
        [_call("1", "read", name="a"), _call("2", "read", name="b")],
        max_workers=2,
        cancel_event=cancel_event,
    )

    assert calls == []
    assert len(results) == 2
    assert all(result.is_error and result.metadata.get("aborted") for result in results)


def test_raise_mode_stays_serial():
    calls: list[str] = []

    def boom(name: str) -> str:
        calls.append(name)
        raise ValueError("kaboom")

    def after(name: str) -> str:
        calls.append(name)
        return name

    registry = ToolRegistry(
        [
            Tool.from_callable(boom, name="boom", effect="read"),
            Tool.from_callable(after, name="after", effect="read"),
        ]
    )

    with pytest.raises(ToolExecutionError):
        registry.invoke_many(
            [_call("1", "boom", name="a"), _call("2", "after", name="b")],
            error_mode="raise",
            max_workers=2,
        )

    assert calls == ["a"]


def test_unknown_tool_breaks_batch_and_returns_error():
    def read_tool(name: str) -> str:
        return name

    registry = ToolRegistry([Tool.from_callable(read_tool, name="read", effect="read")])

    results = registry.invoke_many(
        [_call("1", "read", name="a"), _call("2", "missing"), _call("3", "read", name="c")],
        max_workers=2,
    )

    assert len(results) == 3
    assert results[1].is_error
    assert not results[0].is_error
    assert not results[2].is_error


def test_runtime_parallelizes_safe_calls_and_keeps_message_order():
    barrier = threading.Barrier(2, timeout=3)

    def wait_for_peer(name: str) -> str:
        barrier.wait()
        return name

    tools = [
        Tool.from_callable(wait_for_peer, name="read_a", effect="read"),
        Tool.from_callable(wait_for_peer, name="read_b", effect="read"),
    ]
    provider = FakeProvider(
        [
            ModelResponse(
                content=None,
                finish_reason="tool_calls",
                tool_calls=[
                    _call("call_1", "read_a", name="a"),
                    _call("call_2", "read_b", name="b"),
                ],
            ),
            ModelResponse(content="done", finish_reason="stop"),
        ]
    )
    runtime = AgentRuntime(
        AgentConfig(tool_concurrency=2),
        provider=provider,
        tools=tools,
        store=InMemorySessionStore(),
    )

    result = runtime.run("go", session_id="s1")

    assert result.content == "done"
    assert [item.tool_call_id for item in result.tool_results] == ["call_1", "call_2"]
    assert not any(item.is_error for item in result.tool_results)
    tool_messages = [message for message in result.new_messages if message["role"] == "tool"]
    assert [message["tool_call_id"] for message in tool_messages] == ["call_1", "call_2"]


def test_default_concurrency_keeps_serial_event_order():
    def read_tool(name: str) -> str:
        return name

    tools = [Tool.from_callable(read_tool, name="read", effect="read")]
    provider = FakeProvider(
        [
            ModelResponse(
                content=None,
                finish_reason="tool_calls",
                tool_calls=[
                    _call("call_1", "read", name="a"),
                    _call("call_2", "read", name="b"),
                ],
            ),
            ModelResponse(content="done", finish_reason="stop"),
        ]
    )
    events: list[tuple[str, str]] = []

    def on_event(event):
        if event.type in {EventType.TOOL_CALL, EventType.TOOL_RESULT}:
            events.append((event.data["tool_call_id"], event.data["status"]))

    runtime = AgentRuntime(
        AgentConfig(),  # tool_concurrency defaults to 1
        provider=provider,
        tools=tools,
        store=InMemorySessionStore(),
        on_event=on_event,
    )

    runtime.run("go", session_id="s1")

    assert events == [
        ("call_1", "running"),
        ("call_1", "completed"),
        ("call_2", "running"),
        ("call_2", "completed"),
    ]


def test_runtime_cancellation_keeps_every_declared_call_paired():
    cancel_event = threading.Event()

    def cancels(name: str) -> str:
        cancel_event.set()
        return name

    def waits(name: str) -> str:
        time.sleep(0.5)
        return name

    tools = [
        Tool.from_callable(cancels, name="cancels", effect="read"),
        Tool.from_callable(waits, name="waits", effect="read"),
    ]
    provider = FakeProvider(
        [
            ModelResponse(
                content=None,
                finish_reason="tool_calls",
                tool_calls=[
                    _call("call_1", "cancels", name="a"),
                    _call("call_2", "waits", name="b"),
                ],
            )
        ]
    )
    runtime = AgentRuntime(
        AgentConfig(tool_concurrency=2, max_iterations=1),
        provider=provider,
        tools=tools,
        store=InMemorySessionStore(),
    )

    with pytest.raises(RunCancelled) as exc_info:
        runtime.run("go", session_id="s1", cancel_event=cancel_event)

    result = exc_info.value.result
    assert result is not None
    assert result.stop_reason == "cancelled"
    assert len(result.tool_results) == 2
    tool_messages = [message for message in result.new_messages if message["role"] == "tool"]
    assert [message["tool_call_id"] for message in tool_messages] == ["call_1", "call_2"]
