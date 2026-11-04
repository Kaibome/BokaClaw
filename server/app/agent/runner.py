"""Background execution and resumable event fanout for agent runs.

One ``AgentRunManager`` lives for the lifetime of the FastAPI process. It owns
an in-flight task per run, persists every streamed event through
``RunEventWriter``, and keeps subscribers attached by ``run_id`` rather than by
the HTTP request that created the run.

Single-replica assumption: run claiming is a conditional ``queued -> running``
update, not a lease-based queue. Running more than one server process against
the same database would let two workers race for the same run.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
import socket
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable

from app.agent.events import (
    AgentCompleted,
    AgentDelta,
    AgentEvent,
    AgentLoopError,
    AgentToolCall,
    AgentToolResult,
)
from app.agent.loop import stream_agent
from app.agent.payloads import attach_trace, should_persist_reply, tool_call_payload, tool_result_payload
from app.agent.repo import RunRepository, utcnow
from app.agent.schemas import ActiveRunOut, AgentRunOut
from app.agent.store import serialize_message
from app.agent.trace import ToolTrace
from app.models import TERMINAL_RUN_STATUSES, AgentRun

logger = logging.getLogger(__name__)

SUBSCRIBER_QUEUE_SIZE = 256
TERMINAL = frozenset(TERMINAL_RUN_STATUSES)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class RunConfig:
    background: bool
    concurrency: int
    max_seconds: float
    heartbeat_seconds: float
    lease_seconds: float
    event_retention_days: int
    run_retention_days: int
    delta_flush_ms: int
    delta_flush_bytes: int
    worker_id: str


def load_run_config() -> RunConfig:
    return RunConfig(
        background=_env_bool("AGENT_RUN_BACKGROUND", True),
        concurrency=max(1, _env_int("AGENT_RUN_CONCURRENCY", 2)),
        max_seconds=max(1.0, _env_float("AGENT_RUN_MAX_SECONDS", 1800.0)),
        heartbeat_seconds=max(1.0, _env_float("AGENT_RUN_HEARTBEAT_SECONDS", 10.0)),
        lease_seconds=max(5.0, _env_float("AGENT_RUN_LEASE_SECONDS", 60.0)),
        event_retention_days=max(1, _env_int("AGENT_RUN_EVENT_RETENTION_DAYS", 7)),
        run_retention_days=max(1, _env_int("AGENT_RUN_RETENTION_DAYS", 30)),
        delta_flush_ms=max(10, _env_int("AGENT_RUN_DELTA_FLUSH_MS", 150)),
        delta_flush_bytes=max(256, _env_int("AGENT_RUN_DELTA_FLUSH_BYTES", 4096)),
        worker_id=f"{socket.gethostname()}:{os.getpid()}",
    )


class RunSubscription:
    """One live subscriber, fed from an unbounded history read plus a bounded
    queue so a slow client can never block the run itself."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        self.history: list[tuple[str, dict[str, Any]]] = []
        self.initial_status: str = "queued"
        self.initial_last_seq: int = 0
        self.closed = False


class RunEventWriter:
    """Serializes one run's events, merging small text deltas.

    The manager guarantees a single writer per run, so ``last_seq`` can live in
    memory between flushes while the database row remains the durable copy.
    """

    def __init__(self, manager: "AgentRunManager", run: AgentRun) -> None:
        self.manager = manager
        self.repo = manager.repo
        self.run_id = run.id
        self.conversation_id = run.conversation_id
        self.assistant_id = run.assistant_message_id
        self.last_seq = int(run.last_seq or 0)
        self.content = run.partial_content or ""
        self.metadata: dict[str, Any] = dict(run.partial_metadata or {})
        self.available = True
        self._buffer: list[str] = []
        self._buffer_bytes = 0
        self._lock = asyncio.Lock()
        self._closed = False
        self._last_flush = time.monotonic()
        self._flush_task: asyncio.Task | None = None

    async def start(self) -> None:
        self._flush_task = asyncio.create_task(self._flush_loop(), name=f"agent-run-flush-{self.run_id}")

    async def _flush_loop(self) -> None:
        interval = self.manager.config.delta_flush_ms / 1000
        while not self._closed:
            await asyncio.sleep(interval)
            with contextlib.suppress(Exception):
                await self.flush()

    async def add_delta(self, text: str) -> None:
        if not text or not self.available:
            return
        async with self._lock:
            if not self.available:
                return
            self._buffer.append(text)
            self._buffer_bytes += len(text.encode("utf-8"))
            elapsed_ms = (time.monotonic() - self._last_flush) * 1000
            if elapsed_ms >= self.manager.config.delta_flush_ms or self._buffer_bytes >= self.manager.config.delta_flush_bytes:
                await self._flush_locked()

    async def write(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        partial_metadata: dict[str, Any] | None = None,
    ) -> bool:
        if not self.available:
            return False
        async with self._lock:
            if not self.available:
                return False
            await self._flush_locked()
            if partial_metadata is not None:
                self.metadata = dict(partial_metadata)
            return await self._append_locked(event_type, payload)

    async def flush(self) -> None:
        if not self.available:
            return
        async with self._lock:
            await self._flush_locked()

    async def close(self) -> None:
        self._closed = True
        if self._flush_task is not None:
            self._flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flush_task
            self._flush_task = None
        await self.flush()

    async def _flush_locked(self) -> None:
        if not self._buffer or not self.available:
            return
        text = "".join(self._buffer)
        self._buffer.clear()
        self._buffer_bytes = 0
        self.content += text
        self._last_flush = time.monotonic()
        await self._append_locked(
            "message.delta",
            {"message_id": _str_or_none(self.assistant_id), "delta": text},
        )

    async def _append_locked(self, event_type: str, payload: dict[str, Any]) -> bool:
        seq = self.last_seq + 1
        full = {
            "run_id": str(self.run_id),
            "conversation_id": str(self.conversation_id),
            "seq": seq,
            **payload,
        }
        ok = await asyncio.to_thread(
            self.repo.append_event,
            self.run_id,
            seq,
            event_type,
            full,
            partial_content=self.content,
            partial_metadata=self.metadata,
        )
        if not ok:
            self.available = False
            logger.info("Stopped persisting events for run %s (run gone or sequence taken)", self.run_id)
            return False
        self.last_seq = seq
        self.manager.publish(self.run_id, (event_type, full))
        return True


class AgentRunManager:
    def __init__(
        self,
        config: RunConfig | None = None,
        *,
        repo: RunRepository | None = None,
        executor: Callable[..., AsyncIterator[AgentEvent]] | None = None,
    ) -> None:
        self.config = config or load_run_config()
        self.repo = repo or RunRepository()
        self._executor = executor
        self._tasks: dict[uuid.UUID, asyncio.Task] = {}
        self._cancel_events: dict[uuid.UUID, threading.Event] = {}
        self._request_ids: dict[uuid.UUID, str] = {}
        self._waiters: dict[uuid.UUID, asyncio.Event] = {}
        self._subscribers: dict[uuid.UUID, set[RunSubscription]] = defaultdict(set)
        self._locks: dict[uuid.UUID, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._semaphore = asyncio.Semaphore(self.config.concurrency)
        self._shutting_down = False

    # ------------------------------------------------------------------ life

    async def startup(self) -> None:
        recovered = 0
        for run in await asyncio.to_thread(self.repo.list_running):
            try:
                await self._recover_run(run, reason="Agent run was interrupted by a server restart.")
                recovered += 1
            except Exception:  # pragma: no cover - defensive
                logger.exception("Failed to recover agent run %s", run.id)
        resubmitted = 0
        for run in await asyncio.to_thread(self.repo.list_queued):
            if not run.request_payload:
                await self._fail_unstartable(run)
                continue
            self.submit(run.id)
            resubmitted += 1
        logger.info(
            "Agent run manager ready (worker=%s, recovered=%d, resubmitted=%d, concurrency=%d)",
            self.config.worker_id,
            recovered,
            resubmitted,
            self.config.concurrency,
        )

    async def shutdown(self) -> None:
        self._shutting_down = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=5.0)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.wait(pending, timeout=1.0)
        for submission in list(self._subscribers.values()):
            for sub in submission:
                sub.closed = True
                with contextlib.suppress(asyncio.QueueFull):
                    sub.queue.put_nowait(("done", {"status": "interrupted", "last_seq": 0}))
        # Anything still marked running lost its task (shutdown timeout or a
        # crashed task). Best effort; the next boot recovers the rest.
        for run in await asyncio.to_thread(self.repo.list_running):
            try:
                await self._recover_run(run, reason="Agent run was interrupted by a server shutdown.")
            except Exception:  # pragma: no cover - defensive
                logger.exception("Failed to mark run %s interrupted during shutdown", run.id)

    # --------------------------------------------------------------- control

    def submit(self, run_id: uuid.UUID, request_id: str = "") -> None:
        if self._shutting_down or run_id in self._tasks:
            return
        self._request_ids[run_id] = request_id
        self._waiters[run_id] = asyncio.Event()
        self._cancel_events[run_id] = threading.Event()
        task = asyncio.create_task(self._run_worker(run_id), name=f"agent-run-{run_id}")
        self._tasks[run_id] = task
        task.add_done_callback(lambda finished: self._discard_task(run_id, finished))

    def _discard_task(self, run_id: uuid.UUID, task: asyncio.Task) -> None:
        if self._tasks.get(run_id) is task:
            self._tasks.pop(run_id, None)

    async def wait(self, run_id: uuid.UUID, timeout: float | None = None) -> None:
        waiter = self._waiters.get(run_id)
        if waiter is None:
            run = await asyncio.to_thread(self.repo.get_run, run_id)
            if run is None or run.status in TERMINAL:
                return
            waiter = asyncio.Event()
            self._waiters[run_id] = waiter
        try:
            await asyncio.wait_for(waiter.wait(), timeout=timeout)
        except TimeoutError:
            pass

    async def cancel(self, run_id: uuid.UUID, owner_id: uuid.UUID) -> AgentRun | None:
        run = await asyncio.to_thread(self.repo.get_run, run_id, owner_id)
        if run is None:
            return None
        if run.status in TERMINAL:
            return run
        now = utcnow()
        if run.status == "queued":
            if await asyncio.to_thread(self.repo.cancel_queued, run_id, now):
                logger.info("agent run cancel requested run_id=%s status=queued", run_id)
                fresh = await asyncio.to_thread(self.repo.get_run, run_id, owner_id)
                if fresh is not None:
                    await self._append_cancelled_event(fresh)
                    self._signal_done(run_id)
                return fresh
            run = await asyncio.to_thread(self.repo.get_run, run_id, owner_id)
            if run is None or run.status in TERMINAL:
                return run

        await asyncio.to_thread(self.repo.request_cancel, run_id, now)
        logger.info("agent run cancel requested run_id=%s status=%s", run_id, run.status)
        event = self._cancel_events.get(run_id)
        if event is not None:
            event.set()
        if run_id not in self._tasks:
            await self._finalize_orphan_cancel(run_id)
        return await asyncio.to_thread(self.repo.get_run, run_id, owner_id)

    # ------------------------------------------------------------ subscriber

    async def open_subscription(self, run_id: uuid.UUID, after_seq: int) -> RunSubscription | None:
        async with self._locks[run_id]:
            run = await asyncio.to_thread(self.repo.get_run, run_id)
            if run is None:
                return None
            sub = RunSubscription()
            self._subscribers[run_id].add(sub)
            status = run.status
            last_seq = int(run.last_seq or 0)

        events = await asyncio.to_thread(self.repo.list_events, run_id, after_seq)
        sub.history = [(event.event_type, dict(event.payload or {})) for event in events]
        sub.initial_status = status
        sub.initial_last_seq = last_seq
        logger.info("agent subscribe run_id=%s after_seq=%d status=%s", run_id, after_seq, status)
        return sub

    def close_subscription(self, run_id: uuid.UUID, sub: RunSubscription) -> None:
        sub.closed = True
        logger.info("agent unsubscribe run_id=%s after_seq=%d", run_id, sub.initial_last_seq)
        subscribers = self._subscribers.get(run_id)
        if subscribers is not None:
            subscribers.discard(sub)
            if not subscribers:
                self._subscribers.pop(run_id, None)

    def publish(self, run_id: uuid.UUID, item: tuple[str, dict[str, Any]]) -> None:
        for sub in list(self._subscribers.get(run_id, ())):
            if sub.closed:
                continue
            try:
                sub.queue.put_nowait(item)
            except asyncio.QueueFull:
                sub.closed = True
                self._subscribers[run_id].discard(sub)
                logger.warning("Dropped slow subscriber for run %s", run_id)

    # ---------------------------------------------------------------- worker

    async def _run_worker(self, run_id: uuid.UUID) -> None:
        writer: RunEventWriter | None = None
        heartbeat: asyncio.Task | None = None
        trace = ToolTrace()
        metadata: dict[str, Any] = {}
        interrupted = False

        try:
            async with self._semaphore:
                if self._shutting_down:
                    return
                claimed = await asyncio.to_thread(self.repo.claim, run_id, self.config.worker_id)
                if not claimed:
                    return
                run = await asyncio.to_thread(self.repo.get_run, run_id)
                if run is None:
                    return
                logger.info(
                    "agent run started run_id=%s worker_id=%s queued_seconds=%.1f",
                    run_id,
                    self.config.worker_id,
                    _seconds_since(run.created_at),
                )
                writer = RunEventWriter(self, run)
                await writer.start()
                heartbeat = asyncio.create_task(self._heartbeat(run_id))

                try:
                    async for event in self._events(run, metadata):
                        if not writer.available:
                            break
                        await self._handle_event(writer, trace, metadata, event)
                    status, code, detail = self._completion_state(writer, trace)
                    await self._finalize(run, writer, trace, metadata, status, code, detail, interrupted=False)
                except AgentLoopError as exc:
                    status = "cancelled" if exc.code == "agent_cancelled" else "failed"
                    await self._finalize(
                        run,
                        writer,
                        trace,
                        metadata,
                        status,
                        exc.code,
                        exc.detail,
                        interrupted=False,
                    )
                except asyncio.CancelledError:
                    _clear_cancellation()
                    interrupted = self._shutting_down
                    status = "interrupted" if interrupted else "cancelled"
                    code = "agent_interrupted" if interrupted else "agent_cancelled"
                    detail = (
                        "Agent run was interrupted by a server shutdown."
                        if interrupted
                        else "Agent run was cancelled."
                    )
                    await self._finalize(run, writer, trace, metadata, status, code, detail, interrupted=interrupted)
                except Exception as exc:  # pragma: no cover - defensive
                    logger.exception("Agent run %s crashed", run_id)
                    await self._finalize(
                        run,
                        writer,
                        trace,
                        metadata,
                        "failed",
                        "agent_upstream_error",
                        f"Agent run crashed: {exc}",
                        interrupted=False,
                    )
                finally:
                    if heartbeat is not None:
                        heartbeat.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await heartbeat
                    if writer is not None:
                        with contextlib.suppress(Exception):
                            await writer.close()
        except asyncio.CancelledError:
            _clear_cancellation()
            if writer is None:
                # Cancelled before the claim (or while claiming). Leave queued
                # runs untouched; recover the rare claimed-but-unstarted case.
                run = await asyncio.to_thread(self.repo.get_run, run_id)
                if run is not None and run.status == "running" and run.worker_id == self.config.worker_id:
                    with contextlib.suppress(Exception):
                        await self._recover_run(run, reason="Agent run was interrupted by a server shutdown.")
            # Otherwise the inner handler already finalized the run.
        finally:
            self._cancel_events.pop(run_id, None)
            self._request_ids.pop(run_id, None)
            self._signal_done(run_id)

    async def _events(self, run: AgentRun, metadata: dict[str, Any]) -> AsyncIterator[AgentEvent]:
        executor = self._executor or stream_agent
        kwargs: dict[str, Any] = {"max_seconds": self.config.max_seconds}
        cancel_event = self._cancel_events.get(run.id)
        try:
            accepts_cancel = "cancel_event" in inspect.signature(executor).parameters
        except (TypeError, ValueError):
            accepts_cancel = False
        if accepts_cancel:
            kwargs["cancel_event"] = cancel_event
        payload = dict(run.request_payload or {})
        async for event in executor(payload, self._request_ids.get(run.id, ""), **kwargs):
            yield event

    async def _heartbeat(self, run_id: uuid.UUID) -> None:
        while True:
            await asyncio.sleep(self.config.heartbeat_seconds)
            try:
                await asyncio.to_thread(self.repo.heartbeat, run_id, self.config.worker_id)
            except Exception:  # pragma: no cover - defensive
                logger.warning("Heartbeat failed for run %s", run_id, exc_info=True)

    async def _handle_event(
        self,
        writer: RunEventWriter,
        trace: ToolTrace,
        metadata: dict[str, Any],
        event: AgentEvent,
    ) -> None:
        assistant_id = writer.assistant_id
        if assistant_id is None:
            return
        if isinstance(event, AgentDelta):
            await writer.add_delta(event.delta)
        elif isinstance(event, AgentCompleted):
            metadata.update(event.metadata)
        elif isinstance(event, AgentToolCall):
            entry = trace.start(
                event.tool_call_id,
                event.name,
                iteration=event.iteration,
                arguments=event.arguments,
                raw_arguments=event.raw_arguments,
                arguments_error=event.arguments_error,
            )
            partial = dict(metadata)
            attach_trace(trace, partial)
            await writer.write(
                "tool.call",
                tool_call_payload(writer.conversation_id, assistant_id, entry),
                partial_metadata=partial,
            )
        elif isinstance(event, AgentToolResult):
            entry = trace.finish(
                event.tool_call_id,
                event.name,
                event.content,
                is_error=event.is_error,
                iteration=event.iteration,
                status=event.status,
                duration_ms=event.duration_ms,
            )
            partial = dict(metadata)
            attach_trace(trace, partial)
            await writer.write(
                "tool.result",
                tool_result_payload(writer.conversation_id, assistant_id, entry),
                partial_metadata=partial,
            )

    @staticmethod
    def _completion_state(writer: RunEventWriter, trace: ToolTrace) -> tuple[str, str | None, str | None]:
        if not writer.content.strip() and not trace.has_entries():
            return "failed", "agent_protocol_error", "Agent executor produced no content."
        return "completed", None, None

    async def _finalize(
        self,
        run: AgentRun,
        writer: RunEventWriter,
        trace: ToolTrace,
        metadata: dict[str, Any],
        status: str,
        error_code: str | None,
        error_detail: str | None,
        *,
        interrupted: bool,
    ) -> None:
        await writer.flush()
        aborted = trace.abort_open(f"run {status}")
        if aborted and writer.assistant_id is not None:
            for entry in aborted:
                partial = dict(metadata)
                attach_trace(trace, partial)
                await writer.write(
                    "tool.result",
                    tool_result_payload(writer.conversation_id, writer.assistant_id, entry),
                    partial_metadata=partial,
                )

        final_metadata = dict(metadata)
        if interrupted:
            status = "interrupted"
            error_code = "agent_interrupted"
            final_metadata["run_status"] = "interrupted"
            final_metadata["error_code"] = "agent_interrupted"
        elif error_code:
            final_metadata["error_code"] = error_code
        attach_trace(trace, final_metadata)

        message_status = {"completed": "completed", "cancelled": "cancelled"}.get(status, "failed")
        message = None
        if should_persist_reply(writer.content, trace):
            message = await asyncio.to_thread(
                self.repo.insert_assistant_message,
                conversation_id=writer.conversation_id,
                owner_id=run.created_by,
                message_id=writer.assistant_id,
                content=writer.content,
                status=message_status,
                metadata=final_metadata,
            )
            if message is None:
                return

        finished = await asyncio.to_thread(
            self.repo.finish,
            run.id,
            status=status,
            error_code=error_code,
            error_detail=error_detail,
            partial_content=writer.content,
            partial_metadata=final_metadata,
        )
        if not finished:
            return
        logger.info(
            "agent run finished run_id=%s status=%s duration_seconds=%.1f events=%d error_code=%s",
            run.id,
            status,
            _seconds_since(run.started_at or run.created_at),
            writer.last_seq,
            error_code,
        )

        if status == "completed":
            if not await asyncio.to_thread(self.repo.has_event, run.id, ("message.completed",)):
                payload: dict[str, Any] = {"message_id": _str_or_none(writer.assistant_id)}
                if message is not None:
                    payload["message"] = serialize_message(message).model_dump(mode="json")
                await writer.write("message.completed", payload, partial_metadata=final_metadata)
        else:
            if not await asyncio.to_thread(self.repo.has_event, run.id, ("error",)):
                await writer.write(
                    "error",
                    {
                        "code": error_code or "agent_failed",
                        "detail": error_detail or "Agent run failed.",
                        "request_id": self._request_ids.get(run.id, ""),
                        "partial": message is not None,
                    },
                    partial_metadata=final_metadata,
                )

        self.publish(
            run.id,
            (
                "done",
                {
                    "run_id": str(run.id),
                    "conversation_id": str(run.conversation_id),
                    "status": status,
                    "last_seq": writer.last_seq,
                },
            ),
        )

    # -------------------------------------------------------------- recovery

    async def _recover_run(self, run: AgentRun, *, reason: str) -> None:
        context = await asyncio.to_thread(self.repo.get_context, run.id)
        if context is None:
            return
        writer = RunEventWriter(self, context.run)
        message = context.assistant_message

        if message is not None:
            if message.status == "completed":
                status, code, detail = "completed", None, None
            elif message.status == "cancelled":
                status, code, detail = "cancelled", "agent_cancelled", "Agent run was cancelled."
            else:
                status = "failed"
                meta = dict(message.meta or {})
                code = meta.get("error_code") or "agent_interrupted"
                detail = meta.get("error_detail") or reason
            writer.content = message.content or ""
            writer.metadata = dict(message.meta or {})
        else:
            metadata = dict(writer.metadata)
            metadata["run_status"] = "interrupted"
            metadata["error_code"] = "agent_interrupted"
            aborted = _abort_trace_entries(metadata, reason)
            if aborted and writer.assistant_id is not None:
                for entry in aborted:
                    await writer.write(
                        "tool.result",
                        tool_result_payload(writer.conversation_id, writer.assistant_id, entry),
                        partial_metadata=metadata,
                    )
            writer.metadata = metadata
            if should_persist_metadata_reply(writer.content, metadata):
                message = await asyncio.to_thread(
                    self.repo.insert_assistant_message,
                    conversation_id=writer.conversation_id,
                    owner_id=context.run.created_by,
                    message_id=writer.assistant_id,
                    content=writer.content,
                    status="failed",
                    metadata=metadata,
                )
                if message is None:
                    return
            status, code, detail = "interrupted", "agent_interrupted", reason

        finished = await asyncio.to_thread(
            self.repo.finish,
            run.id,
            status=status,
            error_code=code,
            error_detail=detail,
            partial_content=writer.content,
            partial_metadata=writer.metadata,
        )
        if not finished:
            return
        logger.info(
            "agent run interrupted run_id=%s reason=%s status=%s partial_chars=%d",
            run.id,
            reason,
            status,
            len(writer.content or ""),
        )

        if status == "completed":
            if not await asyncio.to_thread(self.repo.has_event, run.id, ("message.completed",)):
                payload: dict[str, Any] = {"message_id": _str_or_none(writer.assistant_id)}
                if message is not None:
                    payload["message"] = serialize_message(message).model_dump(mode="json")
                await writer.write("message.completed", payload, partial_metadata=writer.metadata)
        else:
            if not await asyncio.to_thread(self.repo.has_event, run.id, ("error",)):
                await writer.write(
                    "error",
                    {
                        "code": code or "agent_interrupted",
                        "detail": detail or reason,
                        "partial": message is not None,
                    },
                    partial_metadata=writer.metadata,
                )
        self.publish(
            run.id,
            (
                "done",
                {
                    "run_id": str(run.id),
                    "conversation_id": str(run.conversation_id),
                    "status": status,
                    "last_seq": writer.last_seq,
                },
            ),
        )

    async def _fail_unstartable(self, run: AgentRun) -> None:
        writer = RunEventWriter(self, run)
        detail = "Stored run request is missing; it cannot be resumed."
        await writer.write("error", {"code": "agent_protocol_error", "detail": detail, "partial": False})
        await asyncio.to_thread(
            self.repo.finish,
            run.id,
            status="failed",
            error_code="agent_protocol_error",
            error_detail=detail,
        )
        self.publish(
            run.id,
            ("done", {"run_id": str(run.id), "conversation_id": str(run.conversation_id), "status": "failed", "last_seq": writer.last_seq}),
        )

    async def _append_cancelled_event(self, run: AgentRun) -> None:
        writer = RunEventWriter(self, run)
        await writer.write(
            "error",
            {
                "code": "agent_cancelled",
                "detail": run.error_detail or "Agent run was cancelled before it started.",
                "partial": False,
            },
        )
        self.publish(
            run.id,
            ("done", {"run_id": str(run.id), "conversation_id": str(run.conversation_id), "status": "cancelled", "last_seq": writer.last_seq}),
        )

    async def _finalize_orphan_cancel(self, run_id: uuid.UUID) -> None:
        run = await asyncio.to_thread(self.repo.get_run, run_id)
        if run is None or run.status in TERMINAL:
            return
        writer = RunEventWriter(self, run)
        metadata = dict(writer.metadata)
        metadata["error_code"] = "agent_cancelled"
        aborted = _abort_trace_entries(metadata, "run cancelled")
        if aborted and writer.assistant_id is not None:
            for entry in aborted:
                await writer.write(
                    "tool.result",
                    tool_result_payload(writer.conversation_id, writer.assistant_id, entry),
                    partial_metadata=metadata,
                )
        writer.metadata = metadata
        message = None
        if should_persist_metadata_reply(writer.content, metadata):
            message = await asyncio.to_thread(
                self.repo.insert_assistant_message,
                conversation_id=writer.conversation_id,
                owner_id=run.created_by,
                message_id=writer.assistant_id,
                content=writer.content,
                status="cancelled",
                metadata=metadata,
            )
            if message is None:
                return
        detail = "Agent run was cancelled."
        finished = await asyncio.to_thread(
            self.repo.finish,
            run.id,
            status="cancelled",
            error_code="agent_cancelled",
            error_detail=detail,
            partial_content=writer.content,
            partial_metadata=metadata,
        )
        if not finished:
            return
        if not await asyncio.to_thread(self.repo.has_event, run.id, ("error",)):
            await writer.write(
                "error",
                {"code": "agent_cancelled", "detail": detail, "partial": message is not None},
                partial_metadata=metadata,
            )
        self.publish(
            run.id,
            ("done", {"run_id": str(run.id), "conversation_id": str(run.conversation_id), "status": "cancelled", "last_seq": writer.last_seq}),
        )

    # ------------------------------------------------------------- utilities

    def _signal_done(self, run_id: uuid.UUID) -> None:
        waiter = self._waiters.pop(run_id, None)
        if waiter is not None:
            waiter.set()


def _clear_cancellation() -> None:
    """Let a cancelled task run its cleanup awaits without re-raising."""
    task = asyncio.current_task()
    if task is None:
        return
    uncancel = getattr(task, "uncancel", None)
    cancelling = getattr(task, "cancelling", None)
    if uncancel is None or cancelling is None:
        return
    while cancelling():
        uncancel()


def _abort_trace_entries(metadata: dict[str, Any], reason: str) -> list[dict[str, Any]]:
    entries = metadata.get("tool_trace")
    if not isinstance(entries, list):
        return []
    now = datetime.now(timezone.utc).isoformat()
    aborted: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("status") != "running":
            continue
        entry["status"] = "aborted"
        entry["is_error"] = True
        entry["finished_at"] = now
        if not entry.get("content"):
            entry["content"] = f'{{"error": "{reason}"}}'
        aborted.append(dict(entry))
    return aborted


def should_persist_metadata_reply(content: str, metadata: dict[str, Any]) -> bool:
    return bool(content) or bool(metadata.get("tool_trace"))


def serialize_run(run: AgentRun) -> AgentRunOut:
    return AgentRunOut(
        id=str(run.id),
        conversation_id=str(run.conversation_id),
        status=run.status,
        error_code=run.error_code,
        error_detail=run.error_detail,
        last_seq=int(run.last_seq or 0),
        partial_content=run.partial_content or "",
        assistant_message_id=_str_or_none(run.assistant_message_id),
        cancel_requested_at=run.cancel_requested_at,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def serialize_active_run(run: AgentRun) -> ActiveRunOut:
    return ActiveRunOut(
        id=str(run.id),
        status=run.status,
        last_seq=int(run.last_seq or 0),
        assistant_message_id=_str_or_none(run.assistant_message_id),
        cancel_requested_at=run.cancel_requested_at,
        created_at=run.created_at,
    )


def _str_or_none(value: Any) -> str | None:
    return str(value) if value is not None else None


def _seconds_since(moment: datetime | None) -> float:
    if moment is None:
        return 0.0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0.0, (utcnow() - moment).total_seconds())
