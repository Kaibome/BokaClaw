"""Discover skills from configured roots and cache the merged catalog.

The catalog is a small, process-local index. It is safe to rebuild at any time:
workspace Markdown memory is the system of record for memories, while skills
are files an operator or user installs explicitly.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.agent.skills.config import SkillsConfig
from app.agent.skills.frontmatter import SkillFormatError, read_skill_document
from app.agent.skills.models import SkillCatalog, SkillRecord, SkillResource, SkillRoot
from app.agent.skills.security import classify_resource

logger = logging.getLogger(__name__)

MAX_RESOURCE_ENTRIES = 100
MAX_RESOURCE_LIST_CHARS = 8192
MAX_SCANNED_ENTRIES = 1000
MAX_SCANNED_RESOURCE_ENTRIES = 1000
_SKIP_DIRECTORIES = {".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv", ".next"}
_WARNED: set[str] = set()


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    catalog: SkillCatalog
    built_at: float
    fingerprint: str


def _scan_resources(directory: Path) -> tuple[tuple[SkillResource, ...], tuple[str, ...], bool]:
    resources: list[SkillResource] = []
    scripts: list[str] = []
    truncated = False
    listed_chars = 0
    scanned_dirs = 0
    scanned_files = 0

    for current, dirs, files in os.walk(directory, followlinks=False):
        scanned_dirs += 1
        if scanned_dirs > MAX_SCANNED_ENTRIES:
            truncated = True
            break
        dirs[:] = sorted(
            name
            for name in dirs
            if name not in _SKIP_DIRECTORIES
            and not name.startswith(".")
            and not (Path(current) / name).is_symlink()
        )
        for name in sorted(files):
            scanned_files += 1
            if scanned_files > MAX_SCANNED_RESOURCE_ENTRIES:
                truncated = True
                break
            if name.startswith("."):
                continue
            path = Path(current) / name
            if path.is_symlink():
                continue
            if name == "SKILL.md" and path.parent == directory:
                continue
            try:
                stat = path.stat(follow_symlinks=False)
            except OSError:
                continue
            if not path.is_file():
                continue
            if len(resources) >= MAX_RESOURCE_ENTRIES:
                truncated = True
                break
            relative = path.relative_to(directory).as_posix()
            if listed_chars + len(relative) + 1 > MAX_RESOURCE_LIST_CHARS:
                truncated = True
                break
            listed_chars += len(relative) + 1
            resources.append(
                SkillResource(path=relative, size=stat.st_size, kind=classify_resource(path))  # type: ignore[arg-type]
            )
            if relative.startswith("scripts/"):
                scripts.append(relative)
        if truncated:
            break

    return tuple(resources), tuple(scripts), truncated


def _scan_root(root: SkillRoot, config: SkillsConfig, errors: list[str]) -> tuple[list[SkillRecord], bool]:
    if root.path.is_symlink():
        errors.append(f"{root.source} root {root.path}: symbolic link roots are not allowed")
        return [], False
    if not root.path.is_dir():
        return [], False

    try:
        entries = sorted(root.path.iterdir(), key=lambda entry: entry.name)
    except OSError as exc:
        errors.append(f"{root.source} root {root.path}: cannot list directory ({type(exc).__name__})")
        return [], False

    records: list[SkillRecord] = []
    truncated = False
    for index, entry in enumerate(entries):
        if index >= MAX_SCANNED_ENTRIES:
            truncated = True
            errors.append(
                f"{root.source} root {root.path}: scanned {MAX_SCANNED_ENTRIES} entries; extra entries skipped"
            )
            break
        if entry.name.startswith("."):
            continue
        if entry.is_symlink():
            continue
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue

        skill_md = entry / "SKILL.md"
        try:
            if skill_md.is_symlink() or not skill_md.is_file():
                continue
        except OSError:
            continue

        if len(records) >= config.max_skills:
            truncated = True
            errors.append(
                f"{root.source} root {root.path}: AGENT_SKILLS_MAX_SKILLS={config.max_skills} reached; "
                "extra skills skipped"
            )
            break

        try:
            document = read_skill_document(
                skill_md,
                directory_name=entry.name,
                max_bytes=config.max_skill_md_bytes,
                max_frontmatter_bytes=config.max_frontmatter_bytes,
                max_description_chars=config.max_description_chars,
            )
        except (SkillFormatError, OSError, RecursionError) as exc:
            errors.append(f"{root.source}:{entry.name}: {exc}")
            continue

        resources, scripts, resources_truncated = _scan_resources(entry)
        truncated = truncated or resources_truncated
        records.append(
            SkillRecord(
                name=document.name,
                description=document.description,
                source=root.source,
                root=root.path,
                directory=entry,
                skill_md=skill_md,
                metadata=document.metadata,
                allowed_tools=document.allowed_tools,
                resources=resources,
                scripts=scripts,
            )
        )

    return records, truncated


def discover_skills(
    config: SkillsConfig,
    user_id: str,
    *,
    roots: Optional[tuple[SkillRoot, ...]] = None,
) -> SkillCatalog:
    """Scan every configured root and merge skills by precedence.

    ``user`` beats ``workspace`` beats ``global``. A lower-precedence duplicate
    is ignored; a malformed skill is skipped and recorded in ``errors`` without
    affecting its siblings.
    """

    ordered_roots = roots if roots is not None else config.roots_for_user(user_id)
    errors: list[str] = []
    seen: dict[str, SkillRecord] = {}
    truncated = False

    for root in ordered_roots:
        records, root_truncated = _scan_root(root, config, errors)
        truncated = truncated or root_truncated
        for record in records:
            existing = seen.get(record.name)
            if existing is not None:
                logger.debug(
                    "Skill %r from %s is shadowed by %s",
                    record.name,
                    record.source,
                    existing.source,
                )
                continue
            seen[record.name] = record

    for error in errors:
        if error in _WARNED:
            continue
        _WARNED.add(error)
        logger.warning("Skill skipped: %s", error)

    return SkillCatalog(
        skills=tuple(seen[name] for name in sorted(seen)),
        truncated=truncated,
        errors=tuple(errors),
    )


def _roots_fingerprint(roots: tuple[SkillRoot, ...]) -> str:
    parts: list[str] = []
    for root in roots:
        parts.append(f"{root.source}:{root.path}")
        if root.path.is_symlink():
            parts.append("symlink")
            continue
        try:
            entries = sorted(root.path.iterdir(), key=lambda entry: entry.name)
        except OSError:
            parts.append("missing")
            continue
        for index, entry in enumerate(entries):
            if index >= MAX_SCANNED_ENTRIES:
                parts.append("truncated")
                break
            if entry.name.startswith(".") or entry.is_symlink():
                continue
            try:
                if not entry.is_dir():
                    continue
                skill_md = entry / "SKILL.md"
                if skill_md.is_symlink() or not skill_md.is_file():
                    continue
                directory_stat = entry.stat()
                skill_stat = skill_md.stat()
            except OSError:
                continue
            parts.append(
                f"{entry.name}:{directory_stat.st_mtime_ns}:"
                f"{skill_stat.st_mtime_ns}:{skill_stat.st_size}"
            )
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


class SkillCatalogCache:
    """Process-wide catalog cache keyed by the effective roots for one user."""

    def __init__(self) -> None:
        self._entries: dict[tuple[tuple[str, str], ...], _CacheEntry] = {}
        self._lock = threading.Lock()

    def get_or_load(self, config: SkillsConfig, user_id: str) -> SkillCatalog:
        roots = config.roots_for_user(user_id)
        fingerprint = _roots_fingerprint(roots)
        key = tuple((root.source, str(root.path)) for root in roots)
        now = time.monotonic()

        with self._lock:
            entry = self._entries.get(key)
            if (
                entry is not None
                and config.catalog_ttl_seconds > 0
                and now - entry.built_at <= config.catalog_ttl_seconds
                and entry.fingerprint == fingerprint
            ):
                return entry.catalog

        catalog = discover_skills(config, user_id, roots=roots)
        with self._lock:
            self._entries[key] = _CacheEntry(
                catalog=catalog,
                built_at=time.monotonic(),
                fingerprint=fingerprint,
            )
        return catalog

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_CACHE = SkillCatalogCache()


def get_skill_catalog_cache() -> SkillCatalogCache:
    return _CACHE


def reset_skill_catalog_cache() -> None:
    _CACHE.clear()
