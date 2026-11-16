from __future__ import annotations

import time

import pytest

from bokaclaw_agent import RetryPolicy, TransientToolError
from bokaclaw_agent.core.exceptions import ToolExecutionError
from bokaclaw_agent.core.types import ToolCall
from bokaclaw_agent.tools import Tool, ToolRegistry
from bokaclaw_agent.tools.breaker import BreakerSettings, ToolCircuitBreaker, get_tool_breaker, reset_tool_breakers


@pytest.fixture(autouse=True)
def _clean_breakers():
    reset_tool_breakers()
    yield
    reset_tool_breakers()


def settings(**overrides) -> BreakerSettings:
    values = {"failure_threshold": 2, "cooldown_seconds": 0.02, "max_cooldown_seconds": 1.0}
    values.update(overrides)
    return BreakerSettings(**values)


def fast_policy() -> RetryPolicy:
    return RetryPolicy(max_attempts=3, base_delay=0.0, max_delay=0.0, max_elapsed=5.0, jitter=0.0)


def test_breaker_opens_after_threshold_and_fails_fast():
    calls = {"count": 0}

    def down() -> str:
        calls["count"] += 1
        raise TransientToolError("down")

    tool_def = Tool.from_callable(down, name="DownTool", effect="external", retry=False)
    registry = ToolRegistry([tool_def], breaker=settings(cooldown_seconds=5.0, max_cooldown_seconds=10.0))

    registry.invoke(ToolCall(id="c1", name="DownTool", arguments={}))
    registry.invoke(ToolCall(id="c2", name="DownTool", arguments={}))
    result = registry.invoke(ToolCall(id="c3", name="DownTool", arguments={}))

    assert calls["count"] == 2
    assert result.is_error
    assert result.metadata["breaker"] == "open"
    assert result.metadata["retry_after_s"] > 0
    assert "temporarily unavailable" in result.content


def test_permanent_errors_do_not_open_the_breaker():
    calls = {"count": 0}

    def invalid() -> str:
        calls["count"] += 1
        raise ValueError("bad input")

    tool_def = Tool.from_callable(invalid, name="InvalidTool", effect="external", retry=False)
    registry = ToolRegistry([tool_def], breaker=settings())

    for index in range(4):
        registry.invoke(ToolCall(id=f"c{index}", name="InvalidTool", arguments={}))

    assert calls["count"] == 4
    assert get_tool_breaker().state("InvalidTool") == "closed"


def test_one_logical_call_counts_once_even_with_retries():
    calls = {"count": 0}

    def always_transient() -> str:
        calls["count"] += 1
        raise TransientToolError("down")

    tool_def = Tool.from_callable(always_transient, name="RetriedTool", effect="read")
    registry = ToolRegistry([tool_def], retry=fast_policy(), breaker=settings(failure_threshold=2))

    registry.invoke(ToolCall(id="c1", name="RetriedTool", arguments={}))
    assert get_tool_breaker().state("RetriedTool") == "closed"
    assert calls["count"] == 3

    registry.invoke(ToolCall(id="c2", name="RetriedTool", arguments={}))
    assert get_tool_breaker().state("RetriedTool") == "open"
    assert calls["count"] == 6

    result = registry.invoke(ToolCall(id="c3", name="RetriedTool", arguments={}))
    assert calls["count"] == 6
    assert result.metadata["breaker"] == "open"


def test_half_open_probe_success_closes_the_breaker():
    state = {"down": True, "count": 0}

    def flaky() -> str:
        state["count"] += 1
        if state["down"]:
            raise TransientToolError("down")
        return "ok"

    tool_def = Tool.from_callable(flaky, name="ProbeTool", effect="external", retry=False)
    registry = ToolRegistry([tool_def], breaker=settings(failure_threshold=1, cooldown_seconds=0.02))

    registry.invoke(ToolCall(id="c1", name="ProbeTool", arguments={}))
    assert get_tool_breaker().state("ProbeTool") == "open"

    state["down"] = False
    time.sleep(0.03)
    result = registry.invoke(ToolCall(id="c2", name="ProbeTool", arguments={}))

    assert result.content == "ok"
    assert get_tool_breaker().state("ProbeTool") == "closed"


def test_failed_probe_reopens_and_doubles_the_cooldown():
    now = [0.0]
    breaker = ToolCircuitBreaker(clock=lambda: now[0])
    tuning = settings(failure_threshold=1, cooldown_seconds=10.0, max_cooldown_seconds=40.0)

    assert breaker.allow("tool", tuning) == (True, 0.0)
    breaker.record_failure("tool", tuning)
    assert breaker.state("tool") == "open"

    now[0] = 10.0
    assert breaker.allow("tool", tuning) == (True, 0.0)  # half-open probe admitted
    assert breaker.allow("tool", tuning) == (False, 0.0)  # only one probe at a time

    breaker.record_failure("tool", tuning)
    assert breaker.state("tool") == "open"
    now[0] = 29.0
    allowed, wait = breaker.allow("tool", tuning)
    assert not allowed
    assert wait == pytest.approx(1.0)  # cooldown doubled to 20s


def test_breakers_are_shared_across_registries():
    calls = {"count": 0}

    def down() -> str:
        calls["count"] += 1
        raise TransientToolError("down")

    first = ToolRegistry(
        [Tool.from_callable(down, name="SharedTool", effect="external", retry=False)],
        breaker=settings(failure_threshold=1),
    )
    second = ToolRegistry(
        [Tool.from_callable(down, name="SharedTool", effect="external", retry=False)],
        breaker=settings(failure_threshold=1),
    )

    first.invoke(ToolCall(id="c1", name="SharedTool", arguments={}))
    result = second.invoke(ToolCall(id="c2", name="SharedTool", arguments={}))

    assert calls["count"] == 1
    assert result.metadata["breaker"] == "open"


def test_raise_mode_reports_an_open_breaker_as_transient():
    def down() -> str:
        raise TransientToolError("down")

    registry = ToolRegistry(
        [Tool.from_callable(down, name="RaiseBreaker", effect="external", retry=False)],
        breaker=settings(failure_threshold=1),
    )
    registry.invoke(ToolCall(id="c1", name="RaiseBreaker", arguments={}))

    with pytest.raises(ToolExecutionError) as info:
        registry.invoke(ToolCall(id="c2", name="RaiseBreaker", arguments={}), error_mode="raise")

    assert info.value.kind == "transient"
    assert info.value.cause_type == "CircuitOpen"
