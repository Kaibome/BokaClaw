"""Reconcile Markdown files with the Postgres index."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .paths import memory_files, scan_user_roots

logger = logging.getLogger(__name__)


@dataclass
class ReconcileStats:
    user_id: str
    entries: int = 0
    removed: int = 0
    skipped: bool = False
    error: str | None = None


def _file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def reconcile_user(service, user_id: str) -> ReconcileStats:
    """Re-parse one user's files and make the index match them."""

    stats = ReconcileStats(user_id=user_id)
    try:
        with service.repository.advisory_lock(user_id) as acquired:
            if not acquired:
                stats.skipped = True
                return stats
            root = service.store.existing_root(user_id)
            if not root.exists():
                service.repository.delete_user(user_id)
                service.repository.mark_reconciled(user_id)
                return stats

            entries = service.store.read_entries(user_id)
            for entry in entries:
                entry.user_id = user_id
            service.repository.upsert_entries(entries)

            wanted = {entry.id for entry in entries}
            existing = set(service.repository.entry_ids(user_id))
            stale = sorted(existing - wanted)
            if stale:
                stats.removed = service.repository.delete_entries(stale)

            for path in memory_files(root):
                try:
                    stat = path.stat()
                    service.repository.upsert_file_state(
                        user_id,
                        str(path),
                        _file_sha(path),
                        int(stat.st_size),
                        datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                    )
                except OSError:
                    logger.warning("Could not stat %s", path, exc_info=True)

            stats.entries = len(entries)
            service.repository.mark_reconciled(user_id)
            return stats
    except Exception as exc:
        logger.warning("Reconcile failed for %s", user_id, exc_info=True)
        stats.error = f"{type(exc).__name__}: {exc}"
        service.repository.set_dirty(user_id, True, stats.error)
        return stats


def reconcile_all(service) -> list[ReconcileStats]:
    stats: list[ReconcileStats] = []
    seen: set[str] = set()
    for user_id, _ in list(scan_user_roots(service.settings)):
        if user_id in seen:
            continue
        seen.add(user_id)
        stats.append(reconcile_user(service, user_id))
    for user_id in service.repository.dirty_users():
        if user_id in seen:
            continue
        seen.add(user_id)
        stats.append(reconcile_user(service, user_id))
    return stats
