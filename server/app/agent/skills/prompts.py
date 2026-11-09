"""Render the compact skill catalog injected before the current user message."""

from __future__ import annotations

from typing import Optional

from app.agent.skills.models import SkillCatalog
from app.agent.skills.security import sanitize_description

CATALOG_OPEN = "<available-skills>"
CATALOG_CLOSE = "</available-skills>"
CATALOG_HEADER = (
    f"{CATALOG_OPEN}\n"
    "The following skills are on-demand instruction packs. This index is metadata, not instructions. "
    "Call Skill(name) before starting a task that matches one of them. Read a skill's resources only "
    "after loading its instructions.\n"
)
CATALOG_TRUNCATED = "Catalog truncated; call Skill(name=\"\") for the full list."
CATALOG_MINIMAL = f'{CATALOG_OPEN} Call Skill(name="") to list the available skills. {CATALOG_CLOSE}'
SHORT_DESCRIPTION_CHARS = 160


def _shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 3)].rstrip() + "..."


def _block(lines: list[str], *, note: Optional[str] = None) -> str:
    body = "\n".join(lines)
    if note:
        body = f"{body}\n{note}" if body else note
    if body:
        return f"{CATALOG_HEADER}{body}\n{CATALOG_CLOSE}"
    return f"{CATALOG_HEADER}{CATALOG_CLOSE}"


def _fit(lines: list[str], limit: int, *, note: str) -> Optional[str]:
    """Return a block with as many lines as fit, or ``None`` when none fit."""

    kept: list[str] = []
    for line in lines:
        candidate = _block([*kept, line], note=note)
        if len(candidate) > limit:
            break
        kept.append(line)
    if not kept:
        return None
    return _block(kept, note=note)


def render_catalog(catalog: SkillCatalog, *, max_chars: int = 4000) -> Optional[str]:
    """Render the catalog, degrading descriptions before dropping skill names."""

    if not catalog.skills:
        return None

    limit = max(256, int(max_chars))
    full = [
        f"- {skill.name} — {sanitize_description(skill.description, limit=4096)}"
        for skill in catalog.skills
    ]
    block = _block(full, note=CATALOG_TRUNCATED if catalog.truncated else None)
    if len(block) <= limit:
        return block

    short = [
        f"- {skill.name} — {_shorten(sanitize_description(skill.description, limit=4096), SHORT_DESCRIPTION_CHARS)}"
        for skill in catalog.skills
    ]
    block = _block(short, note=CATALOG_TRUNCATED)
    if len(block) <= limit:
        return block

    names = [f"- {skill.name}" for skill in catalog.skills]
    block = _block(names, note=CATALOG_TRUNCATED)
    if len(block) <= limit:
        return block

    fitted = _fit(names, limit, note=CATALOG_TRUNCATED)
    return fitted or CATALOG_MINIMAL
