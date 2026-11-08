"""Maintenance CLI: ``python -m app.memory.cli reconcile|prune``."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from typing import Sequence

from .paths import scan_user_roots


def _service():
    from app.core.server_state import get_memory_instance

    return get_memory_instance()


def _reconcile(args: argparse.Namespace) -> int:
    service = _service()
    if args.user:
        stats = [service.reconcile_user(args.user)]
    else:
        stats = service.reconcile_all()
    print(json.dumps([asdict(item) for item in stats], ensure_ascii=False, default=str))
    return 0 if all(item.error is None for item in stats) else 1


def _prune(args: argparse.Namespace) -> int:
    service = _service()
    if args.user:
        users = [args.user]
    else:
        users = [user_id for user_id, _ in scan_user_roots(service.settings)]
    removed = 0
    for user_id in users:
        removed += service.store.prune_daily(user_id, args.retention_days)
    print(json.dumps({"removed": removed, "users": len(users)}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.memory.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    reconcile = sub.add_parser("reconcile", help="re-parse Markdown files into the index")
    reconcile.add_argument("--user", help="only this user id")
    reconcile.add_argument("--all", action="store_true", help="all users (default)")
    reconcile.set_defaults(func=_reconcile)

    reindex = sub.add_parser("reindex", help="alias for reconcile")
    reindex.add_argument("--user", help="only this user id")
    reindex.add_argument("--all", action="store_true")
    reindex.set_defaults(func=_reconcile)

    prune = sub.add_parser("prune", help="delete episodic files older than a retention window")
    prune.add_argument("--user", help="only this user id")
    prune.add_argument("--retention-days", type=int, default=180)
    prune.set_defaults(func=_prune)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    # Importing app.main runs the same initialize_state() the API uses.
    import app.main  # noqa: F401

    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
