"""Tests for the read-only /traces router.

The router only reads `agent_run_spans`; these tests stub the SQLAlchemy
session so no Postgres is required. They check the contract the dashboard
relies on: list rows omit the large JSON payloads and the detail row carries
them.
"""

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from app.core.auth import require_admin  # noqa: E402
from app.core.db import get_db  # noqa: E402
from app.models import AgentRunSpan, User  # noqa: E402
from app.routers import traces as traces_router  # noqa: E402


class _ScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _FakeSession:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, stmt):
        rows = self.rows
        if "parent_span_id IS NULL" in str(stmt):
            rows = [row for row in rows if row.parent_span_id is None]
        return _ScalarResult(rows)

    def get(self, _model, ident):
        return next((row for row in self.rows if row.id == ident), None)


def _span(span_pk: int = 1, **overrides) -> AgentRunSpan:
    started_at = datetime.now(timezone.utc)
    values = {
        "id": span_pk,
        "run_id": uuid.uuid4(),
        "span_id": "agent",
        "parent_span_id": None,
        "span_type": "agent",
        "name": "agent.turn",
        "status": "completed",
        "iteration": None,
        "started_at": started_at,
        "ended_at": started_at + timedelta(milliseconds=120),
        "duration_ms": 120,
        "input": {"message": "find the answer"},
        "output": {"content": "done"},
        "attributes": {"model": "test-model"},
        "error": None,
    }
    values.update(overrides)
    return AgentRunSpan(**values)


@pytest.fixture
def spans():
    return [_span(1), _span(2, span_id="llm:1", span_type="llm", name="llm.turn.1")]


@pytest.fixture
def client(spans):
    app = FastAPI()
    app.include_router(traces_router.router)

    fake_user = User(id=uuid.uuid4(), name="t", email="t@e.com", password_hash="x", role="admin")
    app.dependency_overrides[require_admin] = lambda: fake_user
    app.dependency_overrides[get_db] = lambda: _FakeSession(spans)

    return TestClient(app, raise_server_exceptions=False)


def test_list_returns_span_summary(client):
    resp = client.get("/traces")

    assert resp.status_code == 200
    body = resp.json()
    assert [item["id"] for item in body] == [1, 2]
    assert body[0]["span_type"] == "agent"
    assert body[0]["duration_ms"] == 120
    # Payloads are detail-only so a long list stays small.
    assert "input" not in body[0]
    assert "output" not in body[0]
    assert "attributes" not in body[0]


def test_detail_returns_payloads(client):
    resp = client.get("/traces/1")

    assert resp.status_code == 200
    body = resp.json()
    assert body["input"] == {"message": "find the answer"}
    assert body["output"] == {"content": "done"}
    assert body["attributes"] == {"model": "test-model"}


def test_detail_missing_span_returns_404(client):
    resp = client.get("/traces/999")

    assert resp.status_code == 404


@pytest.mark.parametrize(
    "query",
    ["span_type=unknown", "status=unknown", "order=sideways"],
)
def test_invalid_filters_return_422(client, query):
    assert client.get(f"/traces?{query}").status_code == 422


def test_memory_spans_are_a_valid_filter(client):
    assert client.get("/traces?span_type=memory").status_code == 200


def test_compression_spans_are_a_valid_filter(client):
    assert client.get("/traces?span_type=compression").status_code == 200


def test_list_reports_cache_counters_from_the_root_span():
    span = _span(
        1,
        attributes={
            "usage": {
                "coverage": "complete",
                "total_tokens": 20,
                "cached_tokens": 12,
                "cache_miss_tokens": 8,
            }
        },
    )
    app = FastAPI()
    app.include_router(traces_router.router)
    fake_user = User(id=uuid.uuid4(), name="t", email="t@e.com", password_hash="x", role="admin")
    app.dependency_overrides[require_admin] = lambda: fake_user
    app.dependency_overrides[get_db] = lambda: _FakeSession([span])

    body = TestClient(app, raise_server_exceptions=False).get("/traces").json()[0]

    assert body["total_tokens"] == 20
    assert body["cached_tokens"] == 12
    assert body["cache_miss_tokens"] == 8
    assert body["cache_hit_rate"] == 0.6


def test_roots_only_hides_nested_agent_spans():
    root = _span(1)
    child = _span(
        2,
        span_id="subagent:sub_1",
        parent_span_id="agent",
        span_type="agent",
        name="subagent.explore",
    )
    app = FastAPI()
    app.include_router(traces_router.router)
    fake_user = User(id=uuid.uuid4(), name="t", email="t@e.com", password_hash="x", role="admin")
    app.dependency_overrides[require_admin] = lambda: fake_user
    app.dependency_overrides[get_db] = lambda: _FakeSession([root, child])
    client = TestClient(app, raise_server_exceptions=False)

    assert [item["id"] for item in client.get("/traces?span_type=agent").json()] == [1, 2]
    body = client.get("/traces?span_type=agent&roots_only=true").json()

    assert [item["id"] for item in body] == [1]


def test_list_omits_cache_fields_when_the_run_reported_none():
    span = _span(1, attributes={"usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6}})
    app = FastAPI()
    app.include_router(traces_router.router)
    fake_user = User(id=uuid.uuid4(), name="t", email="t@e.com", password_hash="x", role="admin")
    app.dependency_overrides[require_admin] = lambda: fake_user
    app.dependency_overrides[get_db] = lambda: _FakeSession([span])

    body = TestClient(app, raise_server_exceptions=False).get("/traces").json()[0]

    assert body["total_tokens"] == 6
    assert body["cached_tokens"] is None
    assert body["cache_hit_rate"] is None
