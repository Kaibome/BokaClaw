"""SQLAlchemy tables for the rebuildable memory index."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import UserDefinedType

from app.core.db import Base
from app.memory.config import STORAGE_DIMS


class HalfVec(UserDefinedType):
    """Minimal ``halfvec(N)`` type so the models do not require a driver adapter."""

    cache_ok = True

    def __init__(self, dims: int) -> None:
        self.dims = dims

    def get_col_spec(self, **kw: Any) -> str:
        return f"halfvec({self.dims})"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MemoryIndexEntry(Base):
    __tablename__ = "memory_index_entries"
    __table_args__ = (
        Index("ix_memory_entries_user_kind", "user_id", "kind"),
        Index("ix_memory_entries_user_question", "user_id", "question_key"),
        Index("ix_memory_entries_user_updated", "user_id", "updated_at"),
        Index("ix_memory_entries_expires", "expires_at", postgresql_where=text("expires_at IS NOT NULL")),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    agent_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="operational")
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    trust: Mapped[str] = mapped_column(String(32), nullable=False, default="owner")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    search_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    importance: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    project: Mapped[str | None] = mapped_column(Text, nullable=True)
    question_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    meta: Mapped[dict] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    embedding_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    embedding_dims: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embedding_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    embedding_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    embedding: Mapped[Any] = mapped_column(HalfVec(STORAGE_DIMS), nullable=True)
    indexed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    search_tsv: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('simple', coalesce(search_text, content))", persisted=True),
    )


class MemoryIndexFile(Base):
    __tablename__ = "memory_index_files"

    user_id: Mapped[str] = mapped_column(Text, primary_key=True)
    path: Mapped[str] = mapped_column(Text, primary_key=True)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    mtime: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    indexed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


class MemoryHistory(Base):
    __tablename__ = "memory_history"
    __table_args__ = (Index("ix_memory_history_memory_created", "memory_id", "created_at"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    memory_id: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    event: Mapped[str] = mapped_column(String(32), nullable=False, default="ADD")
    old_memory: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_memory: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    actor_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    role: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta: Mapped[dict] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )


class MemoryEmbeddingCache(Base):
    __tablename__ = "memory_embedding_cache"

    model: Mapped[str] = mapped_column(Text, primary_key=True)
    dims: Mapped[int] = mapped_column(Integer, primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    embedding: Mapped[Any] = mapped_column(HalfVec(STORAGE_DIMS), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


class MemoryIndexState(Base):
    __tablename__ = "memory_index_state"

    user_id: Mapped[str] = mapped_column(Text, primary_key=True)
    dirty: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    reembed_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


def new_history_id() -> str:
    return str(uuid.uuid4())
