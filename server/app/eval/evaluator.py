"""Run deepeval for one persisted agent run.

Only the eval worker imports this module. The server must never import it,
because importing deepeval pulls in a pydantic version the server cannot use.
"""

from __future__ import annotations

import contextlib
import io
import json
import time
from dataclasses import dataclass, field
from typing import Any

from app.eval import adapter
from app.eval.goldens import (
    golden_fields_from_snapshot,
    snapshot_hash,
    validate_snapshot,
)
from app.eval.metrics import build_metrics, metric_names


class EvalExecutionError(RuntimeError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass
class EvalOutcome:
    overall_success: bool
    metrics: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    judge_model: str | None = None
    token_cost: float | None = None
    raw_result: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0


def evaluate_run(
    *,
    run: Any,
    spans: list[Any],
    golden_snapshot: dict[str, Any] | None = None,
    golden_hash: str | None = None,
    use_llm_judge: bool,
    judge_max_chars: int = 20000,
) -> EvalOutcome:
    started = time.perf_counter()

    if golden_snapshot is not None and snapshot_hash(golden_snapshot) != golden_hash:
        raise EvalExecutionError(
            "eval_golden_snapshot_corrupt",
            "The frozen golden snapshot does not match its recorded hash.",
        )
    try:
        adapter.require_deepeval()
    except Exception as exc:
        raise EvalExecutionError("eval_deepeval_unavailable", str(exc)) from exc

    from deepeval import evaluate
    from deepeval.evaluate.configs import DisplayConfig, ErrorConfig

    golden = _build_golden(golden_snapshot) if golden_snapshot is not None else None
    test_case = adapter.to_llm_test_case(spans, run=run, golden=golden)

    warnings: list[str] = []
    if use_llm_judge:
        warnings = _truncate_case(test_case, judge_max_chars)

    metrics = build_metrics(test_case, use_llm_judge=use_llm_judge)
    # DisplayConfig does not silence deepeval's end-of-run banner, so capture
    # its console output. The worker only needs the returned TestResult.
    console = io.StringIO()
    try:
        with contextlib.redirect_stdout(console), contextlib.redirect_stderr(console):
            result = evaluate(
                test_cases=[test_case],
                metrics=metrics,
                display_config=DisplayConfig(
                    show_indicator=False,
                    print_results=False,
                    inspect_after_run=False,
                ),
                error_config=ErrorConfig(ignore_errors=True, skip_on_missing_params=True),
            )
    except Exception as exc:
        raise EvalExecutionError("eval_execution_failed", str(exc)) from exc

    if not result.test_results:
        raise EvalExecutionError("eval_execution_failed", "deepeval returned no test results.")
    test_result = result.test_results[0]
    metrics_data = list(test_result.metrics_data or [])

    threshold_by_name = metric_names(metrics)
    metric_rows: list[dict[str, Any]] = []
    returned: set[str] = set()
    for data in metrics_data:
        returned.add(data.name)
        metric_rows.append(_metric_row(data))

    # deepeval omits metrics skipped by skip_on_missing_params instead of
    # emitting a placeholder. Surface them so the UI can explain the gap.
    for name, threshold in threshold_by_name.items():
        if name not in returned:
            metric_rows.append(
                {
                    "name": name,
                    "score": None,
                    "threshold": threshold,
                    "success": None,
                    "reason": None,
                    "evaluation_model": None,
                    "evaluation_cost": None,
                    "skipped": True,
                    "error": None,
                }
            )

    scored = [row for row in metric_rows if not row["skipped"]]
    overall_success = bool(scored) and all(row["success"] is not False and not row["error"] for row in scored)

    costs = [data.evaluation_cost for data in metrics_data if data.evaluation_cost is not None]
    judge_model = next((data.evaluation_model for data in metrics_data if data.evaluation_model), None)
    duration_ms = int((time.perf_counter() - started) * 1000)

    return EvalOutcome(
        overall_success=overall_success,
        metrics=metric_rows,
        warnings=warnings,
        judge_model=judge_model,
        token_cost=float(sum(costs)) if costs else None,
        raw_result={
            "name": test_result.name,
            "success": test_result.success,
            "metrics": [
                {
                    key: value
                    for key, value in row.items()
                    if key in {"name", "score", "threshold", "success", "evaluation_model", "error"}
                }
                for row in metric_rows
            ],
        },
        duration_ms=duration_ms,
    )


def _build_golden(snapshot: dict[str, Any]) -> Any:
    from deepeval.dataset import Golden
    from deepeval.test_case import ToolCall

    validate_snapshot(snapshot)
    fields = golden_fields_from_snapshot(snapshot)
    tools = fields.pop("expected_tools", None)
    if tools:
        fields["expected_tools"] = [ToolCall(**tool) for tool in tools]
    return Golden(**fields)


def _metric_row(data: Any) -> dict[str, Any]:
    return {
        "name": data.name,
        "score": data.score,
        "threshold": data.threshold,
        "success": data.success,
        "reason": data.reason,
        "evaluation_model": data.evaluation_model,
        "evaluation_cost": data.evaluation_cost,
        "skipped": False,
        "error": data.error,
    }


def _truncate_case(test_case: Any, limit: int) -> list[str]:
    warnings: list[str] = []
    for attribute in ("input", "actual_output", "expected_output"):
        value = getattr(test_case, attribute, None)
        if isinstance(value, str) and len(value) > limit:
            setattr(test_case, attribute, value[:limit] + "…")
            warnings.append(f"{attribute} truncated to {limit} characters for the judge.")
    context = getattr(test_case, "context", None)
    if isinstance(context, list):
        test_case.context = [
            value if not isinstance(value, str) or len(value) <= limit else value[:limit] + "…"
            for value in context
        ]
    for tool in getattr(test_case, "tools_called", None) or []:
        if isinstance(getattr(tool, "output", None), str) and len(tool.output) > limit:
            tool.output = tool.output[:limit] + "…"
            warnings.append(f"tool {getattr(tool, 'name', '?')} output truncated for the judge.")
        parameters = getattr(tool, "input_parameters", None)
        if isinstance(parameters, dict):
            encoded = json.dumps(parameters, ensure_ascii=False, default=str)
            if len(encoded) > limit:
                tool.input_parameters = {
                    "truncated": True,
                    "preview": encoded[:limit],
                }
                warnings.append(f"tool {getattr(tool, 'name', '?')} parameters truncated for the judge.")
    return warnings
