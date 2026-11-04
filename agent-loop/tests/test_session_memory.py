from __future__ import annotations

import pytest

from bokaclaw_agent.core.exceptions import PersistenceError
from bokaclaw_agent.sessions.memory import InMemorySessionStore


def test_persist_and_load_round_trip():
    store = InMemorySessionStore()
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]

    store.persist_run("s1", messages, agent_name="agent", user_id="u1", metadata={"a": 1})

    record = store.get_session("s1")
    assert record is not None
    assert record.agent_name == "agent"
    assert record.user_id == "u1"
    assert record.title == "hello"
    assert record.status == "completed"
    assert record.metadata == {"a": 1}
    assert store.load_messages("s1") == messages


def test_second_run_appends_and_merges_metadata():
    store = InMemorySessionStore()
    store.persist_run(
        "s1",
        [{"role": "user", "content": "first"}],
        agent_name="agent",
        user_id="u1",
        metadata={"a": 1},
    )
    store.persist_run(
        "s1",
        [{"role": "assistant", "content": "second"}],
        agent_name="agent",
        metadata={"b": 2},
    )

    record = store.get_session("s1")
    assert record.user_id == "u1"  # preserved when the second call passes None
    assert record.title == "first"
    assert record.metadata == {"a": 1, "b": 2}
    assert store.load_messages("s1") == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
    ]


def test_list_sessions_filters_and_paginates():
    store = InMemorySessionStore()
    store.persist_run("s1", [{"role": "user", "content": "one"}], agent_name="a", user_id="u1")
    store.persist_run("s2", [{"role": "user", "content": "two"}], agent_name="a", user_id="u2")

    assert [record.id for record in store.list_sessions(user_id="u1")] == ["s1"]
    assert len(store.list_sessions()) == 2
    assert len(store.list_sessions(limit=1)) == 1
    assert len(store.list_sessions(limit=1, offset=1)) == 1


def test_delete_session_removes_messages():
    store = InMemorySessionStore()
    store.persist_run("s1", [{"role": "user", "content": "one"}], agent_name="a")

    store.delete_session("s1")

    assert store.get_session("s1") is None
    assert store.load_messages("s1") == []


def test_invalid_status_is_rejected():
    store = InMemorySessionStore()
    with pytest.raises(PersistenceError):
        store.persist_run("s1", [], agent_name="a", status="unknown")


def test_loaded_messages_are_copies():
    store = InMemorySessionStore()
    store.persist_run("s1", [{"role": "user", "content": "one"}], agent_name="a")

    messages = store.load_messages("s1")
    messages[0]["content"] = "mutated"

    assert store.load_messages("s1")[0]["content"] == "one"
