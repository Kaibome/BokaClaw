"""Tests for reusable research conclusions stored in mem0.

The mem0 runtime is replaced with a fake so no embedding model or database is
needed; these tests cover source extraction, the save/update decision, TTL and
score filtering, and the block rendered into the system prompt.
"""

import json
import os
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from app.agent import research_memory as rm  # noqa: E402


class FakeMemory:
    def __init__(self, search_results=None, all_results=None):
        self.search_results = search_results or []
        self.all_results = all_results or []
        self.search_calls = []
        self.get_all_calls = []
        self.add_calls = []
        self.update_calls = []

    def search(self, **kwargs):
        self.search_calls.append(kwargs)
        return {"results": self.search_results}

    def get_all(self, **kwargs):
        self.get_all_calls.append(kwargs)
        return {"results": self.all_results}

    def add(self, **kwargs):
        self.add_calls.append(kwargs)
        return {"results": [{"id": "mem-new", "memory": kwargs["messages"][0]["content"]}]}

    def update(self, memory_id, **kwargs):
        self.update_calls.append((memory_id, kwargs))
        return {"message": "Memory updated successfully!"}


@pytest.fixture(autouse=True)
def clean_research_env(monkeypatch):
    for name in (
        "AGENT_RESEARCH_MEMORY",
        "AGENT_RESEARCH_MEMORY_TTL_DAYS",
        "AGENT_RESEARCH_MEMORY_MIN_SCORE",
    ):
        monkeypatch.delenv(name, raising=False)


def _install_memory(monkeypatch, memory):
    fake_state = types.SimpleNamespace(get_memory_instance=lambda: memory)
    monkeypatch.setitem(sys.modules, "app.core.server_state", fake_state)


def _now():
    return datetime.now(timezone.utc)


def _payload(**overrides):
    payload = {
        "user_id": "user-1",
        "message": "Which Qwen model is best for personal developers?",
        "run_id": "run-1",
        "conversation_id": "conv-1",
    }
    payload.update(overrides)
    return payload


def _search_entry(*results, **overrides):
    entry = {
        "name": "WebSearch",
        "status": "completed",
        "is_error": False,
        "finished_at": "2026-11-04T10:00:00+00:00",
        "content": json.dumps({"results": list(results)}),
    }
    entry.update(overrides)
    return entry


def _fetch_entry(url, **overrides):
    entry = {
        "name": "WebFetch",
        "status": "completed",
        "is_error": False,
        "arguments": {"url": url},
        "finished_at": "2026-11-04T10:05:00+00:00",
        "content": json.dumps({"url": url, "content": "page body"}),
    }
    entry.update(overrides)
    return entry


def _memory_item(text, *, score=0.8, captured_at=None, kind=rm.RESEARCH_KIND):
    captured = captured_at or _now() - timedelta(days=1)
    return {
        "id": "mem-1",
        "memory": text,
        "score": score,
        "created_at": captured.isoformat(),
        "metadata": {"kind": kind, "captured_at": captured.isoformat()},
    }


# ---- source extraction --------------------------------------------------------


def test_extract_sources_collects_search_and_fetch_urls_without_duplicates():
    entries = [
        _search_entry(
            {"url": "https://a.example/qwen", "title": "Qwen"},
            {"url": "https://b.example/", "title": "B"},
        ),
        _fetch_entry("https://a.example/qwen/"),
        _fetch_entry("https://c.example/pricing"),
    ]

    sources, fetched_at = rm.extract_web_sources(entries)

    assert [source["url"] for source in sources] == [
        "https://a.example/qwen",
        "https://b.example/",
        "https://c.example/pricing",
    ]
    assert sources[0]["title"] == "Qwen"
    assert fetched_at == "2026-11-04T10:05:00+00:00"


def test_extract_sources_ignores_errors_and_non_web_tools():
    entries = [
        _search_entry({"url": "https://ignored.example"}, is_error=True),
        _search_entry({"url": "https://ignored2.example"}, status="failed"),
        {"name": "Read", "status": "completed", "content": "file body"},
        {"name": "WebSearch", "status": "completed", "content": "not json"},
    ]

    assert rm.extract_web_sources(entries) == ([], None)


def test_extract_sources_uses_the_fetch_argument_when_the_body_has_no_url():
    entry = _fetch_entry("")
    entry["arguments"] = {"url": "https://args.example/page"}
    entry["content"] = json.dumps({"content": "body"})

    sources, _ = rm.extract_web_sources([entry])

    assert sources == [{"url": "https://args.example/page", "title": ""}]


# ---- saving -------------------------------------------------------------------


def test_identical_questions_update_the_existing_memory(monkeypatch):
    key = rm.question_key("Which Qwen model is best for personal developers?")
    memory = FakeMemory(
        all_results=[
            {
                "id": "mem-existing",
                "metadata": {"kind": rm.RESEARCH_KIND, "question_key": key},
            }
        ]
    )
    _install_memory(monkeypatch, memory)
    entries = [_search_entry({"url": "https://a.example", "title": "A"})]

    memory_id = rm.remember_research(_payload(), "A detailed answer.", entries)

    assert memory_id == "mem-existing"
    assert memory.add_calls == []
    updated_id, update = memory.update_calls[0]
    assert updated_id == "mem-existing"
    assert "A detailed answer." in update["text"]
    assert "https://a.example" in update["text"]
    assert update["metadata"]["kind"] == rm.RESEARCH_KIND
    assert update["metadata"]["sources"] == ["https://a.example"]
    assert update["metadata"]["run_id"] == "run-1"
    assert update["metadata"]["conversation_id"] == "conv-1"
    assert memory.get_all_calls[0]["filters"]["question_key"] == rm.question_key(
        "Which Qwen model is best for personal developers?"
    )


def test_an_unrelated_memory_is_never_overwritten(monkeypatch):
    memory = FakeMemory(all_results=[{"id": "mem-other", "metadata": {"kind": "note"}}])
    _install_memory(monkeypatch, memory)
    entries = [_search_entry({"url": "https://a.example", "title": "A"})]

    assert rm.remember_research(_payload(), "Answer.", entries) == "mem-new"
    assert memory.update_calls == []
    assert len(memory.add_calls) == 1


def test_a_new_question_adds_a_verbatim_memory(monkeypatch):
    memory = FakeMemory()
    _install_memory(monkeypatch, memory)
    entries = [_search_entry({"url": "https://a.example", "title": "A"})]

    memory_id = rm.remember_research(_payload(), "Answer.", entries)

    assert memory_id == "mem-new"
    assert memory.update_calls == []
    call = memory.add_calls[0]
    assert call["user_id"] == "user-1"
    assert call["infer"] is False
    assert call["messages"][0]["role"] == "assistant"
    assert "Question: Which Qwen model" in call["messages"][0]["content"]
    assert call["metadata"]["question_key"] == rm.question_key(_payload()["message"])


def test_saving_is_skipped_without_web_sources_user_or_answer(monkeypatch):
    memory = FakeMemory()
    _install_memory(monkeypatch, memory)

    assert rm.remember_research(_payload(), "answer", [{"name": "Read", "status": "completed", "content": "x"}]) is None
    assert rm.remember_research(_payload(user_id=""), "answer", [_search_entry({"url": "https://a.example"})]) is None
    assert rm.remember_research(_payload(), "   ", [_search_entry({"url": "https://a.example"})]) is None
    assert memory.add_calls == []


def test_saving_can_be_disabled(monkeypatch):
    monkeypatch.setenv("AGENT_RESEARCH_MEMORY", "false")
    memory = FakeMemory()
    _install_memory(monkeypatch, memory)

    assert rm.remember_research(_payload(), "answer", [_search_entry({"url": "https://a.example"})]) is None
    assert memory.search_calls == []


def test_memory_failures_never_raise(monkeypatch):
    class BrokenMemory(FakeMemory):
        def add(self, **kwargs):
            raise RuntimeError("embedding offline")

    memory = BrokenMemory()
    _install_memory(monkeypatch, memory)

    assert rm.remember_research(_payload(), "answer", [_search_entry({"url": "https://a.example"})]) is None


# ---- retrieval ----------------------------------------------------------------


def test_prior_research_block_keeps_fresh_high_scoring_conclusions(monkeypatch):
    now = _now()
    fresh = now - timedelta(days=3)
    stale = now - timedelta(days=60)
    memory = FakeMemory(
        search_results=[
            _memory_item("Fresh conclusion.", score=0.9, captured_at=fresh),
            _memory_item("Stale conclusion.", score=0.9, captured_at=stale),
            _memory_item("Weak match.", score=0.2, captured_at=fresh),
            _memory_item("A user note.", score=0.9, captured_at=fresh, kind="note"),
        ]
    )
    _install_memory(monkeypatch, memory)

    block = rm.prior_research_block("user-1", "a similar question", ttl_days=30, min_score=0.5)

    assert "Fresh conclusion." in block
    assert "Stale conclusion." not in block
    assert "Weak match." not in block
    assert "A user note." not in block
    assert "may be" in block
    assert "verify them with WebSearch/WebFetch" in block
    call = memory.search_calls[0]
    assert call["filters"] == {"user_id": "user-1", "kind": rm.RESEARCH_KIND}
    assert call["threshold"] == pytest.approx(0.4)


def test_prior_research_block_is_bounded_and_skips_missing_times(monkeypatch):
    long_text = "x" * 5_000
    memory = FakeMemory(
        search_results=[
            {"id": "no-time", "memory": "no timestamp", "score": 0.9, "metadata": {"kind": rm.RESEARCH_KIND}},
            _memory_item(long_text, score=0.9),
        ]
    )
    _install_memory(monkeypatch, memory)

    block = rm.prior_research_block("user-1", "question", ttl_days=30, min_score=0.5)

    assert "no timestamp" not in block
    assert len(block) <= rm.MAX_INJECTED_CHARS + 400
    assert block.count("x") == rm.MAX_INJECTED_CONCLUSION_CHARS - 3


def test_prior_research_block_is_empty_without_a_user_question_or_memory(monkeypatch):
    memory = FakeMemory()
    _install_memory(monkeypatch, memory)

    assert rm.prior_research_block(None, "question") == ""
    assert rm.prior_research_block("user-1", "  ") == ""
    assert memory.search_calls == []

    _install_memory(monkeypatch, None)

    assert rm.prior_research_block("user-1", "question") == ""


def test_prior_research_block_failure_is_swallowed(monkeypatch):
    class BrokenMemory(FakeMemory):
        def search(self, **kwargs):
            raise RuntimeError("vector store offline")

    _install_memory(monkeypatch, BrokenMemory())

    assert rm.prior_research_block("user-1", "question") == ""


def test_question_key_ignores_case_and_whitespace():
    assert rm.question_key("  Same   Question ") == rm.question_key("same question")
