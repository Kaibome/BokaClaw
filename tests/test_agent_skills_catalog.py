"""Skill discovery, precedence, caching, and configuration."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from app.agent.skills.catalog import SkillCatalogCache, discover_skills  # noqa: E402
from app.agent.skills.config import SkillsConfig, load_skills_config  # noqa: E402
from app.memory.paths import user_key  # noqa: E402

_ENV_KEYS = (
    "AGENT_SKILLS_ENABLED",
    "AGENT_SKILLS_ROOT",
    "AGENT_SKILLS_USER_ROOT",
    "AGENT_SKILLS_WORKSPACE_DIR",
    "AGENT_SKILLS_CATALOG_TTL_SECONDS",
    "AGENT_SKILLS_MAX_SKILLS",
    "AGENT_SKILLS_ALLOW_SCRIPTS",
    "AGENT_SKILLS_SCRIPT_TIMEOUT_SECONDS",
    "AGENT_SKILLS_SCRIPT_MAX_BYTES",
    "AGENT_SKILLS_SCRIPT_APPROVAL",
    "AGENT_SKILLS_SCRIPT_MAX_BUNDLE_FILES",
    "AGENT_SKILLS_SCRIPT_MAX_BUNDLE_BYTES",
    "AGENT_SKILLS_SCRIPT_MAX_ARGS_CHARS",
    "AGENT_WORKSPACE_ROOT",
    "AGENT_MEMORY_ROOT",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AGENT_SKILLS_MAX_SKILLS", "100")


def _write_skill(
    root: Path,
    name: str,
    *,
    description: str = "A skill",
    body: str = "# Instructions\n",
    resources: dict[str, str | bytes] | None = None,
) -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}",
        encoding="utf-8",
    )
    for relative, content in (resources or {}).items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
    return directory


def test_discover_skills_from_global_root(tmp_path: Path):
    _write_skill(tmp_path, "beta", description="Second")
    _write_skill(tmp_path, "alpha", description="First")
    config = SkillsConfig(global_root=tmp_path)

    catalog = discover_skills(config, "")

    assert [skill.name for skill in catalog.skills] == ["alpha", "beta"]
    assert catalog.skills[0].source == "global"
    assert not catalog.errors


def test_precedence_user_beats_workspace_beats_global(tmp_path: Path):
    global_root = tmp_path / "global"
    workspace_root = tmp_path / "workspace"
    user_root = tmp_path / "users"
    _write_skill(global_root, "demo", description="global")
    _write_skill(workspace_root, "demo", description="workspace")
    _write_skill(user_root / user_key("u1"), "demo", description="user")
    config = SkillsConfig(
        global_root=global_root,
        workspace_root=workspace_root,
        user_root_base=user_root,
    )

    catalog = discover_skills(config, "u1")

    assert catalog.get("demo") is not None
    assert catalog.get("demo").description == "user"
    assert catalog.get("demo").source == "user"


def test_user_skills_are_isolated_per_user(tmp_path: Path):
    user_root = tmp_path / "users"
    _write_skill(user_root / user_key("alice"), "private", description="alice only")
    config = SkillsConfig(user_root_base=user_root)

    alice = discover_skills(config, "alice")
    bob = discover_skills(config, "bob")

    assert alice.get("private") is not None
    assert bob.get("private") is None


def test_symlinked_user_root_is_rejected(tmp_path: Path):
    real = tmp_path / "real"
    _write_skill(real, "private")
    user_root = tmp_path / "users"
    user_root.mkdir()
    link = user_root / user_key("alice")
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(real)
    except OSError:  # pragma: no cover - platform without symlink support
        pytest.skip("symlinks are not available")

    catalog = discover_skills(SkillsConfig(user_root_base=user_root), "alice")

    assert catalog.skills == ()
    assert any("symbolic link" in error for error in catalog.errors)


def test_invalid_skill_is_skipped_and_siblings_survive(tmp_path: Path):
    _write_skill(tmp_path, "good", description="valid")
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "SKILL.md").write_text("---\nname: bad\n---\nmissing description\n", encoding="utf-8")
    config = SkillsConfig(global_root=tmp_path)

    catalog = discover_skills(config, "")

    assert [skill.name for skill in catalog.skills] == ["good"]
    assert any("bad" in error for error in catalog.errors)


def test_hidden_and_symlinked_directories_are_skipped(tmp_path: Path):
    _write_skill(tmp_path, "visible")
    _write_skill(tmp_path, ".hidden")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text(
        "---\nname: linked\ndescription: linked\n---\nbody\n",
        encoding="utf-8",
    )
    link = tmp_path / "linked"
    try:
        link.symlink_to(outside)
    except OSError:  # pragma: no cover - platform without symlink support
        pytest.skip("symlinks are not available")

    catalog = discover_skills(SkillsConfig(global_root=tmp_path), "")

    assert [skill.name for skill in catalog.skills] == ["visible"]


def test_max_skills_truncates_and_reports(tmp_path: Path, monkeypatch):
    for name in ("one", "two", "three"):
        _write_skill(tmp_path, name)
    config = SkillsConfig(global_root=tmp_path, max_skills=2)

    catalog = discover_skills(config, "")

    assert len(catalog.skills) == 2
    assert catalog.truncated is True
    assert any("MAX_SKILLS" in error for error in catalog.errors)


def test_resource_index_lists_files_and_scripts(tmp_path: Path):
    _write_skill(
        tmp_path,
        "demo",
        resources={
            "references/guide.md": "# Guide\n",
            "assets/logo.png": b"\x89PNG\r\n\x1a\n",
            "scripts/run.sh": "#!/bin/sh\necho hi\n",
            ".hidden/secret.md": "secret",
        },
    )

    catalog = discover_skills(SkillsConfig(global_root=tmp_path), "")
    skill = catalog.get("demo")

    assert skill is not None
    assert [resource.path for resource in skill.resources] == [
        "assets/logo.png",
        "references/guide.md",
        "scripts/run.sh",
    ]
    assert skill.scripts == ("scripts/run.sh",)
    assert skill.has_scripts is True


def test_cache_reuses_catalog_until_a_skill_file_changes(tmp_path: Path):
    directory = _write_skill(tmp_path, "demo", description="old")
    config = SkillsConfig(global_root=tmp_path, catalog_ttl_seconds=60)
    cache = SkillCatalogCache()

    first = cache.get_or_load(config, "")
    _write_skill(tmp_path, "demo", description="new")
    skill_md = directory / "SKILL.md"
    future = time.time() + 10
    os.utime(skill_md, (future, future))

    second = cache.get_or_load(config, "")

    assert first.get("demo").description == "old"
    assert second.get("demo").description == "new"


def test_ttl_zero_rescans_every_call(tmp_path: Path):
    _write_skill(tmp_path, "demo", description="old")
    config = SkillsConfig(global_root=tmp_path, catalog_ttl_seconds=0)
    cache = SkillCatalogCache()

    assert cache.get_or_load(config, "").get("demo").description == "old"
    _write_skill(tmp_path, "demo", description="new")

    assert cache.get_or_load(config, "").get("demo").description == "new"


def test_load_skills_config_is_off_by_default():
    assert load_skills_config() is None


def test_load_skills_config_reads_roots_and_limits(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("AGENT_SKILLS_ENABLED", "true")
    monkeypatch.setenv("AGENT_SKILLS_ROOT", str(tmp_path / "global"))
    monkeypatch.setenv("AGENT_SKILLS_USER_ROOT", str(tmp_path / "users"))
    monkeypatch.setenv("AGENT_SKILLS_CATALOG_TTL_SECONDS", "0")
    monkeypatch.setenv("AGENT_SKILLS_MAX_SKILLS", "7")

    config = load_skills_config()

    assert config is not None
    assert config.global_root == (tmp_path / "global").resolve()
    assert config.user_root_base == (tmp_path / "users").resolve()
    assert config.catalog_ttl_seconds == 0
    assert config.max_skills == 7


def test_load_skills_config_reads_script_settings(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("AGENT_SKILLS_ENABLED", "true")
    monkeypatch.setenv("AGENT_SKILLS_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENT_SKILLS_ALLOW_SCRIPTS", "true")
    monkeypatch.setenv("AGENT_SKILLS_SCRIPT_TIMEOUT_SECONDS", "90")
    monkeypatch.setenv("AGENT_SKILLS_SCRIPT_MAX_BYTES", "4096")
    monkeypatch.setenv("AGENT_SKILLS_SCRIPT_APPROVAL", "auto")
    monkeypatch.setenv("AGENT_SKILLS_SCRIPT_MAX_BUNDLE_FILES", "10")
    monkeypatch.setenv("AGENT_SKILLS_SCRIPT_MAX_BUNDLE_BYTES", "10000")
    monkeypatch.setenv("AGENT_SKILLS_SCRIPT_MAX_ARGS_CHARS", "512")

    config = load_skills_config()

    assert config is not None
    assert config.allow_scripts is True
    assert config.script_timeout_seconds == 90
    assert config.script_max_bytes == 4096
    assert config.script_approval == "auto"
    assert config.script_max_bundle_files == 10
    assert config.script_max_bundle_bytes == 10_000
    assert config.script_max_args_chars == 512


def test_invalid_script_approval_mode_falls_back_to_always(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("AGENT_SKILLS_ENABLED", "true")
    monkeypatch.setenv("AGENT_SKILLS_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENT_SKILLS_SCRIPT_APPROVAL", "sometimes")

    config = load_skills_config()

    assert config is not None
    assert config.script_approval == "always"
    assert any("SCRIPT_APPROVAL" in warning for warning in config.warnings)


def test_workspace_skill_dir_must_be_relative(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("AGENT_SKILLS_ENABLED", "true")
    monkeypatch.setenv("AGENT_SKILLS_ROOT", str(tmp_path / "global"))
    monkeypatch.setenv("AGENT_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("AGENT_SKILLS_WORKSPACE_DIR", "../outside")

    config = load_skills_config()

    assert config is not None
    assert config.workspace_root is None
    assert any("relative path" in warning for warning in config.warnings)


def test_config_warns_when_skills_live_inside_the_workspace(monkeypatch, tmp_path: Path):
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("AGENT_SKILLS_ENABLED", "true")
    monkeypatch.setenv("AGENT_SKILLS_ROOT", str(workspace / "skills"))
    monkeypatch.setenv("AGENT_WORKSPACE_ROOT", str(workspace))

    config = load_skills_config()

    assert config is not None
    assert any("AGENT_WORKSPACE_ROOT" in warning for warning in config.warnings)
