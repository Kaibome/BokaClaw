"""Per-run model token accounting."""

import os
import sys
import threading
from types import SimpleNamespace

import pytest

_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from app.agent.usage import (  # noqa: E402
    RunUsageMeter,
    normalize_usage,
    usage_snapshot_from_metadata,
)


def test_normalize_usage_accepts_provider_shapes():
    assert normalize_usage({"prompt_tokens": 3, "completion_tokens": 2}) == {
        "prompt_tokens": 3,
        "completion_tokens": 2,
        "total_tokens": 5,
    }
    assert normalize_usage({"input_tokens": 4, "output_tokens": 1, "total_tokens": 5}) == {
        "prompt_tokens": 4,
        "completion_tokens": 1,
        "total_tokens": 5,
    }
    assert normalize_usage(SimpleNamespace(prompt_tokens=1, completion_tokens=2, total_tokens=3)) == {
        "prompt_tokens": 1,
        "completion_tokens": 2,
        "total_tokens": 3,
    }
    assert normalize_usage(None) == {}
    assert normalize_usage({"unrelated": 9}) == {}


def test_meter_aggregates_by_source_and_reports_complete():
    meter = RunUsageMeter()
    meter.record("agent_loop", {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14})
    meter.record("compression", {"prompt_tokens": 6, "completion_tokens": 2, "total_tokens": 8})
    meter.record("memory_embedding", {"prompt_tokens": 3, "completion_tokens": 0, "total_tokens": 3})

    snapshot = meter.snapshot()

    assert snapshot["coverage"] == "complete"
    assert snapshot["input_tokens"] == 19
    assert snapshot["output_tokens"] == 6
    assert snapshot["total_tokens"] == 25
    assert snapshot["model_calls"] == 3
    assert snapshot["sources"]["agent_loop"]["calls"] == 1
    assert snapshot["sources"]["memory_llm"]["calls"] == 0


def test_meter_marks_unknown_calls_and_partial_coverage():
    meter = RunUsageMeter()
    meter.record("agent_loop", {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
    meter.record("memory_llm", None)

    snapshot = meter.snapshot()

    assert snapshot["coverage"] == "partial"
    assert snapshot["unknown_calls"] == 1
    assert snapshot["total_tokens"] == 15


def test_meter_reports_unknown_when_no_usage_was_seen():
    meter = RunUsageMeter()
    meter.record("agent_loop", {})

    snapshot = meter.snapshot()

    assert snapshot["coverage"] == "unknown"
    assert snapshot["input_tokens"] is None
    assert snapshot["total_tokens"] is None
    assert snapshot["model_calls"] == 1
    assert snapshot["unknown_calls"] == 1


def test_meter_reports_none_without_calls():
    snapshot = RunUsageMeter().snapshot()

    assert snapshot["coverage"] == "none"
    assert snapshot["model_calls"] == 0
    assert snapshot["total_tokens"] is None


def test_pending_request_without_response_becomes_unknown():
    meter = RunUsageMeter()
    meter.record_request("agent_loop")

    snapshot = meter.snapshot()

    assert snapshot["coverage"] == "unknown"
    assert snapshot["unknown_calls"] == 1
    assert snapshot["total_tokens"] is None


def test_record_response_settles_a_pending_request():
    meter = RunUsageMeter()
    meter.record_request("agent_loop")
    meter.record_response("agent_loop", {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3})

    snapshot = meter.snapshot()

    assert snapshot["coverage"] == "complete"
    assert snapshot["model_calls"] == 1
    assert snapshot["unknown_calls"] == 0


def test_aggregate_is_ignored_when_per_call_usage_exists():
    meter = RunUsageMeter()
    meter.record("agent_loop", {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})

    assert meter.record_aggregate({"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}) is False
    assert meter.snapshot()["total_tokens"] == 15


def test_aggregate_is_used_when_only_an_aggregate_exists():
    meter = RunUsageMeter()

    assert meter.record_aggregate({"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}) is True
    snapshot = meter.snapshot()

    assert snapshot["coverage"] == "aggregate"
    assert snapshot["total_tokens"] == 150
    assert snapshot["model_calls"] == 1


def test_meter_is_thread_safe():
    meter = RunUsageMeter()

    def worker() -> None:
        for _ in range(200):
            meter.record("agent_loop", {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    snapshot = meter.snapshot()
    assert snapshot["model_calls"] == 1600
    assert snapshot["total_tokens"] == 3200


def test_usage_snapshot_from_metadata_round_trips_and_upgrades_legacy():
    meter = RunUsageMeter()
    meter.record("agent_loop", {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3})
    snapshot = meter.snapshot()

    assert usage_snapshot_from_metadata({"usage": snapshot}) == snapshot

    legacy = usage_snapshot_from_metadata(
        {"usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}}
    )
    assert legacy["coverage"] == "aggregate"
    assert legacy["total_tokens"] == 10

    assert usage_snapshot_from_metadata({})["coverage"] == "unavailable"


@pytest.mark.parametrize("usage", [None, {}, {"prompt_tokens": 0}])
def test_zero_usage_is_not_fabricated(usage):
    meter = RunUsageMeter()
    meter.record("agent_loop", usage)
    snapshot = meter.snapshot()
    assert snapshot["coverage"] in {"unknown", "complete"}
    if not normalize_usage(usage):
        assert snapshot["total_tokens"] is None


def test_normalize_usage_keeps_cache_and_reasoning_counters():
    assert normalize_usage(
        {
            "prompt_tokens": 100,
            "completion_tokens": 5,
            "total_tokens": 105,
            "prompt_cache_hit_tokens": 64,
            "prompt_cache_miss_tokens": 36,
            "reasoning_tokens": 3,
        }
    ) == {
        "prompt_tokens": 100,
        "completion_tokens": 5,
        "total_tokens": 105,
        "cached_tokens": 64,
        "cache_miss_tokens": 36,
        "reasoning_tokens": 3,
    }


def test_meter_aggregates_cache_counters_and_reports_hit_rate():
    meter = RunUsageMeter()
    meter.record(
        "agent_loop",
        {
            "prompt_tokens": 100,
            "completion_tokens": 5,
            "total_tokens": 105,
            "cached_tokens": 64,
            "cache_miss_tokens": 36,
        },
    )
    meter.record(
        "agent_loop",
        {"prompt_tokens": 20, "completion_tokens": 1, "total_tokens": 21, "cached_tokens": 0, "cache_miss_tokens": 20},
    )
    meter.record("memory_embedding", {"prompt_tokens": 3, "completion_tokens": 0, "total_tokens": 3})

    snapshot = meter.snapshot()

    assert snapshot["cached_tokens"] == 64
    assert snapshot["cache_miss_tokens"] == 56
    assert snapshot["cache_hit_rate"] == pytest.approx(64 / 120, abs=0.0001)
    assert snapshot["sources"]["agent_loop"]["cached_tokens"] == 64
    assert "cached_tokens" not in snapshot["sources"]["memory_embedding"]


def test_meter_omits_cache_fields_when_no_call_reports_them():
    meter = RunUsageMeter()
    meter.record("agent_loop", {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12})

    snapshot = meter.snapshot()

    assert "cached_tokens" not in snapshot
    assert "cache_hit_rate" not in snapshot


def test_usage_snapshot_from_metadata_upgrades_legacy_cache_counters():
    snapshot = usage_snapshot_from_metadata(
        {
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 1,
                "total_tokens": 11,
                "prompt_cache_hit_tokens": 4,
                "prompt_cache_miss_tokens": 6,
            }
        }
    )

    assert snapshot["coverage"] == "aggregate"
    assert snapshot["cached_tokens"] == 4
    assert snapshot["cache_miss_tokens"] == 6
    assert snapshot["cache_hit_rate"] == 0.4
    assert snapshot["sources"]["agent_loop"]["cached_tokens"] == 4
