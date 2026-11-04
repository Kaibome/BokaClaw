from __future__ import annotations

from typing import Any

from conftest import FakeProvider

from bokaclaw_agent.config import AgentConfig
from bokaclaw_agent.runtime import AgentRuntime
from bokaclaw_agent.sessions.memory import InMemorySessionStore
from bokaclaw_agent.tools import tool
from bokaclaw_agent.types import EventType, ModelResponse, ModelStreamChunk, ToolCallDelta


class StreamingProvider:
    """Scripted streaming provider; every call consumes one scripted stream."""

    def __init__(self, streams: list[list[ModelStreamChunk]]) -> None:
        self.streams = [list(stream) for stream in streams]
        self.calls: list[dict[str, Any]] = []

    def stream_complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = "auto",
        **kwargs: Any,
    ):
        self.calls.append(
            {
                "messages": [dict(message) for message in messages],
                "tools": tools,
                "tool_choice": tool_choice,
                "kwargs": kwargs,
            }
        )
        if not self.streams:
            raise AssertionError("StreamingProvider has no scripted streams left")
        yield from self.streams.pop(0)

    def complete(self, *args: Any, **kwargs: Any) -> ModelResponse:
        raise AssertionError("stream() must not call complete() when stream_complete exists")


@tool
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


def _collect(generator):
    deltas = []
    while True:
        try:
            deltas.append(next(generator))
        except StopIteration as finished:
            return deltas, finished.value


def test_stream_yields_text_deltas_and_returns_result():
    provider = StreamingProvider(
        [
            [
                ModelStreamChunk(content="Hel"),
                ModelStreamChunk(content="lo", usage={"total_tokens": 3}),
                ModelStreamChunk(finish_reason="stop"),
            ]
        ]
    )
    store = InMemorySessionStore()
    runtime = AgentRuntime(AgentConfig(), provider=provider, store=store)

    deltas, result = _collect(runtime.stream("hi", session_id="s1"))

    assert deltas == ["Hel", "lo"]
    assert result.content == "Hello"
    assert result.stop_reason == "final"
    assert result.usage == {"total_tokens": 3}
    assert store.load_messages("s1") == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "Hello"},
    ]


def test_stream_reassembles_tool_call_fragments_and_streams_final_answer():
    provider = StreamingProvider(
        [
            [
                ModelStreamChunk(
                    tool_calls=[ToolCallDelta(index=0, id="call_1", name="ad", arguments='{"a": 1')],
                ),
                ModelStreamChunk(
                    tool_calls=[ToolCallDelta(index=0, name="d", arguments=', "b": 2}')],
                    finish_reason="tool_calls",
                ),
            ],
            [
                ModelStreamChunk(content="3"),
                ModelStreamChunk(finish_reason="stop"),
            ],
        ]
    )
    store = InMemorySessionStore()
    runtime = AgentRuntime(AgentConfig(), provider=provider, tools=[add], store=store)

    deltas, result = _collect(runtime.stream("1 + 2?", session_id="s1"))

    assert deltas == ["3"]
    assert result.content == "3"
    assert result.iterations == 2
    assert result.tool_results[0].tool_call_id == "call_1"
    assert result.tool_results[0].content == "3"

    second_request = provider.calls[1]["messages"]
    assert second_request[-2]["tool_calls"][0]["id"] == "call_1"
    assert second_request[-2]["tool_calls"][0]["function"]["arguments"] == '{"a": 1, "b": 2}'
    assert second_request[-1] == {"role": "tool", "tool_call_id": "call_1", "content": "3"}


def test_stream_emits_tool_events_alongside_the_final_text():
    events = []
    provider = StreamingProvider(
        [
            [
                ModelStreamChunk(
                    tool_calls=[ToolCallDelta(index=0, id="call_1", name="add", arguments='{"a": 1, "b": 2}')],
                    finish_reason="tool_calls",
                ),
            ],
            [
                ModelStreamChunk(content="3"),
                ModelStreamChunk(finish_reason="stop"),
            ],
        ]
    )
    runtime = AgentRuntime(
        AgentConfig(),
        provider=provider,
        tools=[add],
        store=InMemorySessionStore(),
        on_event=events.append,
    )

    deltas, result = _collect(runtime.stream("1 + 2?", session_id="s1"))

    assert deltas == ["3"]
    assert result.content == "3"
    tool_events = [event for event in events if event.type in {EventType.TOOL_CALL, EventType.TOOL_RESULT}]
    assert [event.type for event in tool_events] == [EventType.TOOL_CALL, EventType.TOOL_RESULT]
    assert tool_events[0].data["arguments"] == {"a": 1, "b": 2}
    assert tool_events[0].data["status"] == "running"
    assert tool_events[1].data["status"] == "completed"
    assert tool_events[1].data["content"] == "3"
    model_response_events = [event for event in events if event.type == EventType.MODEL_RESPONSE]
    assert [event.data["content"] for event in model_response_events] == [None, "3"]


def test_stream_falls_back_to_a_single_chunk_for_non_streaming_providers():
    provider = FakeProvider([ModelResponse(content="hello", finish_reason="stop")])
    store = InMemorySessionStore()
    runtime = AgentRuntime(AgentConfig(), provider=provider, store=store)

    deltas, result = _collect(runtime.stream("hi", session_id="s1"))

    assert deltas == ["hello"]
    assert result.content == "hello"
    assert store.load_messages("s1")[-1] == {"role": "assistant", "content": "hello"}


def test_closing_stream_early_does_not_persist():
    provider = StreamingProvider(
        [
            [
                ModelStreamChunk(content="Hel"),
                ModelStreamChunk(content="lo", finish_reason="stop"),
            ]
        ]
    )
    store = InMemorySessionStore()
    runtime = AgentRuntime(AgentConfig(), provider=provider, store=store)

    turn = runtime.stream("hi", session_id="s1")
    assert next(turn) == "Hel"
    turn.close()

    assert store.get_session("s1") is None
    assert store.load_messages("s1") == []


def test_stream_without_text_returns_empty_content():
    provider = StreamingProvider([[ModelStreamChunk(finish_reason="stop")]])
    runtime = AgentRuntime(AgentConfig(), provider=provider)

    deltas, result = _collect(runtime.stream("hi", session_id="s1"))

    assert deltas == []
    assert result.content is None
