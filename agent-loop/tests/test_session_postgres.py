from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from bokaclaw_agent.exceptions import PersistenceError
from bokaclaw_agent.sessions.postgres import PostgresConfig, PostgresSessionStore


class FakeCursor:
    def __init__(self, pool: "FakePool") -> None:
        self.pool = pool

    def execute(self, sql, params=None):
        self.pool.executed.append((sql, params))

    def executemany(self, sql, rows):
        self.pool.executed.append((sql, rows))

    def fetchone(self):
        return self.pool.fetchone_values.pop(0) if self.pool.fetchone_values else None

    def fetchall(self):
        return self.pool.fetchall_values.pop(0) if self.pool.fetchall_values else []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeConnection:
    def __init__(self, pool: "FakePool") -> None:
        self.pool = pool

    def cursor(self):
        return FakeCursor(self.pool)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakePool:
    def __init__(self, *, fetchone=None, fetchall=None) -> None:
        self.executed = []
        self.fetchone_values = list(fetchone or [])
        self.fetchall_values = list(fetchall or [])
        self.closed = False

    def connection(self):
        return FakeConnection(self)

    def close(self):
        self.closed = True


def make_store(pool, **config_kwargs) -> PostgresSessionStore:
    config_kwargs.setdefault("auto_create_schema", False)
    return PostgresSessionStore(PostgresConfig(**config_kwargs), pool=pool)


def test_ensure_schema_creates_both_tables_once():
    pool = FakePool()
    store = PostgresSessionStore(PostgresConfig(auto_create_schema=True), pool=pool)

    create_statements = [sql for sql, _ in pool.executed if "CREATE TABLE" in sql]
    assert len(create_statements) == 2
    executed_before = len(pool.executed)

    store.ensure_schema()
    store.ensure_schema()
    assert len(pool.executed) == executed_before


LEGACY_STATUS_CHECK = "CHECK ((status = ANY (ARRAY['active'::text, 'completed'::text, 'failed'::text])))"
UPGRADED_STATUS_CHECK = (
    "CHECK ((status = ANY (ARRAY['active'::text, 'completed'::text, 'failed'::text, 'cancelled'::text])))"
)


def test_sessions_ddl_allows_the_cancelled_status():
    pool = FakePool()
    PostgresSessionStore(PostgresConfig(auto_create_schema=True), pool=pool)

    sessions_ddl = next(
        sql for sql, _ in pool.executed if "CREATE TABLE IF NOT EXISTS bokaclaw_agent_sessions (" in sql
    )
    assert "'cancelled'" in sessions_ddl


def test_ensure_schema_upgrades_a_legacy_status_check():
    pool = FakePool(fetchall=[[("bokaclaw_agent_sessions_status_check", LEGACY_STATUS_CHECK)]])

    PostgresSessionStore(PostgresConfig(auto_create_schema=True), pool=pool)

    alters = [(sql, params) for sql, params in pool.executed if "ALTER TABLE" in sql]
    assert len(alters) == 2
    assert "DROP CONSTRAINT" in alters[0][0]
    assert '"bokaclaw_agent_sessions_status_check"' in alters[0][0]
    assert "ADD CONSTRAINT" in alters[1][0]
    assert '"bokaclaw_agent_sessions_status_check"' in alters[1][0]
    assert "'cancelled'" in alters[1][0]


def test_ensure_schema_uses_the_actual_constraint_name():
    pool = FakePool(fetchall=[[("custom_status_check", LEGACY_STATUS_CHECK)]])

    PostgresSessionStore(PostgresConfig(auto_create_schema=True), pool=pool)

    alters = [sql for sql, _ in pool.executed if "ALTER TABLE" in sql]
    assert len(alters) == 2
    assert '"custom_status_check"' in alters[0]
    assert '"custom_status_check"' in alters[1]


def test_ensure_schema_leaves_an_upgraded_status_check_alone():
    pool = FakePool(fetchall=[[("bokaclaw_agent_sessions_status_check", UPGRADED_STATUS_CHECK)]])

    PostgresSessionStore(PostgresConfig(auto_create_schema=True), pool=pool)

    assert not any("ALTER TABLE" in sql for sql, _ in pool.executed)


def test_status_check_migration_does_not_run_twice():
    pool = FakePool(fetchall=[[("bokaclaw_agent_sessions_status_check", LEGACY_STATUS_CHECK)]])
    store = PostgresSessionStore(PostgresConfig(auto_create_schema=True), pool=pool)
    executed_after_init = len(pool.executed)

    store.ensure_schema()

    assert len(pool.executed) == executed_after_init


def test_persist_run_accepts_the_cancelled_status():
    pool = FakePool(fetchone=[(0,)])
    store = make_store(pool)

    store.persist_run("s1", [{"role": "user", "content": "hi"}], agent_name="agent", status="cancelled")

    session_params = pool.executed[0][1]
    assert session_params[5] == "cancelled"


def test_get_session_maps_row():
    now = datetime.now(timezone.utc)
    pool = FakePool(fetchone=[("s1", "agent", "u1", "title", {"a": 1}, "completed", None, now, now)])
    store = make_store(pool)

    record = store.get_session("s1")

    assert record is not None
    assert record.id == "s1"
    assert record.agent_name == "agent"
    assert record.user_id == "u1"
    assert record.title == "title"
    assert record.metadata == {"a": 1}
    assert record.status == "completed"


def test_get_session_returns_none_for_missing_row():
    store = make_store(FakePool())
    assert store.get_session("missing") is None


def test_load_messages_reconstructs_wire_messages():
    tool_calls = [{"id": "call_1", "type": "function", "function": {"name": "add", "arguments": "{}"}}]
    pool = FakePool(
        fetchall=[
            [
                ("user", "hi", None, None, None),
                ("assistant", None, tool_calls, None, None),
                ("tool", "3", None, "call_1", "add"),
            ]
        ]
    )
    store = make_store(pool)

    messages = store.load_messages("s1")

    assert messages == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "tool_calls": tool_calls},
        {"role": "tool", "content": "3", "tool_call_id": "call_1"},
    ]


def test_persist_run_upserts_session_and_numbers_messages():
    pool = FakePool(fetchone=[(3,)])
    store = make_store(pool)

    store.persist_run(
        "s1",
        [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "ok"},
        ],
        agent_name="agent",
        user_id="u1",
        metadata={"k": "v"},
        status="completed",
    )

    session_sql, session_params = pool.executed[0]
    assert "INSERT INTO bokaclaw_agent_sessions" in session_sql
    assert "ON CONFLICT (id) DO UPDATE" in session_sql
    assert session_params == ("s1", "agent", "u1", "hi", '{"k": "v"}', "completed", None)

    max_seq_sql, max_seq_params = pool.executed[1]
    assert "COALESCE(MAX(seq), 0)" in max_seq_sql
    assert max_seq_params == ("s1",)

    insert_sql, rows = pool.executed[2]
    assert "INSERT INTO bokaclaw_agent_messages" in insert_sql
    assert [row[1] for row in rows] == [4, 5]
    assert [row[2] for row in rows] == ["user", "assistant"]


def test_persist_run_serializes_tool_calls():
    tool_calls = [{"id": "call_1", "type": "function", "function": {"name": "add", "arguments": "{}"}}]
    pool = FakePool(fetchone=[(0,)])
    store = make_store(pool)

    store.persist_run(
        "s1",
        [{"role": "assistant", "content": None, "tool_calls": tool_calls}],
        agent_name="agent",
    )

    rows = pool.executed[2][1]
    assert json.loads(rows[0][4]) == tool_calls
    assert rows[0][3] is None


def test_persist_run_rejects_invalid_role():
    store = make_store(FakePool(fetchone=[(0,)]))
    with pytest.raises(PersistenceError):
        store.persist_run("s1", [{"role": "invalid", "content": "x"}], agent_name="agent")


def test_list_sessions_with_user_filter():
    now = datetime.now(timezone.utc)
    row = ("s1", "agent", "u1", "title", {}, "completed", None, now, now)
    pool = FakePool(fetchall=[[row]])
    store = make_store(pool)

    records = store.list_sessions(user_id="u1", limit=10, offset=2)

    assert [record.id for record in records] == ["s1"]
    sql, params = pool.executed[0]
    assert "WHERE user_id = %s" in sql
    assert params == ("u1", 10, 2)


def test_delete_session_uses_cascade():
    pool = FakePool()
    store = make_store(pool)

    store.delete_session("s1")

    sql, params = pool.executed[0]
    assert "DELETE FROM bokaclaw_agent_sessions WHERE id = %s" in sql
    assert params == ("s1",)


def test_errors_are_wrapped():
    class BrokenPool:
        def connection(self):
            raise RuntimeError("db down")

    store = make_store(BrokenPool())
    with pytest.raises(PersistenceError, match="db down"):
        store.load_messages("s1")


def test_close_only_closes_owned_pool():
    pool = FakePool()
    store = make_store(pool)
    store.close()
    assert pool.closed is False


@pytest.mark.integration
@pytest.mark.skipif(
    not os.getenv("BOKACLAW_AGENT_TEST_DSN"),
    reason="set BOKACLAW_AGENT_TEST_DSN to a Postgres DSN to run integration tests",
)
def test_postgres_round_trip():
    store = PostgresSessionStore(
        PostgresConfig(
            dsn=os.environ["BOKACLAW_AGENT_TEST_DSN"],
            table_prefix="bokaclaw_agent_it",
        )
    )
    session_id = f"it-{uuid4().hex}"
    tool_calls = [{"id": "call_1", "type": "function", "function": {"name": "add", "arguments": "{}"}}]
    try:
        store.persist_run(
            session_id,
            [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": None, "tool_calls": tool_calls},
                {"role": "tool", "tool_call_id": "call_1", "content": "3"},
            ],
            agent_name="it",
            user_id="u1",
            metadata={"x": 1},
            status="completed",
        )

        record = store.get_session(session_id)
        assert record is not None
        assert record.status == "completed"
        assert record.metadata == {"x": 1}
        assert store.load_messages(session_id) == [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": None, "tool_calls": tool_calls},
            {"role": "tool", "content": "3", "tool_call_id": "call_1"},
        ]

        store.persist_run(
            session_id,
            [{"role": "assistant", "content": "done"}],
            agent_name="it",
            status="completed",
        )
        messages = store.load_messages(session_id)
        assert len(messages) == 4
        assert messages[-1] == {"role": "assistant", "content": "done"}
    finally:
        store.delete_session(session_id)
        store.close()
