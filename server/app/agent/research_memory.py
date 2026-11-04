"""Persist and reuse research conclusions across runs.

A successful web-research turn is valuable beyond the conversation it happened
in: the same user often asks the same question again weeks later and the agent
otherwise starts from zero. This module has two halves:

* ``remember_research`` runs after a completed turn. When the tool trace shows
  a successful ``WebSearch`` or ``WebFetch``, it stores the final answer, the
  source URLs, and the capture time as one user-scoped mem0 memory. Memories
  are labeled ``kind="research_conclusion"`` and written with ``infer=False``
  so the conclusion and its sources are preserved verbatim. An identical
  question updates the existing memory instead of appending a duplicate.
* ``prior_research_block`` runs before a turn. It first looks up an identical
  question by its stable key, then searches by similarity for paraphrases,
  drops anything older than the TTL, and renders a short block for the system
  prompt. The block tells the model that the conclusion may be stale and that
  re-verification is allowed.

Both halves are best-effort: memory being unavailable or slow must never fail
the turn. The caller can disable the feature with ``AGENT_RESEARCH_MEMORY``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

RESEARCH_KIND = "research_conclusion"
DEFAULT_TTL_DAYS = 30
DEFAULT_MIN_SCORE = 0.5
DEFAULT_TOP_K = 3

MAX_SOURCES = 8
MAX_SAVED_ANSWER_CHARS = 4_000
MAX_INJECTED_CHARS = 3_000
MAX_INJECTED_CONCLUSION_CHARS = 1_200

_WEB_SEARCH_NAMES = {"websearch"}
_WEB_FETCH_NAMES = {"webfetch"}


def memory_enabled() -> bool:
    raw = os.environ.get("AGENT_RESEARCH_MEMORY")
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def ttl_days_from_env() -> int:
    return max(1, _env_int("AGENT_RESEARCH_MEMORY_TTL_DAYS", DEFAULT_TTL_DAYS))


def min_score_from_env() -> float:
    return min(1.0, max(0.0, _env_float("AGENT_RESEARCH_MEMORY_MIN_SCORE", DEFAULT_MIN_SCORE)))


def question_key(question: str) -> str:
    """Stable key for exact question repeats, used to update instead of append."""

    normalized = " ".join(question.lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def extract_web_sources(entries: Iterable[Dict[str, Any]]) -> Tuple[List[Dict[str, str]], Optional[str]]:
    """Collect successful web sources from a ``ToolTrace`` snapshot.

    Returns ``(sources, fetched_at)`` where ``sources`` preserves first-seen
    order and ``fetched_at`` is the latest web tool ``finished_at`` value.
    """

    sources: Dict[str, Dict[str, str]] = {}
    fetched_at: Optional[str] = None

    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("is_error") or entry.get("status") != "completed":
            continue

        name = _normalize_tool_name(entry.get("name"))
        if name not in _WEB_SEARCH_NAMES and name not in _WEB_FETCH_NAMES:
            continue

        payload = _json_object(entry.get("content"))
        if name in _WEB_SEARCH_NAMES:
            if isinstance(payload, dict):
                for item in payload.get("results") or []:
                    if not isinstance(item, dict):
                        continue
                    url = str(item.get("url") or "").strip()
                    if url:
                        _add_source(sources, url, str(item.get("title") or "").strip())
        else:
            arguments = entry.get("arguments") if isinstance(entry.get("arguments"), dict) else {}
            url = ""
            if isinstance(payload, dict):
                url = str(payload.get("url") or "").strip()
            if not url:
                url = str(arguments.get("url") or "").strip()
            if url:
                _add_source(sources, url, "")

        finished_at = entry.get("finished_at")
        if isinstance(finished_at, str) and finished_at:
            fetched_at = finished_at if fetched_at is None else max(fetched_at, finished_at)

    return list(sources.values())[:MAX_SOURCES], fetched_at


def build_conclusion_text(
    question: str,
    answer: str,
    sources: List[Dict[str, str]],
    captured_at: str,
) -> str:
    lines = [
        "Research conclusion",
        f"Question: {_clip(question.strip(), 500)}",
        f"Answer: {_clip(answer.strip(), MAX_SAVED_ANSWER_CHARS)}",
        "Sources:",
    ]
    for source in sources:
        title = source.get("title") or ""
        prefix = f"{title} - " if title else ""
        lines.append(f"- {prefix}{source['url']}")
    lines.append(f"Captured at: {captured_at}")
    return "\n".join(lines)


def remember_research(
    payload: Dict[str, Any],
    content: str,
    entries: Iterable[Dict[str, Any]],
) -> Optional[str]:
    """Store one completed turn's research conclusion. Returns the memory id.

    The write is skipped unless the feature is enabled, the run is user-scoped,
    the answer is non-empty, and at least one successful web source exists.
    """

    if not memory_enabled():
        return None

    user_id = str(payload.get("user_id") or "").strip()
    question = str(payload.get("message") or "").strip()
    answer = (content or "").strip()
    if not user_id or not question or not answer:
        return None

    sources, fetched_at = extract_web_sources(entries)
    if not sources:
        return None

    captured_at = datetime.now(timezone.utc).isoformat()
    text = build_conclusion_text(question, answer, sources, captured_at)
    key = question_key(question)
    metadata: Dict[str, Any] = {
        "kind": RESEARCH_KIND,
        "question_key": key,
        "question": _clip(question, 500),
        "captured_at": captured_at,
        "sources": [source["url"] for source in sources],
    }
    if fetched_at:
        metadata["fetched_at"] = fetched_at
    for field in ("run_id", "conversation_id"):
        value = payload.get(field)
        if value:
            metadata[field] = str(value)

    memory = _memory()
    if memory is None:
        return None

    existing = _find_existing(memory, user_id, key)
    if existing:
        try:
            memory.update(existing, text=text, metadata=metadata)
            logger.info("research memory updated user=%s memory_id=%s", user_id, existing)
            return existing
        except Exception:
            # The memory may have been deleted between the lookup and the
            # update; fall through and add a fresh one.
            logger.warning("Failed to update research memory %s; adding a new one", existing, exc_info=True)

    try:
        response = memory.add(
            messages=[{"role": "assistant", "content": text}],
            user_id=user_id,
            metadata=metadata,
            infer=False,
        )
    except Exception:
        logger.warning("Failed to save research memory for user %s", user_id, exc_info=True)
        return None

    return _first_memory_id(response)


def prior_research_block(
    user_id: Optional[str],
    question: Optional[str],
    *,
    ttl_days: Optional[int] = None,
    min_score: Optional[float] = None,
    top_k: int = DEFAULT_TOP_K,
) -> str:
    """Render saved conclusions that are similar to ``question`` and still fresh."""

    if not memory_enabled():
        return ""
    user_id = (user_id or "").strip()
    question = (question or "").strip()
    if not user_id or not question:
        return ""

    ttl = ttl_days if ttl_days is not None else ttl_days_from_env()
    score_floor = min_score if min_score is not None else min_score_from_env()
    memory = _memory()
    if memory is None:
        return ""

    limit = max(1, top_k)
    now = datetime.now(timezone.utc)
    fresh: List[Dict[str, Any]] = []
    seen: set = set()

    # An identical question is a certain match, so try the stable question key
    # first. Its vector score is not reliable: a long stored conclusion can sit
    # below the threshold even when the question is word-for-word the same.
    for item in _exact_matches(memory, user_id, question_key(question)):
        _append_fresh(fresh, seen, item, now=now, ttl=ttl)
        if len(fresh) >= limit:
            return _render_block(fresh)

    # Different phrasings are found by similarity. Here the floor does real
    # work: unrelated Chinese questions can score around 0.4 against a stored
    # conclusion, so the default 0.5 keeps them out while paraphrases pass.
    try:
        response = memory.search(
            query=question,
            filters={"user_id": user_id, "kind": RESEARCH_KIND},
            top_k=limit,
            threshold=max(0.0, score_floor - 0.1),
        )
    except Exception:
        logger.warning("Failed to search research memories for user %s", user_id, exc_info=True)
        return _render_block(fresh)

    for item in _results(response):
        _append_fresh(fresh, seen, item, now=now, ttl=ttl, score_floor=score_floor)
        if len(fresh) >= limit:
            break

    return _render_block(fresh)


def _render_block(items: List[Dict[str, Any]]) -> str:
    if not items:
        return ""

    lines = [
        "## Reusable research from earlier runs",
        "The conclusions below were saved by you for this user on earlier turns. They may be",
        "outdated. Reuse one only if it still answers the current question; when the facts may",
        "have changed, verify them with WebSearch/WebFetch and prefer what you just read.",
        "",
    ]
    used = 0
    for index, item in enumerate(items, start=1):
        captured: datetime = item["captured_at"]
        age_days: float = item["age_days"]
        age = "today" if age_days < 1 else f"{int(age_days)} day(s) ago"
        text = _clip(item["text"], MAX_INJECTED_CONCLUSION_CHARS)
        entry = f"{index}. [saved {captured.date().isoformat()}, {age}]\n{text}"
        if used + len(entry) > MAX_INJECTED_CHARS:
            break
        lines.append(entry)
        used += len(entry)

    return "\n".join(lines)


def _find_existing(memory: Any, user_id: str, key: str) -> Optional[str]:
    for item in _exact_matches(memory, user_id, key):
        if item.get("id"):
            return str(item["id"])
    return None


def _exact_matches(memory: Any, user_id: str, key: str) -> List[Dict[str, Any]]:
    """Return memories whose metadata proves they are this question's conclusion."""

    try:
        response = memory.get_all(
            filters={"user_id": user_id, "kind": RESEARCH_KIND, "question_key": key},
            top_k=5,
        )
    except Exception:
        return []

    matches: List[Dict[str, Any]] = []
    for item in _results(response):
        if not isinstance(item, dict):
            continue
        # Some vector stores ignore metadata filters; never trust an item that
        # is not provably this question's conclusion.
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        if metadata.get("kind") == RESEARCH_KIND and metadata.get("question_key") == key:
            matches.append(item)
    return matches


def _append_fresh(
    fresh: List[Dict[str, Any]],
    seen: set,
    item: Any,
    *,
    now: datetime,
    ttl: int,
    score_floor: Optional[float] = None,
) -> None:
    entry = _fresh_entry(item, now=now, ttl=ttl, score_floor=score_floor)
    if entry is None:
        return
    key = entry["id"] or entry["text"][:80]
    if key in seen:
        return
    seen.add(key)
    fresh.append(entry)


def _fresh_entry(
    item: Any,
    *,
    now: datetime,
    ttl: int,
    score_floor: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Validate one memory item and turn it into a renderable conclusion."""

    if not isinstance(item, dict):
        return None
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    if metadata.get("kind") != RESEARCH_KIND:
        return None
    if score_floor is not None and _float(item.get("score"), 1.0) < score_floor:
        return None
    captured = _parse_datetime(metadata.get("captured_at") or item.get("created_at"))
    if captured is None:
        return None
    age_days = max(0.0, (now - captured).total_seconds() / 86_400)
    if age_days > ttl:
        return None
    text = str(item.get("memory") or "").strip()
    if not text:
        return None
    return {
        "id": str(item.get("id") or ""),
        "text": text,
        "captured_at": captured,
        "age_days": age_days,
    }


def _memory() -> Any:
    try:
        from app.core.server_state import get_memory_instance

        return get_memory_instance()
    except Exception:
        logger.debug("Research memory is unavailable", exc_info=True)
        return None


def _results(response: Any) -> List[Any]:
    if isinstance(response, dict):
        results = response.get("results")
    else:
        results = response
    return results if isinstance(results, list) else []


def _first_memory_id(response: Any) -> Optional[str]:
    for item in _results(response):
        if isinstance(item, dict) and item.get("id"):
            return str(item["id"])
    return None


def _add_source(sources: Dict[str, Dict[str, str]], url: str, title: str) -> None:
    key = _source_key(url)
    if not key:
        return
    current = sources.get(key)
    if current is None:
        sources[key] = {"url": url.strip(), "title": title}
    elif not current.get("title") and title:
        current["title"] = title


def _source_key(url: str) -> str:
    """Dedupe URLs without treating path case as insignificant."""

    text = url.strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return text.rstrip("/")
    if not parts.scheme and not parts.netloc:
        return text.rstrip("/")
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def _normalize_tool_name(name: Any) -> str:
    return str(name or "").replace("_", "").replace("-", "").strip().lower()


def _json_object(text: Any) -> Any:
    if not isinstance(text, str):
        return None
    value = text.strip()
    if not value or value[0] not in "[{":
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _parse_datetime(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
