"""Markdown grammar tests."""

from datetime import datetime, timezone

from app.memory.grammar import (
    MemoryEntry,
    default_importance,
    parse_document,
    render_document,
    stable_id,
)


def test_parse_metadata_and_multiline_content() -> None:
    text = """# Memory

## Preferences
- 用户喜欢黑咖啡
  并且周末去徒步
  <!-- memory: {"id":"m-123","kind":"preference","importance":0.9,"source":"curated","trust":"owner","created":"2026-11-01T10:00:00Z","updated":"2026-11-02T10:00:00Z","sources":["https://example.com"]} -->
"""
    entries = parse_document(text, path="MEMORY.md")
    assert len(entries) == 1
    entry = entries[0]
    assert entry.id == "m-123"
    assert entry.kind == "preference"
    assert entry.content == "用户喜欢黑咖啡\n并且周末去徒步"
    assert entry.importance == 0.9
    assert entry.sources == ["https://example.com"]
    assert entry.created.isoformat().startswith("2026-11-01")


def test_manual_entry_without_metadata() -> None:
    text = """# User

## Identity
- 用户住在深圳
"""
    entries = parse_document(text, path="USER.md")
    assert len(entries) == 1
    entry = entries[0]
    assert entry.kind == "identity"
    assert entry.source == "manual"
    assert entry.trust == "owner"
    assert entry.id.startswith("m-")
    assert entry.importance == default_importance("identity")


def test_malformed_metadata_is_tolerated() -> None:
    text = """## Rules
- 总是先备份
  <!-- memory: {not-json} -->
"""
    entries = parse_document(text, path="MEMORY.md")
    assert len(entries) == 1
    assert entries[0].content == "总是先备份"
    assert entries[0].source == "manual"


def test_render_roundtrip() -> None:
    now = datetime(2026, 11, 8, tzinfo=timezone.utc)
    entries = [
        MemoryEntry(
            id=stable_id("preference", "喜欢黑咖啡"),
            kind="preference",
            content="喜欢黑咖啡",
            importance=0.85,
            source="curated",
            trust="owner",
            created=now,
            updated=now,
        )
    ]
    rendered = render_document(entries, "Memory")
    parsed = parse_document(rendered, path="MEMORY.md")
    assert len(parsed) == 1
    assert parsed[0].id == entries[0].id
    assert parsed[0].content == "喜欢黑咖啡"
    assert parsed[0].kind == "preference"


def test_daily_capture_heading() -> None:
    text = """# 2026-11-08

## Captured
- **User:** 帮我整理目录
  **Assistant:** 已整理
  <!-- memory: {"id":"m-ep","kind":"episode","source":"capture","trust":"owner"} -->
"""
    entries = parse_document(text, path="memory/2026-11-08.md")
    assert len(entries) == 1
    assert entries[0].kind == "episode"
    assert "**User:**" in entries[0].content
