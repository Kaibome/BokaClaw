"""REST-compatible memory service backed by workspace Markdown files."""

from __future__ import annotations

import logging
from datetime import datetime, time, timezone
from typing import Any, Callable, Dict, List, Optional

from .config import MemorySettings
from .embeddings import create_embedder
from .errors import MemoryNotFound, MemoryValidationError
from .grammar import (
    DEFAULT_IMPORTANCE,
    STANDARD_META_KEYS,
    MemoryEntry,
    default_importance,
    parse_datetime,
    stable_id,
)
from .paths import scan_user_roots
from .repository import MemoryIndexRepository
from .workspace import WorkspaceStore

logger = logging.getLogger(__name__)

UNSET = object()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _scope(user_id: str | None, agent_id: str | None, run_id: str | None) -> str:
    if user_id:
        return str(user_id)
    if agent_id:
        return f"agent:{agent_id}"
    if run_id:
        return f"run:{run_id}"
    raise MemoryValidationError("At least one identifier (user_id, agent_id, run_id) is required.")


def _message_text(messages: Any) -> str:
    if isinstance(messages, str):
        return messages.strip()
    if isinstance(messages, dict):
        messages = [messages]
    if not isinstance(messages, (list, tuple)):
        raise MemoryValidationError("messages must be a string or a list of message objects")

    rows: list[tuple[str, str]] = []
    for message in messages:
        if hasattr(message, "model_dump"):
            message = message.model_dump()
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user")
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(
                str(part.get("text") or "") if isinstance(part, dict) else str(part) for part in content
            )
        text = str(content or "").strip()
        if text:
            rows.append((role, text))
    if not rows:
        raise MemoryValidationError("messages contain no text")
    if len(rows) == 1:
        return rows[0][1]
    return "\n".join(f"**{role.title()}:** {text}" for role, text in rows)


def _parse_expiration(value: Any) -> datetime | None:
    if value is None or value is UNSET:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            parsed = datetime.combine(datetime.fromisoformat(text).date(), time.max, tzinfo=timezone.utc)
        except ValueError as exc:
            raise MemoryValidationError("expiration_date must be a valid date in YYYY-MM-DD format.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _coerce_sources(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item).strip()]
    return []


def _entry_metadata(meta: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in meta.items() if key not in STANDARD_META_KEYS}


class WorkspaceMemory:
    """Workspace-Markdown implementation of the memory service surface."""

    def __init__(
        self,
        settings: MemorySettings,
        *,
        session_factory: Callable[[], Any] | None = None,
        embedder: Any | None = None,
    ) -> None:
        self.settings = settings
        self.store = WorkspaceStore(settings)
        self.embedder = embedder or create_embedder(settings)
        if session_factory is None:
            from app.core.db import SessionLocal

            session_factory = SessionLocal
        self.repository = MemoryIndexRepository(session_factory, self.embedder, settings)

    @classmethod
    def from_config(cls, config: Dict[str, Any] | None) -> "WorkspaceMemory":
        return cls(MemorySettings.from_config(config))

    @classmethod
    def from_settings(
        cls,
        settings: MemorySettings,
        *,
        session_factory: Callable[[], Any] | None = None,
    ) -> "WorkspaceMemory":
        return cls(settings, session_factory=session_factory)

    # --------------------------------------------------------------- writes

    def add(
        self,
        messages: Any,
        *,
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        run_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        timestamp: Any = None,
        expiration_date: Any = None,
        infer: bool = True,
        memory_type: Optional[str] = None,
        prompt: Optional[str] = None,
    ) -> Dict[str, Any]:
        scope = _scope(user_id, agent_id, run_id)
        text = _message_text(messages)
        meta = dict(metadata or {})
        kind = str(memory_type or meta.get("kind") or "operational")
        if kind not in DEFAULT_IMPORTANCE:
            kind = "operational"
        now = _now()
        created = parse_datetime(meta.get("created_at") or meta.get("captured_at") or timestamp, now)
        entry = MemoryEntry(
            id=str(meta.get("id") or stable_id(kind, text)),
            kind=kind,
            content=text,
            importance=float(meta.get("importance") or default_importance(kind)),
            source=str(meta.get("source") or "curated"),
            trust=str(meta.get("trust") or "owner"),
            created=created,
            updated=parse_datetime(meta.get("updated_at"), created),
            expires=_parse_expiration(expiration_date),
            question_key=str(meta["question_key"]) if meta.get("question_key") else None,
            sources=_coerce_sources(meta.get("sources")),
            metadata=_entry_metadata(meta),
            user_id=scope,
            agent_id=agent_id,
            run_id=run_id,
            conversation_id=str(meta["conversation_id"]) if meta.get("conversation_id") else None,
            project=str(meta["project"]) if meta.get("project") else None,
        )
        if infer:
            logger.debug("Markdown memory stores verbatim content; infer=True is ignored")
        existing = self.repository.get_entry(entry.id, show_expired=True)
        if existing is not None and existing.get("user_id") == scope:
            return {"results": [{"id": entry.id, "memory": entry.content, "event": "ADD"}]}
        self.store.append_entry(scope, entry)
        self.repository.upsert_entries([entry])
        self.repository.record_history(entry.id, scope, "ADD", None, entry.content)
        return {"results": [{"id": entry.id, "memory": entry.content, "event": "ADD"}]}

    def update(
        self,
        memory_id: str,
        *,
        data: Any = None,
        text: Any = None,
        metadata: Optional[Dict[str, Any]] = None,
        expiration_date: Any = UNSET,
    ) -> Dict[str, Any]:
        existing = self.repository.get_entry(memory_id, show_expired=True)
        if existing is None:
            raise MemoryNotFound(f"Memory {memory_id} not found")
        user_id = str(existing.get("user_id") or "")
        entry, _ = self.store.find_entry(user_id, memory_id)
        old = entry.content
        new_text = data if data is not None else text
        if new_text is not None:
            entry.content = str(new_text)
        if metadata:
            entry.metadata = {**entry.metadata, **metadata}
            if "kind" in metadata:
                kind = str(metadata["kind"])
                if kind in DEFAULT_IMPORTANCE:
                    entry.kind = kind
                    entry.heading = None
            if "importance" in metadata:
                try:
                    entry.importance = max(0.0, min(1.0, float(metadata["importance"])))
                except (TypeError, ValueError):
                    pass
            if "trust" in metadata:
                entry.trust = str(metadata["trust"])
            if "source" in metadata:
                entry.source = str(metadata["source"])
        if expiration_date is not UNSET:
            entry.expires = _parse_expiration(expiration_date)
        entry.updated = _now()
        self.store.replace_entry(user_id, entry)
        self.repository.upsert_entries([entry])
        self.repository.record_history(memory_id, user_id, "UPDATE", old, entry.content)
        return {"message": "Memory updated successfully!"}

    def delete(self, memory_id: str) -> Dict[str, Any]:
        existing = self.repository.get_entry(memory_id, show_expired=True)
        if existing is None:
            raise MemoryNotFound(f"Memory {memory_id} not found")
        user_id = str(existing.get("user_id") or "")
        self.store.delete_entry(user_id, memory_id)
        self.repository.delete_entries([memory_id])
        self.repository.record_history(memory_id, user_id, "DELETE", existing.get("memory"), None)
        return {"message": "Memory deleted successfully"}

    def delete_all(
        self,
        *,
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        filters = {"user_id": user_id, "agent_id": agent_id, "run_id": run_id}
        filters = {key: value for key, value in filters.items() if value}
        if not filters:
            raise MemoryValidationError("At least one identifier (user_id, agent_id, run_id) is required.")
        rows = self.repository.get_all(filters, top_k=100_000, show_expired=True)
        by_user: Dict[str, List[str]] = {}
        for row in rows:
            by_user.setdefault(str(row.get("user_id") or ""), []).append(str(row["id"]))
        for scope, ids in by_user.items():
            if not scope:
                continue
            self.store.delete_entries(scope, ids)
            for memory_id in ids:
                self.repository.record_history(memory_id, scope, "DELETE", None, None)
        self.repository.delete_entries([str(row["id"]) for row in rows])
        return {"message": "All relevant memories deleted"}

    def reset(self) -> Dict[str, Any]:
        for user_id, _ in list(scan_user_roots(self.settings)):
            try:
                self.store.archive_user(user_id)
            except Exception:
                logger.warning("Could not archive workspace for %s", user_id, exc_info=True)
        self.repository.clear_all()
        return {"message": "All memories reset"}

    # ---------------------------------------------------------------- reads

    def get(self, memory_id: str) -> Dict[str, Any] | None:
        return self.repository.get_entry(memory_id)

    def get_all(
        self,
        *,
        filters: Dict[str, Any] | None = None,
        top_k: int = 20,
        show_expired: bool = False,
    ) -> Dict[str, Any]:
        return {
            "results": self.repository.get_all(
                filters or {},
                top_k=top_k,
                show_expired=show_expired,
            )
        }

    def search(
        self,
        query: str,
        *,
        filters: Dict[str, Any] | None = None,
        top_k: int = 20,
        threshold: float | None = None,
        explain: bool = False,
        show_expired: bool = False,
        **_: Any,
    ) -> Dict[str, Any]:
        return {
            "results": self.repository.search(
                query,
                filters=filters,
                top_k=top_k,
                threshold=threshold,
                explain=explain,
                show_expired=show_expired,
            )
        }

    def list_all(self, top_k: int = 1000) -> Dict[str, Any]:
        return {"results": self.repository.list_all(top_k=top_k)}

    def history(self, memory_id: str) -> List[Dict[str, Any]]:
        return self.repository.history(memory_id)

    def count(self, user_id: str | None = None) -> int:
        return self.repository.count(user_id)

    # --------------------------------------------------------------- agent

    def recall_for_turn(
        self,
        user_id: str,
        question: str,
        *,
        conversation_id: str | None = None,
    ) -> Any:
        from .retrieval import recall_for_turn

        return recall_for_turn(self, user_id, question, conversation_id=conversation_id)

    def capture_turn(self, *args: Any, **kwargs: Any) -> Any:
        from .capture import capture_turn

        return capture_turn(self, *args, **kwargs)

    def reconcile_user(self, user_id: str) -> Any:
        from .maintenance import reconcile_user

        return reconcile_user(self, user_id)

    def reconcile_all(self) -> Any:
        from .maintenance import reconcile_all

        return reconcile_all(self)

    def close(self) -> None:
        return None
