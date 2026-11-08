from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

_TESTS_DIR = os.path.dirname(__file__)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
_SERVER_DIR = os.path.join(os.path.dirname(_TESTS_DIR), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import app.agent.interactions as interactions  # noqa: E402
import app.routers.agent as agent_router  # noqa: E402
from app.agent.events import (  # noqa: E402
    AgentCompleted,
    AgentDelta,
    AgentToolCall,
    AgentToolResult,
)
from app.agent.interactions import PendingQuestion, RunInteraction  # noqa: E402
from app.agent.runner import AgentRunManager  # noqa: E402
from app.agent.schemas import AgentAnswerRequest  # noqa: E402
from app.agent.user_input import build_user_input_tool  # noqa: E402
from app.core.auth import require_auth  # noqa: E402
from app.core.rate_limit import limiter  # noqa: E402
from test_agent_runs import FakeRepo, TEST_USER, _config, _run  # noqa: E402


class FakeWriter:
    def __init__(self) -> None:
        self.assistant_id = uuid.uuid4()
        self.events: list[tuple[str, dict]] = []
        self.available = True

    async def write(self, event_type, payload, **_kwargs):
        if not self.available:
            return False
        self.events.append((event_type, dict(payload)))
        return True


def _start_ask(interaction: RunInteraction, **kwargs) -> tuple[threading.Thread, dict]:
    box: dict = {}

    def run():
        box["result"] = interaction.ask(**kwargs)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, box


async def _wait_thread(thread: threading.Thread, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while thread.is_alive() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert not thread.is_alive(), "ask_user worker stayed blocked"


async def _wait_for_question(writer: FakeWriter, timeout: float = 2.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for event_type, payload in writer.events:
            if event_type == "user.question":
                return payload
        await asyncio.sleep(0.01)
    raise AssertionError("no user.question event was written")


@pytest.mark.asyncio
async def test_ask_waits_for_answer_and_returns_it():
    interaction = RunInteraction(asyncio.get_running_loop())
    writer = FakeWriter()
    interaction.attach_writer(writer)

    thread, box = _start_ask(
        interaction,
        question="Which environment?",
        options=["staging", "production"],
        allow_free_text=True,
        timeout_seconds=5,
    )
    payload = await _wait_for_question(writer)
    assert payload["question"] == "Which environment?"
    assert payload["options"] == ["staging", "production"]
    assert payload["message_id"] == str(writer.assistant_id)

    accepted, code = await interaction.answer(payload["question_id"], "staging")
    assert (accepted, code) == (True, "accepted")
    await _wait_thread(thread)
    assert box["result"]["answer"] == "staging"
    assert writer.events[-1][0] == "user.answer"
    assert writer.events[-1][1]["answer"] == "staging"


@pytest.mark.asyncio
async def test_ask_times_out(monkeypatch):
    monkeypatch.setattr(interactions, "MIN_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(interactions, "MAX_TIMEOUT_SECONDS", 5.0)
    interaction = RunInteraction(asyncio.get_running_loop())
    writer = FakeWriter()
    interaction.attach_writer(writer)

    thread, box = _start_ask(interaction, question="Still there?", timeout_seconds=0.2)
    await _wait_for_question(writer)
    await _wait_thread(thread)

    assert box["result"]["error"] == "timeout"
    assert [event[0] for event in writer.events] == ["user.question"]


@pytest.mark.asyncio
async def test_ask_stops_when_cancelled():
    interaction = RunInteraction(asyncio.get_running_loop())
    writer = FakeWriter()
    interaction.attach_writer(writer)
    cancel = threading.Event()

    thread, box = _start_ask(interaction, question="Need input", timeout_seconds=5, cancel_event=cancel)
    await _wait_for_question(writer)
    cancel.set()
    await _wait_thread(thread)

    assert box["result"]["error"] == "cancelled"


@pytest.mark.asyncio
async def test_close_wakes_waiting_question():
    interaction = RunInteraction(asyncio.get_running_loop())
    writer = FakeWriter()
    interaction.attach_writer(writer)

    thread, box = _start_ask(interaction, question="Need input", timeout_seconds=5)
    await _wait_for_question(writer)
    interaction.close("run finished")
    await _wait_thread(thread)

    assert box["result"]["error"] == "run finished"


@pytest.mark.asyncio
async def test_only_one_question_can_wait_at_a_time():
    interaction = RunInteraction(asyncio.get_running_loop())
    writer = FakeWriter()
    interaction.attach_writer(writer)

    thread, box = _start_ask(interaction, question="First?", timeout_seconds=5)
    payload = await _wait_for_question(writer)
    second = interaction.ask(question="Second?", timeout_seconds=5)
    assert "already waiting" in second["error"]

    await interaction.answer(payload["question_id"], "ok")
    await _wait_thread(thread)


@pytest.mark.asyncio
async def test_unknown_and_answered_questions_are_rejected():
    interaction = RunInteraction(asyncio.get_running_loop())
    writer = FakeWriter()
    interaction.attach_writer(writer)

    assert await interaction.answer("q_missing", "x") == (False, "question_not_pending")

    interaction._pending["q_test"] = PendingQuestion(question_id="q_test", payload={})
    interaction._pending["q_test"].event.set()
    ok, code = await interaction.answer("q_test", "x")
    assert ok is False
    assert code == "question_already_answered"


@pytest.mark.asyncio
async def test_manager_wires_question_into_the_running_executor():
    repo = FakeRepo()
    run = repo.add_run(_run(status="queued"))
    asked: dict = {}

    async def executor(
        _payload,
        _request_id,
        *,
        interaction=None,
        cancel_event=None,
        max_seconds=None,
    ):
        yield AgentToolCall(tool_call_id="t1", name="ask_user", iteration=1)
        result = await asyncio.to_thread(
            interaction.ask,
            question="Which environment?",
            timeout_seconds=5,
        )
        asked.update(result)
        yield AgentToolResult(
            tool_call_id="t1",
            name="ask_user",
            content=json.dumps(result),
            iteration=1,
        )
        yield AgentDelta("done")
        yield AgentCompleted(metadata={"finish_reason": "stop"})

    manager = AgentRunManager(_config(), repo=repo, executor=executor)
    manager.submit(run.id)
    subscription = await manager.open_subscription(run.id, 0)
    assert subscription is not None

    question = None
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        event_type, payload = await asyncio.wait_for(
            subscription.queue.get(), timeout=1
        )
        if event_type == "user.question":
            question = payload
            break
    assert question is not None

    accepted, code = await manager.answer_question(run.id, question["question_id"], "staging")
    assert (accepted, code) == (True, "accepted")
    await manager.wait(run.id, timeout=3)
    await manager.shutdown()

    assert asked["answer"] == "staging"
    assert repo.runs[run.id].status == "completed"
    event_types = [event.event_type for event in repo.events[run.id]]
    assert "user.question" in event_types
    assert "user.answer" in event_types
    assert event_types.index("user.question") < event_types.index("user.answer")


def test_answer_request_rejects_blank_answers():
    with pytest.raises(ValueError):
        AgentAnswerRequest(question_id="q_1", answer="   ")


class RecordingSDK:
    def __init__(self) -> None:
        self.tools: list[dict] = []

    def tool(self, fn, **kwargs):
        record = {"fn": fn, **kwargs}
        self.tools.append(record)
        return record


def test_ask_user_tool_validates_its_arguments():
    sdk = RecordingSDK()

    class FakeInteraction:
        def ask(self, **kwargs):
            return {"answer": "ok", **kwargs}

    interaction = FakeInteraction()
    tool = build_user_input_tool(sdk, interaction, default_timeout_seconds=60)
    assert tool["name"] == "ask_user"
    assert tool["effect"] == "write"
    fn = tool["fn"]

    assert fn("").get("error")
    assert fn("q", options=[str(index) for index in range(9)]).get("error")
    assert fn("q", options=None, allow_free_text=False).get("error")
    assert fn("q", options=["a"], allow_free_text=False)["answer"] == "ok"


@pytest.fixture
def client():
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.include_router(agent_router.router)
    app.dependency_overrides[require_auth] = lambda: TEST_USER
    return TestClient(app, raise_server_exceptions=False)


def _manager_with_run(client, status="running"):
    repo = FakeRepo()
    run = repo.add_run(_run(status=status))
    manager = AgentRunManager(_config(), repo=repo)
    client.app.state.agent_runs = manager
    return manager, run


def test_answer_route_accepts(client, monkeypatch):
    manager, run = _manager_with_run(client)

    async def fake_answer(_run_id, _question_id, _answer):
        return True, "accepted"

    monkeypatch.setattr(manager, "answer_question", fake_answer)
    response = client.post(
        f"/agent/runs/{run.id}/answers",
        json={"question_id": "q_1", "answer": "staging"},
    )

    assert response.status_code == 200
    assert response.json()["accepted"] is True


def test_answer_route_rejects_terminal_runs(client):
    manager, run = _manager_with_run(client, status="completed")
    response = client.post(
        f"/agent/runs/{run.id}/answers",
        json={"question_id": "q_1", "answer": "staging"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "agent_run_not_running"


def test_answer_route_returns_404_for_unknown_question(client, monkeypatch):
    manager, run = _manager_with_run(client)

    async def fake_answer(_run_id, _question_id, _answer):
        return False, "question_not_pending"

    monkeypatch.setattr(manager, "answer_question", fake_answer)
    response = client.post(
        f"/agent/runs/{run.id}/answers",
        json={"question_id": "q_missing", "answer": "staging"},
    )

    assert response.status_code == 404
    assert response.json()["code"] == "agent_question_not_found"


def test_answer_route_returns_404_for_unknown_run(client):
    manager = AgentRunManager(_config(), repo=FakeRepo())
    client.app.state.agent_runs = manager
    response = client.post(
        f"/agent/runs/{uuid.uuid4()}/answers",
        json={"question_id": "q_1", "answer": "staging"},
    )

    assert response.status_code == 404
    assert response.json()["code"] == "agent_run_not_found"
