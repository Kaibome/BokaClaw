"""Workspace store tests: isolation, atomic writes, update/delete."""

from datetime import datetime, timezone

import pytest

from app.memory.config import MemorySettings
from app.memory.errors import MemoryNotFound, MemoryValidationError
from app.memory.grammar import MemoryEntry
from app.memory.paths import resolve_under, user_root
from app.memory.workspace import WorkspaceStore


def _entry(kind: str, text: str, *, user_id: str = "user-a", entry_id: str | None = None) -> MemoryEntry:
    now = datetime(2026, 11, 8, tzinfo=timezone.utc)
    return MemoryEntry(
        id=entry_id or f"m-{kind}-{abs(hash(text)) % 100000}",
        kind=kind,
        content=text,
        importance=0.8,
        source="curated",
        trust="owner",
        created=now,
        updated=now,
        user_id=user_id,
    )


def test_append_and_read(tmp_path) -> None:
    store = WorkspaceStore(MemorySettings(root=tmp_path))
    path = store.append_entry("user-a", _entry("preference", "喜欢黑咖啡"))
    assert path.name == "MEMORY.md"
    entries = store.read_entries("user-a")
    assert [entry.content for entry in entries] == ["喜欢黑咖啡"]


def test_identity_goes_to_user_file(tmp_path) -> None:
    store = WorkspaceStore(MemorySettings(root=tmp_path))
    path = store.append_entry("user-a", _entry("identity", "用户住在深圳"))
    assert path.name == "USER.md"


def test_episode_goes_to_daily_file(tmp_path) -> None:
    store = WorkspaceStore(MemorySettings(root=tmp_path))
    entry = _entry("episode", "**User:** 帮我整理目录")
    path = store.append_entry("user-a", entry)
    assert path.parent.name == "memory"
    assert path.name == "2026-11-08.md"


def test_update_and_delete(tmp_path) -> None:
    store = WorkspaceStore(MemorySettings(root=tmp_path))
    store.append_entry("user-a", _entry("preference", "喜欢黑咖啡", entry_id="m-1"))
    updated = store.update_entry("user-a", "m-1", content="喜欢拿铁")
    assert updated.content == "喜欢拿铁"
    assert store.read_entries("user-a")[0].content == "喜欢拿铁"

    store.delete_entry("user-a", "m-1")
    assert store.read_entries("user-a") == []
    with pytest.raises(MemoryNotFound):
        store.delete_entry("user-a", "m-1")


def test_users_are_isolated(tmp_path) -> None:
    store = WorkspaceStore(MemorySettings(root=tmp_path))
    store.append_entry("user-a", _entry("preference", "A 的偏好"))
    store.append_entry("user-b", _entry("preference", "B 的偏好"))
    assert [entry.content for entry in store.read_entries("user-a")] == ["A 的偏好"]
    assert [entry.content for entry in store.read_entries("user-b")] == ["B 的偏好"]


def test_path_traversal_is_rejected(tmp_path) -> None:
    root = user_root(MemorySettings(root=tmp_path), "user-a")
    with pytest.raises(MemoryValidationError):
        resolve_under(root, "../../etc/passwd")
    with pytest.raises(MemoryValidationError):
        resolve_under(root, "/etc/passwd")


def test_duplicate_ids_are_deduplicated_on_read(tmp_path) -> None:
    store = WorkspaceStore(MemorySettings(root=tmp_path))
    store.append_entry("user-a", _entry("preference", "第一条", entry_id="m-dup"))
    # Append a second entry with a different id, then hand-write a duplicate.
    store.append_entry("user-a", _entry("preference", "第二条", entry_id="m-2"))
    path = user_root(store.settings, "user-a") / "MEMORY.md"
    path.write_text(path.read_text() + "\n## Preferences\n- 重复\n  <!-- memory: {\"id\":\"m-dup\"} -->\n")
    entries = store.read_entries("user-a")
    assert [entry.id for entry in entries].count("m-dup") == 1
