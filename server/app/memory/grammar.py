"""Markdown grammar for workspace memory.

Each entry is a Markdown bullet followed by an indented ``<!-- memory: {...} -->``
comment. The bullet is the human-readable fact; the comment carries the stable
id and the metadata the index needs. A missing or malformed comment is not fatal:
the entry is treated as a manual one and defaults are derived from the heading
and the file mtime.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

logger = logging.getLogger(__name__)

HEADING_KIND: Dict[str, str] = {
    "identity": "identity",
    "profile": "identity",
    "configuration": "configuration",
    "config": "configuration",
    "rules": "rule",
    "rule": "rule",
    "preferences": "preference",
    "preference": "preference",
    "decisions": "decision",
    "decision": "decision",
    "technical": "technical",
    "relationships": "relationship",
    "relationship": "relationship",
    "projects": "project",
    "project": "project",
    "operational": "operational",
    "captured": "episode",
    "episodes": "episode",
    "research": "research_conclusion",
}

KIND_HEADING: Dict[str, str] = {
    "identity": "Identity",
    "configuration": "Configuration",
    "rule": "Rules",
    "preference": "Preferences",
    "decision": "Decisions",
    "technical": "Technical",
    "relationship": "Relationships",
    "project": "Projects",
    "operational": "Operational",
    "episode": "Captured",
    "research_conclusion": "Research",
}

DEFAULT_IMPORTANCE: Dict[str, float] = {
    "identity": 0.95,
    "configuration": 0.95,
    "rule": 0.90,
    "preference": 0.85,
    "decision": 0.80,
    "technical": 0.80,
    "relationship": 0.75,
    "project": 0.75,
    "operational": 0.60,
    "episode": 0.60,
    "research_conclusion": 0.80,
}

CURATED_KINDS = frozenset(
    {
        "identity",
        "configuration",
        "rule",
        "preference",
        "decision",
        "technical",
        "relationship",
        "project",
        "operational",
    }
)
DAILY_KINDS = frozenset({"episode", "research_conclusion"})

STANDARD_META_KEYS = frozenset(
    {
        "id",
        "kind",
        "importance",
        "source",
        "trust",
        "created",
        "updated",
        "expires",
        "question_key",
        "sources",
        "agent_id",
        "run_id",
        "conversation_id",
        "project",
    }
)

_META_RE = re.compile(r"^\s*<!--\s*memory:\s*(\{.*\})\s*-->\s*$")
_BULLET_RE = re.compile(r"^-\s+(.*)$")
_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")


@dataclass
class MemoryEntry:
    id: str
    kind: str
    content: str
    importance: float
    source: str = "manual"
    trust: str = "owner"
    created: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires: datetime | None = None
    question_key: str | None = None
    sources: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    heading: str | None = None
    path: str = ""
    ordinal: int = 0
    user_id: str = ""
    agent_id: str | None = None
    run_id: str | None = None
    conversation_id: str | None = None
    project: str | None = None

    def meta_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "importance": round(float(self.importance), 4),
            "source": self.source,
            "trust": self.trust,
            "created": format_datetime(self.created),
            "updated": format_datetime(self.updated),
        }
        if self.expires is not None:
            payload["expires"] = format_datetime(self.expires)
        if self.question_key:
            payload["question_key"] = self.question_key
        if self.sources:
            payload["sources"] = list(self.sources)
        for key, value in (
            ("agent_id", self.agent_id),
            ("run_id", self.run_id),
            ("conversation_id", self.conversation_id),
            ("project", self.project),
        ):
            if value:
                payload[key] = value
        for key, value in self.metadata.items():
            if key not in STANDARD_META_KEYS:
                payload[key] = value
        return payload


def normalize_text(text: str) -> str:
    value = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value


def stable_id(kind: str, content: str) -> str:
    digest = hashlib.sha256(f"{kind}\n{normalize_text(content)}".encode("utf-8")).hexdigest()[:16]
    return f"m-{digest}"


def default_importance(kind: str) -> float:
    return DEFAULT_IMPORTANCE.get(kind, 0.5)


def heading_for_kind(kind: str) -> str:
    return KIND_HEADING.get(kind, "Operational")


def kind_for_heading(heading: str | None, default: str = "operational") -> str:
    if not heading:
        return default
    return HEADING_KIND.get(heading.strip().lower(), default)


def parse_datetime(value: Any, default: datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return default
    else:
        return default
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _coerce_float(value: Any, default: float) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _coerce_sources(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return []


def _entry_from(
    *,
    meta: Dict[str, Any] | None,
    content: str,
    heading: str | None,
    path: str,
    ordinal: int,
    mtime: datetime,
) -> MemoryEntry | None:
    text = normalize_text(content)
    if not text:
        return None
    meta = meta or {}
    kind = str(meta.get("kind") or kind_for_heading(heading))
    if kind not in DEFAULT_IMPORTANCE:
        kind = "operational"
    created = parse_datetime(meta.get("created"), mtime)
    updated = parse_datetime(meta.get("updated"), created)
    expires = parse_datetime(meta.get("expires"), None) if meta.get("expires") else None
    entry_id = str(meta.get("id") or stable_id(kind, text))
    extra = {key: value for key, value in meta.items() if key not in STANDARD_META_KEYS}
    return MemoryEntry(
        id=entry_id,
        kind=kind,
        content=text,
        importance=_coerce_float(meta.get("importance"), default_importance(kind)),
        source=str(meta.get("source") or "manual"),
        trust=str(meta.get("trust") or "owner"),
        created=created,
        updated=updated,
        expires=expires,
        question_key=str(meta["question_key"]) if meta.get("question_key") else None,
        sources=_coerce_sources(meta.get("sources")),
        metadata=extra,
        heading=heading,
        path=path,
        ordinal=ordinal,
        agent_id=str(meta["agent_id"]) if meta.get("agent_id") else None,
        run_id=str(meta["run_id"]) if meta.get("run_id") else None,
        conversation_id=str(meta["conversation_id"]) if meta.get("conversation_id") else None,
        project=str(meta["project"]) if meta.get("project") else None,
    )


def parse_document(
    text: str,
    *,
    path: str = "",
    mtime: datetime | None = None,
    default_heading: str | None = None,
) -> List[MemoryEntry]:
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    mtime = mtime or datetime.now(timezone.utc)
    entries: List[MemoryEntry] = []
    heading = default_heading
    ordinal = 0
    index = 0

    while index < len(lines):
        line = lines[index]
        heading_match = _HEADING_RE.match(line)
        if heading_match:
            heading = heading_match.group(1).strip()
            index += 1
            continue

        bullet = _BULLET_RE.match(line)
        if not bullet:
            index += 1
            continue

        content_lines = [bullet.group(1).rstrip()]
        meta: Dict[str, Any] | None = None
        index += 1
        while index < len(lines):
            current = lines[index]
            if _META_RE.match(current):
                raw = _META_RE.match(current).group(1)
                try:
                    parsed = json.loads(raw)
                    meta = parsed if isinstance(parsed, dict) else None
                except (TypeError, ValueError):
                    logger.warning("Ignoring malformed memory metadata in %s", path or "<memory>")
                index += 1
                break
            if current.strip() == "":
                # A blank line only continues the entry when the next line is
                # still part of it; otherwise it is the gap before the next item.
                lookahead = index + 1
                if lookahead < len(lines) and (
                    lines[lookahead].startswith((" ", "\t")) or _META_RE.match(lines[lookahead])
                ):
                    content_lines.append("")
                    index += 1
                    continue
                break
            if current.startswith((" ", "\t")):
                content_lines.append(current[2:] if current.startswith("  ") else current.lstrip())
                index += 1
                continue
            break

        entry = _entry_from(
            meta=meta,
            content="\n".join(content_lines),
            heading=heading,
            path=path,
            ordinal=ordinal,
            mtime=mtime,
        )
        if entry is not None:
            entries.append(entry)
            ordinal += 1

    return entries


def _render_meta(entry: MemoryEntry) -> str:
    return json.dumps(entry.meta_payload(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def render_entry(entry: MemoryEntry) -> str:
    content_lines = normalize_text(entry.content).split("\n") or [""]
    rendered = [f"- {content_lines[0]}"]
    for line in content_lines[1:]:
        rendered.append(f"  {line}" if line else "")
    rendered.append(f"  <!-- memory: {_render_meta(entry)} -->")
    return "\n".join(rendered)


def render_document(entries: Iterable[MemoryEntry], title: str) -> str:
    grouped: Dict[str, List[MemoryEntry]] = {}
    for entry in entries:
        heading = entry.heading or heading_for_kind(entry.kind)
        grouped.setdefault(heading, []).append(entry)

    blocks = [f"# {title}"]
    for heading, items in grouped.items():
        blocks.append(f"## {heading}")
        for entry in items:
            blocks.append(render_entry(entry))
        blocks.append("")
    return "\n".join(blocks).rstrip() + "\n"


def document_title(path: Path) -> str:
    name = path.name
    if name == "USER.md":
        return "User"
    if name == "MEMORY.md":
        return "Memory"
    if path.parent.name == "memory":
        return path.stem
    return path.stem
