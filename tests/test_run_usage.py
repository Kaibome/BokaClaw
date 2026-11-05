"""Per-run model token accounting and mem0 instrumentation."""

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
    install_memory_metering,
    meter_context,
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


class _Completions:
    def __init__(self) -> None:
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=4, total_tokens=14)
        )


class _Chat:
    def __init__(self) -> None:
        self.completions = _Completions()


class _Embeddings:
    def __init__(self) -> None:
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=3, completion_tokens=0, total_tokens=3))


class _Client:
    def __init__(self) -> None:
        self.chat = _Chat()
        self.embeddings = _Embeddings()


def test_memory_metering_wraps_openai_clients():
    memory = SimpleNamespace(llm=SimpleNamespace(client=_Client()), embedding_model=SimpleNamespace(client=_Client()))
    assert install_memory_metering(memory) is True
    assert install_memory_metering(memory) is True  # idempotent

    meter = RunUsageMeter()
    with meter_context(meter):
        memory.llm.client.chat.completions.create(model="m", messages=[])
        memory.embedding_model.client.embeddings.create(model="e", input="x")

    snapshot = meter.snapshot()
    assert snapshot["sources"]["memory_llm"]["total_tokens"] == 14
    assert snapshot["sources"]["memory_embedding"]["total_tokens"] == 3
    assert snapshot["coverage"] == "complete"


class _LocalLLM:
    def __init__(self) -> None:
        self.calls = 0

    def generate_response(self, *args, **kwargs):
        self.calls += 1
        return "ok"


def test_memory_metering_counts_unknown_for_providers_without_a_client():
    memory = SimpleNamespace(llm=_LocalLLM(), embedding_model=None)
    assert install_memory_metering(memory) is True

    meter = RunUsageMeter()
    with meter_context(meter):
        memory.llm.generate_response(messages=[])

    snapshot = meter.snapshot()
    assert snapshot["coverage"] == "unknown"
    assert snapshot["unknown_calls"] == 1
    assert snapshot["total_tokens"] is None


def test_memory_metering_is_inert_without_a_bound_meter():
    memory = SimpleNamespace(llm=SimpleNamespace(client=_Client()), embedding_model=None)
    assert install_memory_metering(memory) is True

    # No meter_context: the wrapped call must still succeed.
    assert memory.llm.client.chat.completions.create(model="m", messages=[]) is not None


@pytest.mark.parametrize("usage", [None, {}, {"prompt_tokens": 0}])
def test_zero_usage_is_not_fabricated(usage):
    meter = RunUsageMeter()
    meter.record("agent_loop", usage)
    snapshot = meter.snapshot()
    assert snapshot["coverage"] in {"unknown", "complete"}
    if not normalize_usage(usage):
        assert snapshot["total_tokens"] is None
