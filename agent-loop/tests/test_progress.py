from __future__ import annotations

import json

from bokaclaw_agent.agent.progress import ProgressGuard
from bokaclaw_agent.core.types import ToolResult


def _result(content: str, *, error: bool = False) -> ToolResult:
    return ToolResult(tool_call_id="call_1", name="tool", content=content, is_error=error)


def _search(*urls: str) -> ToolResult:
    return _result(json.dumps({"results": [{"title": url, "url": url, "snippet": ""} for url in urls]}))


def _fetch(url: str, content: str) -> ToolResult:
    return _result(json.dumps({"url": url, "content": content}))


def test_a_new_result_is_progress_and_resets_the_counter():
    guard = ProgressGuard(nudge_after=2, stop_after=3)

    assert guard.record(_search("https://a.example/1")) == "ok"
    assert guard.record(_search("https://a.example/1")) == "ok"
    assert guard.stalled_calls == 1
    assert guard.record(_search("https://a.example/2")) == "ok"
    assert guard.stalled_calls == 0


def test_the_nudge_fires_once_then_the_stop():
    guard = ProgressGuard(nudge_after=2, stop_after=3)
    result = _search("https://a.example/1")

    assert guard.record(result) == "ok"
    assert guard.record(result) == "ok"
    assert guard.record(result) == "nudge"
    assert guard.record(result) == "stop"


def test_no_nudge_again_until_new_evidence_arrives():
    guard = ProgressGuard(nudge_after=2, stop_after=4)
    repeated = _search("https://a.example/1")

    assert guard.record(repeated) == "ok"
    assert guard.record(repeated) == "ok"
    assert guard.record(repeated) == "nudge"
    assert guard.record(repeated) == "ok"
    assert guard.record(repeated) == "stop"


def test_errors_and_empty_results_never_count_as_progress():
    guard = ProgressGuard(nudge_after=2, stop_after=3)

    assert guard.record(_result('{"error": "HTTP 404"}', error=True)) == "ok"
    assert guard.record(_result("   ")) == "nudge"
    assert guard.record(_result("{}")) == "stop"


def test_cache_hits_are_never_progress():
    guard = ProgressGuard(nudge_after=2, stop_after=3)
    cached_search = _result(json.dumps({"results": [{"url": "https://a.example"}], "cached": True}))
    cached_fetch = _result(json.dumps({"url": "https://a.example", "content": "digest", "cached": True}))

    assert guard.record(cached_search) == "ok"
    assert guard.record(cached_fetch) == "nudge"


def test_different_urls_with_identical_content_are_not_progress():
    guard = ProgressGuard(nudge_after=2, stop_after=3)

    assert guard.record(_fetch("https://a.example/1", "same body")) == "ok"
    assert guard.record(_fetch("https://b.example/2", "same body")) == "ok"
    assert guard.record(_fetch("https://c.example/3", "same body")) == "nudge"


def test_different_content_is_progress():
    guard = ProgressGuard(nudge_after=2, stop_after=2)
    guard.record(_fetch("https://a.example/1", "first body"))

    assert guard.record(_fetch("https://a.example/2", "second body")) == "ok"
    assert guard.stalled_calls == 0


def test_reordered_search_results_are_the_same_evidence():
    guard = ProgressGuard(nudge_after=2, stop_after=3)
    first = _search("https://a.example/1", "https://a.example/2")
    second = _search("https://a.example/2", "https://a.example/1")

    assert guard.record(first) == "ok"
    assert guard.record(second) == "ok"
    assert guard.record(second) == "nudge"


def test_identical_stdout_is_not_progress_but_new_stdout_is():
    guard = ProgressGuard(nudge_after=2, stop_after=3)
    same = _result(json.dumps({"stdout": "a.txt\nb.txt\n", "exit_code": 0}))
    assert guard.record(same) == "ok"
    assert guard.record(same) == "ok"
    assert guard.record(same) == "nudge"

    fresh = _result(json.dumps({"stdout": "c.txt\n", "exit_code": 0}))
    assert guard.record(fresh) == "ok"
    assert guard.stalled_calls == 0


def test_plain_text_results_are_hashed():
    guard = ProgressGuard(nudge_after=2, stop_after=3)

    assert guard.record(_result("plain answer")) == "ok"
    assert guard.record(_result("plain answer")) == "ok"
    assert guard.record(_result("plain answer")) == "nudge"


def test_thresholds_are_clamped_to_sane_values():
    guard = ProgressGuard(nudge_after=0, stop_after=0)
    result = _search("https://a.example/1")

    assert guard.record(result) == "ok"
    assert guard.record(result) == "ok"
    assert guard.record(result) == "stop"
