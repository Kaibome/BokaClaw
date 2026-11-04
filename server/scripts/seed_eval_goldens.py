"""Load evaluation goldens from JSON into ``eval_goldens``.

Run inside the server container:

    docker cp tests/agent_eval/goldens.minimax.json bokaclaw-server:/tmp/goldens.json
    docker exec bokaclaw-server python scripts/seed_eval_goldens.py --file /tmp/goldens.json

The file is a JSON array; each item needs ``eval_case_id`` and ``input``.
Existing rows are updated in place. ``--delete-missing`` removes cases that are
not present in the file (never deletes when the file is empty).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from app.core.db import SessionLocal  # noqa: E402
from app.eval.goldens import delete_missing_goldens, upsert_golden, validate_goldens_file  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True, help="JSON file, or - for stdin.")
    parser.add_argument("--dry-run", action="store_true", help="Validate and print without writing.")
    parser.add_argument(
        "--delete-missing",
        action="store_true",
        help="Delete cases that are not present in the file (skipped for an empty file).",
    )
    return parser.parse_args()


def _read_payload(path: str) -> object:
    if path == "-":
        return json.load(sys.stdin)
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main() -> int:
    args = _parse_args()
    try:
        payload = _read_payload(args.file)
        goldens = validate_goldens_file(payload)
    except Exception as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2

    if args.dry_run:
        for golden in goldens:
            sys.stdout.write(f"ok {golden.eval_case_id} ({len(golden.expected_tools)} expected tools)\n")
        return 0

    with SessionLocal() as db:
        for golden in goldens:
            upsert_golden(db, golden)
        removed = 0
        if args.delete_missing and goldens:
            removed = delete_missing_goldens(db, {golden.eval_case_id for golden in goldens})

    sys.stdout.write(f"Upserted {len(goldens)} golden(s)")
    if args.delete_missing:
        sys.stdout.write(f", deleted {removed} missing")
    sys.stdout.write(".\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
