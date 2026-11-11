"""Database-queue worker for queued evaluations.

Run with ``python -m app.eval.worker`` inside the ``bokaclaw-eval`` image. The
row itself is the queue: claim with ``FOR UPDATE SKIP LOCKED``, renew the lease
with a heartbeat, and settle exactly once with a conditional ``finish``.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "1")
os.environ.setdefault("DEEPEVAL_CACHE_FOLDER", "/tmp/deepeval")

from app.eval.config import EvalConfig, apply_judge_env, load_eval_config
from app.eval.evaluator import EvalExecutionError, evaluate_run
from app.eval.repo import EvalRepository

log = logging.getLogger("eval.worker")

HEALTH_FILE = Path(os.environ.get("EVAL_HEALTH_FILE", "/tmp/eval-worker-heartbeat"))


class EvalWorker:
    def __init__(self, config: EvalConfig | None = None, repo: EvalRepository | None = None):
        self.config = config or load_eval_config()
        self.repo = repo or EvalRepository()
        self.worker_id = os.environ.get("EVAL_WORKER_ID") or f"eval-{socket.gethostname()}"
        self._stop = threading.Event()
        self._last_recover = 0.0
        self._last_prune = 0.0

    def run_forever(self) -> int:
        apply_judge_env(self.config)
        self._install_signal_handlers()
        log.info(
            "eval worker %s started (judge=%s, poll=%.1fs, lease=%ss)",
            self.worker_id,
            self.config.judge_available,
            self.config.worker_poll_seconds,
            self.config.lease_seconds,
        )
        while not self._stop.is_set():
            self._touch_health()
            try:
                self._recover_if_due()
                if not self.config.enabled:
                    self._stop.wait(self.config.worker_poll_seconds)
                    continue
                row = self.repo.claim(self.worker_id)
                if row is None:
                    self._stop.wait(self.config.worker_poll_seconds)
                    continue
                log.info("claimed eval %s (attempt %s)", row.id, row.attempt)
                self._process(row)
            except Exception:
                log.exception("eval worker loop failed")
                self._stop.wait(self.config.worker_poll_seconds)
        log.info("eval worker %s stopped", self.worker_id)
        return 0

    def _install_signal_handlers(self) -> None:
        def _handle(signum: int, _frame: Any) -> None:
            log.info("received signal %s, stopping after the current eval", signum)
            self._stop.set()

        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(signum, _handle)
            except ValueError:
                # Not the main thread (tests); skipping is fine.
                pass

    def _touch_health(self) -> None:
        try:
            HEALTH_FILE.touch()
        except OSError:
            log.warning("cannot write health file %s", HEALTH_FILE)

    def _recover_if_due(self) -> None:
        now = time.monotonic()
        if now - self._last_recover < max(30.0, self.config.lease_seconds / 2):
            return
        self._last_recover = now
        counts = self.repo.recover_stale(
            lease_seconds=self.config.lease_seconds,
            max_attempts=self.config.max_attempts,
        )
        if any(counts.values()):
            log.warning("recovered stale evals: %s", counts)
        if now - self._last_prune >= 3600:
            self._last_prune = now
            pruned = self.repo.prune_finished(retention_days=self.config.retention_days)
            if pruned:
                log.info("pruned %s finished evals older than %s days", pruned, self.config.retention_days)

    def _process(self, row: Any) -> None:
        hb_stop = threading.Event()
        cancel_flag = threading.Event()
        lost_flag = threading.Event()
        # A cancel may have arrived between claim and this point; do not start
        # the evaluation just to notice it ten seconds later.
        owned, cancel_requested = self.repo.heartbeat_state(row.id, self.worker_id)
        if not owned:
            log.warning("eval %s lost its lease before starting", row.id)
            return
        if cancel_requested:
            self.repo.finish(
                row.id,
                self.worker_id,
                status="cancelled",
                error_code="eval_cancelled",
                error_detail="Cancellation was requested before the evaluation started.",
            )
            log.info("eval %s cancelled before it started", row.id)
            return
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            args=(row.id, hb_stop, cancel_flag, lost_flag),
            name=f"eval-heartbeat-{row.id}",
            daemon=True,
        )
        heartbeat.start()
        try:
            self._execute(row, cancel_flag, lost_flag)
        finally:
            hb_stop.set()
            heartbeat.join(timeout=2)

    def _execute(self, row: Any, cancel_flag: threading.Event, lost_flag: threading.Event) -> None:
        run = self.repo.get_run(row.run_id)
        if run is None:
            self._finish_failed(row, "eval_run_not_found", "The target agent run no longer exists.")
            return
        spans = self.repo.list_spans(row.run_id)
        if not spans:
            self._finish_failed(
                row,
                "eval_no_spans",
                "The run has no persisted spans (capture disabled or unsampled).",
            )
            return

        box: dict[str, Any] = {}

        def target() -> None:
            try:
                box["outcome"] = evaluate_run(
                    run=run,
                    spans=spans,
                    golden_snapshot=row.golden_snapshot,
                    golden_hash=row.golden_hash,
                    use_llm_judge=row.use_llm_judge,
                    judge_max_chars=self.config.judge_max_chars,
                    judge_language=self.config.judge_language,
                )
            except EvalExecutionError as exc:
                box["error"] = exc
            except Exception as exc:  # noqa: BLE001 - worker must always settle the row
                box["error"] = EvalExecutionError("eval_execution_failed", str(exc))

        thread = threading.Thread(target=target, name=f"eval-run-{row.id}", daemon=True)
        thread.start()
        deadline = time.monotonic() + self.config.max_run_seconds
        while thread.is_alive() and time.monotonic() < deadline and not self._stop.is_set():
            thread.join(timeout=1.0)

        if thread.is_alive():
            if self._stop.is_set():
                # Graceful shutdown mid-eval: do not settle a half-finished run as
                # timed out. Stop heartbeating and let stale recovery requeue it.
                log.info("eval %s still running at shutdown; leaving the row to stale recovery", row.id)
                return
            log.error("eval %s exceeded %ss", row.id, self.config.max_run_seconds)
            self._finish_failed(
                row,
                "eval_timeout",
                f"Evaluation exceeded EVAL_MAX_RUN_SECONDS={self.config.max_run_seconds}.",
            )
            return

        if lost_flag.is_set():
            log.warning("eval %s lost its lease; leaving the row to stale recovery", row.id)
            return

        error = box.get("error")
        if error is not None:
            self._finish_failed(row, error.code, error.detail)
            return

        outcome = box["outcome"]
        if cancel_flag.is_set():
            self.repo.finish(
                row.id,
                self.worker_id,
                status="cancelled",
                metrics=outcome.metrics,
                warnings=outcome.warnings,
                overall_success=outcome.overall_success,
                judge_model=outcome.judge_model,
                token_cost=outcome.token_cost,
                duration_ms=outcome.duration_ms,
                error_code="eval_cancelled",
                error_detail="Cancellation was requested while the evaluation was running.",
            )
            log.info("eval %s cancelled", row.id)
            return

        settled = self.repo.finish(
            row.id,
            self.worker_id,
            status="completed",
            metrics=outcome.metrics,
            warnings=outcome.warnings,
            raw_result=outcome.raw_result,
            overall_success=outcome.overall_success,
            judge_model=outcome.judge_model,
            token_cost=outcome.token_cost,
            duration_ms=outcome.duration_ms,
        )
        if not settled:
            log.warning("eval %s changed owner before it could be settled", row.id)
        else:
            log.info(
                "eval %s completed: overall=%s in %sms",
                row.id,
                outcome.overall_success,
                outcome.duration_ms,
            )

    def _finish_failed(self, row: Any, code: str, detail: str) -> None:
        self.repo.finish(
            row.id,
            self.worker_id,
            status="failed",
            error_code=code,
            error_detail=detail,
        )
        log.error("eval %s failed: %s: %s", row.id, code, detail)

    def _heartbeat_loop(
        self,
        eval_id: Any,
        stop: threading.Event,
        cancel_flag: threading.Event,
        lost_flag: threading.Event,
    ) -> None:
        while not stop.is_set():
            try:
                owned, cancel_requested = self.repo.heartbeat_state(eval_id, self.worker_id)
            except Exception:
                log.exception("heartbeat for eval %s failed", eval_id)
            else:
                if not owned:
                    lost_flag.set()
                    return
                if cancel_requested:
                    cancel_flag.set()
                self._touch_health()
            if stop.wait(self.config.heartbeat_seconds):
                return


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("EVAL_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s - %(levelname)s - [%(name)s] %(message)s",
    )
    try:
        return EvalWorker().run_forever()
    except Exception:
        log.exception("eval worker crashed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
