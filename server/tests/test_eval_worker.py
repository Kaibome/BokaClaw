"""Worker settlement rules, exercised with a fake repository and a fake evaluator.

The live stack has been validated end to end; these tests pin the branch logic
that is hard to reproduce on purpose: lease loss, shutdown mid-run, timeout,
and the exact terminal payload for each outcome.
"""

import threading
import time
import uuid
from types import SimpleNamespace

import pytest

from app.eval import worker as worker_module
from app.eval.config import EvalConfig
from app.eval.evaluator import EvalExecutionError, EvalOutcome
from app.eval.worker import EvalWorker

ROW_ID = uuid.uuid4()
RUN_ID = uuid.uuid4()


def _config(**overrides):
    values = dict(
        enabled=True,
        worker_poll_seconds=2.0,
        heartbeat_seconds=10,
        lease_seconds=60,
        max_attempts=3,
        max_run_seconds=30,
        retention_days=30,
        judge_enabled=False,
        judge_provider="deepseek",
        judge_model="deepseek-chat",
        judge_api_key="",
        judge_max_chars=20000,
        judge_language="zh",
    )
    values.update(overrides)
    return EvalConfig(**values)


def _row(**overrides):
    values = dict(
        id=ROW_ID,
        run_id=RUN_ID,
        golden_snapshot=None,
        golden_hash=None,
        use_llm_judge=False,
        attempt=1,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class _FakeRepo:
    def __init__(self, run=object(), spans=(object(),), owned=True, cancel_before_start=False):
        self.run = run
        self.spans = list(spans)
        self.owned = owned
        self.cancel_before_start = cancel_before_start
        self.finishes = []

    def get_run(self, run_id):
        return self.run

    def list_spans(self, run_id):
        return self.spans

    def heartbeat_state(self, eval_id, worker_id):
        return self.owned, self.cancel_before_start

    def finish(self, eval_id, worker_id, **kwargs):
        self.finishes.append((eval_id, worker_id, kwargs))
        return True


def _worker(repo, **config_overrides):
    worker = EvalWorker(config=_config(**config_overrides), repo=repo)
    worker.worker_id = "test-worker"
    return worker


def _execute(worker, row, cancel=False, lost=False):
    cancel_flag = threading.Event()
    lost_flag = threading.Event()
    if cancel:
        cancel_flag.set()
    if lost:
        lost_flag.set()
    worker._execute(row, cancel_flag, lost_flag)


def _outcome(**overrides):
    values = dict(
        overall_success=True,
        metrics=[{"name": "Agent Loop Detection", "score": 1.0, "success": True}],
        warnings=[],
        judge_model=None,
        token_cost=None,
        raw_result={"name": "test run"},
        duration_ms=42,
    )
    values.update(overrides)
    return EvalOutcome(**values)


def test_cancel_between_claim_and_start_skips_the_evaluator(monkeypatch):
    repo = _FakeRepo(cancel_before_start=True)
    monkeypatch.setattr(
        worker_module,
        "evaluate_run",
        lambda **_: pytest.fail("evaluator must not run after a cancel"),
    )

    _worker(repo)._process(_row())

    _, _, kwargs = repo.finishes[0]
    assert kwargs["status"] == "cancelled"
    assert kwargs["error_code"] == "eval_cancelled"


def test_lost_lease_before_start_does_not_evaluate(monkeypatch):
    repo = _FakeRepo(owned=False)
    monkeypatch.setattr(
        worker_module,
        "evaluate_run",
        lambda **_: pytest.fail("evaluator must not run without the lease"),
    )

    _worker(repo)._process(_row())

    assert repo.finishes == []


def test_completed_outcome_is_persisted(monkeypatch):
    repo = _FakeRepo()
    monkeypatch.setattr(worker_module, "evaluate_run", lambda **_: _outcome())

    _execute(_worker(repo), _row())

    assert len(repo.finishes) == 1
    _, worker_id, kwargs = repo.finishes[0]
    assert worker_id == "test-worker"
    assert kwargs["status"] == "completed"
    assert kwargs["overall_success"] is True
    assert kwargs["duration_ms"] == 42
    assert kwargs["metrics"][0]["name"] == "Agent Loop Detection"
    assert "error_code" not in kwargs


def test_cancel_flag_settles_cancelled_with_partial_result(monkeypatch):
    repo = _FakeRepo()
    monkeypatch.setattr(
        worker_module,
        "evaluate_run",
        lambda **_: _outcome(overall_success=False, judge_model="deepseek-chat", token_cost=0.002),
    )

    _execute(_worker(repo), _row(use_llm_judge=True), cancel=True)

    _, _, kwargs = repo.finishes[0]
    assert kwargs["status"] == "cancelled"
    assert kwargs["error_code"] == "eval_cancelled"
    assert kwargs["overall_success"] is False
    assert kwargs["judge_model"] == "deepseek-chat"
    assert kwargs["token_cost"] == 0.002


def test_lost_lease_leaves_the_row_to_recovery(monkeypatch):
    repo = _FakeRepo()
    monkeypatch.setattr(worker_module, "evaluate_run", lambda **_: _outcome())

    _execute(_worker(repo), _row(), lost=True)

    assert repo.finishes == []


def test_missing_run_fails_without_evaluating(monkeypatch):
    repo = _FakeRepo(run=None)
    monkeypatch.setattr(
        worker_module,
        "evaluate_run",
        lambda **_: pytest.fail("evaluator must not run without a target run"),
    )

    _execute(_worker(repo), _row())

    _, _, kwargs = repo.finishes[0]
    assert kwargs["status"] == "failed"
    assert kwargs["error_code"] == "eval_run_not_found"


def test_missing_spans_fails_without_evaluating(monkeypatch):
    repo = _FakeRepo(spans=())
    monkeypatch.setattr(
        worker_module,
        "evaluate_run",
        lambda **_: pytest.fail("evaluator must not run without spans"),
    )

    _execute(_worker(repo), _row())

    _, _, kwargs = repo.finishes[0]
    assert kwargs["status"] == "failed"
    assert kwargs["error_code"] == "eval_no_spans"


def test_execution_error_is_passed_through(monkeypatch):
    repo = _FakeRepo()

    def fail(**_):
        raise EvalExecutionError("eval_deepeval_unavailable", "deepeval missing")

    monkeypatch.setattr(worker_module, "evaluate_run", fail)

    _execute(_worker(repo), _row())

    _, _, kwargs = repo.finishes[0]
    assert kwargs["status"] == "failed"
    assert kwargs["error_code"] == "eval_deepeval_unavailable"
    assert kwargs["error_detail"] == "deepeval missing"


def test_unexpected_error_becomes_execution_failed(monkeypatch):
    repo = _FakeRepo()

    def boom(**_):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(worker_module, "evaluate_run", boom)

    _execute(_worker(repo), _row())

    _, _, kwargs = repo.finishes[0]
    assert kwargs["error_code"] == "eval_execution_failed"
    assert "kaboom" in kwargs["error_detail"]


def test_timeout_settles_failed(monkeypatch):
    release = threading.Event()
    repo = _FakeRepo()

    def slow(**_):
        release.wait(10)
        return _outcome()

    monkeypatch.setattr(worker_module, "evaluate_run", slow)
    try:
        _execute(_worker(repo, max_run_seconds=1), _row())
    finally:
        release.set()

    _, _, kwargs = repo.finishes[0]
    assert kwargs["status"] == "failed"
    assert kwargs["error_code"] == "eval_timeout"


def test_shutdown_mid_run_leaves_the_row_to_recovery(monkeypatch):
    release = threading.Event()
    repo = _FakeRepo()

    def slow(**_):
        release.wait(10)
        return _outcome()

    monkeypatch.setattr(worker_module, "evaluate_run", slow)
    worker = _worker(repo)
    worker._stop.set()
    try:
        _execute(worker, _row())
    finally:
        release.set()
    time.sleep(0.05)

    assert repo.finishes == []
