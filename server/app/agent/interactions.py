"""Human-in-the-loop questions for one background run.

The agent runs in a worker thread while ``RunEventWriter`` lives on the event
loop. ``RunInteraction`` owns that boundary: it registers a question, asks the
writer to persist and publish it (blocking the worker thread until the write
completes), then waits for an answer posted by the HTTP route. Timeouts and
cancellation wake the waiter without writing extra events.

Ordering rule: an answer is written under ``_answer_lock`` and ``seal`` waits
for that lock, so no ``user.answer`` can be appended after the run's terminal
events. The worker thread never awaits; it schedules loop work and waits on a
``threading.Event``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .runner import RunEventWriter

logger = logging.getLogger(__name__)

QUESTION_EVENT = "user.question"
ANSWER_EVENT = "user.answer"

MIN_TIMEOUT_SECONDS = 5.0
MAX_TIMEOUT_SECONDS = 600.0
DEFAULT_TIMEOUT_SECONDS = 240.0
POLL_SECONDS = 0.2
WRITE_TIMEOUT_SECONDS = 10.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class PendingQuestion:
    """One question waiting for the user.

    ``event`` is set by the loop and read by the worker thread; the worker
    reads ``answer``/``error`` only after the event is set.
    """

    question_id: str
    payload: dict[str, Any]
    event: threading.Event = field(default_factory=threading.Event)
    answer: dict[str, Any] | None = None
    error: str | None = None


class RunInteraction:
    """Question broker for one run."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._writer: "RunEventWriter | None" = None
        self._pending: dict[str, PendingQuestion] = {}
        self._lock = threading.Lock()
        # Serializes answer writes with seal() on the event loop.
        self._answer_lock = asyncio.Lock()
        self._closed = False

    def attach_writer(self, writer: "RunEventWriter") -> None:
        with self._lock:
            if not self._closed:
                self._writer = writer

    def detach_writer(self) -> None:
        with self._lock:
            self._writer = None

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def ask(
        self,
        *,
        question: str,
        options: list[str] | None = None,
        allow_free_text: bool = True,
        timeout_seconds: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Ask the user and block until an answer, timeout, or cancellation."""

        timeout = _clamp_timeout(timeout_seconds)
        question_id = f"q_{uuid.uuid4().hex[:16]}"
        with self._lock:
            if self._closed or self._writer is None:
                return {"error": "User input is not available for this run."}
            if self._pending:
                return {"error": "A question is already waiting for the user."}
            writer = self._writer
            expires_at = (_utcnow() + timedelta(seconds=timeout)).isoformat()
            pending = PendingQuestion(
                question_id=question_id,
                payload={
                    "message_id": str(writer.assistant_id) if writer.assistant_id else None,
                    "question_id": question_id,
                    "question": question,
                    "options": list(options or ()),
                    "allow_free_text": allow_free_text,
                    "expires_at": expires_at,
                    "timeout_seconds": int(timeout),
                },
            )
            self._pending[question_id] = pending

        try:
            written = self._call(self._write_question(pending))
        except Exception as exc:  # noqa: BLE001 - asking must never crash the run
            self._drop(question_id)
            logger.warning("Failed to write agent question: %s", exc)
            return {"error": "Could not deliver the question to the user."}
        if not written:
            self._drop(question_id)
            return {"error": "The run is no longer accepting user input."}

        deadline = time.monotonic() + timeout
        try:
            while True:
                if pending.event.is_set():
                    if pending.answer is not None:
                        return {"question_id": question_id, **pending.answer}
                    return {"question_id": question_id, "error": pending.error or "cancelled"}
                if cancel_event is not None and cancel_event.is_set():
                    return self._resolve(question_id, "cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._resolve(question_id, "timeout")
                pending.event.wait(min(POLL_SECONDS, remaining))
        finally:
            self._drop(question_id)

    async def answer(self, question_id: str, answer: str) -> tuple[bool, str]:
        """Record a user answer and wake the waiting tool."""

        async with self._answer_lock:
            with self._lock:
                if self._closed:
                    return False, "run_not_accepting_input"
                pending = self._pending.get(question_id)
                if pending is None:
                    return False, "question_not_pending"
                if pending.event.is_set():
                    return False, "question_already_answered"
                writer = self._writer
            if writer is None:
                return False, "run_not_accepting_input"

            payload = {
                "message_id": str(writer.assistant_id) if writer.assistant_id else None,
                "question_id": question_id,
                "answer": answer,
                "answered_at": _utcnow().isoformat(),
            }
            if not await writer.write(ANSWER_EVENT, payload):
                return False, "run_not_accepting_input"

            with self._lock:
                pending.answer = {"answer": answer}
                pending.event.set()
            return True, "accepted"

    async def seal(self, reason: str = "run finished") -> None:
        """Stop accepting questions/answers and release every waiter.

        Setting ``_closed`` happens before waiting on ``_answer_lock`` so a new
        answer cannot slip in; the lock wait lets an accepted answer finish its
        write before finalization continues.
        """

        with self._lock:
            self._closed = True
        async with self._answer_lock:
            self._release(reason)

    def close(self, reason: str = "run finished") -> None:
        """Synchronous fallback used after the writer is already closed."""

        with self._lock:
            self._closed = True
        self._release(reason)

    def _release(self, reason: str) -> None:
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for item in pending:
            item.error = reason
            item.event.set()

    def _resolve(self, question_id: str, reason: str) -> dict[str, Any]:
        """Resolve a timeout/cancel on the loop, racing deterministically with answer()."""

        try:
            return self._call(self._resolve_async(question_id, reason))
        except Exception:  # noqa: BLE001 - the loop may already be gone
            return {"question_id": question_id, "error": reason}

    async def _resolve_async(self, question_id: str, reason: str) -> dict[str, Any]:
        async with self._answer_lock:
            with self._lock:
                pending = self._pending.pop(question_id, None)
                if pending is None:
                    return {"question_id": question_id, "error": reason}
                if pending.answer is not None:
                    return {"question_id": question_id, **pending.answer}
                pending.error = reason
                pending.event.set()
                return {"question_id": question_id, "error": reason}

    async def _write_question(self, pending: PendingQuestion) -> bool:
        # Share the answer lock so seal() cannot slip between this check and the
        # write: a question either lands before terminal events or not at all.
        async with self._answer_lock:
            with self._lock:
                if self._closed or self._writer is None:
                    return False
                writer = self._writer
            return await writer.write(QUESTION_EVENT, pending.payload)

    def _call(self, awaitable: Any) -> Any:
        future = asyncio.run_coroutine_threadsafe(_as_coroutine(awaitable), self._loop)
        return future.result(timeout=WRITE_TIMEOUT_SECONDS)

    def _drop(self, question_id: str) -> None:
        with self._lock:
            self._pending.pop(question_id, None)


async def _as_coroutine(value: Any) -> Any:
    return await value


def _clamp_timeout(value: float | None) -> float:
    if value is None:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS
    if timeout <= 0:
        return DEFAULT_TIMEOUT_SECONDS
    return max(MIN_TIMEOUT_SECONDS, min(MAX_TIMEOUT_SECONDS, timeout))
