"""Import DRBench tasks into ``eval_goldens`` for the benchmark runner.

Run inside the server container after copying the DRBench data:

    docker cp ~/BokaClaw/drbench_data bokaclaw-server:/tmp/drbench_data
    docker exec bokaclaw-server python scripts/import_drbench_goldens.py \
        --data-root /tmp/drbench_data --subset minival --dry-run

The dataset is public (Apache-2.0) and stays outside the repository. Only task
metadata and env file references are stored in the database; the files stay in
``--data-root`` and are materialized into the workspace by the runner.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from app.core.db import SessionLocal  # noqa: E402
from app.eval.goldens import GoldenPayload, delete_missing_goldens, upsert_golden  # noqa: E402


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _env_files(data_root: Path, task_id: str) -> list[dict[str, Any]]:
    config = _read_json(data_root / "data" / "tasks" / task_id / "config" / "env.json")
    files: list[dict[str, Any]] = []
    for item in config.get("env_files") or []:
        source = str(item.get("source") or "")
        relative = source[len("drbench/") :] if source.startswith("drbench/") else source
        files.append(
            {
                "source": relative,
                "destination": str(item.get("destination") or ""),
                "app": str(item.get("app") or ""),
                "qa_type": str(item.get("qa_type") or ""),
            }
        )
    return files


def _build_input(task: dict[str, Any], task_id: str, env_files: list[dict[str, Any]]) -> str:
    company = task.get("company_info") or {}
    persona = task.get("persona") or {}
    destinations = [item["destination"] for item in env_files if item.get("destination")]
    preview = "\n".join(f"- {path}" for path in destinations[:20])
    more = f"\n- ... ({len(destinations) - 20} more)" if len(destinations) > 20 else ""
    return (
        f"You are {persona.get('name') or 'the analyst'}, "
        f"{persona.get('role') or 'researcher'} at {company.get('name') or 'the company'}.\n"
        f"Company context: {company.get('description') or ''}\n\n"
        f"Research question: {task.get('dr_question') or ''}\n\n"
        f"Your enterprise environment is in `drbench/{task_id}/`. Read "
        f"`drbench/{task_id}/ENVIRONMENT.md` first; it lists every file and its original citation path.\n"
        f"Files include:\n{preview}{more}\n\n"
        "Use the files and public web sources. Every claim in the final report must cite an original "
        "path from ENVIRONMENT.md or a URL."
    )


def build_golden(data_root: Path, subset: str, task_id: str) -> GoldenPayload:
    task = _read_json(data_root / "data" / "tasks" / task_id / "config" / "task.json")
    eval_path = data_root / "data" / "tasks" / task_id / "config" / "eval.json"
    env_files = _env_files(data_root, task_id)
    company = task.get("company_info") or {}
    persona = task.get("persona") or {}
    metadata = {
        "benchmark": "drbench",
        "scorer": "drbench_score_report_v1",
        "dataset": "ServiceNow/drbench",
        "subset": subset,
        "task_id": task_id,
        "domain": persona.get("domain") or "",
        "company": company.get("name") or "",
        "question": task.get("dr_question") or "",
        "persona": {
            "name": persona.get("name") or "",
            "role": persona.get("role") or "",
            "department": persona.get("department") or "",
        },
        "env_files": env_files,
        "eval_hash": _sha256(eval_path),
    }
    return GoldenPayload(
        eval_case_id=f"drbench-{subset}-{task_id}",
        input=_build_input(task, task_id, env_files),
        expected_output=None,
        expected_tools=[],
        context=[],
        metadata=metadata,
        notes=f"DRBench {subset} task {task_id}; scored by the official DRBench scorer.",
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--subset", default="minival", choices=("minival", "val", "all"))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--delete-missing", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    metadata_path = args.data_root / "metadata" / f"{args.subset}.jsonl"
    if not metadata_path.is_file():
        metadata_path = args.data_root / "data" / "subsets" / f"{args.subset}.jsonl"
    if not metadata_path.is_file():
        sys.stderr.write(f"error: {metadata_path} does not exist\n")
        return 2
    rows = _read_jsonl(metadata_path)
    if args.limit is not None:
        rows = rows[: args.limit]
    goldens = [build_golden(args.data_root, args.subset, row["task_id"]) for row in rows]
    sys.stdout.write(f"selected {len(goldens)} DRBench {args.subset} task(s)\n")
    if args.dry_run:
        for golden in goldens:
            print(
                f"ok {golden.eval_case_id} domain={golden.metadata.get('domain')} "
                f"files={len(golden.metadata.get('env_files') or [])}"
            )
        return 0
    with SessionLocal() as db:
        for golden in goldens:
            upsert_golden(db, golden)
        removed = 0
        if args.delete_missing and goldens:
            removed = delete_missing_goldens(
                db,
                {golden.eval_case_id for golden in goldens},
                prefix=f"drbench-{args.subset}-",
            )
    sys.stdout.write(f"Upserted {len(goldens)} DRBench golden(s)")
    if args.delete_missing:
        sys.stdout.write(f", deleted {removed} missing")
    sys.stdout.write(".\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
