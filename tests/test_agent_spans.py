"""Unit tests for the neutral span recorder (no Postgres, no deepeval)."""

import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1] / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

agent_events = pytest.importorskip("app.agent.events")
agent_spans = pytest.importorskip("app.agent.spans")


class RecordingRepo:
    def __init__(self):
        self.spans = {}
        self.settled = []

    def upsert_span(self, run_id, **fields):
        self.spans[fields["span_id"]] = dict(fields)
        return True

    def settle_open_spans(self, run_id, status, *, ended_at=None):
        count = 0
        for span in self.spans.values():
            if span.get("status") == "running":
                span["status"] = status
                count += 1
        self.settled.append((status, count))
        return count


def _run():
    return SimpleNamespace(
        id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        client_run_id="client-1",
        started_at=None,
        request_payload={
            "message": "Find the answer",
            "conversation_id": str(uuid.uuid4()),
            "client_run_id": "client-1",
            "metadata": {"eval_case_id": "case-1"},
        },
    )


def _recorder(repo, run, **options):
    recorder = agent_spans.SpanRecorder(
        repo,
        run.id,
        options=agent_spans.SpanOptions(**options),
    )
    recorder.start(run)
    return recorder


def test_model_request_and_response_form_one_llm_span():
    repo = RecordingRepo()
    run = _run()
    recorder = _recorder(repo, run)

    recorder.handle(agent_events.AgentContextCompressed(iteration=1, stats={"before_tokens": 100}))
    recorder.handle(
        agent_events.AgentModelRequest(
            iteration=1,
            message_count=2,
            roles=["system", "user"],
            last_message_hash="abc",
            seq=10,
        )
    )
    recorder.handle(
        agent_events.AgentModelResponse(
            iteration=1,
            content="the answer",
            tool_calls=[],
            finish_reason="stop",
            usage={"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
            seq=11,
        )
    )

    span = repo.spans["llm:10"]
    assert span["parent_span_id"] == "agent"
    assert repo.spans["agent"]["parent_span_id"] is None
    assert span["status"] == "completed"
    assert span["input"]["summary"]["message_count"] == 2
    assert span["output"]["content"] == "the answer"
    assert span["attributes"]["context_compressed"] == {"before_tokens": 100}
    assert span["attributes"]["usage"] == {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16}
    assert span["output"]["usage"]["total_tokens"] == 16


def test_repeated_iterations_do_not_collide():
    repo = RecordingRepo()
    run = _run()
    recorder = _recorder(repo, run)

    for request_seq, response_seq in ((10, 11), (20, 21)):
        recorder.handle(agent_events.AgentModelRequest(iteration=1, seq=request_seq))
        recorder.handle(agent_events.AgentModelResponse(iteration=1, content="x", seq=response_seq))

    assert {"llm:10", "llm:20"} <= set(repo.spans)
    assert repo.spans["llm:10"]["status"] == "completed"
    assert repo.spans["llm:20"]["status"] == "completed"


def test_tool_result_without_call_still_creates_a_span():
    repo = RecordingRepo()
    run = _run()
    recorder = _recorder(repo, run)

    recorder.handle(
        agent_events.AgentToolResult(
            tool_call_id="t1",
            name="search",
            content="result",
            status="aborted",
        )
    )

    span = repo.spans["tool:t1"]
    assert span["span_type"] == "tool"
    assert span["status"] == "aborted"


def test_finalize_settles_open_spans_and_root():
    repo = RecordingRepo()
    run = _run()
    recorder = _recorder(repo, run)
    recorder.handle(agent_events.AgentModelRequest(iteration=1, seq=10))

    recorder.finalize(status="failed", content="", metadata={"usage": {"total_tokens": 5}}, error_detail="boom")

    assert repo.spans["agent"]["status"] == "failed"
    assert repo.spans["agent"]["output"]["content"] == ""
    assert repo.spans["agent"]["attributes"]["usage"]["total_tokens"] == 5
    assert repo.spans["llm:10"]["status"] == "failed"
    assert repo.settled[-1][0] == "failed"


def test_capture_can_be_disabled():
    repo = RecordingRepo()
    run = _run()
    recorder = _recorder(repo, run, capture=False)
    recorder.handle(agent_events.AgentModelRequest(iteration=1, seq=10))

    assert repo.spans == {}


def test_sampling_is_deterministic_for_a_run_id():
    run_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    assert agent_spans._sampled(run_id, 1.0) is True
    assert agent_spans._sampled(run_id, 0.0) is False
    assert agent_spans._sampled(run_id, 0.5) == agent_spans._sampled(run_id, 0.5)


def test_tool_output_redacts_secrets_when_content_is_captured():
    repo = RecordingRepo()
    run = _run()
    recorder = _recorder(repo, run, tool_content=True)

    recorder.handle(agent_events.AgentToolCall(tool_call_id="t1", name="shell", arguments={"key": "sk-abcdef123456"}))
    recorder.handle(agent_events.AgentToolResult(tool_call_id="t1", name="shell", content="Bearer abcdefghijklmnop"))

    span = repo.spans["tool:t1"]
    assert "sk-abcdef123456" not in str(span["input"])
    assert "abcdefghijklmnop" not in str(span["output"])
