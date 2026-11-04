"""Evaluate persisted agent runs with deepeval.

Example:
    python tests/agent_eval/run.py --run-id <uuid> --goldens tests/agent_eval/goldens.example.json

Run this from an environment where deepeval 4.3.3 is installed (see the ``eval``
extra), not the server environment: deepeval requires a newer pydantic than the
server pins. Nothing is uploaded; results are printed to stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "1")

_EVAL_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _EVAL_DIR.parents[1]
_SERVER_DIR = _REPO_ROOT / "server"
for _path in (str(_EVAL_DIR), str(_SERVER_DIR), str(_REPO_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import adapter  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", action="append", required=True, help="Run UUID; repeat for several runs.")
    parser.add_argument("--goldens", help="JSON file with golden cases keyed by eval_case_id.")
    return parser.parse_args()


def _load_goldens(path: str | None) -> dict[str, dict]:
    if not path:
        return {}
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise SystemExit("Goldens file must be a JSON array.")
    goldens: dict[str, dict] = {}
    for item in payload:
        if not isinstance(item, dict) or not item.get("eval_case_id"):
            raise SystemExit("Each golden needs an eval_case_id.")
        goldens[str(item["eval_case_id"])] = item
    return goldens


def _metrics(has_golden: bool, judge_available: bool) -> list:
    from deepeval.metrics import AgentLoopDetectionMetric

    metrics = [AgentLoopDetectionMetric()]
    if not judge_available:
        return metrics

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
    if has_golden:
        from deepeval.metrics import ToolCorrectnessMetric

        metrics.append(ToolCorrectnessMetric(threshold=0.5))
    return metrics


def main() -> int:
    adapter.require_deepeval()
    from deepeval import evaluate
    from deepeval.dataset import Golden

    args = _parse_args()
    goldens = _load_goldens(args.goldens)
    judge_available = bool(os.environ.get("OPENAI_API_KEY") or os.environ.get("DEEPSEEK_API_KEY"))

    cases = []
    for run_id in args.run_id:
        run = adapter.load_run(run_id)
        if run is None:
            raise SystemExit(f"Run {run_id} not found.")
        spans = adapter.load_run_spans(run_id)
        if not spans:
            raise SystemExit(f"Run {run_id} has no spans (capture disabled or unsampled).")
        payload = getattr(run, "request_payload", None) or {}
        eval_case_id = (
            ((payload.get("metadata") or {}).get("eval_case_id")) if isinstance(payload.get("metadata"), dict) else None
        )
        golden_data = goldens.get(str(eval_case_id)) if eval_case_id else None
        # `eval_case_id` is our join key, not a Golden field.
        golden_fields = {key: value for key, value in (golden_data or {}).items() if key != "eval_case_id"}
        golden = Golden(**golden_fields) if golden_fields else None
        cases.append(adapter.to_llm_test_case(spans, run=run, golden=golden))

    result = evaluate(test_cases=cases, metrics=_metrics(any(goldens), judge_available))
    for test_result in result.test_results:
        print(f"\ncase: {test_result.name or test_result.input[:60]}")
        for metric_data in test_result.metrics_data or []:
            print(f"  {metric_data.name}: score={metric_data.score} success={metric_data.success}")
            if metric_data.reason:
                print(f"    reason: {metric_data.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
