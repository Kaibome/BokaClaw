"""Postgres-backed, rebuildable index for workspace memory."""

from __future__ import annotations

import hashlib
import json
import logging
import math
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Sequence

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from .config import MemorySettings
from .embeddings import Embedder, normalize_vector, vector_literal
from .errors import MemoryUnavailable
from .grammar import MemoryEntry, format_datetime

logger = logging.getLogger(__name__)

_FILTER_COLUMNS = {
    "id",
    "user_id",
    "agent_id",
    "run_id",
    "kind",
    "source",
    "trust",
    "question_key",
    "project",
}


def content_hash(content: str) -> str:
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


def search_text(content: str) -> str:
    """Tokenize CJK text for Postgres FTS; fall back to the raw content."""

    try:
        import jieba  # type: ignore

        tokens = [token.strip() for token in jieba.cut_for_search(content or "") if token.strip()]
        if tokens:
            return " ".join(tokens)
    except Exception:  # pragma: no cover - optional dependency
        logger.debug("jieba unavailable; using raw content for FTS", exc_info=True)
    return content or ""


class MemoryIndexRepository:
    def __init__(
        self,
        session_factory: Callable[[], Any],
        embedder: Embedder,
        settings: MemorySettings,
    ) -> None:
        self._session_factory = session_factory
        self.embedder = embedder
        self.settings = settings

    # --------------------------------------------------------------- writes

    def upsert_entries(self, entries: Sequence[MemoryEntry]) -> None:
        if not entries:
            return
        texts = [entry.content for entry in entries]
        try:
            vectors = self.embedder.embed_documents(texts)
        except Exception as exc:
            logger.warning("Embedding batch failed; indexing text only: %s", exc, exc_info=True)
            vectors = [None] * len(entries)
            embedding_error = f"{type(exc).__name__}: {exc}"[:500]
        else:
            embedding_error = None
        if len(vectors) != len(entries):
            vectors = [None] * len(entries)
            embedding_error = "embedder returned a different number of vectors"
        self._upsert_rows(entries, vectors, embedding_error)

    def _upsert_rows(
        self,
        entries: Sequence[MemoryEntry],
        vectors: Sequence[Sequence[float] | None],
        embedding_error: str | None,
    ) -> None:
        sql = text(
            """
            INSERT INTO memory_index_entries (
                id, user_id, agent_id, run_id, kind, source, trust, content,
                content_hash, search_text, importance, project, question_key,
                metadata, created_at, updated_at, expires_at, source_path,
                source_ordinal, embedding_model, embedding_dims,
                embedding_attempts, embedding_error, embedding, indexed_at
            ) VALUES (
                :id, :user_id, :agent_id, :run_id, :kind, :source, :trust, :content,
                :content_hash, :search_text, :importance, :project, :question_key,
                CAST(:metadata AS jsonb), :created_at, :updated_at, :expires_at,
                :source_path, :source_ordinal, :embedding_model, :embedding_dims,
                :embedding_attempts, :embedding_error, CAST(:embedding AS halfvec), now()
            )
            ON CONFLICT (id) DO UPDATE SET
                user_id = EXCLUDED.user_id,
                agent_id = EXCLUDED.agent_id,
                run_id = EXCLUDED.run_id,
                kind = EXCLUDED.kind,
                source = EXCLUDED.source,
                trust = EXCLUDED.trust,
                content = EXCLUDED.content,
                content_hash = EXCLUDED.content_hash,
                search_text = EXCLUDED.search_text,
                importance = EXCLUDED.importance,
                project = EXCLUDED.project,
                question_key = EXCLUDED.question_key,
                metadata = EXCLUDED.metadata,
                created_at = EXCLUDED.created_at,
                updated_at = EXCLUDED.updated_at,
                expires_at = EXCLUDED.expires_at,
                source_path = EXCLUDED.source_path,
                source_ordinal = EXCLUDED.source_ordinal,
                embedding_model = EXCLUDED.embedding_model,
                embedding_dims = EXCLUDED.embedding_dims,
                embedding_attempts = EXCLUDED.embedding_attempts,
                embedding_error = EXCLUDED.embedding_error,
                embedding = EXCLUDED.embedding,
                indexed_at = now()
            """
        )
        rows: List[Dict[str, Any]] = []
        for entry, vector in zip(entries, vectors, strict=False):
            if not entry.user_id:
                raise ValueError("memory entry is missing user_id")
            rows.append(
                {
                    "id": entry.id,
                    "user_id": entry.user_id,
                    "agent_id": entry.agent_id,
                    "run_id": entry.run_id,
                    "kind": entry.kind,
                    "source": entry.source,
                    "trust": entry.trust,
                    "content": entry.content,
                    "content_hash": content_hash(entry.content),
                    "search_text": search_text(entry.content),
                    "importance": float(entry.importance),
                    "project": entry.project,
                    "question_key": entry.question_key,
                    "metadata": json.dumps(entry.metadata, ensure_ascii=False, default=str),
                    "created_at": entry.created,
                    "updated_at": entry.updated,
                    "expires_at": entry.expires,
                    "source_path": entry.path,
                    "source_ordinal": entry.ordinal,
                    "embedding_model": self.embedder.model_id if vector is not None else None,
                    "embedding_dims": self.embedder.dims if vector is not None else None,
                    "embedding_attempts": 1 if vector is None else 0,
                    "embedding_error": embedding_error if vector is None else None,
                    "embedding": (
                        vector_literal(normalize_vector(list(vector))) if vector is not None else None
                    ),
                }
            )
        try:
            with self._session() as session:
                session.execute(sql, rows)
                session.commit()
        except SQLAlchemyError as exc:
            raise MemoryUnavailable(f"could not write the memory index: {exc}") from exc

    def delete_entries(self, ids: Iterable[str]) -> int:
        values = [str(value) for value in ids if value]
        if not values:
            return 0
        try:
            with self._session() as session:
                result = session.execute(
                    text("DELETE FROM memory_index_entries WHERE id = ANY(:ids)"),
                    {"ids": values},
                )
                session.commit()
                return int(result.rowcount or 0)
        except SQLAlchemyError as exc:
            raise MemoryUnavailable(f"could not delete memory index rows: {exc}") from exc

    def delete_user(self, user_id: str) -> int:
        try:
            with self._session() as session:
                result = session.execute(
                    text("DELETE FROM memory_index_entries WHERE user_id = :user_id"),
                    {"user_id": user_id},
                )
                session.commit()
                return int(result.rowcount or 0)
        except SQLAlchemyError as exc:
            raise MemoryUnavailable(f"could not clear the user index: {exc}") from exc

    def clear_all(self) -> None:
        try:
            with self._session() as session:
                session.execute(text("DELETE FROM memory_index_entries"))
                session.execute(text("DELETE FROM memory_index_files"))
                session.execute(text("DELETE FROM memory_history"))
                session.execute(text("DELETE FROM memory_index_state"))
                session.commit()
        except SQLAlchemyError as exc:
            raise MemoryUnavailable(f"could not reset the memory index: {exc}") from exc

    def record_history(
        self,
        memory_id: str,
        user_id: str | None,
        event: str,
        old_memory: str | None,
        new_memory: str | None,
        *,
        actor_id: str | None = None,
        role: str | None = None,
        metadata: Dict[str, Any] | None = None,
    ) -> None:
        import uuid

        now = datetime.now(timezone.utc)
        try:
            with self._session() as session:
                session.execute(
                    text(
                        """
                        INSERT INTO memory_history (
                            id, memory_id, user_id, event, old_memory, new_memory,
                            created_at, updated_at, is_deleted, actor_id, role, metadata
                        ) VALUES (
                            :id, :memory_id, :user_id, :event, :old_memory, :new_memory,
                            :created_at, :updated_at, :is_deleted, :actor_id, :role,
                            CAST(:metadata AS jsonb)
                        )
                        """
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "memory_id": memory_id,
                        "user_id": user_id,
                        "event": event,
                        "old_memory": old_memory,
                        "new_memory": new_memory,
                        "created_at": now,
                        "updated_at": None,
                        "is_deleted": event == "DELETE",
                        "actor_id": actor_id,
                        "role": role,
                        "metadata": json.dumps(metadata or {}, ensure_ascii=False, default=str),
                    },
                )
                session.commit()
        except SQLAlchemyError as exc:
            logger.warning("Could not record memory history for %s: %s", memory_id, exc, exc_info=True)

    def import_history(self, rows: Sequence[Dict[str, Any]]) -> int:
        """Copy mem0 history rows into the new audit table (idempotent)."""

        if not rows:
            return 0
        sql = text(
            """
            INSERT INTO memory_history (
                id, memory_id, user_id, event, old_memory, new_memory,
                created_at, updated_at, is_deleted, actor_id, role, metadata
            ) VALUES (
                :id, :memory_id, :user_id, :event, :old_memory, :new_memory,
                :created_at, :updated_at, :is_deleted, :actor_id, :role,
                CAST(:metadata AS jsonb)
            )
            ON CONFLICT (id) DO NOTHING
            """
        )
        try:
            with self._session() as session:
                session.execute(sql, list(rows))
                session.commit()
                return len(rows)
        except SQLAlchemyError as exc:
            raise MemoryUnavailable(f"could not import memory history: {exc}") from exc

    # ---------------------------------------------------------------- reads

    def get_entry(self, memory_id: str, *, show_expired: bool = False) -> Dict[str, Any] | None:
        clauses = ["id = :id"]
        if not show_expired:
            clauses.append("(expires_at IS NULL OR expires_at > now())")
        with self._session() as session:
            row = session.execute(
                text(f"SELECT * FROM memory_index_entries WHERE {' AND '.join(clauses)}"),
                {"id": memory_id},
            ).first()
        return self._row_to_dict(row) if row is not None else None

    def get_all(
        self,
        filters: Dict[str, Any] | None = None,
        *,
        top_k: int = 20,
        show_expired: bool = False,
    ) -> List[Dict[str, Any]]:
        where, params = self._where(filters or {}, show_expired=show_expired)
        params["limit"] = max(1, int(top_k))
        query = (
            "SELECT * FROM memory_index_entries "
            f"WHERE {where} ORDER BY updated_at DESC, created_at DESC LIMIT :limit"
        )
        with self._session() as session:
            rows = session.execute(text(query), params).all()
        return [self._row_to_dict(row) for row in rows]

    def list_all(self, top_k: int = 1000) -> List[Dict[str, Any]]:
        return self.get_all({}, top_k=top_k, show_expired=True)

    def search(
        self,
        query: str,
        *,
        filters: Dict[str, Any] | None = None,
        top_k: int = 20,
        threshold: float | None = None,
        explain: bool = False,
        show_expired: bool = False,
        exclude_untrusted: bool = True,
    ) -> List[Dict[str, Any]]:
        threshold = self.settings.recall_threshold if threshold is None else float(threshold)
        filters = dict(filters or {})
        if exclude_untrusted and "trust" not in filters:
            filters["trust"] = {"ne": "untrusted"}
        where, params = self._where(filters, show_expired=show_expired)
        candidate_limit = max(30, int(top_k) * 4)

        semantic: Dict[str, Dict[str, Any]] = {}
        try:
            vector = self.embedder.embed_query(query)
        except Exception as exc:
            logger.warning("Query embedding failed; falling back to keyword search: %s", exc)
            vector = None
        if vector is not None:
            vector_params = dict(params)
            vector_params["vec"] = vector_literal(normalize_vector(list(vector)))
            vector_params["candidate_limit"] = candidate_limit
            vector_params["model"] = self.embedder.model_id
            vector_query = (
                "SELECT *, 1 - (embedding <=> CAST(:vec AS halfvec)) AS semantic_score "
                "FROM memory_index_entries "
                f"WHERE {where} AND embedding IS NOT NULL AND embedding_model = :model "
                "ORDER BY embedding <=> CAST(:vec AS halfvec) LIMIT :candidate_limit"
            )
            with self._session() as session:
                for row in session.execute(text(vector_query), vector_params).all():
                    item = self._row_to_dict(row)
                    item["semantic_score"] = max(0.0, min(1.0, float(item.get("semantic_score") or 0.0)))
                    semantic[item["id"]] = item

        fts_query = search_text(query)
        keyword: Dict[str, Dict[str, Any]] = {}
        if fts_query.strip():
            fts_params = dict(params)
            fts_params["fts_query"] = fts_query
            fts_params["candidate_limit"] = candidate_limit
            fts_sql = (
                "SELECT *, ts_rank_cd(search_tsv, plainto_tsquery('simple', :fts_query)) AS fts_score "
                "FROM memory_index_entries "
                f"WHERE {where} AND search_tsv @@ plainto_tsquery('simple', :fts_query) "
                "ORDER BY fts_score DESC LIMIT :candidate_limit"
            )
            with self._session() as session:
                for row in session.execute(text(fts_sql), fts_params).all():
                    item = self._row_to_dict(row)
                    item["fts_score"] = max(0.0, float(item.get("fts_score") or 0.0))
                    keyword[item["id"]] = item

        now = datetime.now(timezone.utc)
        combined: List[Dict[str, Any]] = []
        for memory_id in set(semantic) | set(keyword):
            item = semantic.get(memory_id) or keyword.get(memory_id)
            if item is None:
                continue
            semantic_score = float(item.get("semantic_score") or 0.0)
            fts_score = float(item.get("fts_score") or 0.0)
            fts_norm = min(1.0, fts_score * 2.0)
            if semantic_score < threshold and fts_norm < 0.3:
                continue
            relevance = 0.7 * semantic_score + 0.3 * fts_norm
            recency = self._recency_factor(item, now)
            importance = float(item.get("importance") or 0.5)
            final = relevance * recency * (0.5 + 0.5 * importance)
            item["score"] = round(final, 6)
            item["score_details"] = {
                "semantic_score": round(semantic_score, 6),
                "bm25_score": round(fts_norm, 6),
                "fts_score": round(fts_score, 6),
                "relevance": round(relevance, 6),
                "recency": round(recency, 6),
                "importance": importance,
                "final_score": round(final, 6),
            }
            combined.append(item)

        combined.sort(key=lambda item: item.get("score", 0.0), reverse=True)
        results = combined[: max(1, int(top_k))]
        if not explain:
            for item in results:
                item.pop("score_details", None)
        return results

    def history(self, memory_id: str) -> List[Dict[str, Any]]:
        with self._session() as session:
            rows = session.execute(
                text(
                    "SELECT * FROM memory_history WHERE memory_id = :memory_id "
                    "ORDER BY created_at ASC, updated_at ASC NULLS FIRST"
                ),
                {"memory_id": memory_id},
            ).all()
        return [dict(row._mapping) for row in rows]

    def entities(self, limit: int = 10_000) -> List[Dict[str, Any]]:
        query = text(
            """
            SELECT 'user' AS type, user_id AS id,
                   count(*) AS total_memories,
                   min(created_at) AS created_at, max(updated_at) AS updated_at
            FROM memory_index_entries WHERE user_id IS NOT NULL GROUP BY user_id
            UNION ALL
            SELECT 'agent' AS type, agent_id AS id,
                   count(*) AS total_memories,
                   min(created_at) AS created_at, max(updated_at) AS updated_at
            FROM memory_index_entries WHERE agent_id IS NOT NULL GROUP BY agent_id
            UNION ALL
            SELECT 'run' AS type, run_id AS id,
                   count(*) AS total_memories,
                   min(created_at) AS created_at, max(updated_at) AS updated_at
            FROM memory_index_entries WHERE run_id IS NOT NULL GROUP BY run_id
            ORDER BY type, id
            LIMIT :limit
            """
        )
        with self._session() as session:
            rows = session.execute(query, {"limit": limit}).all()
        return [dict(row._mapping) for row in rows]

    # --------------------------------------------------------------- state

    @contextmanager
    def advisory_lock(self, user_id: str) -> Any:
        """Hold a Postgres advisory lock for one user, or report that it is busy."""

        session = self._session_factory()
        acquired = False
        try:
            row = session.execute(
                text("SELECT pg_try_advisory_lock(hashtext(:user_id))"),
                {"user_id": user_id},
            ).first()
            acquired = bool(row[0]) if row is not None else False
            yield acquired
        finally:
            if acquired:
                try:
                    session.execute(
                        text("SELECT pg_advisory_unlock(hashtext(:user_id))"),
                        {"user_id": user_id},
                    )
                    session.commit()
                except SQLAlchemyError:
                    session.rollback()
            session.close()

    def set_dirty(self, user_id: str, dirty: bool, error: str | None = None) -> None:
        try:
            with self._session() as session:
                session.execute(
                    text(
                        """
                        INSERT INTO memory_index_state (user_id, dirty, last_error, updated_at)
                        VALUES (:user_id, :dirty, :error, now())
                        ON CONFLICT (user_id) DO UPDATE SET
                            dirty = EXCLUDED.dirty,
                            last_error = EXCLUDED.last_error,
                            updated_at = now()
                        """
                    ),
                    {"user_id": user_id, "dirty": dirty, "error": error},
                )
                session.commit()
        except SQLAlchemyError as exc:
            logger.warning("Could not update memory index state for %s: %s", user_id, exc, exc_info=True)

    def mark_reconciled(self, user_id: str, *, error: str | None = None) -> None:
        try:
            with self._session() as session:
                session.execute(
                    text(
                        """
                        INSERT INTO memory_index_state
                            (user_id, dirty, last_reconciled_at, last_error, updated_at)
                        VALUES (:user_id, false, now(), :error, now())
                        ON CONFLICT (user_id) DO UPDATE SET
                            dirty = false,
                            last_reconciled_at = now(),
                            last_error = EXCLUDED.last_error,
                            updated_at = now()
                        """
                    ),
                    {"user_id": user_id, "error": error},
                )
                session.commit()
        except SQLAlchemyError as exc:
            logger.warning("Could not mark %s reconciled: %s", user_id, exc, exc_info=True)

    def dirty_users(self) -> List[str]:
        with self._session() as session:
            rows = session.execute(
                text("SELECT user_id FROM memory_index_state WHERE dirty = true ORDER BY updated_at ASC")
            ).all()
        return [str(row[0]) for row in rows]

    def index_user_ids(self) -> List[str]:
        with self._session() as session:
            rows = session.execute(
                text("SELECT DISTINCT user_id FROM memory_index_entries ORDER BY user_id")
            ).all()
        return [str(row[0]) for row in rows]

    def entry_ids(self, user_id: str) -> List[str]:
        with self._session() as session:
            rows = session.execute(
                text("SELECT id FROM memory_index_entries WHERE user_id = :user_id"),
                {"user_id": user_id},
            ).all()
        return [str(row[0]) for row in rows]

    def upsert_file_state(
        self,
        user_id: str,
        path: str,
        sha256: str,
        size_bytes: int,
        mtime: datetime,
    ) -> None:
        try:
            with self._session() as session:
                session.execute(
                    text(
                        """
                        INSERT INTO memory_index_files
                            (user_id, path, content_sha256, size_bytes, mtime, indexed_at)
                        VALUES (:user_id, :path, :sha256, :size_bytes, :mtime, now())
                        ON CONFLICT (user_id, path) DO UPDATE SET
                            content_sha256 = EXCLUDED.content_sha256,
                            size_bytes = EXCLUDED.size_bytes,
                            mtime = EXCLUDED.mtime,
                            indexed_at = now()
                        """
                    ),
                    {
                        "user_id": user_id,
                        "path": path,
                        "sha256": sha256,
                        "size_bytes": size_bytes,
                        "mtime": mtime,
                    },
                )
                session.commit()
        except SQLAlchemyError as exc:
            logger.warning("Could not record file state for %s: %s", path, exc, exc_info=True)

    def count(self, user_id: str | None = None) -> int:
        with self._session() as session:
            if user_id is None:
                row = session.execute(text("SELECT count(*) FROM memory_index_entries")).first()
            else:
                row = session.execute(
                    text("SELECT count(*) FROM memory_index_entries WHERE user_id = :user_id"),
                    {"user_id": user_id},
                ).first()
        return int(row[0]) if row is not None else 0

    # -------------------------------------------------------------- helpers

    def _session(self) -> Any:
        return self._session_factory()

    def _where(
        self,
        filters: Dict[str, Any],
        *,
        show_expired: bool,
    ) -> tuple[str, Dict[str, Any]]:
        clauses: List[str] = ["true"]
        params: Dict[str, Any] = {}
        counter = 0
        for key, value in filters.items():
            counter += 1
            param = f"f{counter}"
            if key in _FILTER_COLUMNS:
                expression = key
            else:
                params[f"k{counter}"] = str(key)
                expression = f"(metadata ->> :k{counter})"
            if isinstance(value, dict):
                if "in" in value:
                    values = [item for item in value["in"] if item is not None]
                    if values:
                        clauses.append(f"{expression} = ANY(:{param})")
                        params[param] = values
                elif "ne" in value:
                    clauses.append(f"({expression} IS NULL OR {expression} <> :{param})")
                    params[param] = value["ne"]
                elif "eq" in value:
                    clauses.append(f"{expression} = :{param}")
                    params[param] = value["eq"]
            elif isinstance(value, (list, tuple, set)):
                values = [item for item in value if item is not None]
                if values:
                    clauses.append(f"{expression} = ANY(:{param})")
                    params[param] = list(values)
            elif value is not None:
                clauses.append(f"{expression} = :{param}")
                params[param] = value
        if not show_expired:
            clauses.append("(expires_at IS NULL OR expires_at > now())")
        return " AND ".join(clauses), params

    def _row_to_dict(self, row: Any) -> Dict[str, Any]:
        mapping = dict(row._mapping)
        meta = dict(mapping.get("meta")) if isinstance(mapping.get("meta"), dict) else {}
        # Legacy-shaped callers (research_memory.py) read kind/question_key from
        # metadata, so expose the indexed columns there as well.
        if mapping.get("kind"):
            meta.setdefault("kind", mapping.get("kind"))
        if mapping.get("question_key"):
            meta.setdefault("question_key", mapping.get("question_key"))
        created = mapping.get("created_at")
        updated = mapping.get("updated_at")
        expires = mapping.get("expires_at")
        item: Dict[str, Any] = {
            "id": mapping.get("id"),
            "memory": mapping.get("content") or "",
            "hash": mapping.get("content_hash"),
            "user_id": mapping.get("user_id"),
            "agent_id": mapping.get("agent_id"),
            "run_id": mapping.get("run_id"),
            "kind": mapping.get("kind"),
            "importance": mapping.get("importance"),
            "created_at": format_datetime(created) if isinstance(created, datetime) else created,
            "updated_at": format_datetime(updated) if isinstance(updated, datetime) else updated,
            "expiration_date": expires.date().isoformat() if isinstance(expires, datetime) else None,
            "metadata": meta,
        }
        for key in ("semantic_score", "fts_score", "score", "score_details"):
            if key in mapping:
                item[key] = mapping[key]
        return item

    def _recency_factor(self, item: Dict[str, Any], now: datetime) -> float:
        kind = str(item.get("kind") or "operational")
        if kind in {"identity", "configuration", "rule"}:
            return 1.0
        raw = item.get("updated_at") or item.get("created_at")
        if not isinstance(raw, datetime):
            return 1.0
        if raw.tzinfo is None:
            raw = raw.replace(tzinfo=timezone.utc)
        age_days = max(0.0, (now - raw).total_seconds() / 86_400)
        half_life = (
            self.settings.recall_half_life_days
            if kind in {"episode", "research_conclusion"}
            else self.settings.curated_half_life_days
        )
        if half_life <= 0:
            return 1.0
        return math.pow(0.5, age_days / half_life)
