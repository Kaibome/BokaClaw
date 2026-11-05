"""Background agent-run tests: manager lifecycle, replay, cancel, recovery.

The manager is exercised against an in-memory repository and a scripted
executor. No Postgres or model call is involved, but every seq, status, and
persistence decision goes through the same code path as production.
"""

import asyncio
import copy
import json
import os
import sys
import threading
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")
pytest.importorskip("slowapi", reason="slowapi not installed")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from slowapi import _rate_limit_exceeded_handler  # noqa: E402
from slowapi.errors import RateLimitExceeded  # noqa: E402

_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import app.routers.agent as agent_router  # noqa: E402
import app.agent.runner as agent_runner  # noqa: E402
from app.agent.events import (  # noqa: E402
    AgentCompleted,
    AgentDelta,
    AgentLoopError,
    AgentToolCall,
    AgentToolResult,
    AgentUsageUpdate,
)
from app.agent.runner import AgentRunManager, RunConfig  # noqa: E402
from app.agent.schemas import ConversationMessageOut, ConversationOut  # noqa: E402
from app.core.auth import require_auth  # noqa: E402
from app.core.rate_limit import limiter  # noqa: E402
from app.models import User  # noqa: E402


def _now():
    return datetime.now(timezone.utc)


TEST_USER = User(id=uuid.uuid4(), name="t", email="t@e.com", password_hash="x", role="admin")


def _run(**overrides):
    values = {
        "id": uuid.uuid4(),
        "conversation_id": uuid.uuid4(),
        "created_by": TEST_USER.id,
        "user_message_id": uuid.uuid4(),
        "assistant_message_id": uuid.uuid4(),
        "request_payload": {"message": "hello"},
        "client_run_id": None,
        "agent_id": None,
        "status": "queued",
        "error_code": None,
        "error_detail": None,
        "partial_content": "",
        "partial_metadata": {},
        "last_seq": 0,
        "attempt": 1,
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "model_calls": 0,
        "unknown_usage_calls": 0,
        "usage_breakdown": {},
        "worker_id": None,
        "heartbeat_at": None,
        "cancel_requested_at": None,
        "started_at": None,
        "finished_at": None,
        "created_at": _now(),
        "updated_at": _now(),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _message(message_id, conversation_id, content, status, metadata):
    return SimpleNamespace(
        id=message_id,
        conversation_id=conversation_id,
        role="assistant",
        content=content,
        status=status,
        meta=copy.deepcopy(metadata),
        created_at=_now(),
    )


class FakeRepo:
    def __init__(self):
        self.runs = {}
        self.events = {}
        self.messages = {}
        self.spans = {}
        self._lock = threading.RLock()

    def add_run(self, run):
        with self._lock:
            self.runs[run.id] = run
            self.events[run.id] = []
        return run

    def add_event(self, run_id, seq, event_type, payload):
        with self._lock:
            self.events[run_id].append(SimpleNamespace(seq=seq, event_type=event_type, payload=copy.deepcopy(payload)))
            self.runs[run_id].last_seq = seq

    def get_run(self, run_id, owner_id=None):
        with self._lock:
            run = self.runs.get(uuid.UUID(str(run_id)))
            if run is None or (owner_id is not None and run.created_by != owner_id):
                return None
            return SimpleNamespace(**vars(run))

    def get_active_run(self, conversation_id):
        with self._lock:
            for run in self.runs.values():
                if run.conversation_id == conversation_id and run.status in ("queued", "running"):
                    return SimpleNamespace(**vars(run))
        return None

    def get_context(self, run_id):
        with self._lock:
            run = self.runs.get(uuid.UUID(str(run_id)))
            if run is None:
                return None
            message = self.messages.get(run.assistant_message_id) if run.assistant_message_id else None
            return SimpleNamespace(run=SimpleNamespace(**vars(run)), conversation=None, user_message=None, assistant_message=message)

    def list_running(self):
        with self._lock:
            return [SimpleNamespace(**vars(run)) for run in self.runs.values() if run.status == "running"]

    def list_queued(self):
        with self._lock:
            return [SimpleNamespace(**vars(run)) for run in self.runs.values() if run.status == "queued"]

    def claim(self, run_id, worker_id, now=None):
        with self._lock:
            run = self.runs.get(uuid.UUID(str(run_id)))
            if run is None or run.status != "queued":
                return False
            run.status = "running"
            run.started_at = now or _now()
            run.heartbeat_at = run.started_at
            run.worker_id = worker_id
            return True

    def heartbeat(self, run_id, worker_id, now=None):
        with self._lock:
            run = self.runs.get(uuid.UUID(str(run_id)))
            if run is not None and run.status == "running" and run.worker_id == worker_id:
                run.heartbeat_at = now or _now()

    def request_cancel(self, run_id, now=None):
        with self._lock:
            run = self.runs.get(uuid.UUID(str(run_id)))
            if run is not None and run.status in ("queued", "running") and run.cancel_requested_at is None:
                run.cancel_requested_at = now or _now()

    def cancel_queued(self, run_id, now=None):
        with self._lock:
            run = self.runs.get(uuid.UUID(str(run_id)))
            if run is None or run.status != "queued":
                return False
            run.status = "cancelled"
            run.error_code = "agent_cancelled"
            run.error_detail = "cancelled before start"
            run.cancel_requested_at = run.cancel_requested_at or now or _now()
            run.finished_at = now or _now()
            return True

    def finish(
        self,
        run_id,
        *,
        status,
        error_code=None,
        error_detail=None,
        partial_content=None,
        partial_metadata=None,
        usage=None,
        finished_at=None,
    ):
        with self._lock:
            run = self.runs.get(uuid.UUID(str(run_id)))
            if run is None or run.status not in ("queued", "running"):
                return False
            run.status = status
            run.error_code = error_code
            run.error_detail = error_detail
            if partial_content is not None:
                run.partial_content = partial_content
            if partial_metadata is not None:
                run.partial_metadata = copy.deepcopy(partial_metadata)
            if usage is not None:
                run.input_tokens = usage.get("input_tokens")
                run.output_tokens = usage.get("output_tokens")
                run.total_tokens = usage.get("total_tokens")
                run.model_calls = usage.get("model_calls", 0)
                run.unknown_usage_calls = usage.get("unknown_calls", 0)
                run.usage_breakdown = copy.deepcopy(usage)
            run.finished_at = finished_at or _now()
            return True

    def update_usage(self, run_id, usage, now=None):
        with self._lock:
            run = self.runs.get(uuid.UUID(str(run_id)))
            if run is None:
                return False
            run.input_tokens = usage.get("input_tokens")
            run.output_tokens = usage.get("output_tokens")
            run.total_tokens = usage.get("total_tokens")
            run.model_calls = usage.get("model_calls", 0)
            run.unknown_usage_calls = usage.get("unknown_calls", 0)
            run.usage_breakdown = copy.deepcopy(usage)
            return True

    def append_event(self, run_id, seq, event_type, payload, *, partial_content=None, partial_metadata=None):
        with self._lock:
            run = self.runs.get(uuid.UUID(str(run_id)))
            if run is None or run.last_seq >= seq:
                return False
            run.last_seq = seq
            if partial_content is not None:
                run.partial_content = partial_content
            if partial_metadata is not None:
                run.partial_metadata = copy.deepcopy(partial_metadata)
            self.events[run.id].append(SimpleNamespace(seq=seq, event_type=event_type, payload=copy.deepcopy(payload)))
            return True

    def oldest_seq(self, run_id):
        with self._lock:
            events = self.events.get(uuid.UUID(str(run_id))) or []
            return min((event.seq for event in events), default=None)

    def list_events(self, run_id, after_seq=0):
        with self._lock:
            return [event for event in self.events.get(uuid.UUID(str(run_id)), []) if event.seq > after_seq]

    def has_event(self, run_id, event_types):
        with self._lock:
            return any(event.event_type in event_types for event in self.events.get(uuid.UUID(str(run_id)), []))

    def get_message(self, message_id):
        if message_id is None:
            return None
        with self._lock:
            return self.messages.get(uuid.UUID(str(message_id)))

    def insert_assistant_message(self, *, conversation_id, owner_id, message_id, content, status, metadata):
        with self._lock:
            existing = self.messages.get(message_id)
            if existing is not None:
                return existing
            message = _message(message_id, conversation_id, content, status, metadata)
            self.messages[message_id] = message
            return message

    def upsert_span(self, run_id, *, span_id, span_type, name, **fields):
        with self._lock:
            run_uuid = uuid.UUID(str(run_id))
            if run_uuid not in self.runs:
                return False
            key = (run_uuid, span_id)
            existing = self.spans.get(key)
            if existing is not None and existing["status"] in (
                "completed",
                "failed",
                "aborted",
                "interrupted",
            ):
                return True
            span = dict(fields)
            span.update({"run_id": run_uuid, "span_id": span_id, "span_type": span_type, "name": name})
            self.spans[key] = span
            return True

    def list_spans(self, run_id):
        with self._lock:
            run_uuid = uuid.UUID(str(run_id))
            return [span for (span_run, _), span in self.spans.items() if span_run == run_uuid]

    def settle_open_spans(self, run_id, status, *, ended_at=None):
        with self._lock:
            run_uuid = uuid.UUID(str(run_id))
            settled = 0
            for (span_run, _), span in self.spans.items():
                if span_run == run_uuid and span.get("status") == "running":
                    span["status"] = status
                    span["ended_at"] = ended_at or _now()
                    settled += 1
            return settled

    def prune_events(self, **_kwargs):
        return 0

    def prune_spans(self, **_kwargs):
        return 0

    def prune_runs(self, **_kwargs):
        return 0


def _config():
    return RunConfig(
        background=True,
        concurrency=2,
        max_seconds=5.0,
        heartbeat_seconds=0.05,
        lease_seconds=1.0,
        event_retention_days=7,
        run_retention_days=30,
        delta_flush_ms=10,
        delta_flush_bytes=4096,
        worker_id="test-worker",
    )


def _happy_executor(*_args, **_kwargs):
    async def run(_payload, _request_id, *, cancel_event=None, max_seconds=None):
        yield AgentDelta("hel")
        yield AgentToolCall(
            tool_call_id="t1",
            name="search",
            iteration=1,
            arguments={"q": "x"},
            raw_arguments='{"q": "x"}',
        )
        yield AgentToolResult(tool_call_id="t1", name="search", content="found", iteration=1)
        yield AgentDelta("lo")
        yield AgentCompleted(metadata={"finish_reason": "stop"})

    return run


async def _collect_until_done(subscription, timeout=2.0):
    items = []
    while True:
        event_type, payload = await asyncio.wait_for(subscription.queue.get(), timeout)
        items.append((event_type, payload))
        if event_type == "done":
            return items


@pytest.mark.asyncio
async def test_manager_persists_ordered_events_and_completes():
    repo = FakeRepo()
    run = repo.add_run(_run())
    manager = AgentRunManager(_config(), repo=repo, executor=_happy_executor())

    manager.submit(run.id)
    subscription = await manager.open_subscription(run.id, 0)
    assert subscription is not None
    await manager.wait(run.id, timeout=2)
    items = await _collect_until_done(subscription)
    await manager.shutdown()

    stored = repo.runs[run.id]
    assert stored.status == "completed"
    assert stored.partial_content == "hello"
    assert stored.partial_metadata["finish_reason"] == "stop"

    types = [event_type for event_type, _ in items]
    assert types[-1] == "done"
    assert "tool.call" in types and "tool.result" in types and "message.completed" in types

    seqs = [payload["seq"] for event_type, payload in items if event_type not in {"done"}]
    assert seqs == sorted(seqs)
    assert len(seqs) == len(set(seqs))
    stored_events = repo.events[run.id]
    assert [event.seq for event in stored_events] == list(range(1, len(stored_events) + 1))
    assert stored_events[-1].event_type == "message.completed"

    message = repo.messages[stored.assistant_message_id]
    assert message.content == "hello"
    assert message.status == "completed"
    assert message.meta["tool_trace"][0]["status"] == "completed"
    # The dashboard copies this straight from the persisted reply.
    assert message.meta["run_id"] == str(run.id)
    assert stored.partial_metadata["run_id"] == str(run.id)
    assert items[-1][1]["run_id"] == str(run.id)


@pytest.mark.asyncio
async def test_manager_persists_token_usage_from_per_call_events():
    repo = FakeRepo()
    run = repo.add_run(_run())

    async def executor(_payload, _request_id, *, cancel_event=None, max_seconds=None):
        yield AgentUsageUpdate(source="agent_loop", phase="request", iteration=1)
        yield AgentUsageUpdate(
            source="agent_loop",
            phase="response",
            iteration=1,
            usage={"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
        )
        yield AgentUsageUpdate(
            source="compression",
            phase="response",
            iteration=1,
            usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            calls=1,
        )
        yield AgentDelta("hi")
        yield AgentCompleted(metadata={"finish_reason": "stop"})

    manager = AgentRunManager(_config(), repo=repo, executor=executor)
    manager.submit(run.id)
    subscription = await manager.open_subscription(run.id, 0)
    assert subscription is not None
    await manager.wait(run.id, timeout=2)
    items = await _collect_until_done(subscription)
    await manager.shutdown()

    stored = repo.runs[run.id]
    assert stored.status == "completed"
    assert stored.input_tokens == 15
    assert stored.output_tokens == 5
    assert stored.total_tokens == 20
    assert stored.model_calls == 2
    assert stored.unknown_usage_calls == 0
    assert stored.usage_breakdown["coverage"] == "complete"
    assert stored.usage_breakdown["sources"]["agent_loop"]["total_tokens"] == 13
    assert stored.usage_breakdown["sources"]["compression"]["total_tokens"] == 7

    done = items[-1][1]
    assert done["usage"]["coverage"] == "complete"
    assert done["usage"]["total_tokens"] == 20
    message = repo.messages[stored.assistant_message_id]
    assert message.meta["usage"]["total_tokens"] == 20


@pytest.mark.asyncio
async def test_manager_marks_coverage_unknown_without_usage():
    repo = FakeRepo()
    run = repo.add_run(_run())

    async def executor(_payload, _request_id, *, cancel_event=None, max_seconds=None):
        yield AgentUsageUpdate(source="agent_loop", phase="request", iteration=1)
        yield AgentUsageUpdate(source="agent_loop", phase="response", iteration=1, usage={})
        yield AgentDelta("hi")
        yield AgentCompleted(metadata={"finish_reason": "stop"})

    manager = AgentRunManager(_config(), repo=repo, executor=executor)
    manager.submit(run.id)
    await manager.wait(run.id, timeout=2)
    await manager.shutdown()

    stored = repo.runs[run.id]
    assert stored.total_tokens is None
    assert stored.model_calls == 1
    assert stored.unknown_usage_calls == 1
    assert stored.usage_breakdown["coverage"] == "unknown"


@pytest.mark.asyncio
async def test_manager_uses_transport_aggregate_when_no_per_call_events():
    repo = FakeRepo()
    run = repo.add_run(_run())

    async def executor(_payload, _request_id, *, cancel_event=None, max_seconds=None):
        yield AgentDelta("hi")
        yield AgentCompleted(
            metadata={
                "finish_reason": "stop",
                "usage": {"prompt_tokens": 30, "completion_tokens": 10, "total_tokens": 40},
            }
        )

    manager = AgentRunManager(_config(), repo=repo, executor=executor)
    manager.submit(run.id)
    await manager.wait(run.id, timeout=2)
    await manager.shutdown()

    stored = repo.runs[run.id]
    assert stored.total_tokens == 40
    assert stored.usage_breakdown["coverage"] == "aggregate"


@pytest.mark.asyncio
async def test_manager_saves_research_memory_after_completion(monkeypatch):
    saved = []

    def fake_remember(payload, content, entries):
        saved.append((payload, content, entries))
        return "mem-1"

    monkeypatch.setattr(agent_runner, "remember_research", fake_remember)

    async def executor(_payload, _request_id, *, cancel_event=None, max_seconds=None):
        yield AgentToolCall(
            tool_call_id="t1",
            name="WebFetch",
            iteration=1,
            arguments={"url": "https://a.example"},
        )
        yield AgentToolResult(
            tool_call_id="t1",
            name="WebFetch",
            content='{"url": "https://a.example", "content": "body"}',
            iteration=1,
        )
        yield AgentDelta("answer")
        yield AgentCompleted(metadata={"finish_reason": "stop"})

    repo = FakeRepo()
    run = repo.add_run(
        _run(
            request_payload={
                "message": "which model?",
                "user_id": "user-1",
                "run_id": "client-run-1",
            }
        )
    )
    manager = AgentRunManager(_config(), repo=repo, executor=executor)

    manager.submit(run.id)
    await manager.wait(run.id, timeout=2)
    await manager.shutdown()

    assert repo.runs[run.id].status == "completed"
    assert len(saved) == 1
    payload, content, entries = saved[0]
    assert payload["user_id"] == "user-1"
    assert payload["message"] == "which model?"
    assert content == "answer"
    assert entries[0]["name"] == "WebFetch"
    assert entries[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_manager_cancel_settles_partial_message():
    repo = FakeRepo()
    run = repo.add_run(_run())

    async def executor(_payload, _request_id, *, cancel_event=None, max_seconds=None):
        yield AgentDelta("partial")
        while cancel_event is not None and not cancel_event.is_set():
            await asyncio.sleep(0.01)
        raise AgentLoopError("agent_cancelled", "Agent run was cancelled.")

    manager = AgentRunManager(_config(), repo=repo, executor=executor)
    manager.submit(run.id)
    for _ in range(100):
        if repo.events[run.id]:
            break
        await asyncio.sleep(0.01)
    assert repo.events[run.id], "worker never started"

    await manager.cancel(run.id, run.created_by)
    await manager.wait(run.id, timeout=2)
    await manager.shutdown()

    stored = repo.runs[run.id]
    assert stored.status == "cancelled"
    assert stored.error_code == "agent_cancelled"
    message = repo.messages[stored.assistant_message_id]
    assert message.status == "cancelled"
    assert message.content == "partial"
    assert message.meta["run_id"] == str(run.id)
    assert any(event.event_type == "error" and event.payload["code"] == "agent_cancelled" for event in repo.events[run.id])
    assert repo.events[run.id][-1].event_type == "error"


@pytest.mark.asyncio
async def test_startup_marks_running_run_interrupted_and_keeps_partial_text():
    repo = FakeRepo()
    run = repo.add_run(_run(status="running", partial_content="half an answer", worker_id="dead-worker"))
    manager = AgentRunManager(_config(), repo=repo, executor=_happy_executor())

    await manager.startup()
    await manager.shutdown()

    stored = repo.runs[run.id]
    assert stored.status == "interrupted"
    assert stored.error_code == "agent_interrupted"
    message = repo.messages[stored.assistant_message_id]
    assert message.status == "failed"
    assert message.content == "half an answer"
    assert message.meta["run_status"] == "interrupted"
    assert message.meta["error_code"] == "agent_interrupted"
    assert [event.event_type for event in repo.events[run.id]].count("error") == 1


@pytest.mark.asyncio
async def test_startup_resubmits_queued_run():
    repo = FakeRepo()
    run = repo.add_run(_run(status="queued"))
    manager = AgentRunManager(_config(), repo=repo, executor=_happy_executor())

    await manager.startup()
    await manager.wait(run.id, timeout=2)
    await manager.shutdown()

    assert repo.runs[run.id].status == "completed"


# ---------------------------------------------------------------------------
# HTTP surface


def _fake_user() -> User:
    return TEST_USER


def _conversation_out(conversation_id):
    now = _now()
    return ConversationOut(
        id=str(conversation_id),
        title="Hello",
        agent_id=None,
        metadata={},
        created_at=now,
        updated_at=now,
    )


def _background_turn(repo, run):
    conversation = _conversation_out(run.conversation_id)
    user_message = ConversationMessageOut(
        id=str(run.user_message_id),
        conversation_id=str(run.conversation_id),
        role="user",
        content="hello",
        status="completed",
        metadata={},
        created_at=_now(),
    )
    return agent_router.BackgroundTurn(
        conversation=conversation,
        user_message=user_message,
        run_id=run.id,
        assistant_id=run.assistant_message_id,
        conversation_uuid=run.conversation_id,
        payload=run.request_payload,
    )


@pytest.fixture
def background_client(monkeypatch):
    monkeypatch.setenv("AGENT_RUN_BACKGROUND", "true")
    monkeypatch.setattr(agent_router, "is_configured", lambda: True)
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.include_router(agent_router.router)
    app.dependency_overrides[require_auth] = _fake_user
    return TestClient(app, raise_server_exceptions=False)


def test_post_rejects_a_second_active_run_with_409(background_client, monkeypatch):
    conversation_id = uuid.uuid4()
    existing_run_id = uuid.uuid4()

    def _conflict(*_args, **_kwargs):
        raise agent_router.ActiveRunConflict(existing_run_id, conversation_id)

    monkeypatch.setattr(agent_router, "_create_background_turn", _conflict)
    response = background_client.post("/agent", json={"message": "hello"})

    assert response.status_code == 409
    payload = response.json()
    assert payload["code"] == "agent_run_active"
    assert payload["run_id"] == str(existing_run_id)


def test_background_stream_replays_persisted_events_with_cursor_ids(background_client, monkeypatch):
    repo = FakeRepo()
    run = repo.add_run(_run(status="queued"))
    manager = AgentRunManager(_config(), repo=repo, executor=_happy_executor())
    background_client.app.state.agent_runs = manager
    monkeypatch.setattr(agent_router, "_create_background_turn", lambda *_args, **_kwargs: _background_turn(repo, run))

    response = background_client.post("/agent", json={"message": "hello", "stream": True})

    assert response.status_code == 200
    events = [line[len("event: ") :] for line in response.text.splitlines() if line.startswith("event: ")]
    assert events[0] == "meta"
    assert events[-1] == "done"
    ids = [line[len("id: ") :] for line in response.text.splitlines() if line.startswith("id: ")]
    assert ids == [str(index) for index in range(1, len(ids) + 1)]
    assert repo.runs[run.id].status == "completed"


def test_events_endpoint_returns_410_for_an_expired_cursor(background_client):
    repo = FakeRepo()
    run = repo.add_run(_run(status="completed", last_seq=5, started_at=_now(), finished_at=_now()))
    repo.add_event(run.id, 4, "message.delta", {"run_id": str(run.id), "conversation_id": str(run.conversation_id), "seq": 4, "delta": "x"})
    repo.add_event(run.id, 5, "message.completed", {"run_id": str(run.id), "conversation_id": str(run.conversation_id), "seq": 5, "message_id": str(run.assistant_message_id)})
    background_client.app.state.agent_runs = AgentRunManager(_config(), repo=repo)

    response = background_client.get(f"/agent/runs/{run.id}/events?after_seq=0")

    assert response.status_code == 410
    payload = response.json()
    assert payload["code"] == "agent_events_expired"
    assert payload["oldest_seq"] == 4
    assert payload["last_seq"] == 5


def test_events_endpoint_rejects_a_negative_cursor(background_client):
    repo = FakeRepo()
    run = repo.add_run(_run(status="completed", last_seq=5, started_at=_now(), finished_at=_now()))
    background_client.app.state.agent_runs = AgentRunManager(_config(), repo=repo)

    response = background_client.get(f"/agent/runs/{run.id}/events?after_seq=-1")

    assert response.status_code == 400
    assert response.json()["code"] == "agent_invalid_cursor"


def test_events_endpoint_clamps_a_cursor_beyond_last_seq(background_client):
    repo = FakeRepo()
    run = repo.add_run(_run(status="completed", last_seq=5, started_at=_now(), finished_at=_now()))
    repo.add_event(run.id, 4, "message.delta", {"run_id": str(run.id), "conversation_id": str(run.conversation_id), "seq": 4, "delta": "x"})
    repo.add_event(run.id, 5, "message.completed", {"run_id": str(run.id), "conversation_id": str(run.conversation_id), "seq": 5, "message_id": str(run.assistant_message_id)})
    background_client.app.state.agent_runs = AgentRunManager(_config(), repo=repo)

    response = background_client.get(f"/agent/runs/{run.id}/events?after_seq=99")

    assert response.status_code == 200
    events = [line[len("event: ") :] for line in response.text.splitlines() if line.startswith("event: ")]
    assert events == ["meta", "done"]
    meta = json.loads(response.text.split("event: meta\ndata: ", 1)[1].split("\n\n", 1)[0])
    assert meta["after_seq"] == 5


def test_events_endpoint_prefers_after_seq_over_last_event_id(background_client):
    repo = FakeRepo()
    run = repo.add_run(_run(status="completed", last_seq=5, started_at=_now(), finished_at=_now()))
    repo.add_event(run.id, 4, "message.delta", {"run_id": str(run.id), "conversation_id": str(run.conversation_id), "seq": 4, "delta": "x"})
    repo.add_event(run.id, 5, "message.completed", {"run_id": str(run.id), "conversation_id": str(run.conversation_id), "seq": 5, "message_id": str(run.assistant_message_id)})
    background_client.app.state.agent_runs = AgentRunManager(_config(), repo=repo)

    response = background_client.get(
        f"/agent/runs/{run.id}/events?after_seq=3",
        headers={"Last-Event-ID": "4"},
    )

    assert response.status_code == 200
    ids = [line[len("id: ") :] for line in response.text.splitlines() if line.startswith("id: ")]
    assert ids == ["4", "5"]


def test_events_endpoint_replays_after_cursor_then_done(background_client):
    repo = FakeRepo()
    run = repo.add_run(_run(status="completed", last_seq=5, started_at=_now(), finished_at=_now()))
    repo.add_event(run.id, 4, "message.delta", {"run_id": str(run.id), "conversation_id": str(run.conversation_id), "seq": 4, "delta": "x"})
    repo.add_event(run.id, 5, "message.completed", {"run_id": str(run.id), "conversation_id": str(run.conversation_id), "seq": 5, "message_id": str(run.assistant_message_id)})
    background_client.app.state.agent_runs = AgentRunManager(_config(), repo=repo)

    response = background_client.get(f"/agent/runs/{run.id}/events?after_seq=3")

    assert response.status_code == 200
    events = [line[len("event: ") :] for line in response.text.splitlines() if line.startswith("event: ")]
    assert events == ["meta", "message.delta", "message.completed", "done"]
    ids = [line[len("id: ") :] for line in response.text.splitlines() if line.startswith("id: ")]
    assert ids == ["4", "5"]


def test_events_endpoint_accepts_last_event_id_header(background_client):
    repo = FakeRepo()
    run = repo.add_run(_run(status="completed", last_seq=5, started_at=_now(), finished_at=_now()))
    repo.add_event(run.id, 4, "message.delta", {"run_id": str(run.id), "conversation_id": str(run.conversation_id), "seq": 4, "delta": "x"})
    repo.add_event(run.id, 5, "message.completed", {"run_id": str(run.id), "conversation_id": str(run.conversation_id), "seq": 5, "message_id": str(run.assistant_message_id)})
    background_client.app.state.agent_runs = AgentRunManager(_config(), repo=repo)

    response = background_client.get(f"/agent/runs/{run.id}/events", headers={"Last-Event-ID": "4"})

    assert response.status_code == 200
    ids = [line[len("id: ") :] for line in response.text.splitlines() if line.startswith("id: ")]
    assert ids == ["5"]


def test_cancel_endpoint_is_idempotent_for_a_terminal_run(background_client):
    repo = FakeRepo()
    run = repo.add_run(_run(status="completed", error_code=None, started_at=_now(), finished_at=_now()))
    background_client.app.state.agent_runs = AgentRunManager(_config(), repo=repo)

    response = background_client.post(f"/agent/runs/{run.id}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "completed"


def test_unknown_run_is_404(background_client):
    background_client.app.state.agent_runs = AgentRunManager(_config(), repo=FakeRepo())

    response = background_client.get(f"/agent/runs/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["code"] == "agent_run_not_found"


def test_malformed_run_id_is_404(background_client):
    background_client.app.state.agent_runs = AgentRunManager(_config(), repo=FakeRepo())

    response = background_client.get("/agent/runs/not-a-uuid")

    assert response.status_code == 404
    assert response.json()["code"] == "agent_run_not_found"


def test_run_snapshot_exposes_status_and_cursor(background_client):
    repo = FakeRepo()
    run = repo.add_run(_run(status="running", last_seq=3, started_at=_now()))
    background_client.app.state.agent_runs = AgentRunManager(_config(), repo=repo)

    response = background_client.get(f"/agent/runs/{run.id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == str(run.id)
    assert payload["status"] == "running"
    assert payload["last_seq"] == 3
    assert payload["assistant_message_id"] == str(run.assistant_message_id)
