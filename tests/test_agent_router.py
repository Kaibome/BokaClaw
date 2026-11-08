"""Tests for the /agent and /conversations routers.

These tests stub the two points where the router touches shared state:
`_prepare_turn` (persists the user turn) and `_save_assistant` (persists the
reply). Everything else -- auth, request validation, SSE framing, executor
selection, and error mapping -- runs for real.

Set AGENT_LOOP_MOCK=true to exercise the built-in stream without a network
call; leave it false with no AGENT_LOOP_URL to exercise the in-process path
(stubbed at `agent_loop.stream_local_agent`); point AGENT_LOOP_URL at a closed
port to exercise the unreachable remote path.
"""

import json
import os
import sys
import uuid
from datetime import datetime, timezone

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")
pytest.importorskip("slowapi", reason="slowapi not installed")
httpx = pytest.importorskip("httpx", reason="httpx not installed")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from slowapi import _rate_limit_exceeded_handler  # noqa: E402
from slowapi.errors import RateLimitExceeded  # noqa: E402

# server/ code uses absolute `app.*` imports, so the server directory must be
# on sys.path, mirroring how it runs in Docker.
_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import app.agent.loop as agent_loop  # noqa: E402
from app.agent.loop import MOCK_RESPONSE, sse_event  # noqa: E402
from app.agent.schemas import ConversationMessageOut, ConversationOut  # noqa: E402
from app.agent.store import DEFAULT_TITLE, MAX_TITLE_LENGTH, derive_title, get_owned_conversation  # noqa: E402
from app.core.auth import require_auth  # noqa: E402
from app.core.db import get_db  # noqa: E402
from app.core.rate_limit import limiter  # noqa: E402
from app.models import User  # noqa: E402
from app.routers import agent as agent_router  # noqa: E402
from app.routers import conversations as conversations_router  # noqa: E402


def _fake_user() -> User:
    return User(id=uuid.uuid4(), name="t", email="t@e.com", password_hash="x", role="admin")


def _conversation_out(conversation_id: str | None = None, title: str = "Hello") -> ConversationOut:
    now = datetime.now(timezone.utc)
    return ConversationOut(
        id=conversation_id or str(uuid.uuid4()),
        title=title,
        agent_id=None,
        metadata={},
        created_at=now,
        updated_at=now,
    )


def _user_message(conversation_id: str) -> ConversationMessageOut:
    return ConversationMessageOut(
        id=str(uuid.uuid4()),
        conversation_id=conversation_id,
        role="user",
        content="Hello",
        status="completed",
        metadata={},
        created_at=datetime.now(timezone.utc),
    )


def _prepared_turn() -> agent_router.PreparedTurn:
    conversation = _conversation_out()
    return agent_router.PreparedTurn(
        conversation=conversation,
        user_message=_user_message(conversation.id),
        history=[],
    )


def _events(body: str) -> list[str]:
    return [line[len("event: ") :] for line in body.splitlines() if line.startswith("event: ")]


@pytest.fixture(autouse=True)
def _legacy_request_bound_path(monkeypatch):
    """These tests cover the original request-bound route; background runs have
    their own suite in test_agent_runs.py."""
    monkeypatch.setenv("AGENT_RUN_BACKGROUND", "false")


@pytest.fixture
def client():
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.include_router(agent_router.router)
    app.include_router(conversations_router.router)
    app.dependency_overrides[require_auth] = _fake_user
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def stub_persistence(monkeypatch):
    monkeypatch.setattr(agent_router, "_prepare_turn", lambda body, user, limit: _prepared_turn())

    def _save(conversation_id, owner_id, message_id, content, status, metadata):
        return ConversationMessageOut(
            id=str(message_id),
            conversation_id=str(conversation_id),
            role="assistant",
            content=content,
            status=status,
            metadata=metadata,
            created_at=datetime.now(timezone.utc),
        )

    monkeypatch.setattr(agent_router, "_save_assistant", _save)


@pytest.fixture
def not_configured(monkeypatch):
    monkeypatch.delenv("AGENT_LOOP_URL", raising=False)
    monkeypatch.setenv("AGENT_LOOP_MOCK", "false")
    monkeypatch.setattr(agent_loop, "is_locally_configured", lambda: False)


@pytest.fixture
def mock_agent(monkeypatch):
    monkeypatch.setenv("AGENT_LOOP_MOCK", "true")


@pytest.fixture
def local_agent(monkeypatch):
    monkeypatch.delenv("AGENT_LOOP_URL", raising=False)
    monkeypatch.setenv("AGENT_LOOP_MOCK", "false")
    monkeypatch.setattr(agent_loop, "is_locally_configured", lambda: True)


def _local_events(*events):
    async def _stream(_payload, **_kwargs):
        for event in events:
            yield event

    return _stream


def _local_failure(code, detail):
    async def _stream(_payload, **_kwargs):
        raise agent_loop.AgentLoopError(code, detail)
        yield  # pragma: no cover - makes this an async generator

    return _stream


def test_unconfigured_agent_returns_503(client, not_configured):
    response = client.post("/agent", json={"message": "hello"})

    assert response.status_code == 503
    assert response.json()["code"] == "agent_not_configured"


def test_unconfigured_agent_streams_nothing(client, not_configured):
    response = client.post("/agent", json={"message": "hello", "stream": True})

    assert response.status_code == 503
    assert response.json()["code"] == "agent_not_configured"


def test_non_stream_turn_returns_persisted_message(client, mock_agent, stub_persistence):
    response = client.post("/agent", json={"message": "hello"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["message"]["role"] == "assistant"
    assert payload["message"]["content"] == MOCK_RESPONSE
    assert payload["message"]["status"] == "completed"


def test_stream_turn_emits_ordered_events(client, mock_agent, stub_persistence):
    response = client.post("/agent", json={"message": "hello", "stream": True})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _events(response.text)
    assert events[0] == "meta"
    assert "message.delta" in events
    assert "message.completed" in events
    assert events[-1] == "done"


def test_stream_deltas_reassemble_to_the_full_reply(client, mock_agent, stub_persistence):
    response = client.post("/agent", json={"message": "hello", "stream": True})

    deltas = []
    for block in response.text.split("\n\n"):
        if "event: message.delta" not in block:
            continue
        data = next(line[len("data: ") :] for line in block.splitlines() if line.startswith("data: "))
        deltas.append(json.loads(data)["delta"])

    assert "".join(deltas) == MOCK_RESPONSE


def test_unreachable_executor_maps_to_502(client, monkeypatch, stub_persistence):
    monkeypatch.setenv("AGENT_LOOP_MOCK", "false")
    monkeypatch.setenv("AGENT_LOOP_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("AGENT_LOOP_CONNECT_TIMEOUT_SECONDS", "1")

    response = client.post("/agent", json={"message": "hello"})

    assert response.status_code == 502
    assert response.json()["code"] == "agent_unreachable"


def test_local_runtime_is_used_when_no_executor_url(client, local_agent, monkeypatch, stub_persistence):
    monkeypatch.setattr(
        agent_loop,
        "stream_local_agent",
        _local_events(
            agent_loop.AgentDelta("hel"),
            agent_loop.AgentDelta("lo"),
            agent_loop.AgentCompleted({"finish_reason": "final"}),
        ),
    )

    response = client.post("/agent", json={"message": "hello", "stream": True})

    assert response.status_code == 200
    deltas = []
    completed = None
    for block in response.text.split("\n\n"):
        if "event: message.delta" in block:
            data = next(line[len("data: ") :] for line in block.splitlines() if line.startswith("data: "))
            deltas.append(json.loads(data)["delta"])
        elif "event: message.completed" in block:
            data = next(line[len("data: ") :] for line in block.splitlines() if line.startswith("data: "))
            completed = json.loads(data)["message"]
    assert "".join(deltas) == "hello"
    assert completed["content"] == "hello"
    assert completed["metadata"]["finish_reason"] == "final"
    assert _events(response.text)[-1] == "done"


def test_local_runtime_error_maps_to_503(client, local_agent, monkeypatch, stub_persistence):
    monkeypatch.setattr(
        agent_loop,
        "stream_local_agent",
        _local_failure("agent_not_configured", "no model"),
    )

    response = client.post("/agent", json={"message": "hello"})

    assert response.status_code == 503
    assert response.json()["code"] == "agent_not_configured"


def test_local_runtime_timeout_maps_to_504(client, local_agent, monkeypatch, stub_persistence):
    monkeypatch.setattr(
        agent_loop,
        "stream_local_agent",
        _local_failure("agent_timeout", "timed out"),
    )

    response = client.post("/agent", json={"message": "hello"})

    assert response.status_code == 504
    assert response.json()["code"] == "agent_timeout"


def test_blank_message_is_rejected(client, mock_agent):
    response = client.post("/agent", json={"message": "   "})

    assert response.status_code == 422


class _EmptySession:
    def scalar(self, *_args, **_kwargs):
        return None


@pytest.fixture
def conversations_client():
    app = FastAPI()
    app.include_router(conversations_router.router)
    app.dependency_overrides[require_auth] = _fake_user
    app.dependency_overrides[get_db] = lambda: _EmptySession()
    return TestClient(app, raise_server_exceptions=False)


def test_missing_conversation_is_404(conversations_client):
    response = conversations_client.get(f"/conversations/{uuid.uuid4()}")

    assert response.status_code == 404


def test_malformed_conversation_id_is_404(conversations_client):
    response = conversations_client.delete("/conversations/not-a-uuid")

    assert response.status_code == 404


def test_get_owned_conversation_rejects_malformed_ids():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        get_owned_conversation(_EmptySession(), "nope", uuid.uuid4())

    assert excinfo.value.status_code == 404


def test_derive_title_collapses_and_truncates():
    assert derive_title("  hello   world  ") == "hello world"
    assert derive_title("") == DEFAULT_TITLE

    long_title = derive_title("a" * 500)
    assert len(long_title) == MAX_TITLE_LENGTH + 3
    assert long_title.endswith("...")


def test_sse_event_round_trips_multiline_json():
    frame = sse_event("message.delta", {"delta": "line one\nline two"}, event_id="abc:1")

    assert frame.startswith("id: abc:1\nevent: message.delta\ndata: ")
    assert frame.endswith("\n\n")
    assert json.loads(frame.split("data: ", 1)[1])["delta"] == "line one\nline two"
