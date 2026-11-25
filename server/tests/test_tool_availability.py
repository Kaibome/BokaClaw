"""Tool availability probing: cache, jitter grace, failure states and filtering."""

import time
from types import SimpleNamespace

from app.agent.tool_availability import (
    CACHE_TTL_SECONDS,
    EXTERNAL_PROBE_TIMEOUT_SECONDS,
    JITTER_GRACE_SECONDS,
    ToolAvailabilityTracker,
    ToolProbe,
    build_tool_probes,
    filter_tool_mapping,
    filter_tools,
    probes_enabled,
)


class FakeClock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _counting_probe(calls: list[bool], result=True) -> ToolProbe:
    def check() -> bool:
        calls.append(True)
        if isinstance(result, Exception):
            raise result
        return bool(result)

    return ToolProbe(check)


def test_cache_hit_returns_value_without_probing() -> None:
    clock = FakeClock()
    tracker = ToolAvailabilityTracker(clock=clock)
    calls: list[bool] = []
    probe = _counting_probe(calls)

    assert tracker.check({"Read": probe}) == {"Read": True}
    clock.advance(CACHE_TTL_SECONDS - 0.1)
    assert tracker.check({"Read": probe}) == {"Read": True}
    assert len(calls) == 1


def test_cache_expires_after_ttl_and_probes_again() -> None:
    clock = FakeClock()
    tracker = ToolAvailabilityTracker(clock=clock)
    calls: list[bool] = []
    probe = _counting_probe(calls)

    tracker.check({"Read": probe})
    clock.advance(CACHE_TTL_SECONDS)
    tracker.check({"Read": probe})
    assert len(calls) == 2


def test_success_caches_true_and_updates_last_good() -> None:
    clock = FakeClock()
    tracker = ToolAvailabilityTracker(clock=clock)
    calls: list[bool] = []
    probe = _counting_probe(calls)

    assert tracker.check({"Read": probe}) == {"Read": True}
    assert tracker._cache["Read"].available is True
    assert tracker._last_good["Read"] == clock.now


def test_recent_failure_is_jitter_and_not_cached() -> None:
    clock = FakeClock()
    tracker = ToolAvailabilityTracker(clock=clock)
    calls: list[bool] = []

    tracker.check({"Read": _counting_probe(calls, result=True)})
    clock.advance(CACHE_TTL_SECONDS)
    failing = _counting_probe(calls, result=False)

    assert tracker.check({"Read": failing}) == {"Read": True}
    assert len(calls) == 2
    # No cache entry was written, so the next check probes again instead of
    # serving a stale decision.
    assert tracker.check({"Read": failing}) == {"Read": True}
    assert len(calls) == 3


def test_failure_without_prior_success_is_cached_false() -> None:
    clock = FakeClock()
    tracker = ToolAvailabilityTracker(clock=clock)
    calls: list[bool] = []
    probe = _counting_probe(calls, result=False)

    assert tracker.check({"Read": probe}) == {"Read": False}
    assert tracker._cache["Read"].available is False
    assert tracker.check({"Read": probe}) == {"Read": False}
    assert len(calls) == 1


def test_failure_past_grace_is_cached_false() -> None:
    clock = FakeClock()
    tracker = ToolAvailabilityTracker(clock=clock)
    calls: list[bool] = []

    tracker.check({"Read": _counting_probe(calls, result=True)})
    clock.advance(JITTER_GRACE_SECONDS + 0.1)
    failing = _counting_probe(calls, result=False)

    assert tracker.check({"Read": failing}) == {"Read": False}
    assert tracker.check({"Read": failing}) == {"Read": False}
    assert len(calls) == 2


def test_shared_probe_runs_once_per_batch() -> None:
    clock = FakeClock()
    tracker = ToolAvailabilityTracker(clock=clock)
    calls: list[bool] = []
    probe = _counting_probe(calls)

    assert tracker.check({"Read": probe, "Glob": probe}) == {"Read": True, "Glob": True}
    assert len(calls) == 1


def test_exception_counts_as_failure() -> None:
    clock = FakeClock()
    tracker = ToolAvailabilityTracker(clock=clock)
    calls: list[bool] = []
    probe = _counting_probe(calls, result=RuntimeError("boom"))

    assert tracker.check({"Read": probe}) == {"Read": False}


def test_probe_timeout_counts_as_failure() -> None:
    tracker = ToolAvailabilityTracker()
    calls: list[bool] = []

    def slow() -> bool:
        calls.append(True)
        time.sleep(0.2)
        return True

    started = time.monotonic()
    assert tracker.check({"Read": ToolProbe(slow, timeout=0.05)}) == {"Read": False}
    assert time.monotonic() - started < 1.0
    assert calls == [True]


def test_hard_timeout_bounds_the_batch() -> None:
    tracker = ToolAvailabilityTracker(hard_timeout=0.15)

    def slow() -> bool:
        time.sleep(0.5)
        return True

    probes = {
        "Read": ToolProbe(slow, timeout=5.0),
        "Write": ToolProbe(slow, timeout=5.0),
    }
    started = time.monotonic()
    decisions = tracker.check(probes)
    assert decisions == {"Read": False, "Write": False}
    assert time.monotonic() - started < 0.5


def test_filter_tools_drops_unavailable_and_keeps_unprobed() -> None:
    clock = FakeClock()
    tracker = ToolAvailabilityTracker(clock=clock)
    tools = [
        SimpleNamespace(name="Read"),
        SimpleNamespace(name="Bash"),
        SimpleNamespace(name="ToolSearch"),
    ]
    probes = {
        "Read": ToolProbe(lambda: False),
        "Bash": ToolProbe(lambda: True),
    }

    kept = filter_tools(tools, probes, tracker=tracker)

    assert [tool.name for tool in kept] == ["Bash", "ToolSearch"]


def test_filter_tool_mapping_rechecks_deferred_catalog() -> None:
    clock = FakeClock()
    tracker = ToolAvailabilityTracker(clock=clock)
    items = {"mcp__alpha__search": object(), "PptxUpdateSlide": object()}
    probes = {
        "mcp__alpha__search": ToolProbe(lambda: False),
        "PptxUpdateSlide": ToolProbe(lambda: True),
    }

    kept = filter_tool_mapping(items, probes, tracker=tracker)

    assert list(kept) == ["PptxUpdateSlide"]


def test_build_tool_probes_covers_local_and_external_tools(tmp_path) -> None:
    dev_config = SimpleNamespace(workspace=tmp_path, executor=None)
    tools = [
        SimpleNamespace(name=name)
        for name in (
            "Read",
            "Write",
            "Bash",
            "WebFetch",
            "WebSearch",
            "PptxBuild",
            "SheetInspect",
            "SheetCreate",
            "search_memories",
            "Skill",
            "ToolSearch",
        )
    ]

    probes = build_tool_probes(tools, dev_config=dev_config)

    expected = {tool.name for tool in tools} - {"Skill", "ToolSearch"}
    assert set(probes) == expected
    assert probes["WebFetch"].external is True
    assert probes["WebFetch"].timeout == EXTERNAL_PROBE_TIMEOUT_SECONDS
    assert probes["Read"].external is False


def test_probes_enabled_defaults_true_and_can_be_disabled(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_TOOL_PROBES", raising=False)
    assert probes_enabled() is True
    monkeypatch.setenv("AGENT_TOOL_PROBES", "off")
    assert probes_enabled() is False


def test_build_tool_probes_maps_mcp_tools_to_their_server(tmp_path) -> None:
    server = SimpleNamespace(name="alpha")
    mcp_config = SimpleNamespace(servers=(server,))
    tools = [SimpleNamespace(name="mcp__alpha__search"), SimpleNamespace(name="mcp__alpha__fetch")]

    probes = build_tool_probes(tools, mcp_config=mcp_config)

    assert set(probes) == {"mcp__alpha__search", "mcp__alpha__fetch"}
    assert probes["mcp__alpha__search"].external is True
