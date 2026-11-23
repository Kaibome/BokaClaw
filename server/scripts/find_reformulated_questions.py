"""Find sessions where the user repeated or reformulated the first question.

Run inside the server container (needs the database; embeddings are optional):

    docker exec bokaclaw-server python scripts/find_reformulated_questions.py \
        --days 30 --out /tmp/reformulated.json

The script compares every later user message with the first user message in the
same conversation. Lexical checks are always on (normalized equality, substring
containment, trigram overlap, sequence ratio). Vector checks use fastembed when
available and can be disabled with ``--lexical-only``.

Use ``--export-dir`` to write one JSON file per flagged conversation containing
messages, runs, spans and events.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from sqlalchemy import bindparam, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.db import SessionLocal  # noqa: E402

_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_NORMALIZE_RE = re.compile(r"[\s\W_]+", re.UNICODE)
_RELATIONS = ("exact", "extended", "paraphrase", "modified")


@dataclass(frozen=True)
class Thresholds:
    containment: float = 0.75
    trigram: float = 0.60
    sequence: float = 0.75
    embedding: float = 0.85


@dataclass
class Message:
    message_id: str
    conversation_id: str
    user_id: str
    content: str
    norm: str
    created_at: datetime
    turn: int


def normalize_text(value: str) -> str:
    """Fold width/case and drop punctuation/whitespace so trivial edits match."""

    folded = unicodedata.normalize("NFKC", value or "").lower()
    return _NORMALIZE_RE.sub("", folded)


def numbers(value: str) -> set[str]:
    return set(_NUMBER_RE.findall(value or ""))


def trigrams(value: str, size: int = 3) -> set[str]:
    if len(value) < size:
        return {value} if value else set()
    return {value[index : index + size] for index in range(len(value) - size + 1)}


def trigram_scores(first: str, later: str) -> tuple[float, float]:
    """Return (jaccard, containment); containment mirrors pg_trgm word_similarity."""

    first_grams = trigrams(first)
    later_grams = trigrams(later)
    if not first_grams or not later_grams:
        return 0.0, 0.0
    overlap = len(first_grams & later_grams)
    union = len(first_grams | later_grams)
    return overlap / union if union else 0.0, overlap / len(first_grams)


def sequence_ratio(first: str, later: str) -> float:
    if not first or not later:
        return 0.0
    # SequenceMatcher is quadratic; long prompts only need the cheap trigram score.
    if len(first) > 2_000 or len(later) > 2_000:
        return 0.0
    return SequenceMatcher(None, first, later).ratio()


def classify_relation(
    first_norm: str,
    later_norm: str,
    *,
    containment: float,
    jaccard: float,
    sequence: float,
    cosine: float | None,
    thresholds: Thresholds,
) -> str:
    if not first_norm or not later_norm:
        return "different"
    if first_norm == later_norm:
        return "exact"
    if first_norm in later_norm or (
        containment >= thresholds.containment and len(later_norm) > len(first_norm)
    ):
        return "extended"
    lexical_hit = jaccard >= thresholds.trigram or sequence >= thresholds.sequence
    vector_hit = cosine is not None and cosine >= thresholds.embedding
    if not lexical_hit and not vector_hit:
        return "different"
    # A changed number/date means the user changed the request, not the wording.
    return "modified" if numbers(first_norm) != numbers(later_norm) else "paraphrase"


def cosine(left: Iterable[float], right: Iterable[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=False))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30, help="Look back this many days.")
    parser.add_argument("--conversation-id", help="Only one conversation id.")
    parser.add_argument("--user-id", help="Only one user id (conversations.created_by).")
    parser.add_argument("--limit", type=int, default=20_000, help="Maximum user messages to load.")
    parser.add_argument("--min-length", type=int, default=4, help="Skip normalized questions shorter than this.")
    parser.add_argument("--lexical-only", action="store_true", help="Skip embeddings.")
    parser.add_argument("--embedding-model", default="BAAI/bge-small-zh-v1.5")
    parser.add_argument("--max-embed-messages", type=int, default=5_000)
    parser.add_argument("--containment-threshold", type=float, default=0.75)
    parser.add_argument("--trigram-threshold", type=float, default=0.60)
    parser.add_argument("--sequence-threshold", type=float, default=0.75)
    parser.add_argument("--embedding-threshold", type=float, default=0.85)
    parser.add_argument("--out", type=Path, help="Write candidates as JSON.")
    parser.add_argument("--export-dir", type=Path, help="Write full sessions for flagged conversations.")
    parser.add_argument("--max-span-chars", type=int, default=8_000, help="Truncate span strings; 0 keeps them.")
    return parser.parse_args()


def _load_messages(db: Session, args: argparse.Namespace) -> list[Message]:
    filters = ["m.role = 'user'", "m.created_at >= now() - make_interval(days => :days)"]
    params: dict[str, Any] = {"days": args.days, "limit": args.limit}
    if args.conversation_id:
        filters.append("m.conversation_id = :conversation_id")
        params["conversation_id"] = args.conversation_id
    if args.user_id:
        filters.append("c.created_by = :user_id")
        params["user_id"] = args.user_id
    statement = text(
        f"""
        SELECT m.id::text AS message_id,
               m.conversation_id::text AS conversation_id,
               c.created_by::text AS user_id,
               m.content,
               m.created_at
        FROM conversation_messages m
        JOIN conversations c ON c.id = m.conversation_id
        WHERE {' AND '.join(filters)}
        ORDER BY m.conversation_id, m.created_at, m.id
        LIMIT :limit
        """
    )
    rows = db.execute(statement, params).mappings().all()
    messages = [
        Message(
            message_id=row["message_id"],
            conversation_id=row["conversation_id"],
            user_id=row["user_id"],
            content=row["content"] or "",
            norm=normalize_text(row["content"] or ""),
            created_at=row["created_at"],
            turn=0,
        )
        for row in rows
    ]
    turn: dict[str, int] = defaultdict(int)
    for message in messages:
        turn[message.conversation_id] += 1
        message.turn = turn[message.conversation_id]
    return messages


def _load_run_ids(db: Session, conversations: Iterable[str]) -> dict[str, list[str]]:
    conversations = list(dict.fromkeys(conversations))
    if not conversations:
        return {}
    statement = text(
        """
        SELECT id::text AS run_id, user_message_id::text AS message_id
        FROM agent_runs
        WHERE conversation_id IN :conversations
        ORDER BY created_at
        """
    ).bindparams(bindparam("conversations", expanding=True))
    rows = db.execute(statement, {"conversations": conversations}).mappings().all()
    run_ids: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        if row["message_id"]:
            run_ids[row["message_id"]].append(row["run_id"])
    return run_ids


def _embed(texts: list[str], model_name: str) -> list[list[float]] | None:
    try:
        from fastembed import TextEmbedding
    except ImportError:
        sys.stderr.write("warning: fastembed is not installed; falling back to lexical only\n")
        return None
    kwargs: dict[str, Any] = {"model_name": model_name}
    cache_dir = os.getenv("FASTEMBED_CACHE_PATH")
    if cache_dir:
        kwargs["cache_dir"] = cache_dir
    try:
        model = TextEmbedding(**kwargs)
        vectors = [list(map(float, vector)) for vector in model.embed(texts)]
    except Exception as exc:  # noqa: BLE001 - optional accelerator, lexical still works
        sys.stderr.write(f"warning: embedding failed ({exc}); falling back to lexical only\n")
        return None
    normalized = []
    for vector in vectors:
        norm = math.sqrt(sum(value * value for value in vector))
        normalized.append([value / norm for value in vector] if norm else vector)
    return normalized


def _candidate_payload(
    message: Message,
    first: Message,
    relation: str,
    scores: dict[str, float],
    run_ids: dict[str, list[str]],
) -> dict[str, Any]:
    return {
        "conversation_id": message.conversation_id,
        "user_id": message.user_id,
        "relation": relation,
        "scores": {key: round(value, 4) for key, value in scores.items()},
        "numbers_changed": numbers(first.norm) != numbers(message.norm),
        "first": {
            "message_id": first.message_id,
            "turn": first.turn,
            "content": first.content,
            "created_at": first.created_at,
        },
        "later": {
            "message_id": message.message_id,
            "turn": message.turn,
            "content": message.content,
            "created_at": message.created_at,
        },
        "run_ids": run_ids.get(message.message_id, []),
    }


def _find_candidates(
    db: Session,
    messages: list[Message],
    args: argparse.Namespace,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[Message]] = defaultdict(list)
    for message in messages:
        if len(message.norm) >= args.min_length:
            grouped[message.conversation_id].append(message)

    thresholds = Thresholds(
        containment=args.containment_threshold,
        trigram=args.trigram_threshold,
        sequence=args.sequence_threshold,
        embedding=args.embedding_threshold,
    )
    vectors: list[list[float]] | None = None
    vector_index: dict[str, int] = {}
    if not args.lexical_only:
        flat = [message for group in grouped.values() for message in group]
        if len(flat) > args.max_embed_messages:
            sys.stderr.write(
                f"warning: {len(flat)} messages exceed --max-embed-messages; "
                "using vectors only for the first "
                f"{args.max_embed_messages}\n"
            )
            flat = flat[: args.max_embed_messages]
        if flat:
            vectors = _embed([message.content for message in flat], args.embedding_model)
            if vectors is not None:
                vector_index = {message.message_id: index for index, message in enumerate(flat)}

    candidates: list[dict[str, Any]] = []
    for group in grouped.values():
        if len(group) < 2:
            continue
        first = group[0]
        for message in group[1:]:
            jaccard, containment = trigram_scores(first.norm, message.norm)
            sequence = sequence_ratio(first.norm, message.norm)
            vector_cosine = None
            if vectors is not None:
                first_index = vector_index.get(first.message_id)
                later_index = vector_index.get(message.message_id)
                if first_index is not None and later_index is not None:
                    vector_cosine = cosine(vectors[first_index], vectors[later_index])
            relation = classify_relation(
                first.norm,
                message.norm,
                containment=containment,
                jaccard=jaccard,
                sequence=sequence,
                cosine=vector_cosine,
                thresholds=thresholds,
            )
            if relation == "different":
                continue
            scores = {"jaccard": jaccard, "containment": containment, "sequence": sequence}
            if vector_cosine is not None:
                scores["cosine"] = vector_cosine
            candidates.append(_candidate_payload(message, first, relation, scores, {}))

    run_ids = _load_run_ids(db, (candidate["conversation_id"] for candidate in candidates))
    for candidate in candidates:
        candidate["run_ids"] = run_ids.get(candidate["later"]["message_id"], [])
    return candidates


def _truncate(value: Any, limit: int) -> Any:
    if limit <= 0:
        return value
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "...[truncated]"
    if isinstance(value, dict):
        return {key: _truncate(item, limit) for key, item in value.items()}
    if isinstance(value, list):
        return [_truncate(item, limit) for item in value]
    return value


def _export_session(db: Session, conversation_id: str, directory: Path, max_chars: int) -> Path:
    messages = db.execute(
        text(
            """
            SELECT id::text, role, content, status, metadata, created_at
            FROM conversation_messages
            WHERE conversation_id = :conversation_id
            ORDER BY created_at
            """
        ),
        {"conversation_id": conversation_id},
    ).mappings().all()
    runs = db.execute(
        text(
            """
            SELECT id::text, status, error_code, error_detail, total_tokens,
                   user_message_id::text, created_at, finished_at
            FROM agent_runs
            WHERE conversation_id = :conversation_id
            ORDER BY created_at
            """
        ),
        {"conversation_id": conversation_id},
    ).mappings().all()
    spans = db.execute(
        text(
            """
            SELECT s.run_id::text, s.span_id, s.parent_span_id, s.span_type, s.name,
                   s.status, s.started_at, s.ended_at, s.input, s.output, s.attributes, s.error
            FROM agent_run_spans s
            JOIN agent_runs r ON r.id = s.run_id
            WHERE r.conversation_id = :conversation_id
            ORDER BY s.run_id, s.started_at, s.id
            """
        ),
        {"conversation_id": conversation_id},
    ).mappings().all()
    events = db.execute(
        text(
            """
            SELECT e.run_id::text, e.seq, e.event_type, e.payload, e.created_at
            FROM agent_run_events e
            JOIN agent_runs r ON r.id = e.run_id
            WHERE r.conversation_id = :conversation_id
            ORDER BY e.run_id, e.seq
            """
        ),
        {"conversation_id": conversation_id},
    ).mappings().all()
    payload = {
        "conversation_id": conversation_id,
        "messages": [_truncate(dict(row), max_chars) for row in messages],
        "runs": [dict(row) for row in runs],
        "spans": [_truncate(dict(row), max_chars) for row in spans],
        "events": [_truncate(dict(row), max_chars) for row in events],
    }
    path = directory / f"{conversation_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def main() -> int:
    args = _parse_args()
    with SessionLocal() as db:
        messages = _load_messages(db, args)
        candidates = _find_candidates(db, messages, args)
        candidates.sort(key=lambda row: (row["conversation_id"], row["later"]["turn"]))

        counts: dict[str, int] = defaultdict(int)
        for candidate in candidates:
            counts[candidate["relation"]] += 1

        # Write files before printing so a closed stdout (for example a pipe to
        # `head`) cannot skip the export.
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(candidates, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

        if args.export_dir:
            args.export_dir.mkdir(parents=True, exist_ok=True)
            conversations = sorted({candidate["conversation_id"] for candidate in candidates})
            for conversation_id in conversations:
                _export_session(db, conversation_id, args.export_dir, args.max_span_chars)

        sys.stdout.write(
            f"loaded {len(messages)} user messages; found {len(candidates)} candidate(s): "
            f"{dict(sorted(counts.items()))}\n"
        )
        for candidate in candidates[:10]:
            sys.stdout.write(
                f"  {candidate['conversation_id']} turn {candidate['later']['turn']} "
                f"{candidate['relation']} {candidate['scores']}\n"
                f"    {candidate['first']['content'][:80]!r} -> "
                f"{candidate['later']['content'][:80]!r}\n"
            )
        if args.out:
            sys.stdout.write(f"candidates: {args.out}\n")
        if args.export_dir:
            sys.stdout.write(f"exported {len(conversations)} session(s) to {args.export_dir}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
