"""Postgres-backed session persistence.

Two tables are created lazily with ``CREATE TABLE IF NOT EXISTS`` so the SDK
can share a database with the rest of the BokaClaw application tables without
a migration framework. The Postgres driver is optional; install
``bokaclaw-agent[postgres]``.
"""

from __future__ import annotations

import json
import os
import re
import threading
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..core.exceptions import ConfigurationError, PersistenceError, SessionStoreError
from .base import SessionRecord, derive_title

_VALID_STATUSES = {"active", "completed", "failed", "cancelled"}
_N_SESSION_COLUMNS = 9


class PostgresConfig(BaseModel):
    """Connection and table settings for :class:`PostgresSessionStore`."""

    model_config = ConfigDict(extra="forbid")

    dsn: str | None = Field(default=None, description="Full libpq DSN; overrides the fields below.")
    host: str = "postgres"
    port: int = Field(default=5432, gt=0, lt=65536)
    dbname: str = "postgres"
    user: str = "postgres"
    password: str = "postgres"
    table_prefix: str = Field(default="bokaclaw_agent", min_length=1, max_length=30)
    auto_create_schema: bool = True
    minconn: int = Field(default=1, ge=1)
    maxconn: int = Field(default=5, ge=1)

    @field_validator("table_prefix")
    @classmethod
    def _validate_table_prefix(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", value):
            raise ValueError("table_prefix must match ^[a-z_][a-z0-9_]*$")
        return value

    @field_validator("maxconn")
    @classmethod
    def _validate_pool_sizes(cls, value: int, info: Any) -> int:
        if value < info.data.get("minconn", 1):
            raise ValueError("maxconn must be greater than or equal to minconn")
        return value

    @classmethod
    def from_env(cls) -> "PostgresConfig":
        """Read the same environment variables used by the BokaClaw server."""

        dsn = os.getenv("BOKACLAW_AGENT_POSTGRES_DSN") or os.getenv("POSTGRES_DSN")
        return cls(
            dsn=dsn,
            host=os.getenv("POSTGRES_HOST", "postgres"),
            port=int(os.getenv("POSTGRES_PORT", "5432")),
            dbname=os.getenv("POSTGRES_DB", "postgres"),
            user=os.getenv("POSTGRES_USER", "postgres"),
            password=os.getenv("POSTGRES_PASSWORD", "postgres"),
        )


class PostgresSessionStore:
    """``SessionStore`` backed by two tables in the shared Postgres database."""

    def __init__(self, config: PostgresConfig | None = None, *, pool: Any = None) -> None:
        self.config = config or PostgresConfig.from_env()
        self._schema_ready = False
        self._schema_lock = threading.Lock()
        self._owns_pool = pool is None

        if pool is None:
            try:
                from psycopg_pool import ConnectionPool
            except ImportError as exc:  # pragma: no cover - depends on extras
                raise ConfigurationError(
                    "Postgres support is not installed. Run: pip install 'bokaclaw-agent[postgres]'"
                ) from exc

            conninfo = self.config.dsn or self._build_conninfo()
            try:
                pool = ConnectionPool(
                    conninfo=conninfo,
                    min_size=self.config.minconn,
                    max_size=self.config.maxconn,
                    open=False,
                )
                pool.open(wait=False)
            except Exception as exc:  # noqa: BLE001 - normalize driver errors
                raise SessionStoreError(f"Could not open the Postgres connection pool: {exc}") from exc

        self._pool = pool
        if self.config.auto_create_schema:
            self.ensure_schema()

    @property
    def sessions_table(self) -> str:
        return f"{self.config.table_prefix}_sessions"

    @property
    def messages_table(self) -> str:
        return f"{self.config.table_prefix}_messages"

    def _build_conninfo(self) -> str:
        from psycopg.conninfo import make_conninfo

        return make_conninfo(
            host=self.config.host,
            port=self.config.port,
            dbname=self.config.dbname,
            user=self.config.user,
            password=self.config.password,
        )

    def _sessions_ddl(self) -> str:
        return f"""
            CREATE TABLE IF NOT EXISTS {self.sessions_table} (
                id TEXT PRIMARY KEY,
                agent_name TEXT NOT NULL,
                user_id TEXT,
                title TEXT,
                metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','completed','failed','cancelled')),
                error TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """

    def _sessions_index_ddl(self) -> str:
        return f"""
            CREATE INDEX IF NOT EXISTS {self.config.table_prefix}_sessions_user_idx
            ON {self.sessions_table} (user_id, updated_at DESC)
        """

    def _messages_ddl(self) -> str:
        return f"""
            CREATE TABLE IF NOT EXISTS {self.messages_table} (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                session_id TEXT NOT NULL
                    REFERENCES {self.sessions_table}(id) ON DELETE CASCADE,
                seq BIGINT NOT NULL,
                role TEXT NOT NULL
                    CHECK (role IN ('system','user','assistant','tool')),
                content TEXT,
                tool_calls JSONB,
                tool_call_id TEXT,
                name TEXT,
                metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE (session_id, seq)
            )
        """

    def _ensure_status_check(self, cursor: Any) -> None:
        """Upgrade a pre-existing ``status`` CHECK constraint to allow ``cancelled``.

        Older tables created the constraint inline, so Postgres named it
        automatically (usually ``{table}_status_check``). Look up the actual
        name in ``pg_constraint`` instead of assuming it, and do nothing when
        the constraint is absent or already accepts ``cancelled``.
        """

        cursor.execute(
            """
            SELECT con.conname, pg_get_constraintdef(con.oid)
            FROM pg_constraint AS con
            JOIN pg_class AS rel ON rel.oid = con.conrelid
            JOIN pg_namespace AS ns ON ns.oid = rel.relnamespace
            WHERE rel.relname = %s
              AND ns.nspname = current_schema()
              AND con.contype = 'c'
            """,
            (self.sessions_table,),
        )
        for name, definition in cursor.fetchall() or []:
            if "status" not in definition or "active" not in definition:
                continue
            if "cancelled" in definition:
                return
            escaped_name = name.replace('"', '""')
            quoted_name = f'"{escaped_name}"'
            cursor.execute(f"ALTER TABLE {self.sessions_table} DROP CONSTRAINT {quoted_name}")
            cursor.execute(
                f"ALTER TABLE {self.sessions_table} ADD CONSTRAINT {quoted_name} "
                "CHECK (status IN ('active','completed','failed','cancelled'))"
            )
            return

    def ensure_schema(self) -> None:
        """Create the two tables once per store instance."""

        if self._schema_ready or not self.config.auto_create_schema:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            try:
                with self._pool.connection() as connection, connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(%s))",
                        (f"{self.config.table_prefix}_schema",),
                    )
                    cursor.execute(self._sessions_ddl())
                    cursor.execute(self._sessions_index_ddl())
                    cursor.execute(self._messages_ddl())
                    self._ensure_status_check(cursor)
            except Exception as exc:  # noqa: BLE001 - normalize driver errors
                raise SessionStoreError(f"Could not ensure the agent session schema: {exc}") from exc
            self._schema_ready = True

    def get_session(self, session_id: str) -> SessionRecord | None:
        self.ensure_schema()
        try:
            with self._pool.connection() as connection, connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT id, agent_name, user_id, title, metadata, status, error, created_at, updated_at "
                    f"FROM {self.sessions_table} WHERE id = %s",
                    (session_id,),
                )
                row = cursor.fetchone()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(f"Could not load session {session_id!r}: {exc}") from exc

        if row is None:
            return None
        if len(row) != _N_SESSION_COLUMNS:
            raise PersistenceError("Unexpected session row shape returned by Postgres")
        return SessionRecord(
            id=row[0],
            agent_name=row[1],
            user_id=row[2],
            title=row[3],
            metadata=row[4] or {},
            status=row[5],
            error=row[6],
            created_at=row[7],
            updated_at=row[8],
        )

    def load_messages(self, session_id: str) -> list[dict[str, Any]]:
        self.ensure_schema()
        try:
            with self._pool.connection() as connection, connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT role, content, tool_calls, tool_call_id, name "
                    f"FROM {self.messages_table} WHERE session_id = %s ORDER BY seq",
                    (session_id,),
                )
                rows = cursor.fetchall()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(f"Could not load messages for session {session_id!r}: {exc}") from exc

        messages: list[dict[str, Any]] = []
        for role, content, tool_calls, tool_call_id, _name in rows or []:
            message: dict[str, Any] = {"role": role, "content": content}
            if tool_calls is not None:
                message["tool_calls"] = tool_calls
            if tool_call_id is not None:
                message["tool_call_id"] = tool_call_id
            messages.append(message)
        return messages

    def persist_run(
        self,
        session_id: str,
        new_messages: list[dict[str, Any]],
        *,
        agent_name: str,
        user_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        status: str = "completed",
        error: str | None = None,
    ) -> None:
        if status not in _VALID_STATUSES:
            raise PersistenceError(f"Invalid session status {status!r}")

        self.ensure_schema()
        title = derive_title(new_messages)
        try:
            metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
            with self._pool.connection() as connection, connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    INSERT INTO {self.sessions_table}
                        (id, agent_name, user_id, title, metadata, status, error)
                    VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        agent_name = EXCLUDED.agent_name,
                        user_id = COALESCE(EXCLUDED.user_id, {self.sessions_table}.user_id),
                        title = COALESCE({self.sessions_table}.title, EXCLUDED.title),
                        metadata = {self.sessions_table}.metadata || EXCLUDED.metadata,
                        status = EXCLUDED.status,
                        error = EXCLUDED.error,
                        updated_at = now()
                    """,
                    (session_id, agent_name, user_id, title, metadata_json, status, error),
                )
                cursor.execute(
                    f"SELECT COALESCE(MAX(seq), 0) FROM {self.messages_table} WHERE session_id = %s",
                    (session_id,),
                )
                row = cursor.fetchone()
                next_seq = int(row[0] or 0) + 1

                if new_messages:
                    rows = []
                    for index, message in enumerate(new_messages):
                        role = message.get("role")
                        if role not in {"system", "user", "assistant", "tool"}:
                            raise PersistenceError(f"Invalid message role {role!r}")
                        tool_calls = message.get("tool_calls")
                        rows.append(
                            (
                                session_id,
                                next_seq + index,
                                role,
                                message.get("content"),
                                json.dumps(tool_calls, ensure_ascii=False) if tool_calls is not None else None,
                                message.get("tool_call_id"),
                                message.get("name"),
                                json.dumps(message.get("metadata") or {}, ensure_ascii=False),
                            )
                        )
                    cursor.executemany(
                        f"""
                        INSERT INTO {self.messages_table}
                            (session_id, seq, role, content, tool_calls, tool_call_id, name, metadata)
                        VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s::jsonb)
                        """,
                        rows,
                    )
        except PersistenceError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize driver errors
            raise PersistenceError(f"Could not persist session {session_id!r}: {exc}") from exc

    def list_sessions(
        self,
        *,
        user_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SessionRecord]:
        self.ensure_schema()
        try:
            with self._pool.connection() as connection, connection.cursor() as cursor:
                if user_id is None:
                    cursor.execute(
                        f"SELECT id, agent_name, user_id, title, metadata, status, error, created_at, updated_at "
                        f"FROM {self.sessions_table} ORDER BY updated_at DESC LIMIT %s OFFSET %s",
                        (limit, offset),
                    )
                else:
                    cursor.execute(
                        f"SELECT id, agent_name, user_id, title, metadata, status, error, created_at, updated_at "
                        f"FROM {self.sessions_table} WHERE user_id = %s "
                        f"ORDER BY updated_at DESC LIMIT %s OFFSET %s",
                        (user_id, limit, offset),
                    )
                rows = cursor.fetchall()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(f"Could not list sessions: {exc}") from exc

        records: list[SessionRecord] = []
        for row in rows or []:
            if len(row) != _N_SESSION_COLUMNS:
                raise PersistenceError("Unexpected session row shape returned by Postgres")
            records.append(
                SessionRecord(
                    id=row[0],
                    agent_name=row[1],
                    user_id=row[2],
                    title=row[3],
                    metadata=row[4] or {},
                    status=row[5],
                    error=row[6],
                    created_at=row[7],
                    updated_at=row[8],
                )
            )
        return records

    def delete_session(self, session_id: str) -> None:
        self.ensure_schema()
        try:
            with self._pool.connection() as connection, connection.cursor() as cursor:
                cursor.execute(f"DELETE FROM {self.sessions_table} WHERE id = %s", (session_id,))
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(f"Could not delete session {session_id!r}: {exc}") from exc

    def close(self) -> None:
        if self._owns_pool and self._pool is not None:
            self._pool.close()
