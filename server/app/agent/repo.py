"""Postgres persistence for agent runs and their replayable event log.

Every method opens a short-lived session: run workers, subscriptions, and HTTP
handlers must not hold a pooled connection for the lifetime of a long stream.
The manager serializes writes per run, so ``append_event`` only needs a row
lock plus its ``last_seq`` check to stay monotonic.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from app.core.db import SessionLocal
from app.models import (
    TERMINAL_RUN_STATUSES,
    AgentRun,
    AgentRunEvent,
    Conversation,
    ConversationMessage,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class RunContext:
    run: AgentRun
    conversation: Conversation | None
    user_message: ConversationMessage | None
    assistant_message: ConversationMessage | None


class RunRepository:
    """SQLAlchemy implementation used by the manager and the HTTP layer."""

    def get_run(self, run_id: uuid.UUID | str, owner_id: uuid.UUID | None = None) -> AgentRun | None:
        with SessionLocal() as db:
            query = select(AgentRun).where(AgentRun.id == _uuid(run_id))
            if owner_id is not None:
                query = query.where(AgentRun.created_by == owner_id)
            return db.scalar(query)

    def get_active_run(self, conversation_id: uuid.UUID | str) -> AgentRun | None:
        with SessionLocal() as db:
            return db.scalar(
                select(AgentRun)
                .where(
                    AgentRun.conversation_id == _uuid(conversation_id),
                    AgentRun.status.in_(("queued", "running")),
                )
                .order_by(AgentRun.created_at.desc())
                .limit(1)
            )

    def get_context(self, run_id: uuid.UUID | str) -> RunContext | None:
        with SessionLocal() as db:
            run = db.get(AgentRun, _uuid(run_id))
            if run is None:
                return None
            conversation = db.get(Conversation, run.conversation_id)
            user_message = db.get(ConversationMessage, run.user_message_id) if run.user_message_id else None
            assistant_message = (
                db.get(ConversationMessage, run.assistant_message_id) if run.assistant_message_id else None
            )
            return RunContext(
                run=run,
                conversation=conversation,
                user_message=user_message,
                assistant_message=assistant_message,
            )

    def list_running(self) -> list[AgentRun]:
        with SessionLocal() as db:
            return list(db.scalars(select(AgentRun).where(AgentRun.status == "running")).all())

    def list_queued(self) -> list[AgentRun]:
        with SessionLocal() as db:
            return list(db.scalars(select(AgentRun).where(AgentRun.status == "queued")).all())

    def claim(self, run_id: uuid.UUID, worker_id: str, now: datetime | None = None) -> bool:
        now = now or utcnow()
        with SessionLocal() as db:
            result = db.execute(
                update(AgentRun)
                .where(AgentRun.id == run_id, AgentRun.status == "queued")
                .values(
                    status="running",
                    started_at=now,
                    heartbeat_at=now,
                    worker_id=worker_id,
                    updated_at=now,
                )
            )
            db.commit()
            return result.rowcount > 0

    def heartbeat(self, run_id: uuid.UUID, worker_id: str, now: datetime | None = None) -> None:
        now = now or utcnow()
        with SessionLocal() as db:
            db.execute(
                update(AgentRun)
                .where(AgentRun.id == run_id, AgentRun.status == "running", AgentRun.worker_id == worker_id)
                .values(heartbeat_at=now, updated_at=now)
            )
            db.commit()

    def request_cancel(self, run_id: uuid.UUID, now: datetime | None = None) -> None:
        now = now or utcnow()
        with SessionLocal() as db:
            db.execute(
                update(AgentRun)
                .where(
                    AgentRun.id == run_id,
                    AgentRun.status.in_(("queued", "running")),
                    AgentRun.cancel_requested_at.is_(None),
                )
                .values(cancel_requested_at=now, updated_at=now)
            )
            db.commit()

    def cancel_queued(self, run_id: uuid.UUID, now: datetime | None = None) -> bool:
        now = now or utcnow()
        with SessionLocal() as db:
            result = db.execute(
                update(AgentRun)
                .where(AgentRun.id == run_id, AgentRun.status == "queued")
                .values(
                    status="cancelled",
                    error_code="agent_cancelled",
                    error_detail="Agent run was cancelled before it started.",
                    cancel_requested_at=func.coalesce(AgentRun.cancel_requested_at, now),
                    finished_at=now,
                    updated_at=now,
                )
            )
            db.commit()
            return result.rowcount > 0

    def finish(
        self,
        run_id: uuid.UUID,
        *,
        status: str,
        error_code: str | None = None,
        error_detail: str | None = None,
        partial_content: str | None = None,
        partial_metadata: dict[str, Any] | None = None,
        finished_at: datetime | None = None,
    ) -> bool:
        """Move an active run to a terminal state. Returns False if another
        path already finalized it."""
        if status not in TERMINAL_RUN_STATUSES:
            raise ValueError(f"Invalid terminal run status {status!r}")
        now = finished_at or utcnow()
        values: dict[str, Any] = {
            "status": status,
            "error_code": error_code,
            "error_detail": error_detail,
            "finished_at": now,
            "updated_at": now,
        }
        if partial_content is not None:
            values["partial_content"] = partial_content
        if partial_metadata is not None:
            values["partial_metadata"] = partial_metadata
        with SessionLocal() as db:
            result = db.execute(
                update(AgentRun)
                .where(AgentRun.id == run_id, AgentRun.status.in_(("queued", "running")))
                .values(**values)
            )
            db.commit()
            return result.rowcount > 0

    def append_event(
        self,
        run_id: uuid.UUID,
        seq: int,
        event_type: str,
        payload: dict[str, Any],
        *,
        partial_content: str | None = None,
        partial_metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Append one event and advance ``last_seq`` in the same transaction.

        Returns False when the sequence was already written or the run vanished
        (for example because its conversation was deleted).
        """
        with SessionLocal() as db:
            run = db.get(AgentRun, run_id, with_for_update=True)
            if run is None or run.last_seq >= seq:
                db.rollback()
                return False
            run.last_seq = seq
            if partial_content is not None:
                run.partial_content = partial_content
            if partial_metadata is not None:
                run.partial_metadata = partial_metadata
            run.updated_at = utcnow()
            db.add(
                AgentRunEvent(
                    run_id=run_id,
                    seq=seq,
                    event_type=event_type,
                    payload=payload,
                )
            )
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                return False
            return True

    def oldest_seq(self, run_id: uuid.UUID) -> int | None:
        with SessionLocal() as db:
            return db.scalar(select(func.min(AgentRunEvent.seq)).where(AgentRunEvent.run_id == run_id))

    def list_events(self, run_id: uuid.UUID, after_seq: int = 0) -> list[AgentRunEvent]:
        with SessionLocal() as db:
            return list(
                db.scalars(
                    select(AgentRunEvent)
                    .where(AgentRunEvent.run_id == run_id, AgentRunEvent.seq > after_seq)
                    .order_by(AgentRunEvent.seq.asc())
                ).all()
            )

    def has_event(self, run_id: uuid.UUID, event_types: tuple[str, ...]) -> bool:
        with SessionLocal() as db:
            return (
                db.scalar(
                    select(func.count(AgentRunEvent.id)).where(
                        AgentRunEvent.run_id == run_id,
                        AgentRunEvent.event_type.in_(event_types),
                    )
                )
                or 0
            ) > 0

    def get_message(self, message_id: uuid.UUID | None) -> ConversationMessage | None:
        if message_id is None:
            return None
        with SessionLocal() as db:
            return db.get(ConversationMessage, _uuid(message_id))

    def insert_assistant_message(
        self,
        *,
        conversation_id: uuid.UUID,
        owner_id: uuid.UUID,
        message_id: uuid.UUID,
        content: str,
        status: str,
        metadata: dict[str, Any],
    ) -> ConversationMessage | None:
        """Idempotently write the pre-generated assistant message.

        Returns ``None`` when the conversation was deleted (the run row is gone
        too through the cascade, so the caller should stop quietly).
        """
        with SessionLocal() as db:
            existing = db.get(ConversationMessage, message_id)
            if existing is not None:
                return existing
            conversation = db.scalar(
                select(Conversation).where(
                    Conversation.id == conversation_id,
                    Conversation.created_by == owner_id,
                )
            )
            if conversation is None:
                return None
            message = ConversationMessage(
                id=message_id,
                conversation_id=conversation_id,
                role="assistant",
                content=content,
                status=status,
                meta=metadata,
            )
            conversation.updated_at = utcnow()
            db.add(message)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                return db.get(ConversationMessage, message_id)
            return message

    def prune_events(self, *, retention_days: int) -> int:
        cutoff = utcnow() - timedelta(days=retention_days)
        with SessionLocal() as db:
            result = db.execute(
                delete(AgentRunEvent).where(
                    AgentRunEvent.run_id.in_(
                        select(AgentRun.id).where(
                            AgentRun.status.in_(TERMINAL_RUN_STATUSES),
                            AgentRun.finished_at.is_not(None),
                            AgentRun.finished_at < cutoff,
                        )
                    )
                )
            )
            db.commit()
            return result.rowcount or 0

    def prune_runs(self, *, retention_days: int) -> int:
        cutoff = utcnow() - timedelta(days=retention_days)
        with SessionLocal() as db:
            result = db.execute(
                delete(AgentRun).where(
                    AgentRun.status.in_(TERMINAL_RUN_STATUSES),
                    AgentRun.finished_at.is_not(None),
                    AgentRun.finished_at < cutoff,
                )
            )
            db.commit()
            return result.rowcount or 0


def _uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
