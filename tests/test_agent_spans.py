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
    assert "context_compressed" not in span["attributes"]
    assert span["attributes"]["usage"] == {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16}
    assert span["output"]["usage"]["total_tokens"] == 16


def test_context_compression_creates_its_own_child_of_the_agent_span():
    repo = RecordingRepo()
    run = _run()
    recorder = _recorder(repo, run)

    recorder.handle(
        agent_events.AgentContextCompressed(
            iteration=3,
            stats={"before_tokens": 9000, "after_tokens": 2000, "summarized": True},
        )
    )

    span = repo.spans["compression:3:1"]
    assert span["span_type"] == "compression"
    assert span["parent_span_id"] == "agent"
    assert span["name"] == "context.compress.3"
    assert span["status"] == "completed"
    assert span["duration_ms"] == 0
    assert span["attributes"] == {"before_tokens": 9000, "after_tokens": 2000, "summarized": True}


def test_twice_in_one_iteration_keeps_both_compression_spans():
    repo = RecordingRepo()
    run = _run()
    recorder = _recorder(repo, run)

    for after_tokens in (2000, 1500):
        recorder.handle(
            agent_events.AgentContextCompressed(
                iteration=150,
                stats={"before_tokens": 9000, "after_tokens": after_tokens},
            )
        )

    assert repo.spans["compression:150:1"]["attributes"]["after_tokens"] == 2000
    assert repo.spans["compression:150:2"]["attributes"]["after_tokens"] == 1500
    assert repo.spans["compression:150:2"]["parent_span_id"] == "agent"


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


# ---- memory spans -------------------------------------------------------------


def test_memory_recall_span_records_the_lookup_and_its_rejections():
    repo = RecordingRepo()
    run = _run()
    recorder = _recorder(repo, run)

    recorder.handle(
        agent_events.AgentMemoryRecall(
            question="a question",
            hits=[{"memory_id": "m1", "source": "vector", "score": 0.74, "combined_score": 0.37}],
            floor=0.5,
            ttl_days=30,
            candidates=4,
            skipped_low_score=1,
            skipped_stale=1,
            skipped_invalid=2,
            chars=120,
            duration_ms=42,
        )
    )

    span = repo.spans["memory:recall"]
    assert span["span_type"] == "memory"
    assert span["name"] == "memory.recall"
    assert span["parent_span_id"] == "agent"
    assert span["status"] == "completed"
    assert span["duration_ms"] == 42
    assert span["attributes"]["floor"] == 0.5
    assert span["attributes"]["candidates"] == 4
    assert span["attributes"]["skipped_low_score"] == 1
    assert span["attributes"]["skipped_stale"] == 1
    assert span["attributes"]["skipped_invalid"] == 2
    assert span["output"]["chars"] == 120
    assert span["output"]["hits"][0]["memory_id"] == "m1"


def test_memory_recall_span_marks_a_failed_lookup():
    repo = RecordingRepo()
    run = _run()
    recorder = _recorder(repo, run)

    recorder.handle(agent_events.AgentMemoryRecall(question="q", error="RuntimeError: offline", duration_ms=7))

    span = repo.spans["memory:recall"]
    assert span["status"] == "failed"
    assert span["error"] == "RuntimeError: offline"


def test_memory_save_span_records_skips_as_completed():
    repo = RecordingRepo()
    run = _run()
    recorder = _recorder(repo, run)

    recorder.handle(agent_events.AgentMemorySave(question="q", action="skipped", reason="no_sources", duration_ms=3))

    span = repo.spans["memory:save"]
    assert span["span_type"] == "memory"
    assert span["status"] == "completed"
    assert span["attributes"]["action"] == "skipped"
    assert span["attributes"]["reason"] == "no_sources"


def test_a_memory_save_written_after_finalize_stays_completed():
    repo = RecordingRepo()
    run = _run()
    recorder = _recorder(repo, run)

    recorder.finalize(status="completed", content="done", metadata={}, error_detail=None)
    recorder.handle(agent_events.AgentMemorySave(question="q", action="added", memory_id="m1", sources=2, chars=90))

    span = repo.spans["memory:save"]
    assert span["status"] == "completed"
    assert span["attributes"]["memory_id"] == "m1"
    assert repo.spans["agent"]["status"] == "completed"


def test_subagent_spans_nest_under_the_parent_tool_span():
    repo = RecordingRepo()
    run = _run()
    recorder = _recorder(repo, run)

    recorder.handle(agent_events.AgentToolCall(tool_call_id="call_1", name="explore", iteration=1))
    recorder.handle(
        agent_events.AgentSubagentStarted(
            subagent_id="sub_1",
            subagent_type="explore",
            label="read a.py",
            depth=1,
            goal_chars=6,
            parent_tool_call_id="call_1",
            model="child-model",
            tool_names=["Read", "Grep"],
        )
    )
    recorder.handle(
        agent_events.AgentSubagentEvent(
            subagent_id="sub_1",
            phase="model_request",
            iteration=1,
            message_count=2,
            tool_count=2,
        )
    )
    recorder.handle(
        agent_events.AgentSubagentEvent(
            subagent_id="sub_1",
            phase="tool_call",
            iteration=1,
            tool_call_id="child_1",
            name="Read",
            arguments={"file_path": "a.py"},
        )
    )
    recorder.handle(
        agent_events.AgentSubagentEvent(
            subagent_id="sub_1",
            phase="tool_result",
            iteration=1,
            tool_call_id="child_1",
            name="Read",
            content="hello",
            status="completed",
        )
    )
    recorder.handle(
        agent_events.AgentSubagentEvent(
            subagent_id="sub_1",
            phase="model_response",
            iteration=1,
            content_chars=40,
            finish_reason="stop",
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )
    )
    recorder.handle(
        agent_events.AgentSubagentFinished(
            subagent_id="sub_1",
            subagent_type="explore",
            status="completed",
            turns=2,
            usage={"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
            isolated_chars=5,
            result_chars=30,
            duration_ms=900,
            resumable=True,
        )
    )

    agent_span = repo.spans["subagent:sub_1"]
    assert agent_span["span_type"] == "agent"
    assert agent_span["parent_span_id"] == "tool:call_1"
    assert agent_span["status"] == "completed"
    assert agent_span["duration_ms"] == 900
    assert agent_span["attributes"]["model"] == "child-model"
    assert agent_span["attributes"]["tool_names"] == ["Read", "Grep"]
    assert agent_span["attributes"]["turns"] == 2
    assert agent_span["attributes"]["usage"]["total_tokens"] == 28
    assert agent_span["output"]["result_chars"] == 30

    llm = repo.spans["sublm:sub_1:1"]
    assert llm["parent_span_id"] == "subagent:sub_1"
    assert llm["status"] == "completed"
    assert llm["attributes"]["usage"]["total_tokens"] == 15

    tool = repo.spans["subtool:sub_1:child_1"]
    assert tool["parent_span_id"] == "subagent:sub_1"
    assert tool["status"] == "completed"
    assert tool["output"]["content"] == "hello"


def test_finalize_closes_an_unfinished_subagent_tree():
    repo = RecordingRepo()
    run = _run()
    recorder = _recorder(repo, run)
    recorder.handle(
        agent_events.AgentSubagentStarted(
            subagent_id="sub_1",
            subagent_type="explore",
            parent_tool_call_id="call_1",
        )
    )
    recorder.handle(
        agent_events.AgentSubagentEvent(
            subagent_id="sub_1",
            phase="tool_call",
            iteration=1,
            tool_call_id="child_1",
            name="Grep",
        )
    )

    recorder.finalize(status="cancelled", content="", metadata={}, error_detail="cancelled")

    span = repo.spans["subagent:sub_1"]
    assert span["status"] == "aborted"
    assert span["attributes"]["subagent_status"] == "interrupted"
    assert repo.spans["subtool:sub_1:child_1"]["status"] == "aborted"


def test_subagent_events_without_a_started_span_are_ignored():
    repo = RecordingRepo()
    run = _run()
    recorder = _recorder(repo, run)

    recorder.handle(
        agent_events.AgentSubagentEvent(
            subagent_id="sub_missing",
            phase="tool_call",
            tool_call_id="child_1",
            name="Read",
        )
    )

    assert not any(span_id.startswith("subtool:") for span_id in repo.spans)
