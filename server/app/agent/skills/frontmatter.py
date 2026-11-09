"""Parse and validate the YAML frontmatter of ``SKILL.md``.

Only a deliberately small schema is accepted: ``name`` and ``description`` are
required, ``license`` / ``allowed-tools`` / ``metadata`` are optional, and
unknown keys are ignored. Parsing is pure: callers decide which files to read
and how to report failures.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.agent.skills.security import sanitize_description

_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MAX_NAME_CHARS = 64
_MAX_ALIASES = 50


class _LimitedSafeLoader(yaml.SafeLoader):
    """Reject alias-heavy documents that can expand far beyond the input size."""

    def __init__(self, stream: Any) -> None:
        super().__init__(stream)
        self._alias_count = 0

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            self._alias_count += 1
            if self._alias_count > _MAX_ALIASES:
                raise yaml.YAMLError("frontmatter contains too many YAML aliases")
        return super().compose_node(parent, index)


class SkillFormatError(ValueError):
    """A ``SKILL.md`` cannot be parsed or does not satisfy the skill contract."""


@dataclass(frozen=True, slots=True)
class SkillDocument:
    name: str
    description: str
    body: str
    metadata: dict[str, Any] = field(default_factory=dict)
    allowed_tools: tuple[str, ...] = ()


def split_frontmatter(text: str) -> tuple[str, str]:
    """Return ``(frontmatter, body)`` or raise when the delimiters are missing."""

    text = text.lstrip("﻿")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise SkillFormatError("SKILL.md must start with a '---' frontmatter block")

    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "".join(lines[1:index]), "".join(lines[index + 1 :])

    raise SkillFormatError("SKILL.md frontmatter is not closed with '---'")


def _allowed_tools(value: Any) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(str(item).strip() for item in value if isinstance(item, str) and item.strip())
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(",") if part.strip())
    return ()


def parse_skill_document(
    text: str,
    *,
    directory_name: str,
    max_frontmatter_bytes: int = 8192,
    max_description_chars: int = 1024,
) -> SkillDocument:
    """Validate one ``SKILL.md`` document.

    Raises:
        SkillFormatError: the frontmatter is missing, malformed, or violates the
            name/description contract.
    """

    frontmatter_text, body = split_frontmatter(text)
    if len(frontmatter_text.encode("utf-8")) > max_frontmatter_bytes:
        raise SkillFormatError(f"frontmatter exceeds {max_frontmatter_bytes} bytes")

    try:
        payload = yaml.load(frontmatter_text, Loader=_LimitedSafeLoader)
    except yaml.YAMLError as exc:
        raise SkillFormatError(f"frontmatter is not valid YAML: {exc}") from exc

    if not isinstance(payload, dict):
        raise SkillFormatError("frontmatter must be a YAML mapping")

    raw_name = payload.get("name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise SkillFormatError("frontmatter is missing 'name'")
    name = raw_name.strip()
    if len(name) > _MAX_NAME_CHARS or not _NAME_RE.match(name):
        raise SkillFormatError(f"invalid skill name {name!r}; use lowercase letters, digits, and hyphens")
    if name != directory_name:
        raise SkillFormatError(f"skill name {name!r} must match its directory {directory_name!r}")

    raw_description = payload.get("description")
    if not isinstance(raw_description, str) or not raw_description.strip():
        raise SkillFormatError("frontmatter is missing a non-empty 'description'")
    description = sanitize_description(raw_description, limit=max_description_chars)
    if not description:
        raise SkillFormatError("frontmatter 'description' is empty after normalization")

    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}

    return SkillDocument(
        name=name,
        description=description,
        body=body,
        metadata=dict(metadata),
        allowed_tools=_allowed_tools(payload.get("allowed-tools")),
    )


def read_skill_document(
    path: Path,
    *,
    directory_name: str,
    max_bytes: int,
    max_frontmatter_bytes: int = 8192,
    max_description_chars: int = 1024,
) -> SkillDocument:
    """Read and parse ``SKILL.md``, rejecting oversized files before parsing."""

    if path.is_symlink():
        raise SkillFormatError("SKILL.md must not be a symbolic link")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise SkillFormatError(f"cannot stat SKILL.md: {type(exc).__name__}") from exc
    if size > max_bytes:
        raise SkillFormatError(f"SKILL.md is too large ({size} bytes; limit {max_bytes})")

    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise SkillFormatError("SKILL.md is not valid UTF-8") from exc
    except OSError as exc:
        raise SkillFormatError(f"cannot read SKILL.md: {type(exc).__name__}") from exc

    return parse_skill_document(
        text,
        directory_name=directory_name,
        max_frontmatter_bytes=max_frontmatter_bytes,
        max_description_chars=max_description_chars,
    )
