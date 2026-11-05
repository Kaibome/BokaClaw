"""Persist and reuse research conclusions across runs.

A successful web-research turn is valuable beyond the conversation it happened
in: the same user often asks the same question again weeks later and the agent
otherwise starts from zero. This module has two halves:

* ``remember_research`` (and its reporting form ``save_research``) runs after a
  completed turn. When the tool trace shows a successful ``WebSearch`` or
  ``WebFetch``, it stores the final answer, the source URLs, and the capture
  time as one user-scoped mem0 memory. Memories are labeled
  ``kind="research_conclusion"`` and written with ``infer=False`` so the
  conclusion and its sources are preserved verbatim. An identical question
  updates the existing memory instead of appending a duplicate.
* ``prior_research_block`` (and its reporting form ``recall_research``) runs
  before a turn. It first looks up an identical question by its stable key,
  then searches by similarity for paraphrases, drops anything older than the
  TTL, and renders a short block for the system prompt. The block tells the
  model to reuse a saved conclusion instead of repeating the same web research.

The reporting forms return a ``ResearchRecall`` / ``ResearchSave`` dataclass so
the runner can persist a ``memory.recall`` / ``memory.save`` event and span;
without them a lookup that found nothing looks identical to one that never ran.

Both halves are best-effort: memory being unavailable or slow must never fail
the turn. The caller can disable the feature with ``AGENT_RESEARCH_MEMORY``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

RESEARCH_KIND = "research_conclusion"
DEFAULT_TTL_DAYS = 30
DEFAULT_MIN_SCORE = 0.5
DEFAULT_TOP_K = 3
# mem0 v1.1 returns a hybrid ``score``: (semantic + bm25 + entity) / max_possible,
# where ``max_possible`` is 2.0 whenever any candidate in the pool has a keyword
# hit. Comparing that value to a fixed cosine-like floor makes recall depend on
# the rest of the candidate pool, so the floor is applied to the raw semantic
# score exposed by ``explain=True`` instead.
DETAILS_KEY = "score_details"

MAX_SOURCES = 8
MAX_SAVED_ANSWER_CHARS = 4_000
MAX_INJECTED_CHARS = 3_000
MAX_INJECTED_CONCLUSION_CHARS = 1_200

_WEB_SEARCH_NAMES = {"websearch"}
_WEB_FETCH_NAMES = {"webfetch"}


@dataclass
class RecallHit:
    """One saved conclusion that made it into the injected block."""

    memory_id: str
    question: str
    source: str  # "exact" | "vector"
    score: Optional[float] = None  # semantic score when the store reports it
    combined_score: Optional[float] = None
    saved_at: Optional[str] = None
    chars: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "question": _clip(self.question, 200),
            "source": self.source,
            "score": self.score,
            "combined_score": self.combined_score,
            "saved_at": self.saved_at,
            "chars": self.chars,
        }


@dataclass
class ResearchRecall:
    """What one pre-run lookup did, including what it rejected and why."""

    question: str = ""
    user_id: str = ""
    block: str = ""
    hits: List[RecallHit] = field(default_factory=list)
    floor: float = DEFAULT_MIN_SCORE
    ttl_days: int = DEFAULT_TTL_DAYS
    candidates: int = 0
    skipped_low_score: int = 0
    skipped_stale: int = 0
    skipped_invalid: int = 0
    skipped_reason: Optional[str] = None  # disabled | no_user | no_question | memory_unavailable
    error: Optional[str] = None

    @property
    def chars(self) -> int:
        return len(self.block)


@dataclass
class ResearchSave:
    """What the post-turn write decided to do."""

    action: str = "skipped"  # added | updated | skipped | failed
    question: str = ""
    memory_id: Optional[str] = None
    sources: int = 0
    chars: int = 0
    reason: Optional[str] = None  # why a skip happened
    error: Optional[str] = None
    duration_ms: Optional[int] = None


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
    """Store one completed turn's research conclusion. Returns the memory id."""

    report = save_research(payload, content, entries)
    return report.memory_id if report.action in {"added", "updated"} else None


def save_research(
    payload: Dict[str, Any],
    content: str,
    entries: Iterable[Dict[str, Any]],
) -> ResearchSave:
    """Store one completed turn's research conclusion and report the outcome.

    The write is skipped unless the feature is enabled, the run is user-scoped,
    the answer is non-empty, and at least one successful web source exists.
    """

    question = str(payload.get("message") or "").strip()
    report = ResearchSave(question=question)

    if not memory_enabled():
        report.reason = "disabled"
        return report

    user_id = str(payload.get("user_id") or "").strip()
    answer = (content or "").strip()
    if not user_id:
        report.reason = "no_user"
        return report
    if not question:
        report.reason = "no_question"
        return report
    if not answer:
        report.reason = "no_answer"
        return report

    sources, fetched_at = extract_web_sources(entries)
    if not sources:
        report.reason = "no_sources"
        return report

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
    for name in ("run_id", "conversation_id"):
        value = payload.get(name)
        if value:
            metadata[name] = str(value)

    memory = _memory()
    if memory is None:
        report.reason = "memory_unavailable"
        return report

    report.sources = len(sources)
    report.chars = len(text)

    existing = _find_existing(memory, user_id, key)
    if existing:
        try:
            memory.update(existing, text=text, metadata=metadata)
            logger.info("research memory updated user=%s memory_id=%s", user_id, existing)
            report.action = "updated"
            report.memory_id = str(existing)
            return report
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
    except Exception as exc:
        logger.warning("Failed to save research memory for user %s", user_id, exc_info=True)
        report.action = "failed"
        report.error = f"{type(exc).__name__}: {exc}"
        return report

    report.action = "added"
    report.memory_id = _first_memory_id(response)
    return report


def prior_research_block(
    user_id: Optional[str],
    question: Optional[str],
    *,
    ttl_days: Optional[int] = None,
    min_score: Optional[float] = None,
    top_k: int = DEFAULT_TOP_K,
) -> str:
    """Render saved conclusions that are similar to ``question`` and still fresh."""

    return recall_research(
        user_id,
        question,
        ttl_days=ttl_days,
        min_score=min_score,
        top_k=top_k,
    ).block


def recall_research(
    user_id: Optional[str],
    question: Optional[str],
    *,
    ttl_days: Optional[int] = None,
    min_score: Optional[float] = None,
    top_k: int = DEFAULT_TOP_K,
) -> ResearchRecall:
    """Look up saved conclusions before a turn and report what happened."""

    report = ResearchRecall(
        question=(question or "").strip(),
        user_id=(user_id or "").strip(),
        floor=min_score if min_score is not None else min_score_from_env(),
        ttl_days=ttl_days if ttl_days is not None else ttl_days_from_env(),
    )
    if not memory_enabled():
        report.skipped_reason = "disabled"
        return report
    if not report.user_id:
        report.skipped_reason = "no_user"
        return report
    if not report.question:
        report.skipped_reason = "no_question"
        return report

    memory = _memory()
    if memory is None:
        report.skipped_reason = "memory_unavailable"
        return report

    ttl = report.ttl_days
    score_floor = report.floor
    limit = max(1, top_k)
    now = datetime.now(timezone.utc)
    fresh: List[Dict[str, Any]] = []
    seen: set = set()

    # An identical question is a certain match, so try the stable question key
    # first. Its vector score is not reliable: a long stored conclusion can sit
    # below the threshold even when the question is word-for-word the same.
    for item in _exact_matches(memory, report.user_id, question_key(report.question)):
        reason = _append_fresh(fresh, seen, item, now=now, ttl=ttl, source="exact")
        _count_skip(report, reason)
        if len(fresh) >= limit:
            break

    if len(fresh) < limit:
        # Different phrasings are found by similarity. The floor is applied to
        # the raw semantic score, not to mem0's pool-dependent hybrid score.
        items, error = _search(memory, report.question, report.user_id, limit, score_floor)
        if error is not None:
            report.error = error
        report.candidates = len(items)
        for item in items:
            reason = _append_fresh(
                fresh,
                seen,
                item,
                now=now,
                ttl=ttl,
                score_floor=score_floor,
                source="vector",
            )
            _count_skip(report, reason)
            if len(fresh) >= limit:
                break

    report.hits = _build_hits(fresh)
    report.block = _render_block(fresh)
    return report


def _search(
    memory: Any,
    question: str,
    user_id: str,
    limit: int,
    score_floor: float,
) -> Tuple[List[Any], Optional[str]]:
    """Run the similarity search, keeping ``explain`` optional for older stores."""

    params: Dict[str, Any] = {
        "query": question,
        "filters": {"user_id": user_id, "kind": RESEARCH_KIND},
        "top_k": limit,
        "threshold": max(0.0, score_floor - 0.1),
    }
    try:
        try:
            response = memory.search(explain=True, **params)
        except TypeError:
            # A store without ``explain`` support: fall back to the hybrid score.
            response = memory.search(**params)
    except Exception as exc:
        logger.warning("Failed to search research memories for user %s", user_id, exc_info=True)
        return [], f"{type(exc).__name__}: {exc}"
    return _results(response), None


def _count_skip(report: ResearchRecall, reason: Optional[str]) -> None:
    if reason == "low_score":
        report.skipped_low_score += 1
    elif reason == "stale":
        report.skipped_stale += 1
    elif reason in {"not_research", "no_time", "empty"}:
        report.skipped_invalid += 1


def _build_hits(fresh: List[Dict[str, Any]]) -> List[RecallHit]:
    hits: List[RecallHit] = []
    for entry in fresh:
        hits.append(
            RecallHit(
                memory_id=str(entry.get("id") or ""),
                question=str(entry.get("question") or ""),
                source=str(entry.get("source") or "vector"),
                score=entry.get("score"),
                combined_score=entry.get("combined_score"),
                saved_at=entry.get("saved_at"),
                chars=len(str(entry.get("text") or "")),
            )
        )
    return hits


def _render_block(items: List[Dict[str, Any]]) -> str:
    if not items:
        return ""

    lines = [
        "## Reusable research from earlier runs",
        "The conclusions below were saved by you for this user on earlier turns. Reuse one when",
        "it answers the current question instead of repeating the same web research.",
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
    source: str = "vector",
) -> Optional[str]:
    """Add one acceptable conclusion; return why it was rejected, or ``None``."""

    entry, reason = _fresh_entry(item, now=now, ttl=ttl, score_floor=score_floor)
    if entry is None:
        return reason
    key = entry["id"] or entry["text"][:80]
    if key in seen:
        return "duplicate"
    seen.add(key)
    entry["source"] = source
    fresh.append(entry)
    return None


def _fresh_entry(
    item: Any,
    *,
    now: datetime,
    ttl: int,
    score_floor: Optional[float] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Validate one memory item and turn it into a renderable conclusion.

    The floor is compared against the raw semantic score from mem0's
    ``score_details``. The top-level ``score`` is the hybrid value, whose
    divisor shrinks and grows with the candidate pool, so it is only kept for
    reporting and as a fallback for stores that do not explain their scores.
    """

    if not isinstance(item, dict):
        return None, "invalid"
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    if metadata.get("kind") != RESEARCH_KIND:
        return None, "not_research"

    combined_score = _optional_float(item.get("score"))
    details = item.get(DETAILS_KEY)
    semantic_score = None
    if isinstance(details, dict):
        semantic_score = _optional_float(details.get("semantic_score"))
    if semantic_score is None:
        semantic_score = combined_score
    if score_floor is not None and semantic_score is not None and semantic_score < score_floor:
        return None, "low_score"

    captured = _parse_datetime(metadata.get("captured_at") or item.get("created_at"))
    if captured is None:
        return None, "no_time"
    age_days = max(0.0, (now - captured).total_seconds() / 86_400)
    if age_days > ttl:
        return None, "stale"
    text = str(item.get("memory") or "").strip()
    if not text:
        return None, "empty"
    return {
        "id": str(item.get("id") or ""),
        "text": text,
        "question": str(metadata.get("question") or ""),
        "captured_at": captured,
        "age_days": age_days,
        "score": semantic_score,
        "combined_score": combined_score,
        "saved_at": captured.isoformat(),
    }, None


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


def _optional_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
