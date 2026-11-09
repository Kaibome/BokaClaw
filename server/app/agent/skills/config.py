"""Parse ``AGENT_SKILLS_*`` configuration into immutable settings.

Discovery is best-effort: a missing root, an unreadable directory, or one
malformed skill never fails an agent run. ``warnings`` records validation
problems so the caller can log them once.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

from app.agent.skills.models import SkillRoot

logger = logging.getLogger(__name__)

DEFAULT_SKILLS_ROOT = "/app/skills"
DEFAULT_WORKSPACE_ROOT = "/app/workspace"
DEFAULT_MEMORY_ROOT = "/app/memory"
DEFAULT_CATALOG_TTL_SECONDS = 30
DEFAULT_MAX_SKILLS = 100
DEFAULT_MAX_DESCRIPTION_CHARS = 1024
DEFAULT_CATALOG_MAX_CHARS = 4000
DEFAULT_MAX_SKILL_MD_BYTES = 262_144
DEFAULT_MAX_FRONTMATTER_BYTES = 8_192
DEFAULT_MAX_RESOURCE_BYTES = 1_048_576
DEFAULT_RESOURCE_MAX_CHARS = 20_000
DEFAULT_SCRIPT_TIMEOUT_SECONDS = 120
DEFAULT_SCRIPT_MAX_BYTES = 262_144
DEFAULT_SCRIPT_APPROVAL = "always"
DEFAULT_SCRIPT_MAX_BUNDLE_FILES = 200
DEFAULT_SCRIPT_MAX_BUNDLE_BYTES = 5_000_000
DEFAULT_SCRIPT_MAX_ARGS_CHARS = 4_096

_WARNED: set[str] = set()


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, low: int, high: int) -> int:
    try:
        value = int(float(_env(name) or default))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def _resolve_optional(value: str) -> Optional[Path]:
    text = value.strip()
    if not text:
        return None
    try:
        return Path(text).expanduser().resolve()
    except (OSError, RuntimeError):
        return None


def _is_inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class SkillsConfig:
    """Settings for skill discovery and the ``Skill`` tool."""

    global_root: Optional[Path] = None
    user_root_base: Optional[Path] = None
    workspace_root: Optional[Path] = None
    catalog_ttl_seconds: int = DEFAULT_CATALOG_TTL_SECONDS
    max_skills: int = DEFAULT_MAX_SKILLS
    max_description_chars: int = DEFAULT_MAX_DESCRIPTION_CHARS
    catalog_max_chars: int = DEFAULT_CATALOG_MAX_CHARS
    max_skill_md_bytes: int = DEFAULT_MAX_SKILL_MD_BYTES
    max_frontmatter_bytes: int = DEFAULT_MAX_FRONTMATTER_BYTES
    max_resource_bytes: int = DEFAULT_MAX_RESOURCE_BYTES
    resource_max_chars: int = DEFAULT_RESOURCE_MAX_CHARS
    allow_scripts: bool = False
    script_timeout_seconds: int = DEFAULT_SCRIPT_TIMEOUT_SECONDS
    script_max_bytes: int = DEFAULT_SCRIPT_MAX_BYTES
    script_approval: Literal["always", "auto"] = "always"
    script_max_bundle_files: int = DEFAULT_SCRIPT_MAX_BUNDLE_FILES
    script_max_bundle_bytes: int = DEFAULT_SCRIPT_MAX_BUNDLE_BYTES
    script_max_args_chars: int = DEFAULT_SCRIPT_MAX_ARGS_CHARS
    warnings: tuple[str, ...] = ()

    def roots_for_user(self, user_id: str) -> tuple[SkillRoot, ...]:
        """Return roots in precedence order: user, workspace, global."""

        roots: list[SkillRoot] = []
        if user_id and self.user_root_base is not None:
            # Reuse the memory path normalization so UUIDs are canonical and
            # non-UUID ids become a stable hash instead of a raw path segment.
            from app.memory.paths import user_key

            roots.append(SkillRoot(self.user_root_base / user_key(user_id), "user"))
        if self.workspace_root is not None:
            roots.append(SkillRoot(self.workspace_root, "workspace"))
        if self.global_root is not None:
            roots.append(SkillRoot(self.global_root, "global"))

        deduped: list[SkillRoot] = []
        seen: set[str] = set()
        for root in roots:
            key = str(root.path)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(root)
        return tuple(deduped)


def load_skills_config() -> Optional[SkillsConfig]:
    """Return skill settings, or ``None`` when ``AGENT_SKILLS_ENABLED`` is off."""

    if not _env_bool("AGENT_SKILLS_ENABLED", False):
        return None

    warnings: list[str] = []
    global_root = _resolve_optional(_env("AGENT_SKILLS_ROOT", DEFAULT_SKILLS_ROOT))
    user_root_base = _resolve_optional(_env("AGENT_SKILLS_USER_ROOT"))

    workspace_base = Path(_env("AGENT_WORKSPACE_ROOT", DEFAULT_WORKSPACE_ROOT)).expanduser().resolve()
    workspace_dir = _env("AGENT_SKILLS_WORKSPACE_DIR")
    workspace_root: Optional[Path] = None
    if workspace_dir:
        relative = Path(workspace_dir)
        if relative.is_absolute() or ".." in relative.parts:
            warnings.append("AGENT_SKILLS_WORKSPACE_DIR must be a relative path without '..'; workspace skills are off")
        else:
            workspace_root = (workspace_base / relative).resolve()

    script_approval = _env("AGENT_SKILLS_SCRIPT_APPROVAL", DEFAULT_SCRIPT_APPROVAL).lower()
    if script_approval not in {"always", "auto"}:
        warnings.append(
            f"AGENT_SKILLS_SCRIPT_APPROVAL={script_approval!r} is invalid; using 'always'"
        )
        script_approval = "always"

    memory_root = Path(_env("AGENT_MEMORY_ROOT", DEFAULT_MEMORY_ROOT)).expanduser().resolve()
    for label, root in (
        ("AGENT_SKILLS_ROOT", global_root),
        ("AGENT_SKILLS_USER_ROOT", user_root_base),
        ("workspace skill root", workspace_root),
    ):
        if root is None:
            continue
        if _is_inside(root, memory_root):
            warnings.append(f"{label} ({root}) is inside AGENT_MEMORY_ROOT; keep skill files out of memory")
        if _is_inside(root, workspace_base):
            warnings.append(
                f"{label} ({root}) is inside AGENT_WORKSPACE_ROOT; agent write tools can modify those skills"
            )

    for warning in warnings:
        if warning in _WARNED:
            continue
        _WARNED.add(warning)
        logger.warning("Skills config: %s", warning)

    return SkillsConfig(
        global_root=global_root,
        user_root_base=user_root_base,
        workspace_root=workspace_root,
        catalog_ttl_seconds=_env_int(
            "AGENT_SKILLS_CATALOG_TTL_SECONDS", DEFAULT_CATALOG_TTL_SECONDS, low=0, high=3600
        ),
        max_skills=_env_int("AGENT_SKILLS_MAX_SKILLS", DEFAULT_MAX_SKILLS, low=1, high=1000),
        max_description_chars=_env_int(
            "AGENT_SKILLS_MAX_DESCRIPTION_CHARS", DEFAULT_MAX_DESCRIPTION_CHARS, low=64, high=4096
        ),
        catalog_max_chars=_env_int(
            "AGENT_SKILLS_CATALOG_MAX_CHARS", DEFAULT_CATALOG_MAX_CHARS, low=256, high=32_000
        ),
        max_skill_md_bytes=_env_int(
            "AGENT_SKILLS_MAX_SKILL_MD_BYTES", DEFAULT_MAX_SKILL_MD_BYTES, low=1024, high=2_000_000
        ),
        max_resource_bytes=_env_int(
            "AGENT_SKILLS_MAX_RESOURCE_BYTES", DEFAULT_MAX_RESOURCE_BYTES, low=1024, high=10_000_000
        ),
        resource_max_chars=_env_int(
            "AGENT_SKILLS_RESOURCE_MAX_CHARS", DEFAULT_RESOURCE_MAX_CHARS, low=256, high=200_000
        ),
        allow_scripts=_env_bool("AGENT_SKILLS_ALLOW_SCRIPTS", False),
        script_timeout_seconds=_env_int(
            "AGENT_SKILLS_SCRIPT_TIMEOUT_SECONDS", DEFAULT_SCRIPT_TIMEOUT_SECONDS, low=1, high=600
        ),
        script_max_bytes=_env_int(
            "AGENT_SKILLS_SCRIPT_MAX_BYTES", DEFAULT_SCRIPT_MAX_BYTES, low=1024, high=5_000_000
        ),
        script_approval=script_approval,  # type: ignore[arg-type]
        script_max_bundle_files=_env_int(
            "AGENT_SKILLS_SCRIPT_MAX_BUNDLE_FILES", DEFAULT_SCRIPT_MAX_BUNDLE_FILES, low=1, high=1000
        ),
        script_max_bundle_bytes=_env_int(
            "AGENT_SKILLS_SCRIPT_MAX_BUNDLE_BYTES", DEFAULT_SCRIPT_MAX_BUNDLE_BYTES, low=1024, high=20_000_000
        ),
        script_max_args_chars=_env_int(
            "AGENT_SKILLS_SCRIPT_MAX_ARGS_CHARS", DEFAULT_SCRIPT_MAX_ARGS_CHARS, low=0, high=65_536
        ),
        warnings=tuple(warnings),
    )
