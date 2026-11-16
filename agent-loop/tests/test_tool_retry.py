from __future__ import annotations

import json
import threading

import pytest
from conftest import FakeProvider

from bokaclaw_agent import RetryPolicy, TransientToolError
from bokaclaw_agent.agent.runtime import AgentRuntime
from bokaclaw_agent.core.config import AgentConfig
from bokaclaw_agent.core.exceptions import ToolExecutionError
from bokaclaw_agent.core.types import ModelResponse, ToolCall
from bokaclaw_agent.sessions.memory import InMemorySessionStore
from bokaclaw_agent.tools import Tool, ToolRegistry, tool
from bokaclaw_agent.tools.retry import backoff_delay


def fast_policy(**overrides) -> RetryPolicy:
    values = {"max_attempts": 3, "base_delay": 0.0, "max_delay": 0.0, "max_elapsed": 5.0, "jitter": 0.0}
    values.update(overrides)
    return RetryPolicy(**values)


def _registry(*tools: Tool, policy: RetryPolicy | None = None) -> ToolRegistry:
    return ToolRegistry(tools, retry=policy if policy is not None else fast_policy())


def test_transient_failure_is_retried_then_succeeds():
    calls = {"count": 0}

    def flaky() -> str:
        calls["count"] += 1
        if calls["count"] < 3:
            raise TransientToolError("upstream is warming up")
        return "ok"

    tool_def = Tool.from_callable(flaky, name="Flaky", effect="external", retry=True)
    result = _registry(tool_def).invoke(ToolCall(id="c1", name="Flaky", arguments={}))

    assert not result.is_error
    assert result.content == "ok"
    assert calls["count"] == 3
    assert result.metadata["attempts"] == 3
    assert len(result.metadata["retry_errors"]) == 2
    assert result.metadata["retry_errors"][0]["type"] == "TransientToolError"


def test_permanent_failure_is_not_retried():
    calls = {"count": 0}

    def invalid() -> str:
        calls["count"] += 1
        raise ValueError("bad input")

    tool_def = Tool.from_callable(invalid, name="Invalid", effect="read")
    result = _registry(tool_def).invoke(ToolCall(id="c1", name="Invalid", arguments={}))

    assert result.is_error
    assert calls["count"] == 1
    assert result.metadata["attempts"] == 1
    assert "retry_errors" not in result.metadata


def test_read_tools_opt_in_automatically_but_writes_do_not():
    read_calls = {"count": 0}
    write_calls = {"count": 0}

    def flaky_read() -> str:
        read_calls["count"] += 1
        raise TransientToolError("transient")

    def flaky_write() -> str:
        write_calls["count"] += 1
        raise TransientToolError("transient")

    read_tool = Tool.from_callable(flaky_read, name="FlakyRead", effect="read")
    write_tool = Tool.from_callable(flaky_write, name="FlakyWrite", effect="write")
    registry = _registry(read_tool, write_tool)

    registry.invoke(ToolCall(id="c1", name="FlakyRead", arguments={}))
    registry.invoke(ToolCall(id="c2", name="FlakyWrite", arguments={}))

    assert read_calls["count"] == 3
    assert write_calls["count"] == 1


def test_explicit_policy_overrides_the_effect_default():
    calls = {"count": 0}

    def flaky_write() -> str:
        calls["count"] += 1
        if calls["count"] < 2:
            raise TransientToolError("transient")
        return "ok"

    tool_def = Tool.from_callable(flaky_write, name="RetryWrite", retry=fast_policy(max_attempts=2))
    result = _registry(tool_def).invoke(ToolCall(id="c1", name="RetryWrite", arguments={}))

    assert result.content == "ok"
    assert calls["count"] == 2


def test_explicit_false_disables_auto_retry_for_read_tools():
    calls = {"count": 0}

    def flaky_read() -> str:
        calls["count"] += 1
        raise TransientToolError("transient")

    tool_def = Tool.from_callable(flaky_read, name="NoRetryRead", effect="read", retry=False)
    result = _registry(tool_def).invoke(ToolCall(id="c1", name="NoRetryRead", arguments={}))

    assert result.is_error
    assert calls["count"] == 1


def test_retry_budget_exhaustion_returns_the_last_error():
    calls = {"count": 0}

    def always_transient() -> str:
        calls["count"] += 1
        raise TransientToolError("still down")

    tool_def = Tool.from_callable(always_transient, name="AlwaysDown", effect="read")
    result = _registry(tool_def, policy=fast_policy(max_attempts=2)).invoke(
        ToolCall(id="c1", name="AlwaysDown", arguments={})
    )

    assert result.is_error
    assert calls["count"] == 2
    assert result.metadata["attempts"] == 2
    assert result.metadata["retry_exhausted"] is True
    assert "still down" in json.loads(result.content)["error"]


def test_raise_mode_propagates_the_last_classified_error():
    def always_transient() -> str:
        raise TransientToolError("still down")

    tool_def = Tool.from_callable(always_transient, name="RaiseMode", effect="read")
    registry = _registry(tool_def, policy=fast_policy(max_attempts=2))

    with pytest.raises(ToolExecutionError) as info:
        registry.invoke(ToolCall(id="c1", name="RaiseMode", arguments={}), error_mode="raise")

    assert info.value.kind == "transient"
    assert "still down" in str(info.value)


def test_cancellation_interrupts_the_backoff_wait():
    cancel = threading.Event()

    def always_transient() -> str:
        raise TransientToolError("still down")

    tool_def = Tool.from_callable(always_transient, name="CancelDuringWait", effect="read")
    registry = _registry(tool_def, policy=fast_policy(base_delay=5.0, max_delay=5.0, max_elapsed=60.0))
    timer = threading.Timer(0.05, cancel.set)
    timer.start()
    try:
        result = registry.invoke(
            ToolCall(id="c1", name="CancelDuringWait", arguments={}),
            cancel_event=cancel,
        )
    finally:
        timer.cancel()

    assert result.is_error
    assert result.metadata == {"aborted": True}
    assert "cancelled" in result.content


def test_cancellation_before_retry_skips_further_attempts():
    cancel = threading.Event()
    cancel.set()
    calls = {"count": 0}

    def flaky() -> str:
        calls["count"] += 1
        raise TransientToolError("transient")

    tool_def = Tool.from_callable(flaky, name="PreCancelled", effect="read")
    result = _registry(tool_def).invoke(ToolCall(id="c1", name="PreCancelled", arguments={}), cancel_event=cancel)

    # The tool still runs once (the batch layer normally skips cancelled
    # calls); the retry check then stops instead of retrying.
    assert calls["count"] == 1
    assert result.is_error


def test_idempotency_key_is_stable_across_retries_and_scoped_to_the_run():
    from bokaclaw_agent.tools import CURRENT_IDEMPOTENCY_KEY

    seen: list[str] = []

    def flaky() -> str:
        seen.append(CURRENT_IDEMPOTENCY_KEY.get())
        if len(seen) < 2:
            raise TransientToolError("again")
        return "ok"

    tool_def = Tool.from_callable(flaky, name="KeyedTool", effect="external", retry=True)
    registry = _registry(tool_def)

    registry.begin_run("run-1")
    try:
        registry.invoke(ToolCall(id="call-9", name="KeyedTool", arguments={}))
    finally:
        registry.end_run()

    assert seen == ["run-1:call-9", "run-1:call-9"]

    registry.invoke(ToolCall(id="call-9", name="KeyedTool", arguments={}))
    assert seen[-1] == ""


def test_no_policy_means_a_single_attempt_and_no_metadata():
    calls = {"count": 0}

    def flaky() -> str:
        calls["count"] += 1
        raise TransientToolError("transient")

    tool_def = Tool.from_callable(flaky, name="NoPolicy", effect="read")
    result = ToolRegistry([tool_def]).invoke(ToolCall(id="c1", name="NoPolicy", arguments={}))

    assert calls["count"] == 1
    assert result.metadata == {}


def test_backoff_delay_doubles_and_caps():
    policy = RetryPolicy(max_attempts=5, base_delay=0.5, max_delay=2.0, jitter=0.0)

    assert backoff_delay(1, policy) == 0.5
    assert backoff_delay(2, policy) == 1.0
    assert backoff_delay(3, policy) == 2.0
    assert backoff_delay(5, policy) == 2.0


def test_backoff_delay_honors_retry_after_as_a_lower_bound():
    policy = RetryPolicy(base_delay=0.5, max_delay=2.0, jitter=0.0)

    assert backoff_delay(1, policy, retry_after=3.0) == 3.0
    assert backoff_delay(1, policy, retry_after=0.1) == 0.5


def test_backoff_jitter_only_reduces_the_delay():
    class LowerBound:
        def uniform(self, low: float, high: float) -> float:
            return low

    policy = RetryPolicy(base_delay=1.0, max_delay=8.0, jitter=0.4)

    assert backoff_delay(1, policy, rng=LowerBound()) == pytest.approx(0.6)


def test_retries_stay_one_tool_result_for_the_progress_guard():
    calls = {"count": 0}

    @tool(effect="read")
    def flaky_lookup() -> str:
        """Fail twice, then return fresh evidence."""
        calls["count"] += 1
        if calls["count"] < 3:
            raise TransientToolError("try again")
        return "fresh evidence"

    provider = FakeProvider(
        [
            ModelResponse(
                content="",
                tool_calls=[ToolCall(id="call_1", name="flaky_lookup", arguments={})],
                finish_reason="tool_calls",
            ),
            ModelResponse(content="done", finish_reason="stop"),
        ]
    )
    runtime = AgentRuntime(
        AgentConfig(
            max_iterations=3,
            progress_guard=True,
            progress_nudge_after=2,
            progress_stop_after=2,
            tool_retry_max_attempts=3,
            tool_retry_base_delay=0.0,
            tool_retry_max_delay=0.0,
            tool_retry_jitter=0.0,
        ),
        provider=provider,
        tools=[flaky_lookup],
        store=InMemorySessionStore(),
    )

    result = runtime.run("look it up", session_id="retry-progress")

    assert result.stop_reason == "final"
    assert calls["count"] == 3
    tool_messages = [message for message in result.new_messages if message.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "call_1"
