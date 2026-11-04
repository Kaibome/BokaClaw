"""Postgres persistence for the eval queue and results.

Mirrors the lease semantics used by ``app.agent.repo.RunRepository``: short
sessions, conditional updates, and a claim that can be retried after a worker
disappears. This module is safe to import in the server process.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from app.core.db import SessionLocal
from app.models import EVAL_TERMINAL_STATUSES, AgentRun, AgentRunSpan, EvalRun


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


class EvalRepository:
    def create(
        self,
        *,
        run_id: uuid.UUID,
        created_by: uuid.UUID | None,
        eval_case_id: str,
        golden_snapshot: dict[str, Any],
        golden_hash: str,
        use_llm_judge: bool,
    ) -> tuple[EvalRun, bool]:
        """Insert a queued eval.

        Returns ``(row, duplicate)``. ``duplicate`` is True when the partial
        unique index rejected a second active eval for the same target; the
        returned row is then the existing active one.
        """
        now = utcnow()
        with SessionLocal() as db:
            row = EvalRun(
                run_id=run_id,
                created_by=created_by,
                eval_case_id=eval_case_id,
                golden_snapshot=golden_snapshot,
                golden_hash=golden_hash,
                use_llm_judge=use_llm_judge,
                status="queued",
                created_at=now,
                updated_at=now,
            )
            db.add(row)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                existing = db.scalar(
                    select(EvalRun)
                    .where(
                        EvalRun.run_id == run_id,
                        EvalRun.eval_case_id == eval_case_id,
                        EvalRun.use_llm_judge == use_llm_judge,
                        EvalRun.status.in_(("queued", "running")),
                    )
                    .order_by(EvalRun.created_at.desc())
                    .limit(1)
                )
                return existing, True
            return row, False

    def get(self, eval_id: uuid.UUID | str) -> EvalRun | None:
        with SessionLocal() as db:
            return db.get(EvalRun, _uuid(eval_id))

    def get_with_run_status(self, eval_id: uuid.UUID | str) -> tuple[EvalRun, str] | None:
        with SessionLocal() as db:
            row = db.execute(
                select(EvalRun, AgentRun.status)
                .join(AgentRun, AgentRun.id == EvalRun.run_id)
                .where(EvalRun.id == _uuid(eval_id))
            ).first()
            return (row[0], row[1]) if row is not None else None

    def list(
        self,
        *,
        limit: int = 50,
        run_id: uuid.UUID | None = None,
        status: str | None = None,
        eval_case_id: str | None = None,
    ) -> list[tuple[EvalRun, str]]:
        stmt = select(EvalRun, AgentRun.status).join(AgentRun, AgentRun.id == EvalRun.run_id)
        if run_id is not None:
            stmt = stmt.where(EvalRun.run_id == run_id)
        if status is not None:
            stmt = stmt.where(EvalRun.status == status)
        if eval_case_id is not None:
            stmt = stmt.where(EvalRun.eval_case_id == eval_case_id)
        stmt = stmt.order_by(EvalRun.created_at.desc(), EvalRun.id.desc()).limit(limit)
        with SessionLocal() as db:
            return [(row[0], row[1]) for row in db.execute(stmt).all()]

    def claim(self, worker_id: str) -> EvalRun | None:
        now = utcnow()
        next_row = (
            select(EvalRun.id)
            .where(EvalRun.status == "queued", EvalRun.cancel_requested_at.is_(None))
            .order_by(EvalRun.created_at.asc())
            .with_for_update(skip_locked=True)
            .limit(1)
            .cte("next_eval")
        )
        stmt = (
            update(EvalRun)
            .where(EvalRun.id == next_row.c.id)
            .values(
                status="running",
                worker_id=worker_id,
                attempt=EvalRun.attempt + 1,
                started_at=now,
                heartbeat_at=now,
                updated_at=now,
            )
            .returning(EvalRun.id)
        )
        with SessionLocal() as db:
            eval_id = db.execute(stmt).scalar_one_or_none()
            db.commit()
            if eval_id is None:
                return None
            return db.get(EvalRun, eval_id)

    def heartbeat(self, eval_id: uuid.UUID, worker_id: str) -> bool:
        owned, _ = self.heartbeat_state(eval_id, worker_id)
        return owned

    def heartbeat_state(self, eval_id: uuid.UUID, worker_id: str) -> tuple[bool, bool]:
        """Refresh the lease and report ``(still_owner, cancel_requested)``."""
        now = utcnow()
        with SessionLocal() as db:
            row = db.execute(
                update(EvalRun)
                .where(
                    EvalRun.id == eval_id,
                    EvalRun.status == "running",
                    EvalRun.worker_id == worker_id,
                )
                .values(heartbeat_at=now, updated_at=now)
                .returning(EvalRun.cancel_requested_at)
            ).first()
            db.commit()
            if row is None:
                return False, False
            return True, row[0] is not None

    def finish(
        self,
        eval_id: uuid.UUID,
        worker_id: str,
        *,
        status: str,
        metrics: list[dict[str, Any]] | None = None,
        warnings: list[str] | None = None,
        raw_result: dict[str, Any] | None = None,
        overall_success: bool | None = None,
        judge_model: str | None = None,
        token_cost: float | None = None,
        duration_ms: int | None = None,
        error_code: str | None = None,
        error_detail: str | None = None,
    ) -> bool:
        if status not in EVAL_TERMINAL_STATUSES:
            raise ValueError(f"Invalid terminal eval status {status!r}")
        now = utcnow()
        values: dict[str, Any] = {
            "status": status,
            "finished_at": now,
            "heartbeat_at": now,
            "updated_at": now,
            "error_code": error_code,
            "error_detail": _truncate(error_detail),
        }
        if metrics is not None:
            values["metrics"] = metrics
        if warnings is not None:
            values["warnings"] = warnings
        if raw_result is not None:
            values["raw_result"] = raw_result
        if overall_success is not None:
            values["overall_success"] = overall_success
        if judge_model is not None:
            values["judge_model"] = judge_model
        if token_cost is not None:
            values["token_cost"] = token_cost
        if duration_ms is not None:
            values["duration_ms"] = duration_ms
        with SessionLocal() as db:
            result = db.execute(
                update(EvalRun)
                .where(
                    EvalRun.id == eval_id,
                    EvalRun.status == "running",
                    EvalRun.worker_id == worker_id,
                )
                .values(**values)
            )
            db.commit()
            return result.rowcount > 0

    def cancel(self, eval_id: uuid.UUID | str) -> tuple[EvalRun | None, str]:
        """Returns ``(row, outcome)`` with outcome ``not_found``/``cancelled``/
        ``cancelling``/``terminal``."""
        now = utcnow()
        with SessionLocal() as db:
            row = db.get(EvalRun, _uuid(eval_id))
            if row is None:
                return None, "not_found"
            if row.status == "queued":
                row.status = "cancelled"
                row.cancel_requested_at = row.cancel_requested_at or now
                row.finished_at = now
                row.updated_at = now
                db.commit()
                return row, "cancelled"
            if row.status == "running":
                if row.cancel_requested_at is None:
                    row.cancel_requested_at = now
                    row.updated_at = now
                    db.commit()
                return row, "cancelling"
            return row, "terminal"

    def recover_stale(self, *, lease_seconds: int, max_attempts: int) -> dict[str, int]:
        """Requeue or fail runs whose worker stopped heartbeating."""
        cutoff = utcnow() - timedelta(seconds=lease_seconds)
        stale = func.coalesce(EvalRun.heartbeat_at, EvalRun.started_at, EvalRun.created_at) < cutoff
        counts = {"cancelled": 0, "requeued": 0, "failed": 0}
        with SessionLocal() as db:
            cancelled = db.execute(
                update(EvalRun)
                .where(
                    EvalRun.status == "running",
                    stale,
                    EvalRun.cancel_requested_at.is_not(None),
                )
                .values(
                    status="cancelled",
                    error_code="eval_cancelled",
                    error_detail="Cancellation was requested before the worker stopped.",
                    finished_at=utcnow(),
                    updated_at=utcnow(),
                )
            )
            counts["cancelled"] = cancelled.rowcount or 0

            requeued = db.execute(
                update(EvalRun)
                .where(
                    EvalRun.status == "running",
                    stale,
                    EvalRun.cancel_requested_at.is_(None),
                    EvalRun.attempt < max_attempts,
                )
                .values(
                    status="queued",
                    worker_id=None,
                    heartbeat_at=None,
                    updated_at=utcnow(),
                )
            )
            counts["requeued"] = requeued.rowcount or 0

            failed = db.execute(
                update(EvalRun)
                .where(
                    EvalRun.status == "running",
                    stale,
                    EvalRun.cancel_requested_at.is_(None),
                    EvalRun.attempt >= max_attempts,
                )
                .values(
                    status="failed",
                    error_code="eval_lease_expired",
                    error_detail="Worker heartbeat expired; max attempts reached.",
                    finished_at=utcnow(),
                    updated_at=utcnow(),
                )
            )
            counts["failed"] = failed.rowcount or 0
            db.commit()
        return counts

    def get_run(self, run_id: uuid.UUID | str) -> AgentRun | None:
        with SessionLocal() as db:
            return db.get(AgentRun, _uuid(run_id))

    def list_spans(self, run_id: uuid.UUID | str) -> list[AgentRunSpan]:
        with SessionLocal() as db:
            return list(
                db.scalars(
                    select(AgentRunSpan)
                    .where(AgentRunSpan.run_id == _uuid(run_id))
                    .order_by(AgentRunSpan.started_at.asc(), AgentRunSpan.id.asc())
                ).all()
            )

    def prune_finished(self, *, retention_days: int) -> int:
        cutoff = utcnow() - timedelta(days=retention_days)
        with SessionLocal() as db:
            result = db.execute(
                delete(EvalRun).where(
                    EvalRun.status.in_(EVAL_TERMINAL_STATUSES),
                    EvalRun.finished_at.is_not(None),
                    EvalRun.finished_at < cutoff,
                )
            )
            db.commit()
            return result.rowcount or 0


def _truncate(value: str | None, limit: int = 2000) -> str | None:
    if value is None:
        return None
    return value if len(value) <= limit else value[:limit] + "…"
