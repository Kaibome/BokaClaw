from app.agent.runner import _benchmark_token_budget_exceeded
from app.agent.runtime import BENCHMARK_SYSTEM_NOTE, _is_benchmark_payload
from app.agent.turn_memory import save_turn_memory


def test_benchmark_token_budget_only_applies_to_benchmark_runs():
    assert _benchmark_token_budget_exceeded({"benchmark": "gaia"}, 600_001, 600_000)
    assert not _benchmark_token_budget_exceeded({"benchmark": "gaia"}, 600_000, 600_000)
    assert not _benchmark_token_budget_exceeded({}, 10**9, 600_000)
    assert not _benchmark_token_budget_exceeded({"benchmark": "gaia"}, 10**9, 0)


def test_benchmark_prompt_requires_a_single_answer_value():
    assert "Final answer: <answer>" in BENCHMARK_SYSTEM_NOTE
    assert "no variable names" in BENCHMARK_SYSTEM_NOTE
    assert "full=true" in BENCHMARK_SYSTEM_NOTE
    assert "instead of `Unverified`" in BENCHMARK_SYSTEM_NOTE


def test_benchmark_payload_is_detected_from_metadata():
    assert _is_benchmark_payload({"metadata": {"benchmark": "gaia"}})
    assert not _is_benchmark_payload({"metadata": {}})
    assert not _is_benchmark_payload({})


def test_benchmark_turn_memory_is_skipped():
    report = save_turn_memory(
        {"message": "question", "metadata": {"benchmark": "gaia"}},
        "answer",
        [],
    )

    assert report.action == "skipped"
    assert report.reason == "benchmark"
