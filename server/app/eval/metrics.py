"""Metric selection for one evaluation case.

Deterministic metrics run without any LLM provider. Judge-backed metrics are
added only when the caller opted in. ToolCorrectness scores tool names without
an LLM call, but deepeval 4.3.3 still constructs a judge model in its
constructor, so it is gated behind the judge switch as well. It is also gated
per case on ``expected_tools`` + ``tools_called`` rather than per goldens file.
"""

from __future__ import annotations

from typing import Any


def build_metrics(test_case: Any, *, use_llm_judge: bool) -> list[Any]:
    from deepeval.metrics import AgentLoopDetectionMetric

    metrics: list[Any] = [AgentLoopDetectionMetric(threshold=1.0, async_mode=False)]

    if use_llm_judge:
        from deepeval.metrics import (
            AnswerRelevancyMetric,
            ArgumentCorrectnessMetric,
            StepEfficiencyMetric,
            TaskCompletionMetric,
        )

        metrics.extend(
            [
                AnswerRelevancyMetric(threshold=0.5),
                TaskCompletionMetric(threshold=0.5),
                StepEfficiencyMetric(threshold=0.5),
                ArgumentCorrectnessMetric(threshold=0.5),
            ]
        )

    if (
        use_llm_judge
        and getattr(test_case, "expected_tools", None)
        and getattr(test_case, "tools_called", None)
    ):
        from deepeval.metrics import ToolCorrectnessMetric

        metrics.append(ToolCorrectnessMetric(threshold=0.5))

    return metrics


def metric_names(metrics: list[Any]) -> dict[str, float | None]:
    """Map the reported metric name to its threshold for skipped entries.

    ``MetricData.name`` comes from deepeval's ``metric.__name__`` property,
    which is a human-readable label such as "Agent Loop Detection".
    """
    return {getattr(metric, "__name__", type(metric).__name__): getattr(metric, "threshold", None) for metric in metrics}
