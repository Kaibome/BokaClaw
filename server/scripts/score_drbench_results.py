"""Score DRBench reports with the official scorer using a DeepSeek judge.

Run inside a container that has the ``drbench`` package installed and the task
data mounted at ``DRBENCH_DATA_DIR``:

    docker run --rm \
      -v ~/BokaClaw/drbench_data:/data:ro \
      -v ~/BokaClaw/drbench_results:/results \
      -e DRBENCH_DATA_DIR=/data \
      -e DEEPSEEK_API_KEY=... \
      drbench-scorer:test \
      python /app/score_drbench_results.py \
        --results /results/minival.json --out /results/minival-scored.json

The official ``QASimilarityV2`` metric constructs an ``AIAgentManager``; this
script patches its ``prompt_llm`` method to call DeepSeek so the smoke test does
not require an OpenAI key. Scores are therefore not directly comparable with
the official GPT-4o-mini leaderboard.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

DEFAULT_METRICS = ("insights_recall", "distractor_recall")


def _patch_judge() -> None:
    from drbench import config
    from drbench.gen_agent import AIAgentManager
    from openai import OpenAI

    api_key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("DEEPSEEK_API_KEY (or OPENAI_API_KEY) is required for judging")
    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
    # AIAgentManager validates the model against its OpenAI list and needs a key
    # before it builds a client. The client is unused after the patch below.
    config.OPENAI_API_KEY = api_key
    client = OpenAI(api_key=api_key, base_url=base_url)

    def prompt_llm(self, prompt: str, response_format: Any = None, return_json: bool = False) -> str:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
        )
        return response.choices[0].message.content

    AIAgentManager.prompt_llm = prompt_llm


def _score_one(task_id: str, report: dict[str, Any], metrics: list[str], savedir: Path) -> dict[str, Any]:
    from drbench import task_loader
    from drbench.score_report import score_report

    task = task_loader.get_task_from_id(task_id)
    scores = score_report(
        predicted_report_dict=report,
        task=task,
        metrics=metrics,
        savedir=str(savedir / task_id),
        include_per_insight_scores=False,
        model="gpt-4o-mini",
        verbose=False,
    )
    return {key: value for key, value in scores.items() if isinstance(value, (int, float))}


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--savedir", type=Path, default=Path("/results/scores"))
    parser.add_argument("--metrics", default=",".join(DEFAULT_METRICS))
    parser.add_argument("--limit", type=int)
    return parser.parse_args()


def main() -> int:
    args = _args()
    payload = json.loads(args.results.read_text(encoding="utf-8"))
    results = payload.get("results") if isinstance(payload, dict) else payload
    results = list(results or [])
    if args.limit is not None:
        results = results[: args.limit]
    metrics = [part.strip() for part in args.metrics.split(",") if part.strip()]
    _patch_judge()
    args.savedir.mkdir(parents=True, exist_ok=True)

    by_metric: dict[str, list[float]] = defaultdict(list)
    by_domain: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for result in results:
        report = result.get("report")
        if not report:
            result["scores"] = None
            result["score_error"] = "no report"
            continue
        try:
            scores = _score_one(str(result["task_id"]), report, metrics, args.savedir)
        except Exception as exc:  # noqa: BLE001 - keep scoring other tasks
            result["scores"] = None
            result["score_error"] = f"{type(exc).__name__}: {exc}"
            continue
        result["scores"] = scores
        result["score_error"] = None
        for name, value in scores.items():
            by_metric[name].append(float(value))
            by_domain[str(result.get("domain") or "unknown")][name].append(float(value))

    scored = [result for result in results if result.get("scores")]
    summary = {
        "tasks": len(results),
        "scored": len(scored),
        "metrics": {
            name: {
                "mean": sum(values) / len(values) if values else 0.0,
                "n": len(values),
            }
            for name, values in sorted(by_metric.items())
        },
        "distractor_avoidance": (
            1 - sum(by_metric["distractor_recall"]) / len(by_metric["distractor_recall"])
            if by_metric.get("distractor_recall")
            else None
        ),
        "by_domain": {
            domain: {
                name: {
                    "mean": sum(values) / len(values) if values else 0.0,
                    "n": len(values),
                }
                for name, values in metrics_by_name.items()
            }
            for domain, metrics_by_name in sorted(by_domain.items())
        },
        "judge": "deepseek-chat (patched into DRBench QASimilarityV2)",
        "results": results,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    sys.stdout.write(f"scored {len(scored)}/{len(results)} task(s)\n")
    for name, values in summary["metrics"].items():
        sys.stdout.write(f"  {name}: {values['mean']:.3f} (n={values['n']})\n")
    if summary["distractor_avoidance"] is not None:
        sys.stdout.write(f"  distractor_avoidance: {summary['distractor_avoidance']:.3f}\n")
    sys.stdout.write(f"report: {args.out}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
