"""Workspace path resolution and per-user isolation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Tuple

from .config import MemorySettings
from .errors import MemoryValidationError

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_META_FILE = ".user.json"


def is_uuid(value: str) -> bool:
    return bool(_UUID_RE.match(value))


def user_key(user_id: str) -> str:
    """Return a filesystem-safe directory key for a memory scope."""

    text = str(user_id or "").strip()
    if not text:
        raise MemoryValidationError("user_id is required")
    if is_uuid(text):
        # Normalize the case so the same UUID never creates two directories.
        try:
            return str(uuid.UUID(text))
        except ValueError:
            pass
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return f"_external/{digest}"


def _chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        # Best effort: bind-mounted volumes may not allow chmod.
        pass


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _chmod(path, 0o700)
    return path


def ensure_private_file(path: Path) -> Path:
    _chmod(path, 0o600)
    return path


def resolve_under(root: Path, value: str | Path, *, must_exist: bool = False) -> Path:
    """Resolve ``value`` inside ``root``, rejecting traversal and symlink escapes."""

    root = root.expanduser().resolve()
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise MemoryValidationError(f"path {value!r} is outside the memory root") from exc
    if must_exist and not resolved.exists():
        raise MemoryValidationError(f"path {value!r} does not exist")
    return resolved


def assert_not_under(path: Path, denied: Path | None) -> None:
    if denied is None:
        return
    try:
        path.resolve().relative_to(denied.expanduser().resolve())
    except ValueError:
        return
    raise MemoryValidationError(f"memory root {path} must not be inside {denied}")


def user_root(settings: MemorySettings, user_id: str, *, create: bool = True) -> Path:
    key = user_key(user_id)
    root = settings.root / "users" / key
    if create:
        ensure_private_dir(root)
        _write_user_meta(root, user_id)
    return root


def _write_user_meta(root: Path, user_id: str) -> None:
    path = root / _META_FILE
    payload = {"user_id": str(user_id)}
    try:
        if path.exists():
            current = json.loads(path.read_text(encoding="utf-8") or "{}")
            if current.get("user_id") == payload["user_id"]:
                return
        ensure_private_file(path)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except (OSError, ValueError):
        # Metadata is only needed for non-UUID reconciliation; never block a write.
        return


def read_user_id(root: Path) -> str:
    path = root / _META_FILE
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return root.name
    value = payload.get("user_id")
    return str(value) if value else root.name


def scan_user_roots(settings: MemorySettings) -> Iterator[Tuple[str, Path]]:
    """Yield ``(user_id, root)`` for every user workspace on disk."""

    users_root = settings.root / "users"
    if not users_root.exists():
        return
    for entry in sorted(users_root.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name == "_external":
            for external in sorted(entry.iterdir()):
                if external.is_dir():
                    yield read_user_id(external), external
        else:
            yield read_user_id(entry), entry


def memory_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for name in ("USER.md", "MEMORY.md"):
        path = root / name
        if path.exists():
            files.append(path)
    daily = root / "memory"
    if daily.exists():
        files.extend(sorted(path for path in daily.glob("*.md") if path.is_file()))
    return files


def daily_path(root: Path, day: datetime | None = None) -> Path:
    day = day or datetime.now(timezone.utc)
    return root / "memory" / f"{day.date().isoformat()}.md"


def archive_dir(settings: MemorySettings) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = settings.root / ".trash" / stamp
    ensure_private_dir(path)
    return path
