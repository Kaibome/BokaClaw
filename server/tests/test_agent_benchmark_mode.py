from types import SimpleNamespace

from app.agent.runner import _benchmark_token_budget_exceeded, _benchmark_token_limit
from app.agent.runtime import (
    BENCHMARK_SYSTEM_NOTE,
    DRBENCH_SYSTEM_NOTE,
    _benchmark_system_note,
    _is_benchmark_payload,
)
from app.agent.turn_memory import save_turn_memory


def test_drbench_uses_its_own_report_prompt():
    assert _benchmark_system_note({"benchmark": "drbench"}) == DRBENCH_SYSTEM_NOTE
    assert _benchmark_system_note({"benchmark": "gaia"}) == BENCHMARK_SYSTEM_NOTE
    assert "report_insights" in DRBENCH_SYSTEM_NOTE
    assert "ENVIRONMENT.md" in DRBENCH_SYSTEM_NOTE
    assert "one fact per insight" in DRBENCH_SYSTEM_NOTE
    assert "do not dump unrelated operational data" in DRBENCH_SYSTEM_NOTE
    assert "Do not answer the opening question with a verdict" in DRBENCH_SYSTEM_NOTE
    assert "Do not add a `Final answer:` line" in DRBENCH_SYSTEM_NOTE


def test_drbench_uses_a_separate_token_limit():
    config = SimpleNamespace(benchmark_max_tokens=600_000, drbench_max_tokens=1_500_000)

    assert _benchmark_token_limit({"benchmark": "drbench"}, config) == 1_500_000
    assert _benchmark_token_limit({"benchmark": "gaia"}, config) == 600_000
    assert _benchmark_token_limit({}, config) == 600_000


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
