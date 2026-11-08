#!/usr/bin/env python3
"""Migrate mem0 pgvector memories and SQLite history into workspace Markdown.

Run with ``MEMORY_BACKEND=markdown`` so ``get_memory_instance()`` returns the
workspace backend. The old tables are only read; the new index is written.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List


def _user_map(values: List[str]) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for value in values or []:
        if "=" not in value:
            raise SystemExit(f"--user-map must be legacy=uuid, got {value!r}")
        legacy, target = value.split("=", 1)
        mapping[legacy.strip()] = target.strip()
    return mapping


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report what would be written")
    parser.add_argument("--verify", action="store_true", help="print post-migration counts")
    parser.add_argument("--history-db", default="/app/history/history.db")
    parser.add_argument("--backup-dir", default=None)
    parser.add_argument("--source-dsn", default=None)
    parser.add_argument("--user-map", action="append", default=[], metavar="LEGACY=UUID")
    parser.add_argument("--reuse-embeddings", action="store_true", help="accepted for compatibility")
    parser.add_argument("--reembed", action="store_true", help="recompute all embeddings (default)")
    args = parser.parse_args(argv)

    import app.main  # noqa: F401  # initializes the configured memory backend
    from app.core.server_state import get_memory_instance
    from app.memory.migration import migrate

    service = get_memory_instance()
    if not hasattr(service, "store"):
        raise SystemExit("MEMORY_BACKEND=markdown is required for this migration")

    report = migrate(
        service,
        dsn=args.source_dsn,
        history_db=Path(args.history_db) if args.history_db else None,
        backup_dir=Path(args.backup_dir) if args.backup_dir else None,
        user_map=_user_map(args.user_map),
        dry_run=args.dry_run,
        verify=args.verify,
    )
    print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2, default=str))
    return 0 if not report.errors else 1


if __name__ == "__main__":
    sys.exit(main())
