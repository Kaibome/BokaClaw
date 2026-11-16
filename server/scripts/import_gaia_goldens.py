"""Import GAIA validation rows into ``eval_goldens``.

Run inside the server container:

    docker cp gaia_validation.jsonl bokaclaw-server:/tmp/gaia_validation.jsonl
    docker exec bokaclaw-server python scripts/import_gaia_goldens.py \
        --file /tmp/gaia_validation.jsonl --limit 20 --dry-run

The input is the local JSON/JSONL export of the gated HF dataset (or a Parquet
file when ``pyarrow`` is available). The dataset itself is never downloaded or
committed by this script.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from app.core.db import SessionLocal  # noqa: E402
from app.eval.gaia import (  # noqa: E402
    GaiaRecord,
    load_gaia_rows,
    record_to_golden,
    select_gaia_records,
)
from app.eval.goldens import delete_missing_goldens, upsert_golden  # noqa: E402


def _parse_int_list(value: str | None) -> list[int] | None:
    if not value:
        return None
    return [int(part.strip()) for part in value.split(",") if part.strip()]


def _parse_str_list(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [part.strip() for part in value.split(",") if part.strip()]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True, help="GAIA JSON, JSONL or Parquet file.")
    parser.add_argument("--split", default="validation", help="Split name stored in case ids/metadata.")
    parser.add_argument("--levels", help="Comma-separated levels, e.g. 1,2,3.")
    parser.add_argument("--task-ids", help="Comma-separated GAIA task ids.")
    parser.add_argument("--limit", type=int, help="Maximum number of cases to import.")
    parser.add_argument(
        "--sample",
        choices=("stratified", "first"),
        default="stratified",
        help="Selection strategy when --limit is set.",
    )
    parser.add_argument(
        "--include-unsupported",
        action="store_true",
        help="Also import image/xlsx/other attachment cases.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Validate and print without writing.")
    parser.add_argument(
        "--delete-missing",
        action="store_true",
        help="Delete gaia-* cases that are absent from this import.",
    )
    return parser.parse_args()


def _summary(records: list[GaiaRecord]) -> str:
    levels = Counter(record.level for record in records)
    kinds = Counter(
        attachment.kind for record in records for attachment in record.attachments
    ) or Counter({"none": len(records)})
    level_text = ", ".join(f"L{level}={levels[level]}" for level in sorted(levels))
    kind_text = ", ".join(f"{kind}={kinds[kind]}" for kind in sorted(kinds))
    return f"{len(records)} case(s); {level_text}; attachments: {kind_text}"


def main() -> int:
    args = _parse_args()
    if args.delete_missing and args.limit is not None:
        sys.stderr.write("error: --delete-missing cannot be combined with --limit\n")
        return 2
    path = Path(args.file)
    if not path.is_file():
        sys.stderr.write(f"error: {path} does not exist\n")
        return 2
    try:
        rows = load_gaia_rows(path)
        selection = select_gaia_records(
            rows,
            split=args.split,
            levels=_parse_int_list(args.levels),
            task_ids=_parse_str_list(args.task_ids),
            limit=args.limit,
            sample=args.sample,
            include_unsupported=args.include_unsupported,
        )
        goldens = [record_to_golden(record, split=args.split) for record in selection.records]
    except Exception as exc:  # noqa: BLE001 - CLI must report loader/validation errors
        sys.stderr.write(f"error: {exc}\n")
        return 2

    if selection.skipped_unsupported:
        sys.stdout.write(f"skipped {selection.skipped_unsupported} unsupported attachment case(s)\n")
    sys.stdout.write(f"selected {_summary(selection.records)}\n")

    if args.dry_run:
        for golden in goldens:
            sys.stdout.write(f"ok {golden.eval_case_id} (level {golden.metadata.get('level')})\n")
        return 0

    with SessionLocal() as db:
        for golden in goldens:
            upsert_golden(db, golden)
        removed = 0
        if args.delete_missing and goldens:
            removed = delete_missing_goldens(db, {golden.eval_case_id for golden in goldens}, prefix="gaia-")

    sys.stdout.write(f"Upserted {len(goldens)} GAIA golden(s)")
    if args.delete_missing:
        sys.stdout.write(f", deleted {removed} missing")
    sys.stdout.write(".\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
