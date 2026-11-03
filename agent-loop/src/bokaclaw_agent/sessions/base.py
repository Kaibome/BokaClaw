"""Session store protocol and shared types."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel, Field


class SessionRecord(BaseModel):
    """Metadata for one persisted conversation."""

    id: str
    agent_name: str
    user_id: str | None = None
    title: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    status: str = "active"
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class SessionStore(Protocol):
    """Persistence backend for sessions and their messages."""

    def ensure_schema(self) -> None: ...

    def get_session(self, session_id: str) -> SessionRecord | None: ...

    def load_messages(self, session_id: str) -> list[dict[str, Any]]: ...

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
    ) -> None: ...

    def list_sessions(
        self,
        *,
        user_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SessionRecord]: ...

    def delete_session(self, session_id: str) -> None: ...

    def close(self) -> None: ...


def derive_title(messages: list[dict[str, Any]], max_length: int = 80) -> str | None:
    """Use the first user message as a human-readable session title."""

    for message in messages:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    parts.append(str(part.get("text", "")))
            text = " ".join(parts)
        text = " ".join(text.split())
        if text:
            return text[:max_length]
    return None
