"""Migration mapping tests (no database)."""

from app.memory.migration import MigrationReport, _build_entry, _map_user


def test_research_row_mapping() -> None:
    row = {
        "id": "66fa6ce0-0000-0000-0000-000000000000",
        "payload": {
            "data": "Research conclusion\nQuestion: q\nAnswer: a\nSources:\n- https://example.com",
            "kind": "research_conclusion",
            "user_id": "pankaibo",
            "created_at": "2026-11-04T09:58:00Z",
            "captured_at": "2026-11-04T09:58:00Z",
            "question_key": "abc",
            "sources": ["https://example.com"],
        },
    }
    entry = _build_entry(row, "user-1", 30)
    assert entry is not None
    assert entry.id == row["id"]
    assert entry.kind == "research_conclusion"
    assert entry.source == "migration"
    assert entry.trust == "tool"
    assert entry.question_key == "abc"
    assert entry.sources == ["https://example.com"]
    assert entry.expires is not None
    assert entry.expires > entry.created


def test_plain_row_mapping() -> None:
    row = {
        "id": "11111111-1111-1111-1111-111111111111",
        "payload": {
            "data": "用户喜欢黑咖啡",
            "user_id": "user-1",
            "created_at": "2026-11-02T10:23:36Z",
        },
    }
    entry = _build_entry(row, "user-1", 30)
    assert entry is not None
    assert entry.kind == "operational"
    assert entry.trust == "owner"
    assert entry.expires is None


def test_non_uuid_user_maps_to_single_account() -> None:
    report = MigrationReport()
    mapped = _map_user("pankaibo", {}, ["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"], report)
    assert mapped == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    assert report.mappings["pankaibo"] == mapped


def test_explicit_user_map_wins() -> None:
    report = MigrationReport()
    mapped = _map_user("legacy", {"legacy": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"}, ["a"], report)
    assert mapped == "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def test_empty_content_is_invalid() -> None:
    assert _build_entry({"id": "x", "payload": {"data": "  "}}, "u", 30) is None
