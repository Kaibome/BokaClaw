"""Data structures shared by skill discovery, caching, and tool building."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

SkillSource = Literal["global", "workspace", "user"]
ResourceKind = Literal["text", "binary"]


@dataclass(frozen=True, slots=True)
class SkillRoot:
    """One directory scanned for skills, tagged with its trust source."""

    path: Path
    source: SkillSource


@dataclass(frozen=True, slots=True)
class SkillResource:
    """One non-entry file inside a skill directory."""

    path: str
    size: int
    kind: ResourceKind


@dataclass(frozen=True, slots=True)
class SkillRecord:
    """A validated skill ready to be exposed to the model."""

    name: str
    description: str
    source: SkillSource
    root: Path
    directory: Path
    skill_md: Path
    metadata: dict[str, Any] = field(default_factory=dict)
    allowed_tools: tuple[str, ...] = ()
    resources: tuple[SkillResource, ...] = ()
    scripts: tuple[str, ...] = ()

    @property
    def has_scripts(self) -> bool:
        return bool(self.scripts)


@dataclass(frozen=True, slots=True)
class SkillCatalog:
    """The merged, de-duplicated view of every skill for one user."""

    skills: tuple[SkillRecord, ...]
    truncated: bool = False
    errors: tuple[str, ...] = ()

    def get(self, name: str) -> SkillRecord | None:
        for skill in self.skills:
            if skill.name == name:
                return skill
        return None
