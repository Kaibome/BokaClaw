"""Prepare and validate skill scripts before they enter the executor.

P1 never runs a script in the server process. The server validates the entry
point, reads a bounded copy of the skill directory, and sends it to the
sandboxed executor, which writes it into a per-run temporary directory. This
module owns the server-side half of that contract.
"""

from __future__ import annotations

import base64
import os
import shlex
from pathlib import Path
from typing import Dict, List, Tuple

from app.agent.skills.config import SkillsConfig
from app.agent.skills.models import SkillRecord
from app.agent.skills.security import resolve_skill_path, sanitize_skill_body

INTERPRETERS = {
    ".sh": "bash",
    ".py": "python3",
    ".js": "node",
    ".mjs": "node",
}
SCRIPT_PREFIX = "scripts/"
MAX_SCRIPT_PREVIEW_CHARS = 500
_SKIP_DIRECTORIES = {".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv", ".next"}


def validate_script(skill: SkillRecord, script: str, config: SkillsConfig) -> Tuple[Path, str]:
    """Return ``(absolute_path, relative_path)`` for one runnable script.

    The script must live under ``scripts/``, have a supported extension, and be
    a regular file no larger than ``script_max_bytes``.
    """

    path = resolve_skill_path(skill.directory, script)
    try:
        relative = path.relative_to(skill.directory).as_posix()
    except ValueError:  # pragma: no cover - resolve_skill_path already confines it
        raise ValueError("script path escapes the skill directory") from None
    if not relative.startswith(SCRIPT_PREFIX):
        raise ValueError(f"script {script!r} must be under {SCRIPT_PREFIX!r}")
    if path.suffix.lower() not in INTERPRETERS:
        allowed = ", ".join(sorted(INTERPRETERS))
        raise ValueError(f"script {script!r} must use one of: {allowed}")
    if not path.is_file():
        raise ValueError(f"script {script!r} is not a file")
    size = path.stat().st_size
    if size > config.script_max_bytes:
        raise ValueError(f"script {script!r} is too large ({size} bytes; limit {config.script_max_bytes})")
    return path, relative


def validate_args(args: str, config: SkillsConfig) -> str:
    """Reject NUL bytes, oversized arguments, and invalid shell-like quoting."""

    text = str(args or "")
    if "\x00" in text:
        raise ValueError("args must not contain a NUL byte")
    if len(text) > config.script_max_args_chars:
        raise ValueError(f"args exceeds {config.script_max_args_chars} characters")
    try:
        shlex.split(text, posix=True)
    except ValueError as exc:
        raise ValueError(f"args is not valid shell-like quoting: {exc}") from exc
    return text


def clamp_timeout(value: int, default: int) -> int:
    try:
        requested = int(value)
    except (TypeError, ValueError):
        requested = 0
    timeout = default if requested <= 0 else requested
    return max(1, min(600, timeout))


def script_preview(path: Path, limit: int = MAX_SCRIPT_PREVIEW_CHARS) -> str:
    """Return a short, sanitized preview for the approval prompt."""

    try:
        data = path.read_bytes()[: limit * 4]
    except OSError:
        return ""
    text = sanitize_skill_body(data.decode("utf-8", errors="replace"))
    if len(text) > limit:
        text = text[:limit].rstrip() + "\n... [truncated]"
    return text


def build_skill_bundle(skill: SkillRecord, config: SkillsConfig) -> List[Dict[str, str]]:
    """Read the skill directory into a bounded base64 bundle.

    Hidden entries, symlinks, and common dependency/cache directories are
    skipped. The whole skill is included because a script may reference
    ``references/`` or ``assets/`` relative to its own directory.
    """

    files: List[Dict[str, str]] = []
    total_bytes = 0

    for current, dirs, names in os.walk(skill.directory, followlinks=False):
        dirs[:] = sorted(
            name
            for name in dirs
            if name not in _SKIP_DIRECTORIES
            and not name.startswith(".")
            and not (Path(current) / name).is_symlink()
        )
        for name in sorted(names):
            if name.startswith("."):
                continue
            path = Path(current) / name
            if path.is_symlink():
                continue
            try:
                if not path.is_file():
                    continue
                data = path.read_bytes()
            except OSError as exc:
                raise ValueError(f"cannot read skill file {path.name!r}: {type(exc).__name__}") from exc

            if len(files) >= config.script_max_bundle_files:
                raise ValueError(
                    f"skill bundle exceeds {config.script_max_bundle_files} files"
                )
            if total_bytes + len(data) > config.script_max_bundle_bytes:
                raise ValueError(
                    f"skill bundle exceeds {config.script_max_bundle_bytes} bytes"
                )
            relative = path.relative_to(skill.directory).as_posix()
            files.append(
                {
                    "path": relative,
                    "content_b64": base64.b64encode(data).decode("ascii"),
                }
            )
            total_bytes += len(data)

    if not files:
        raise ValueError("skill directory has no files to run")
    return files
