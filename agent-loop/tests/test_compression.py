from __future__ import annotations

import pytest
from conftest import FakeProvider

from bokaclaw_agent.agent.compression import ContextCompressor
from bokaclaw_agent.agent.context import (
    TRUNCATION_MARKER,
    estimate_message_tokens,
    estimate_tokens,
    safe_start_index,
    truncate_text,
)
from bokaclaw_agent.agent.runtime import AgentRuntime
from bokaclaw_agent.core.config import AgentConfig
from bokaclaw_agent.core.types import EventType, ModelResponse, ToolCall
from bokaclaw_agent.tools import tool

# ---- pure helpers -------------------------------------------------------------


def test_estimate_tokens_ascii_and_non_ascii():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 2
    assert estimate_tokens("你好") == 2
    assert estimate_tokens("abcd你好") == 4


def test_estimate_message_tokens_includes_tool_calls():
    plain = {"role": "assistant", "content": "hello"}
    with_calls = {
        "role": "assistant",
        "content": "hello",
        "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "x", "arguments": "{}"}}],
    }
    assert estimate_message_tokens(with_calls) > estimate_message_tokens(plain)


def test_truncate_text_under_limit_is_unchanged():
    assert truncate_text("hello", 10) == ("hello", False)
    assert truncate_text("hello", None) == ("hello", False)


def test_truncate_text_keeps_head_tail_and_marker():
    text = "a" * 100
    capped, truncated = truncate_text(text, 40)
    assert truncated is True
    assert len(capped) == 40
    assert TRUNCATION_MARKER in capped
    assert capped.startswith("a")
    assert capped.endswith("a")


def test_truncate_text_is_codepoint_safe_for_cjk():
    text = "中文内容" * 40
    capped, truncated = truncate_text(text, 30)
    assert truncated is True
    assert len(capped) == 30
    capped.encode("utf-8")  # would raise on a broken surrogate


def test_truncate_text_smaller_than_marker():
    capped, truncated = truncate_text("x" * 10, 5)
    assert truncated is True
    assert capped == TRUNCATION_MARKER[:5]


def test_safe_start_index_skips_tool_messages():
    messages = [
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "x"},
        {"role": "user", "content": "next"},
    ]
    assert safe_start_index(messages, 1) == 2
    assert safe_start_index(messages, 1, allow_tool_call_blocks=True) == 2
    # A user turn wins over an earlier assistant block, even when blocks are allowed.
    assert safe_start_index(messages, 0) == 2


# ---- compressor ---------------------------------------------------------------


def _history() -> list[dict[str, object]]:
    messages: list[dict[str, object]] = [{"role": "system", "content": "sys"}]
    for index in range(6):
        messages.append({"role": "user", "content": f"u{index} " + "x" * 400})
        messages.append({"role": "assistant", "content": f"a{index} " + "y" * 400})
    return messages


def _compressor(**overrides):
    config = AgentConfig(**overrides)
    provider = FakeProvider(
        [
            ModelResponse(content="S1", usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}),
            ModelResponse(content="S2", usage={"total_tokens": 7}),
        ]
    )
    usage: dict[str, int] = {}
    return ContextCompressor(config, provider, usage), provider, usage


def test_compression_is_disabled_by_default():
    provider = FakeProvider([])
    compressor = ContextCompressor(AgentConfig(), provider, {})
    messages = _history()

    result = compressor.compress(messages)

    assert result.changed is False
    assert result.messages is messages
    assert provider.calls == []


def test_compression_noop_when_under_budget():
    compressor, provider, _ = _compressor(context_max_tokens=200_000)
    messages = _history()

    result = compressor.compress(messages)

    assert result.changed is False
    assert provider.calls == []


def test_truncate_mode_drops_old_messages_at_a_safe_boundary():
    compressor, provider, _ = _compressor(
        context_max_tokens=1600,
        context_keep_recent_tokens=400,
        context_compression="truncate",
    )

    result = compressor.compress(_history())

    assert result.changed is True
    assert result.stats["fallback"] is True
    assert result.stats["summarized"] is False
    assert result.stats["dropped_messages"] > 0
    assert result.messages[0]["role"] == "system"
    assert result.messages[0]["content"].startswith("sys")
    assert "earlier messages were omitted" in result.messages[0]["content"]
    assert result.messages[1]["role"] == "user"
    assert provider.calls == []


def test_summary_mode_calls_the_provider_without_tools():
    compressor, provider, usage = _compressor(
        context_max_tokens=1600,
        context_keep_recent_tokens=400,
    )

    result = compressor.compress(_history())

    assert result.stats["summarized"] is True
    assert result.stats["fallback"] is False
    assert result.stats["model_calls"] == 1
    assert result.stats["usage_unknown_calls"] == 0
    assert result.stats["usage"] == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    assert usage["total_tokens"] == 15
    assert len(provider.calls) == 1
    call = provider.calls[0]
    assert call["tools"] is None
    assert call["tool_choice"] is None
    assert call["kwargs"] == {"temperature": 0, "max_tokens": 1024}
    assert "S1" in result.messages[0]["content"]
    assert "## Summary of earlier conversation" in result.messages[0]["content"]


def test_summary_failure_falls_back_to_truncation():
    provider = FakeProvider([RuntimeError("summarizer down")])
    compressor = ContextCompressor(
        AgentConfig(context_max_tokens=1600, context_keep_recent_tokens=400),
        provider,
        {},
    )

    result = compressor.compress(_history())

    assert result.stats["fallback"] is True
    assert result.stats["summarized"] is False
    assert result.stats["model_calls"] == 1
    assert result.stats["usage_unknown_calls"] == 1
    assert "usage" not in result.stats
    assert "earlier messages were omitted" in result.messages[0]["content"]


def test_summary_rolls_forward_with_the_previous_summary():
    compressor, provider, _ = _compressor(
        context_max_tokens=1600,
        context_keep_recent_tokens=400,
    )
    messages = _history()
    compressor.compress(messages)

    expanded = [*messages, *[message for message in _history()[1:]]]
    compressor.compress(expanded)

    assert len(provider.calls) == 2
    second = provider.calls[1]["messages"]
    assert second[0]["role"] == "system"
    assert "Previous summary" in second[1]["content"]
    assert "S1" in second[1]["content"]
    assert "S2" in compressor._summary


def test_compression_pins_the_last_user_turn_in_a_single_turn_run():
    messages: list[dict[str, object]] = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "the task " + "q" * 100},
    ]
    for index in range(8):
        messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": f"c{index}", "type": "function", "function": {"name": "t"}}],
            }
        )
        messages.append({"role": "tool", "tool_call_id": f"c{index}", "content": "z" * 450})

    compressor, provider, _ = _compressor(
        context_max_tokens=1600,
        context_keep_recent_tokens=400,
    )
    result = compressor.compress(messages)

    assert result.stats["dropped_messages"] > 0
    assert len(provider.calls) == 1
    assert result.messages[1]["role"] == "user"
    assert "the task" in result.messages[1]["content"]
    assert all(message.get("role") != "tool" or index > 0 for index, message in enumerate(result.messages))


def test_compression_hard_fits_a_single_oversized_message():
    compressor, provider, _ = _compressor(
        context_max_tokens=1024,
        context_keep_recent_tokens=0,
        context_compression="truncate",
    )
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "x" * 20_000},
    ]

    result = compressor.compress(messages)

    assert provider.calls == []
    assert result.changed is True
    assert len(result.messages[1]["content"]) < 20_000
    assert result.stats["after_tokens"] <= 1200


def test_config_rejects_a_keep_recent_budget_that_fills_the_budget():
    with pytest.raises(ValueError):
        AgentConfig(context_max_tokens=1024, context_keep_recent_tokens=800)


# ---- runtime integration ------------------------------------------------------


@tool
def medium() -> str:
    """Return a medium result."""
    return "z" * 1500


def test_context_compressed_event_precedes_the_model_request():
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=[ToolCall(id="c1", name="medium", arguments={}, raw_arguments="{}")],
                finish_reason="tool_calls",
            ),
            ModelResponse(
                tool_calls=[ToolCall(id="c2", name="medium", arguments={}, raw_arguments="{}")],
                finish_reason="tool_calls",
            ),
            ModelResponse(content="S", usage={"total_tokens": 7}),
            ModelResponse(content="done", finish_reason="stop"),
        ]
    )
    events = []
    runtime = AgentRuntime(
        AgentConfig(context_max_tokens=1024, context_keep_recent_tokens=200),
        provider=provider,
        tools=[medium],
        on_event=events.append,
    )

    result = runtime.run("go", session_id="s1")

    compressed = [event for event in events if event.type == EventType.CONTEXT_COMPRESSED]
    assert len(compressed) == 1
    assert compressed[0].iteration == 3
    assert set(compressed[0].data) == {
        "before_tokens",
        "after_tokens",
        "dropped_messages",
        "summarized",
        "fallback",
        "usage",
        "model_calls",
        "usage_unknown_calls",
    }
    assert compressed[0].data["usage"] == {"total_tokens": 7}
    assert compressed[0].data["model_calls"] == 1
    assert compressed[0].data["usage_unknown_calls"] == 0

    types = [event.type for event in events]
    compressed_index = types.index(EventType.CONTEXT_COMPRESSED)
    next_request = types.index(EventType.MODEL_REQUEST, compressed_index + 1)
    assert compressed_index < next_request

    summary_call = provider.calls[2]
    assert summary_call["tools"] is None
    assert summary_call["tool_choice"] is None
    assert "S" in provider.calls[3]["messages"][0]["content"]
    assert result.usage["total_tokens"] >= 7


def test_summary_failure_does_not_fail_the_run():
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=[ToolCall(id="c1", name="medium", arguments={}, raw_arguments="{}")],
                finish_reason="tool_calls",
            ),
            ModelResponse(
                tool_calls=[ToolCall(id="c2", name="medium", arguments={}, raw_arguments="{}")],
                finish_reason="tool_calls",
            ),
            RuntimeError("summarizer down"),
            ModelResponse(content="done", finish_reason="stop"),
        ]
    )
    runtime = AgentRuntime(
        AgentConfig(context_max_tokens=1024, context_keep_recent_tokens=200),
        provider=provider,
        tools=[medium],
    )

    result = runtime.run("go", session_id="s1")

    assert result.content == "done"
    assert result.stop_reason == "final"


def test_compression_is_off_by_default_and_adds_no_calls_or_events():
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=[ToolCall(id="c1", name="medium", arguments={}, raw_arguments="{}")],
                finish_reason="tool_calls",
            ),
            ModelResponse(content="done", finish_reason="stop"),
        ]
    )
    events = []
    runtime = AgentRuntime(AgentConfig(), provider=provider, tools=[medium], on_event=events.append)

    runtime.run("go", session_id="s1")

    assert len(provider.calls) == 2
    assert all(event.type != EventType.CONTEXT_COMPRESSED for event in events)
