"""In-memory session store, useful for tests and ephemeral agents."""

from __future__ import annotations

from datetime import datetime, timezone
from threading import RLock
from typing import Any

from ..exceptions import PersistenceError
from .base import SessionRecord, derive_title

_VALID_STATUSES = {"active", "completed", "failed"}


class InMemorySessionStore:
    """Thread-safe, process-local implementation of ``SessionStore``."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionRecord] = {}
        self._messages: dict[str, list[dict[str, Any]]] = {}
        self._lock = RLock()

    def ensure_schema(self) -> None:
        return None

    def get_session(self, session_id: str) -> SessionRecord | None:
        with self._lock:
            record = self._sessions.get(session_id)
            return record.model_copy(deep=True) if record is not None else None

    def load_messages(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(message) for message in self._messages.get(session_id, [])]

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

        now = datetime.now(timezone.utc)
        with self._lock:
            existing = self._sessions.get(session_id)
            if existing is None:
                self._sessions[session_id] = SessionRecord(
                    id=session_id,
                    agent_name=agent_name,
                    user_id=user_id,
                    title=derive_title(new_messages),
                    metadata=dict(metadata or {}),
                    status=status,
                    error=error,
                    created_at=now,
                    updated_at=now,
                )
                self._messages[session_id] = []
            else:
                self._sessions[session_id] = existing.model_copy(
                    update={
                        "agent_name": agent_name,
                        "user_id": user_id or existing.user_id,
                        "title": existing.title or derive_title(new_messages),
                        "metadata": {**existing.metadata, **(metadata or {})},
                        "status": status,
                        "error": error,
                        "updated_at": now,
                    }
                )
            self._messages[session_id].extend(dict(message) for message in new_messages)

    def list_sessions(
        self,
        *,
        user_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SessionRecord]:
        with self._lock:
            records = [
                record.model_copy(deep=True)
                for record in self._sessions.values()
                if user_id is None or record.user_id == user_id
            ]
        records.sort(key=lambda record: record.updated_at, reverse=True)
        return records[offset : offset + limit]

    def delete_session(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)
            self._messages.pop(session_id, None)

    def close(self) -> None:
        return None
