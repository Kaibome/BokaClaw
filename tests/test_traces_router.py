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

    def execute(self, _stmt):
        return _ScalarResult(self.rows)

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
