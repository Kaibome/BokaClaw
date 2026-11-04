from __future__ import annotations

import pytest
from conftest import FakeProvider

from bokaclaw_agent.agent.runtime import AgentRuntime
from bokaclaw_agent.core.config import AgentConfig
from bokaclaw_agent.core.types import ModelResponse, ToolCall
from bokaclaw_agent.tools import tool


@tool
async def async_add(a: int, b: int) -> int:
    """Add two integers asynchronously."""
    return a + b


@pytest.mark.asyncio
async def test_arun_returns_final_answer():
    runtime = AgentRuntime(
        AgentConfig(),
        provider=FakeProvider([ModelResponse(content="async hello", finish_reason="stop")]),
    )

    result = await runtime.arun("hi", session_id="s1")

    assert result.content == "async hello"
    assert result.session_id == "s1"


@pytest.mark.asyncio
async def test_arun_supports_async_tools():
    runtime = AgentRuntime(
        AgentConfig(),
        provider=FakeProvider(
            [
                ModelResponse(
                    tool_calls=[ToolCall(id="call_1", name="async_add", arguments={"a": 2, "b": 5})],
                    finish_reason="tool_calls",
                ),
                ModelResponse(content="7", finish_reason="stop"),
            ]
        ),
        tools=[async_add],
    )

    result = await runtime.arun("2 + 5")

    assert result.content == "7"
    assert result.tool_results[0].content == "7"


def test_sync_run_supports_async_tools_outside_an_event_loop():
    runtime = AgentRuntime(
        AgentConfig(),
        provider=FakeProvider(
            [
                ModelResponse(
                    tool_calls=[ToolCall(id="call_1", name="async_add", arguments={"a": 1, "b": 2})],
                    finish_reason="tool_calls",
                ),
                ModelResponse(content="3", finish_reason="stop"),
            ]
        ),
        tools=[async_add],
    )

    result = runtime.run("1 + 2")

    assert result.tool_results[0].content == "3"


@pytest.mark.asyncio
async def test_aclose_is_safe_without_store():
    runtime = AgentRuntime(
        AgentConfig(),
        provider=FakeProvider([]),
    )

    await runtime.aclose()
