"""Configuration for the workspace-Markdown memory backend.

The backend reads the same persisted ``/configure`` object the dashboard uses
(``llm`` and ``embedder``) and adds a ``memory`` block for workspace paths and
recall budgets. Environment variables win over the persisted defaults so an
operator can pin a path or disable capture without editing the database.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

from pydantic import BaseModel, Field, field_validator

STORAGE_DIMS = 3072


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    text = value.strip()
    return text or None


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


class MemorySettings(BaseModel):
    """Settings shared by the Markdown store, the index and the agent hooks."""

    backend: str = "markdown"
    root: Path = Field(default_factory=lambda: Path(os.getenv("AGENT_MEMORY_ROOT", "/app/memory")))
    storage_dims: int = STORAGE_DIMS

    recall_tokens: int = 1500
    profile_tokens: int = 600
    recall_max: int = 15
    recall_threshold: float = 0.4
    recall_half_life_days: int = 90
    curated_half_life_days: int = 365

    capture_enabled: bool = True
    episodic_retention_days: int = 180
    reconcile_on_start: bool = True
    reconcile_interval_seconds: int = 300
    require_approval: bool = False

    embedder_provider: str = "openai"
    embedder_model: str = "text-embedding-3-small"
    embedder_config: Dict[str, Any] = Field(default_factory=dict)
    llm_provider: str = "openai"
    llm_model: str = "gpt-5-mini"
    llm_config: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("root", mode="before")
    @classmethod
    def _expand_root(cls, value: Any) -> Any:
        if isinstance(value, str):
            return Path(value).expanduser()
        return value

    @field_validator("storage_dims")
    @classmethod
    def _bounded_dims(cls, value: int) -> int:
        if value <= 0 or value > STORAGE_DIMS:
            raise ValueError(f"storage_dims must be between 1 and {STORAGE_DIMS}")
        return value

    @classmethod
    def from_env(cls) -> "MemorySettings":
        embedder_config: Dict[str, Any] = {}
        for key, env_name in (
            ("api_key", "OPENAI_API_KEY"),
            ("base_url", "OPENAI_BASE_URL"),
        ):
            value = _env(env_name)
            if value:
                embedder_config[key] = value

        return cls(
            backend=_env("MEMORY_BACKEND") or "markdown",
            root=Path(_env("AGENT_MEMORY_ROOT") or "/app/memory"),
            recall_tokens=_env_int("AGENT_MEMORY_RECALL_TOKENS", 1500),
            profile_tokens=_env_int("AGENT_MEMORY_PROFILE_TOKENS", 600),
            recall_max=_env_int("AGENT_MEMORY_RECALL_MAX", 15),
            recall_threshold=_env_float("AGENT_MEMORY_RECALL_THRESHOLD", 0.4),
            capture_enabled=_env_bool("AGENT_MEMORY_CAPTURE", True),
            episodic_retention_days=_env_int("AGENT_MEMORY_EPISODIC_RETENTION_DAYS", 180),
            reconcile_on_start=_env_bool("AGENT_MEMORY_RECONCILE_ON_START", True),
            reconcile_interval_seconds=_env_int("AGENT_MEMORY_RECONCILE_INTERVAL_SECONDS", 300),
            require_approval=_env_bool("AGENT_MEMORY_REQUIRE_APPROVAL", False),
            embedder_provider=_env("MEMORY_DEFAULT_EMBEDDER_PROVIDER")
            or _env("MEM0_DEFAULT_EMBEDDER_PROVIDER")
            or "openai",
            embedder_model=_env("MEMORY_DEFAULT_EMBEDDER_MODEL")
            or _env("MEM0_DEFAULT_EMBEDDER_MODEL")
            or "text-embedding-3-small",
            embedder_config=embedder_config,
        )

    @classmethod
    def from_config(cls, config: Dict[str, Any] | None) -> "MemorySettings":
        base = cls.from_env()
        config = config or {}
        memory = config.get("memory") if isinstance(config.get("memory"), dict) else {}
        embedder = config.get("embedder") if isinstance(config.get("embedder"), dict) else {}
        llm = config.get("llm") if isinstance(config.get("llm"), dict) else {}

        updates: Dict[str, Any] = {}
        for key in (
            "backend",
            "root",
            "storage_dims",
            "recall_tokens",
            "profile_tokens",
            "recall_max",
            "recall_threshold",
            "recall_half_life_days",
            "curated_half_life_days",
            "capture_enabled",
            "episodic_retention_days",
            "reconcile_on_start",
            "reconcile_interval_seconds",
            "require_approval",
        ):
            if key in memory:
                updates[key] = memory[key]

        if embedder:
            updates["embedder_provider"] = embedder.get("provider") or base.embedder_provider
            embedder_cfg = embedder.get("config") if isinstance(embedder.get("config"), dict) else {}
            updates["embedder_model"] = embedder_cfg.get("model") or base.embedder_model
            updates["embedder_config"] = {**base.embedder_config, **embedder_cfg}
        if llm:
            updates["llm_provider"] = llm.get("provider") or base.llm_provider
            llm_cfg = llm.get("config") if isinstance(llm.get("config"), dict) else {}
            updates["llm_model"] = llm_cfg.get("model") or base.llm_model
            updates["llm_config"] = {**base.llm_config, **llm_cfg}

        return cls.model_validate({**base.model_dump(), **updates})
