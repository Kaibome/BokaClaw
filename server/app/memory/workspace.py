"""Atomic Markdown workspace storage with per-user locking."""

from __future__ import annotations

import fcntl
import logging
import os
import shutil
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, List, Tuple

from .config import MemorySettings
from .errors import MemoryBusy, MemoryNotFound
from .grammar import (
    DAILY_KINDS,
    MemoryEntry,
    document_title,
    parse_document,
    render_document,
)
from .paths import (
    archive_dir,
    daily_path,
    ensure_private_dir,
    ensure_private_file,
    memory_files,
    user_key,
    user_root,
)

logger = logging.getLogger(__name__)

_LOCK_TIMEOUT_SECONDS = 10.0


class WorkspaceStore:
    """Read/write Markdown files under ``AGENT_MEMORY_ROOT``."""

    def __init__(self, settings: MemorySettings) -> None:
        self.settings = settings
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, user_id: str) -> threading.RLock:
        key = user_key(user_id)
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._locks[key] = lock
            return lock

    @contextmanager
    def user_lock(self, user_id: str, *, timeout: float = _LOCK_TIMEOUT_SECONDS) -> Iterator[Path]:
        lock = self._lock_for(user_id)
        with lock:
            root = user_root(self.settings, user_id, create=True)
            lock_path = root / ".memory.lock"
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise MemoryBusy(f"timed out locking workspace for {user_id}") from None
                        time.sleep(0.05)
                yield root
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)

    # ------------------------------------------------------------------ reads

    def existing_root(self, user_id: str) -> Path:
        return user_root(self.settings, user_id, create=False)

    def read_text(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def read_entries(self, user_id: str) -> List[MemoryEntry]:
        root = self.existing_root(user_id)
        if not root.exists():
            return []
        entries = self._read_entries_from(root)
        for entry in entries:
            entry.user_id = user_id
        return entries

    def _read_entries_from(self, root: Path) -> List[MemoryEntry]:
        entries: List[MemoryEntry] = []
        seen: set[str] = set()
        for path in memory_files(root):
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            except OSError:
                mtime = datetime.now(timezone.utc)
            parsed = parse_document(
                self.read_text(path),
                path=str(path),
                mtime=mtime,
            )
            for entry in parsed:
                if entry.id in seen:
                    logger.warning("Duplicate memory id %s in %s; ignoring the later entry", entry.id, path)
                    continue
                seen.add(entry.id)
                entries.append(entry)
        return entries

    def find_entry(self, user_id: str, entry_id: str) -> Tuple[MemoryEntry, Path]:
        root = self.existing_root(user_id)
        if not root.exists():
            raise MemoryNotFound(f"Memory {entry_id} not found")
        for path in memory_files(root):
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            for entry in parse_document(self.read_text(path), path=str(path), mtime=mtime):
                if entry.id == entry_id:
                    entry.user_id = user_id
                    return entry, path
        raise MemoryNotFound(f"Memory {entry_id} not found")

    # ----------------------------------------------------------------- writes

    def target_path(self, user_id: str, entry: MemoryEntry, *, filename: str | None = None) -> Path:
        root = user_root(self.settings, user_id, create=True)
        if filename:
            return root / filename
        if entry.kind == "identity":
            return root / "USER.md"
        if entry.kind in DAILY_KINDS:
            return daily_path(root, entry.created)
        return root / "MEMORY.md"

    def append_entry(
        self,
        user_id: str,
        entry: MemoryEntry,
        *,
        filename: str | None = None,
    ) -> Path:
        with self.user_lock(user_id) as root:
            path = self.target_path(user_id, entry, filename=filename)
            path = path if path.is_absolute() else root / path
            ensure_private_dir(path.parent)
            entries = self._read_entries_from_file(path)
            entries.append(entry)
            self.write_atomic(path, render_document(entries, document_title(path)))
            return path

    def update_entry(
        self,
        user_id: str,
        entry_id: str,
        *,
        content: str | None = None,
        kind: str | None = None,
        importance: float | None = None,
        metadata: dict | None = None,
    ) -> MemoryEntry:
        with self.user_lock(user_id) as root:
            path = self._locate(root, entry_id)
            entries = self._read_entries_from_file(path)
            updated: MemoryEntry | None = None
            for index, entry in enumerate(entries):
                if entry.id != entry_id:
                    continue
                if content is not None:
                    entry.content = content
                if kind is not None:
                    entry.kind = kind
                    entry.heading = None
                if importance is not None:
                    entry.importance = max(0.0, min(1.0, float(importance)))
                if metadata is not None:
                    entry.metadata = {**entry.metadata, **metadata}
                entry.updated = datetime.now(timezone.utc)
                entries[index] = entry
                updated = entry
                break
            if updated is None:
                raise MemoryNotFound(f"Memory {entry_id} not found")
            self.write_atomic(path, render_document(entries, document_title(path)))
            return updated

    def replace_entry(self, user_id: str, entry: MemoryEntry) -> Path:
        """Replace an existing entry in place, preserving its file and position."""

        with self.user_lock(user_id) as root:
            path = self._locate(root, entry.id)
            entries = self._read_entries_from_file(path)
            for index, current in enumerate(entries):
                if current.id == entry.id:
                    entries[index] = entry
                    break
            else:
                raise MemoryNotFound(f"Memory {entry.id} not found")
            self.write_atomic(path, render_document(entries, document_title(path)))
            return path

    def delete_entry(self, user_id: str, entry_id: str) -> Path:
        with self.user_lock(user_id) as root:
            path = self._locate(root, entry_id)
            entries = [entry for entry in self._read_entries_from_file(path) if entry.id != entry_id]
            self.write_atomic(path, render_document(entries, document_title(path)))
            return path

    def delete_entries(self, user_id: str, entry_ids: Iterable[str]) -> int:
        wanted = set(entry_ids)
        if not wanted:
            return 0
        deleted = 0
        with self.user_lock(user_id) as root:
            for path in memory_files(root):
                entries = self._read_entries_from_file(path)
                kept = [entry for entry in entries if entry.id not in wanted]
                if len(kept) == len(entries):
                    continue
                deleted += len(entries) - len(kept)
                self.write_atomic(path, render_document(kept, document_title(path)))
        return deleted

    def archive_user(self, user_id: str) -> Path | None:
        with self.user_lock(user_id) as root:
            if not root.exists():
                return None
            destination = archive_dir(self.settings) / user_key(user_id)
            ensure_private_dir(destination.parent)
            shutil.move(str(root), str(destination))
            return destination

    def prune_daily(self, user_id: str, retention_days: int) -> int:
        if retention_days <= 0:
            return 0
        root = self.existing_root(user_id)
        daily = root / "memory"
        if not daily.exists():
            return 0
        cutoff = datetime.now(timezone.utc).timestamp() - retention_days * 86_400
        removed = 0
        with self.user_lock(user_id):
            for path in daily.glob("*.md"):
                try:
                    if path.stat().st_mtime < cutoff:
                        path.unlink()
                        removed += 1
                except OSError:
                    logger.warning("Could not prune %s", path, exc_info=True)
        return removed

    # ------------------------------------------------------------------ files

    def _locate(self, root: Path, entry_id: str) -> Path:
        for path in memory_files(root):
            for entry in self._read_entries_from_file(path):
                if entry.id == entry_id:
                    return path
        raise MemoryNotFound(f"Memory {entry_id} not found")

    def _read_entries_from_file(self, path: Path) -> List[MemoryEntry]:
        if not path.exists():
            return []
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        return parse_document(self.read_text(path), path=str(path), mtime=mtime)

    def write_atomic(self, path: Path, text: str) -> None:
        ensure_private_dir(path.parent)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, path)
            ensure_private_file(path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
