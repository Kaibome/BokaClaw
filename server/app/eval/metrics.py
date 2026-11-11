"""Metric selection for one evaluation case.

Deterministic metrics run without any LLM provider. Judge-backed metrics are
added only when the caller opted in. ToolCorrectness scores tool names without
an LLM call, but deepeval 4.3.3 still constructs a judge model in its
constructor, so it is gated behind the judge switch as well. It is also gated
per case on ``expected_tools`` + ``tools_called`` rather than per goldens file.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

_CHINESE_INSTRUCTION = (
    "语言要求：所有面向用户的说明性文本字段（reason、analysis、explanation、summary 等）"
    "必须使用简体中文撰写；JSON 键名、枚举值（如 yes/no/borderline）、数字、工具名"
    "和专有名词保持原样。\n\n"
)


def _localize_prompt(prompt: str, language: str) -> str:
    return f"{_CHINESE_INSTRUCTION}{prompt}" if language == "zh" else prompt


@lru_cache(maxsize=2)
def _localized_templates(language: str) -> dict[str, Any]:
    """Template subclasses that ask deepeval's judge for Chinese reasons.

    deepeval renders every judge prompt from a template class, and each affected
    metric accepts an ``evaluation_template`` override. Wrapping only the
    reason-producing methods keeps scores and JSON field names unchanged.
    """
    from deepeval.metrics.answer_relevancy import AnswerRelevancyTemplate
    from deepeval.metrics.argument_correctness import ArgumentCorrectnessTemplate
    from deepeval.metrics.goal_accuracy.goal_accuracy import GoalAccuracyTemplate
    from deepeval.metrics.pii_leakage import PIILeakageTemplate
    from deepeval.metrics.step_efficiency import StepEfficiencyTemplate
    from deepeval.metrics.task_completion import TaskCompletionTemplate
    from deepeval.metrics.tool_correctness import ToolCorrectnessTemplate

    def build(base: Any, *methods: str) -> Any:
        namespace: dict[str, Any] = {}
        for method in methods:

            def render(_method: str = method, **kwargs: Any) -> str:
                return _localize_prompt(getattr(base, _method)(**kwargs), language)

            namespace[method] = staticmethod(render)
        return type(f"Localized{base.__name__}", (base,), namespace)

    return {
        "answer_relevancy": build(AnswerRelevancyTemplate, "generate_reason", "generate_verdicts"),
        "argument_correctness": build(ArgumentCorrectnessTemplate, "generate_reason", "generate_verdicts"),
        "goal_accuracy": build(
            GoalAccuracyTemplate, "get_accuracy_score", "get_plan_evaluation_score", "get_final_reason"
        ),
        "pii_leakage": build(PIILeakageTemplate, "extract_pii", "generate_verdicts", "generate_reason"),
        "step_efficiency": build(StepEfficiencyTemplate, "get_execution_efficiency"),
        "task_completion": build(TaskCompletionTemplate, "generate_verdict"),
        "tool_correctness": build(ToolCorrectnessTemplate, "get_tool_selection_score"),
    }


def _available_tools_from_trace(test_case: Any) -> set[str]:
    """Collect the tool names the run recorded as available.

    The adapter stores the union in ``metadata["available_tools"]`` because the
    runtime records ``tool_names`` on LLM spans, while deepeval's trace schema
    only exposes ``available_tools`` on agent spans. Walking the trace as well
    keeps older cases working.
    """
    found: set[str] = set()
    metadata = getattr(test_case, "metadata", None)
    names = metadata.get("available_tools") if isinstance(metadata, dict) else None
    if isinstance(names, list):
        found.update(name for name in names if isinstance(name, str))

    trace = getattr(test_case, "_trace_dict", None)

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        available = node.get("available_tools")
        if isinstance(available, list):
            found.update(name for name in available if isinstance(name, str))
        for child in node.get("children") or []:
            walk(child)

    walk(trace)
    return found


def build_metrics(
    test_case: Any,
    *,
    use_llm_judge: bool,
    judge_language: str = "zh",
    tool_allowlist: tuple[str, ...] = (),
    tool_denylist: tuple[str, ...] = (),
) -> list[Any]:
    from deepeval.metrics import AgentLoopDetectionMetric

    metrics: list[Any] = [AgentLoopDetectionMetric(threshold=1.0, async_mode=False)]
    templates = _localized_templates(judge_language) if use_llm_judge and judge_language == "zh" else {}

    def template_args(key: str) -> dict[str, Any]:
        return {"evaluation_template": templates[key]} if key in templates else {}

    tools_called = getattr(test_case, "tools_called", None)
    if tools_called:
        allowed = set(tool_allowlist) | _available_tools_from_trace(test_case)
        denied = set(tool_denylist)
        if allowed or denied:
            from deepeval.metrics import ToolPermissionMetric

            metrics.append(
                ToolPermissionMetric(
                    allowed_tools=sorted(allowed) or None,
                    denied_tools=sorted(denied) or None,
                    threshold=1.0,
                )
            )

    if use_llm_judge:
        from deepeval.metrics import (
            AnswerRelevancyMetric,
            ArgumentCorrectnessMetric,
            PIILeakageMetric,
            StepEfficiencyMetric,
            TaskCompletionMetric,
        )

        metrics.extend(
            [
                AnswerRelevancyMetric(threshold=0.5, **template_args("answer_relevancy")),
                TaskCompletionMetric(threshold=0.5, **template_args("task_completion")),
                StepEfficiencyMetric(threshold=0.5, **template_args("step_efficiency")),
                ArgumentCorrectnessMetric(threshold=0.5, **template_args("argument_correctness")),
                PIILeakageMetric(threshold=0.9, **template_args("pii_leakage")),
            ]
        )

    if (
        use_llm_judge
        and getattr(test_case, "expected_tools", None)
        and getattr(test_case, "tools_called", None)
    ):
        from deepeval.metrics import ToolCorrectnessMetric

        metrics.append(ToolCorrectnessMetric(threshold=0.5, **template_args("tool_correctness")))

    return metrics


def build_conversational_metrics(
    test_case: Any, *, use_llm_judge: bool, judge_language: str = "zh"
) -> list[Any]:
    """Metrics that require a ``ConversationalTestCase``.

    deepeval's ``evaluate()`` accepts either all single-turn or all
    conversational test cases, so these run in a second pass and their metric
    rows are merged with the single-turn results by the evaluator.
    """
    if not use_llm_judge:
        return []

    from deepeval.metrics import GoalAccuracyMetric

    templates = _localized_templates(judge_language) if judge_language == "zh" else {}
    args = {"evaluation_template": templates["goal_accuracy"]} if "goal_accuracy" in templates else {}
    return [GoalAccuracyMetric(threshold=0.5, **args)]


def metric_names(metrics: list[Any]) -> dict[str, float | None]:
    """Map the reported metric name to its threshold for skipped entries.

    ``MetricData.name`` comes from deepeval's ``metric.__name__`` property,
    which is a human-readable label such as "Agent Loop Detection".
    """
    return {
        getattr(metric, "__name__", type(metric).__name__): getattr(metric, "threshold", None)
        for metric in metrics
    }
