from __future__ import annotations

import json

from bokaclaw_agent.agent.ledger import ToolLedger
from bokaclaw_agent.core.types import ToolCall, ToolResult


def _call(name: str, arguments: dict | None = None, call_id: str = "call_1") -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments or {}, raw_arguments=json.dumps(arguments or {}))


def _result(content: str, *, call_id: str = "call_1", error: bool = False) -> ToolResult:
    return ToolResult(tool_call_id=call_id, name="tool", content=content, is_error=error)


def test_empty_ledger_renders_nothing():
    assert ToolLedger().render() is None


def test_search_entry_lists_titles_and_urls():
    ledger = ToolLedger()
    results = [
        {"title": "Qwen pricing", "url": "https://example.com/pricing", "snippet": "price"},
        {"title": "Qwen docs", "url": "https://example.com/docs", "snippet": "docs"},
        {"title": "", "url": "https://example.com/untitled", "snippet": ""},
        {"title": "ignored fourth", "url": "https://example.com/fourth", "snippet": ""},
    ]
    ledger.record(_call("WebSearch", {"query": "qwen 价格"}), _result(json.dumps({"results": results})))

    text = ledger.render()

    assert text is not None
    assert "## Research state (this run)" in text
    assert 'WebSearch "qwen 价格"' in text
    assert "4 results" in text
    assert "Qwen pricing <https://example.com/pricing>" in text
    assert "<https://example.com/untitled>" in text
    assert "ignored fourth" not in text


def test_fetch_entry_reports_size_and_excerpt():
    ledger = ToolLedger()
    ledger.record(
        _call("WebFetch", {"url": "https://example.com/page"}),
        _result(json.dumps({"url": "https://example.com/page", "content": "x" * 900, "truncated": True})),
    )

    text = ledger.render()

    assert text is not None
    assert "WebFetch https://example.com/page" in text
    assert "900 chars (truncated) from https://example.com/page" in text


def test_cached_fetch_is_marked():
    ledger = ToolLedger()
    ledger.record(
        _call("WebFetch", {"url": "https://example.com/page"}),
        _result(json.dumps({"url": "https://example.com/page", "content": "body", "cached": True})),
    )

    text = ledger.render()

    assert text is not None
    assert "(already fetched)" in text


def test_error_entry_is_marked_as_error():
    ledger = ToolLedger()
    ledger.record(
        _call("WebFetch", {"url": "https://example.com/missing"}),
        _result(json.dumps({"error": "HTTP 404"}), error=True),
    )

    text = ledger.render()

    assert text is not None
    assert "ERROR: error: HTTP 404" in text


def test_repeated_calls_are_counted_not_duplicated():
    ledger = ToolLedger()
    for _ in range(3):
        ledger.record(_call("WebSearch", {"query": "same"}), _result(json.dumps({"results": []})))

    text = ledger.render()

    assert text is not None
    assert text.count("WebSearch") == 1
    assert "(x3)" in text


def test_argument_key_order_does_not_split_entries():
    ledger = ToolLedger()
    ledger.record(_call("add", {"a": 1, "b": 2}), _result("3"))
    ledger.record(_call("add", {"b": 2, "a": 1}), _result("3"))

    text = ledger.render()

    assert text is not None
    assert text.count("add") == 1
    assert "(x2)" in text


def test_nothing_is_persisted_by_the_ledger_itself():
    ledger = ToolLedger()
    ledger.record(_call("Bash", {"command": "ls -la"}), _result(json.dumps({"stdout": "a\nb", "exit_code": 0})))

    text = ledger.render()

    assert text is not None
    assert 'Bash "ls -la"' in text
    assert "a b" in text


def test_old_entries_are_dropped_to_fit_the_character_budget():
    ledger = ToolLedger()
    for index in range(12):
        ledger.record(
            _call("Read", {"file_path": f"/workspace/file-{index}.txt"}, call_id=f"call_{index}"),
            _result("y" * 240),
        )

    text = ledger.render(max_chars=800)

    assert text is not None
    assert len(text) <= 800
    assert "earlier calls omitted" in text
    assert "file-11.txt" in text
    assert "file-0.txt" not in text


def test_summary_length_is_capped_and_kept_on_one_line():
    ledger = ToolLedger()
    ledger.record(_call("Bash", {"command": "cat notes"}), _result("line one\nline two\n" + "q" * 400))

    text = ledger.render()

    assert text is not None
    body = text.splitlines()[-1]
    assert "line one line two" in body
    assert "\n" not in body
    assert body.endswith("...")


def test_a_single_entry_over_a_tiny_budget_keeps_the_header():
    ledger = ToolLedger()
    ledger.record(
        _call("WebFetch", {"url": "https://example.com/page"}),
        _result(json.dumps({"url": "https://example.com/page", "content": "z" * 5000})),
    )

    text = ledger.render(max_chars=256)

    assert text is not None
    assert len(text) <= 256
    assert text.startswith("## Research state (this run)")
    assert "WebFetch https://example.com/page" in text
