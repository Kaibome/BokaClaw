"""One-time migration from mem0's pgvector + SQLite history to workspace Markdown."""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .grammar import MemoryEntry, default_importance, parse_datetime
from .paths import is_uuid

logger = logging.getLogger(__name__)


@dataclass
class MigrationReport:
    source_rows: int = 0
    written: int = 0
    skipped: int = 0
    invalid: int = 0
    history_rows: int = 0
    backup_dir: Optional[str] = None
    mappings: Dict[str, str] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    dry_run: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "source_rows": self.source_rows,
            "written": self.written,
            "skipped": self.skipped,
            "invalid": self.invalid,
            "history_rows": self.history_rows,
            "backup_dir": self.backup_dir,
            "mappings": self.mappings,
            "errors": self.errors,
            "dry_run": self.dry_run,
        }


def source_dsn() -> str:
    return (
        f"host={os.environ.get('POSTGRES_HOST', 'postgres')} "
        f"port={os.environ.get('POSTGRES_PORT', '5432')} "
        f"dbname={os.environ.get('POSTGRES_DB', 'postgres')} "
        f"user={os.environ.get('POSTGRES_USER', 'postgres')} "
        f"password={os.environ.get('POSTGRES_PASSWORD', 'postgres')}"
    )


def _read_source(dsn: str) -> List[Dict[str, Any]]:
    import psycopg

    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT id, vector::text AS vector, payload FROM memories")
            columns = [item.name for item in cursor.description]
            return [dict(zip(columns, row, strict=False)) for row in cursor.fetchall()]


def _app_user_ids(session_factory: Any) -> List[str]:
    from sqlalchemy import text

    with session_factory() as session:
        rows = session.execute(text("SELECT id FROM users ORDER BY created_at")).all()
    return [str(row[0]) for row in rows]


def _map_user(raw: str, user_map: Dict[str, str], app_users: List[str], report: MigrationReport) -> str:
    text = str(raw or "").strip()
    if text in user_map:
        mapped = user_map[text]
        report.mappings[text] = mapped
        return mapped
    if is_uuid(text):
        return text
    if len(app_users) == 1:
        report.mappings[text] = app_users[0]
        return app_users[0]
    report.mappings[text] = text
    return text


def _infer_kind(data: str, payload: Dict[str, Any]) -> str:
    explicit = str(payload.get("kind") or "").strip()
    if explicit:
        return explicit
    if data.lower().startswith("research conclusion"):
        return "research_conclusion"
    return "operational"


def _parse_date(value: Any, default: datetime | None = None) -> Optional[datetime]:
    if value is None:
        return default
    return parse_datetime(value, default or datetime.now(timezone.utc))


def _build_entry(row: Dict[str, Any], user_id: str, ttl_days: int) -> MemoryEntry | None:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    data = str(payload.get("data") or payload.get("memory") or "").strip()
    if not data:
        return None
    kind = _infer_kind(data, payload)
    now = datetime.now(timezone.utc)
    created = _parse_date(payload.get("created_at"), now) or now
    updated = _parse_date(payload.get("updated_at"), created) or created
    if kind == "research_conclusion":
        captured = _parse_date(payload.get("captured_at"), created) or created
        expires = captured + timedelta(days=max(1, ttl_days))
    else:
        expires = _parse_date(payload.get("expiration_date"))
    metadata: Dict[str, Any] = {}
    for key in (
        "hash",
        "role",
        "attributed_to",
        "text_lemmatized",
        "conversation_id",
        "fetched_at",
        "question",
        "question_key",
        "captured_at",
    ):
        if payload.get(key) not in (None, ""):
            metadata[key] = payload[key]
    sources = payload.get("sources")
    if isinstance(sources, str):
        sources = [sources]
    if not isinstance(sources, list):
        sources = []
    return MemoryEntry(
        id=str(row.get("id")),
        kind=kind,
        content=data,
        importance=default_importance(kind),
        source="migration",
        trust="tool" if kind == "research_conclusion" else "owner",
        created=created,
        updated=updated,
        expires=expires,
        question_key=str(payload["question_key"]) if payload.get("question_key") else None,
        sources=[str(item) for item in sources if str(item).strip()],
        metadata=metadata,
        user_id=user_id,
        agent_id=str(payload["agent_id"]) if payload.get("agent_id") else None,
        run_id=str(payload["run_id"]) if payload.get("run_id") else None,
        conversation_id=str(payload["conversation_id"]) if payload.get("conversation_id") else None,
    )


def _read_history(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    connection = sqlite3.connect(str(path))
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT id, memory_id, old_memory, new_memory, event, created_at, updated_at, "
            "is_deleted, actor_id, role FROM history ORDER BY created_at"
        ).fetchall()
    finally:
        connection.close()
    output: List[Dict[str, Any]] = []
    for row in rows:
        created = _parse_date(row["created_at"], datetime.now(timezone.utc))
        output.append(
            {
                "id": str(row["id"]),
                "memory_id": str(row["memory_id"]),
                "user_id": None,
                "event": str(row["event"] or "ADD"),
                "old_memory": row["old_memory"],
                "new_memory": row["new_memory"],
                "created_at": created,
                "updated_at": _parse_date(row["updated_at"]) if row["updated_at"] else None,
                "is_deleted": bool(row["is_deleted"]),
                "actor_id": row["actor_id"],
                "role": row["role"],
                "metadata": json.dumps({"migrated": True}),
            }
        )
    return output


def _backup(rows: Iterable[Dict[str, Any]], history_db: Path | None, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "memories.json").write_text(
        json.dumps(list(rows), ensure_ascii=False, default=str, indent=2),
        encoding="utf-8",
    )
    if history_db is not None and history_db.exists():
        shutil.copy2(history_db, directory / "history.db")
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source": source_dsn().replace(f"password={os.environ.get('POSTGRES_PASSWORD', 'postgres')}", "password=[redacted]"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return directory


def migrate(
    service: Any,
    *,
    dsn: str | None = None,
    history_db: Path | str | None = None,
    backup_dir: Path | str | None = None,
    user_map: Dict[str, str] | None = None,
    dry_run: bool = False,
    verify: bool = False,
) -> MigrationReport:
    """Migrate mem0 rows into the Markdown workspace. Idempotent by row id."""

    report = MigrationReport(dry_run=dry_run)
    user_map = user_map or {}
    ttl_days = int(os.environ.get("AGENT_RESEARCH_MEMORY_TTL_DAYS", "30"))
    rows = _read_source(dsn or source_dsn())
    report.source_rows = len(rows)
    app_users = _app_user_ids(service.repository._session_factory)
    history_path = Path(history_db) if history_db else None

    if not dry_run:
        directory = Path(backup_dir) if backup_dir else service.settings.root / ".backup" / datetime.now(
            timezone.utc
        ).strftime("%Y%m%dT%H%M%SZ")
        report.backup_dir = str(_backup(rows, history_path, directory))

    for row in rows:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        user_id = _map_user(str(payload.get("user_id") or ""), user_map, app_users, report)
        entry = _build_entry(row, user_id, ttl_days)
        if entry is None:
            report.invalid += 1
            continue
        existing = service.repository.get_entry(entry.id, show_expired=True)
        if existing is not None:
            report.skipped += 1
            continue
        if dry_run:
            report.written += 1
            continue
        try:
            service.store.append_entry(user_id, entry)
            service.repository.upsert_entries([entry])
            service.repository.record_history(
                entry.id,
                user_id,
                "ADD",
                None,
                entry.content,
                metadata={"migrated": True},
            )
            report.written += 1
        except Exception as exc:
            report.errors.append(f"{entry.id}: {type(exc).__name__}: {exc}")
            logger.warning("Could not migrate %s", entry.id, exc_info=True)

    if history_path is not None:
        history_rows = _read_history(history_path)
        if history_rows and not dry_run:
            report.history_rows = service.repository.import_history(history_rows)
        else:
            report.history_rows = len(history_rows)

    if verify and not dry_run:
        indexed = service.repository.count()
        report.errors.append(f"verify: index rows={indexed}, written={report.written}")
    return report
