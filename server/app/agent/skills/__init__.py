"""Agent Skills: discover ``SKILL.md`` packs and expose them via one tool."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from app.agent.skills.catalog import (
    SkillCatalogCache,
    discover_skills,
    get_skill_catalog_cache,
    reset_skill_catalog_cache,
)
from app.agent.skills.config import SkillsConfig, load_skills_config
from app.agent.skills.models import SkillCatalog, SkillRecord, SkillResource, SkillRoot
from app.agent.skills.prompts import render_catalog
from app.agent.skills.tools import build_skill_tools


@dataclass(frozen=True, slots=True)
class SkillContext:
    """Everything one run needs from the skill subsystem."""

    tools: list[Any] = field(default_factory=list)
    block: Optional[str] = None
    catalog: Optional[SkillCatalog] = None


def prepare_skill_context(
    sdk: Any,
    user_id: Optional[str],
    *,
    config: Optional[SkillsConfig] = None,
    interaction: Any = None,
    cancel_event: Any = None,
) -> SkillContext:
    """Load the effective catalog and build the run-scoped skill tools."""

    settings = config if config is not None else load_skills_config()
    if settings is None:
        return SkillContext()

    normalized_user_id = str(user_id or "").strip()
    catalog = get_skill_catalog_cache().get_or_load(settings, normalized_user_id)
    if not catalog.skills:
        return SkillContext(catalog=catalog)

    return SkillContext(
        tools=build_skill_tools(
            sdk,
            catalog,
            settings,
            user_id=normalized_user_id,
            interaction=interaction,
            cancel_event=cancel_event,
        ),
        block=render_catalog(catalog, max_chars=settings.catalog_max_chars),
        catalog=catalog,
    )


__all__ = [
    "SkillCatalog",
    "SkillCatalogCache",
    "SkillContext",
    "SkillRecord",
    "SkillResource",
    "SkillRoot",
    "SkillsConfig",
    "build_skill_tools",
    "discover_skills",
    "get_skill_catalog_cache",
    "load_skills_config",
    "prepare_skill_context",
    "render_catalog",
    "reset_skill_catalog_cache",
]
