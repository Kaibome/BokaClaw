"""Offline evaluation CLI for one run.

    python -m app.eval.cli --run-id <uuid> --eval-case-id <case> [--judge] [--json]

The golden comes from ``--goldens FILE`` when given, otherwise from the
``eval_goldens`` table. Nothing is written back: use the API/worker path for a
queued, persisted evaluation.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "1")
os.environ.setdefault("DEEPEVAL_CACHE_FOLDER", "/tmp/deepeval")

from app.core.db import SessionLocal
from app.eval.config import apply_judge_env, load_eval_config
from app.eval.evaluator import EvalExecutionError, evaluate_run
from app.eval.goldens import (
    GoldenPayload,
    build_snapshot,
    get_golden,
    snapshot_hash,
    validate_goldens_file,
)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_INPUT = 2
EXIT_ERROR = 3


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-id", action="append", required=True, help="Run UUID; repeat for several runs.")
    parser.add_argument("--eval-case-id", help="Golden case; defaults to the run's metadata.eval_case_id.")
    parser.add_argument("--goldens", help="JSON file with golden cases instead of the database.")
    parser.add_argument("--judge", action="store_true", help="Add LLM-judged metrics (sends data to the judge).")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    return parser.parse_args(argv)


def _load_goldens_file(path: str) -> dict[str, GoldenPayload]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Cannot read goldens file: {exc}")
    try:
        parsed = validate_goldens_file(payload)
    except ValueError as exc:
        raise SystemExit(str(exc))
    return {item.eval_case_id: item for item in parsed}


def _load_db_golden(eval_case_id: str) -> GoldenPayload | None:
    with SessionLocal() as db:
        row = get_golden(db, eval_case_id)
        if row is None:
            return None
        return GoldenPayload.model_validate(
            {
                "eval_case_id": row.eval_case_id,
                "input": row.input,
                "expected_output": row.expected_output,
                "expected_tools": row.expected_tools,
                "context": row.context,
            }
        )


def _case_id_from_run(run: Any) -> str | None:
    payload = getattr(run, "request_payload", None) or {}
    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    if isinstance(metadata, dict):
        value = metadata.get("eval_case_id")
        return str(value) if value else None
    return None


def _prepare_judge(enabled: bool) -> None:
    if not enabled:
        return
    config = load_eval_config()
    if config.judge_available:
        apply_judge_env(config)
        return
    # Offline callers may already export the provider key the old CLI used.
    if os.environ.get("DEEPSEEK_API_KEY"):
        os.environ["USE_DEEPSEEK_MODEL"] = "true"
        os.environ.setdefault("DEEPSEEK_MODEL_NAME", "deepseek-chat")
        return
    if os.environ.get("OPENAI_API_KEY"):
        return
    raise SystemExit(
        "The judge needs EVAL_JUDGE_API_KEY (or DEEPSEEK_API_KEY / OPENAI_API_KEY) in the environment."
    )


def _print_text(result: dict[str, Any]) -> None:
    status = "PASS" if result["overall_success"] else "FAIL"
    print(
        f"\nrun {result['run_id']} case {result['eval_case_id'] or '-'}: "
        f"{status} in {result['duration_ms']} ms"
    )
    for metric in result["metrics"]:
        if metric["skipped"]:
            print(f"  {metric['name']}: skipped")
            continue
        outcome = "pass" if metric["success"] else "fail"
        print(
            f"  {metric['name']}: score={metric['score']} "
            f"threshold={metric['threshold']} {outcome}"
        )
        if metric["reason"]:
            print(f"    reason: {metric['reason']}")
        if metric["error"]:
            print(f"    error: {metric['error']}")
    for warning in result["warnings"]:
        print(f"  warning: {warning}")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _prepare_judge(args.judge)

    run_ids: list[uuid.UUID] = []
    for raw in args.run_id:
        try:
            run_ids.append(uuid.UUID(raw))
        except ValueError:
            raise SystemExit(f"Invalid run id: {raw}")

    file_goldens = _load_goldens_file(args.goldens) if args.goldens else {}
    from app.eval.repo import EvalRepository

    repo = EvalRepository()
    results: list[dict[str, Any]] = []
    exit_code = EXIT_OK

    for run_id in run_ids:
        run = repo.get_run(run_id)
        if run is None:
            raise SystemExit(f"Run {run_id} not found.")
        spans = repo.list_spans(run_id)
        if not spans:
            raise SystemExit(f"Run {run_id} has no spans (capture disabled or unsampled).")

        eval_case_id = args.eval_case_id or _case_id_from_run(run)
        golden: GoldenPayload | None = None
        if eval_case_id:
            golden = file_goldens.get(eval_case_id) or _load_db_golden(eval_case_id)
            if golden is None:
                raise SystemExit(f"Golden {eval_case_id!r} not found.")

        snapshot = build_snapshot(golden) if golden is not None else None
        try:
            outcome = evaluate_run(
                run=run,
                spans=spans,
                golden_snapshot=snapshot,
                golden_hash=snapshot_hash(snapshot) if snapshot is not None else None,
                use_llm_judge=args.judge,
                judge_max_chars=load_eval_config().judge_max_chars,
            )
        except EvalExecutionError as exc:
            print(f"eval {run_id} failed: {exc.code}: {exc.detail}", file=sys.stderr)
            return EXIT_ERROR

        result = {
            "run_id": str(run_id),
            "eval_case_id": eval_case_id,
            "overall_success": outcome.overall_success,
            "metrics": outcome.metrics,
            "warnings": outcome.warnings,
            "judge_model": outcome.judge_model,
            "token_cost": outcome.token_cost,
            "duration_ms": outcome.duration_ms,
        }
        results.append(result)
        if not outcome.overall_success:
            exit_code = EXIT_FAILED

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for result in results:
            _print_text(result)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
