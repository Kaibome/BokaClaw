import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.agent.events import AgentToolCall, AgentToolResult
from app.agent.loop import (
    AgentCompleted,
    AgentDelta,
    AgentLoopError,
    is_configured,
    load_config,
    sse_comment,
    sse_event,
    stream_agent,
)
from app.agent.payloads import attach_trace, should_persist_reply, tool_call_payload, tool_result_payload
from app.agent.repo import utcnow
from app.agent.runner import (
    AgentRunManager,
    RunSubscription,
    load_run_config,
    serialize_run,
)
from app.agent.schemas import (
    AgentAnswerRequest,
    AgentInvokeRequest,
    AgentInvokeResponse,
    ConversationMessageOut,
    ConversationOut,
    RunCancelOut,
)
from app.agent.store import (
    DEFAULT_TITLE,
    append_message,
    build_history,
    create_conversation,
    derive_title,
    get_owned_conversation,
    list_messages,
    serialize_conversation,
    serialize_message,
)
from app.agent.trace import ToolTrace
from app.core.auth import require_auth
from app.core.db import SessionLocal
from app.core.errors import request_id_var
from app.core.rate_limit import limiter
from app.models import TERMINAL_RUN_STATUSES, AgentRun, Conversation, ConversationMessage, User

router = APIRouter(prefix="/agent", tags=["agent"])
logger = logging.getLogger(__name__)

NOT_CONFIGURED_DETAIL = (
    "Agent execution is not configured. Set AGENT_MODEL and AGENT_API_KEY (or OPENAI_API_KEY), "
    "or set AGENT_LOOP_URL for an external executor."
)
HEARTBEAT_SECONDS = 15
ACTIVE_RUN_STATUSES = ("queued", "running")
TERMINAL = frozenset(TERMINAL_RUN_STATUSES)


class ActiveRunConflict(Exception):
    def __init__(self, run_id: uuid.UUID, conversation_id: uuid.UUID) -> None:
        super().__init__("A run is already active for this conversation.")
        self.run_id = run_id
        self.conversation_id = conversation_id


@dataclass
class PreparedTurn:
    conversation: ConversationOut
    user_message: ConversationMessageOut
    history: list[dict[str, Any]]


@dataclass
class BackgroundTurn:
    conversation: ConversationOut
    user_message: ConversationMessageOut
    run_id: uuid.UUID
    assistant_id: uuid.UUID
    conversation_uuid: uuid.UUID
    payload: dict[str, Any]


def _prepare_turn(body: AgentInvokeRequest, user: User, history_limit: int) -> PreparedTurn:
    with SessionLocal() as db:
        if body.conversation_id is not None:
            conversation = get_owned_conversation(db, body.conversation_id, user.id)
            if conversation.title == DEFAULT_TITLE and not list_messages(db, conversation.id, limit=1):
                conversation.title = derive_title(body.message)
        else:
            conversation = create_conversation(
                db,
                user.id,
                title=derive_title(body.message),
                agent_id=body.agent_id,
                metadata=body.metadata,
            )

        history = build_history(db, conversation.id, limit=history_limit)

        message_metadata = dict(body.metadata)
        if body.run_id:
            message_metadata["run_id"] = body.run_id
        user_message = append_message(db, conversation, role="user", content=body.message, metadata=message_metadata)

        return PreparedTurn(
            conversation=serialize_conversation(conversation),
            user_message=serialize_message(user_message),
            history=history,
        )


def _create_background_turn(body: AgentInvokeRequest, user: User, history_limit: int) -> BackgroundTurn:
    """Persist the user turn and the queued run atomically.

    The active-run check happens before any write so a second tab gets a clean
    409. The partial unique index is the final guard against two requests that
    pass that check at the same time.
    """
    with SessionLocal() as db:
        if body.conversation_id is not None:
            conversation = get_owned_conversation(db, body.conversation_id, user.id)
            active = _active_run_for(db, conversation.id)
            if active is not None:
                raise ActiveRunConflict(active.id, active.conversation_id)
            if conversation.title == DEFAULT_TITLE and not list_messages(db, conversation.id, limit=1):
                conversation.title = derive_title(body.message)
        else:
            conversation = Conversation(
                created_by=user.id,
                title=derive_title(body.message),
                agent_id=body.agent_id,
                meta=body.metadata,
            )
            db.add(conversation)
            db.flush()

        history = build_history(db, conversation.id, limit=history_limit)

        conversation_id = conversation.id
        user_message_id = uuid.uuid4()
        assistant_id = uuid.uuid4()
        message_metadata = dict(body.metadata)
        if body.run_id:
            message_metadata["run_id"] = body.run_id
        user_message = ConversationMessage(
            id=user_message_id,
            conversation_id=conversation_id,
            role="user",
            content=body.message,
            status="completed",
            meta=message_metadata,
        )
        db.add(user_message)
        # Flush in dependency order: the run references the user message's id,
        # and there is no ORM relationship to teach the unit of work that.
        db.flush()

        payload = {
            "conversation_id": str(conversation_id),
            "message_id": str(user_message_id),
            "message": body.message,
            "history": history,
            "user_id": str(user.id),
            "agent_id": body.agent_id or conversation.agent_id,
            "run_id": body.run_id,
            "client_run_id": body.client_run_id,
            "metadata": body.metadata,
            # Background runs have an event writer, so they can ask the user.
            # The legacy request-bound path never sets this flag.
            "user_input": True,
        }
        run = AgentRun(
            conversation_id=conversation_id,
            created_by=user.id,
            user_message_id=user_message_id,
            assistant_message_id=assistant_id,
            request_payload=payload,
            client_run_id=body.client_run_id or body.run_id,
            agent_id=body.agent_id or conversation.agent_id,
            status="queued",
        )
        db.add(run)
        conversation.updated_at = utcnow()

        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            with SessionLocal() as probe:
                active = _active_run_for(probe, conversation_id)
            if active is not None:
                raise ActiveRunConflict(active.id, active.conversation_id) from exc
            raise

        db.refresh(conversation)
        db.refresh(user_message)
        db.refresh(run)
        logger.info(
            "agent run created run_id=%s conversation_id=%s client_run_id=%s",
            run.id,
            conversation_id,
            run.client_run_id,
        )
        return BackgroundTurn(
            conversation=serialize_conversation(conversation),
            user_message=serialize_message(user_message),
            run_id=run.id,
            assistant_id=assistant_id,
            conversation_uuid=conversation_id,
            payload=payload,
        )


def _active_run_for(db, conversation_id: uuid.UUID) -> AgentRun | None:
    return db.scalar(
        select(AgentRun)
        .where(
            AgentRun.conversation_id == conversation_id,
            AgentRun.status.in_(ACTIVE_RUN_STATUSES),
        )
        .order_by(AgentRun.created_at.desc())
        .limit(1)
    )


def _save_assistant(
    conversation_id: uuid.UUID,
    owner_id: uuid.UUID,
    message_id: uuid.UUID,
    content: str,
    status: str,
    metadata: dict[str, Any],
) -> ConversationMessageOut | None:
    with SessionLocal() as db:
        conversation = db.scalar(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.created_by == owner_id,
            )
        )
        if conversation is None:
            return None
        message = append_message(
            db,
            conversation,
            message_id=message_id,
            role="assistant",
            content=content,
            status=status,
            metadata=metadata,
        )
        return serialize_message(message)


def _error_payload(
    code: str,
    detail: str,
    request_id: str,
    conversation_id: uuid.UUID | str | None = None,
    *,
    run_id: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "detail": detail,
        "code": code,
        "request_id": request_id,
        **extra,
    }
    if conversation_id is not None:
        payload["conversation_id"] = str(conversation_id)
    if run_id is not None:
        payload["run_id"] = run_id
    return payload


def _run_manager(request: Request) -> AgentRunManager:
    manager = getattr(request.app.state, "agent_runs", None)
    if manager is None:
        manager = AgentRunManager()
        request.app.state.agent_runs = manager
    return manager


@router.post("", summary="Invoke the agent for one turn")
@limiter.limit("30/minute")
async def invoke_agent(
    request: Request,
    body: AgentInvokeRequest,
    user: User = Depends(require_auth),
):
    """Start one agent run and persist both sides of the conversation.

    With `stream=true` the response is `text/event-stream`; otherwise the
    completed assistant message is returned as JSON. Execution happens in a
    background task owned by the server, so closing the stream does not cancel
    the run. Set `AGENT_RUN_BACKGROUND=false` to fall back to the legacy
    request-bound path."""
    config = load_config()
    request_id = request_id_var.get()

    if not is_configured():
        return JSONResponse(
            status_code=503,
            content={
                "detail": NOT_CONFIGURED_DETAIL,
                "code": "agent_not_configured",
                "request_id": request_id,
            },
        )

    run_config = load_run_config()
    if not run_config.background:
        return await _legacy_invoke(request, body, user, config.history_messages, request_id)

    try:
        turn = await asyncio.to_thread(_create_background_turn, body, user, config.history_messages)
    except ActiveRunConflict as exc:
        return JSONResponse(
            status_code=409,
            content=_error_payload(
                "agent_run_active",
                "This conversation already has an active agent run.",
                request_id,
                exc.conversation_id,
                run_id=str(exc.run_id),
            ),
        )

    manager = _run_manager(request)
    manager.submit(turn.run_id, request_id)

    if not body.stream:
        return await _complete_background_run(request, user, turn, request_id)

    subscription = await manager.open_subscription(turn.run_id, 0)
    if subscription is None:
        return JSONResponse(
            status_code=404,
            content=_error_payload(
                "agent_run_not_found",
                "Agent run not found.",
                request_id,
                turn.conversation_uuid,
                run_id=str(turn.run_id),
            ),
        )
    meta = _meta_for_turn(turn, subscription)
    if subscription.initial_status in TERMINAL:
        message = await asyncio.to_thread(manager.repo.get_message, turn.assistant_id)
        if message is not None:
            meta["message"] = serialize_message(message).model_dump(mode="json")
    return _stream_response(_subscription_stream(manager, turn.run_id, subscription, meta, 0))


def _meta_for_turn(turn: BackgroundTurn, subscription: RunSubscription) -> dict[str, Any]:
    return {
        "run_id": str(turn.run_id),
        "conversation_id": str(turn.conversation_uuid),
        "conversation": turn.conversation.model_dump(mode="json"),
        "user_message": turn.user_message.model_dump(mode="json"),
        "assistant_message_id": str(turn.assistant_id),
        "status": subscription.initial_status,
        "last_seq": subscription.initial_last_seq,
        "run": {
            "id": str(turn.run_id),
            "conversation_id": str(turn.conversation_uuid),
            "status": subscription.initial_status,
            "last_seq": subscription.initial_last_seq,
            "assistant_message_id": str(turn.assistant_id),
        },
    }


def _stream_response(generator: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _subscription_stream(
    manager: AgentRunManager,
    run_id: uuid.UUID,
    subscription: RunSubscription,
    meta: dict[str, Any],
    after_seq: int,
) -> AsyncIterator[str]:
    """Replay persisted events, then forward live ones until `done`.

    Closing this generator only drops the subscription; the run keeps going.
    """
    last_emitted = max(0, after_seq)
    saw_terminal = False
    try:
        yield sse_event("meta", meta)
        for event_type, payload in subscription.history:
            if event_type in ("message.completed", "error"):
                saw_terminal = True
            seq = int(payload.get("seq") or 0)
            if seq and seq <= last_emitted:
                continue
            yield sse_event(event_type, payload, event_id=str(seq) if seq else None)
            if seq:
                last_emitted = seq

        if subscription.initial_status in TERMINAL:
            # The run status is committed just before its terminal event. If a
            # subscriber lands exactly in that window and still needs events,
            # wait briefly so a resume never ends on `done` with a missing
            # `message.completed`/`error`.
            wait_for_terminal = not saw_terminal and (
                last_emitted < subscription.initial_last_seq or subscription.initial_last_seq == 0
            )
            if wait_for_terminal:
                deadline = asyncio.get_running_loop().time() + 2.0
                while not saw_terminal:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    try:
                        event_type, payload = await asyncio.wait_for(subscription.queue.get(), remaining)
                    except TimeoutError:
                        break
                    if event_type == "done":
                        break
                    if event_type in ("message.completed", "error"):
                        saw_terminal = True
                    seq = int(payload.get("seq") or 0)
                    if seq and seq <= last_emitted:
                        continue
                    yield sse_event(event_type, payload, event_id=str(seq) if seq else None)
                    if seq:
                        last_emitted = seq
            yield sse_event(
                "done",
                {
                    "run_id": str(run_id),
                    "status": subscription.initial_status,
                    "last_seq": subscription.initial_last_seq,
                },
            )
            return

        while True:
            try:
                event_type, payload = await asyncio.wait_for(subscription.queue.get(), timeout=HEARTBEAT_SECONDS)
            except TimeoutError:
                yield sse_comment()
                continue

            seq = int(payload.get("seq") or 0)
            if event_type == "done":
                yield sse_event("done", payload)
                return
            if seq and seq <= last_emitted:
                continue
            yield sse_event(event_type, payload, event_id=str(seq) if seq else None)
            if seq:
                last_emitted = seq
    finally:
        manager.close_subscription(run_id, subscription)


async def _complete_background_run(
    request: Request,
    user: User,
    turn: BackgroundTurn,
    request_id: str,
):
    manager = _run_manager(request)
    repo = manager.repo
    run_config = manager.config
    await manager.wait(turn.run_id, timeout=run_config.max_seconds + 30)
    run = await asyncio.to_thread(repo.get_run, turn.run_id, user.id)
    if run is None:
        return JSONResponse(
            status_code=404,
            content=_error_payload(
                "agent_run_not_found",
                "Agent run not found.",
                request_id,
                turn.conversation_uuid,
                run_id=str(turn.run_id),
            ),
        )

    if run.status == "completed":
        message = await asyncio.to_thread(repo.get_message, run.assistant_message_id)
        if message is None:
            return JSONResponse(
                status_code=502,
                content=_error_payload(
                    "agent_protocol_error",
                    "Agent run completed without a persisted message.",
                    request_id,
                    turn.conversation_uuid,
                    run_id=str(run.id),
                ),
            )
        with SessionLocal() as db:
            conversation = get_owned_conversation(db, run.conversation_id, user.id)
            conversation_out = serialize_conversation(conversation)
        user_message = await asyncio.to_thread(repo.get_message, run.user_message_id)
        return AgentInvokeResponse(
            conversation=conversation_out,
            user_message=serialize_message(user_message) if user_message is not None else turn.user_message,
            message=serialize_message(message),
            run_id=str(run.id),
        )

    if run.status in ("queued", "running"):
        return JSONResponse(
            status_code=504,
            content=_error_payload(
                "agent_timeout",
                "Agent run is still executing; subscribe to its events to follow it.",
                request_id,
                run.conversation_id,
                run_id=str(run.id),
                status=run.status,
            ),
        )

    code = run.error_code or ("agent_interrupted" if run.status == "interrupted" else "agent_failed")
    status_code = {
        "agent_not_configured": 503,
        "agent_timeout": 504,
        "agent_cancelled": 409,
        "agent_interrupted": 503,
        "agent_run_active": 409,
    }.get(code, 502)
    detail = run.error_detail or "Agent run failed."
    return JSONResponse(
        status_code=status_code,
        content=_error_payload(
            code,
            detail,
            request_id,
            run.conversation_id,
            run_id=str(run.id),
            status=run.status,
        ),
    )


@router.get("/runs/{run_id}", summary="Get an agent run")
async def get_agent_run(
    run_id: str,
    request: Request,
    user: User = Depends(require_auth),
):
    repo = _run_manager(request).repo
    try:
        parsed_id = uuid.UUID(run_id)
    except (TypeError, ValueError):
        return JSONResponse(
            status_code=404,
            content={"detail": "Agent run not found.", "code": "agent_run_not_found", "run_id": run_id},
        )
    run = await asyncio.to_thread(repo.get_run, parsed_id, user.id)
    if run is None:
        return JSONResponse(
            status_code=404,
            content={"detail": "Agent run not found.", "code": "agent_run_not_found", "run_id": run_id},
        )
    return serialize_run(run)


@router.get("/runs/{run_id}/events", summary="Subscribe to an agent run")
async def stream_agent_run_events(
    run_id: str,
    request: Request,
    after_seq: int | None = Query(None),
    last_event_id: str | None = Header(None, alias="Last-Event-ID"),
    user: User = Depends(require_auth),
):
    """Replay events after an exclusive cursor, then stream live events.

    `Last-Event-ID` is accepted for SSE reconnects; an explicit `after_seq`
    wins when both are present."""
    request_id = request_id_var.get()
    if after_seq is not None and after_seq < 0:
        return JSONResponse(
            status_code=400,
            content={
                "detail": "after_seq must be greater than or equal to 0.",
                "code": "agent_invalid_cursor",
                "run_id": run_id,
                "request_id": request_id,
            },
        )
    try:
        parsed_id = uuid.UUID(run_id)
    except (TypeError, ValueError):
        return JSONResponse(
            status_code=404,
            content={"detail": "Agent run not found.", "code": "agent_run_not_found", "run_id": run_id},
        )

    manager = _run_manager(request)
    repo = manager.repo
    run = await asyncio.to_thread(repo.get_run, parsed_id, user.id)
    if run is None:
        return JSONResponse(
            status_code=404,
            content={"detail": "Agent run not found.", "code": "agent_run_not_found", "run_id": run_id},
        )

    cursor = after_seq if after_seq is not None else 0
    if after_seq is None and last_event_id:
        try:
            cursor = max(0, int(last_event_id))
        except (TypeError, ValueError):
            cursor = 0
    cursor = min(cursor, int(run.last_seq or 0))

    oldest = await asyncio.to_thread(repo.oldest_seq, parsed_id)
    expired = (oldest is not None and cursor < oldest - 1) or (oldest is None and int(run.last_seq or 0) > 0)
    if expired:
        return JSONResponse(
            status_code=410,
            content=_error_payload(
                "agent_events_expired",
                "Events for this run have been pruned.",
                request_id,
                run.conversation_id,
                run_id=str(run.id),
                oldest_seq=oldest,
                last_seq=int(run.last_seq or 0),
            ),
        )

    subscription = await manager.open_subscription(parsed_id, cursor)
    if subscription is None:
        return JSONResponse(
            status_code=404,
            content={"detail": "Agent run not found.", "code": "agent_run_not_found", "run_id": run_id},
        )

    context = await asyncio.to_thread(repo.get_context, parsed_id)
    meta = {
        "run_id": str(run.id),
        "conversation_id": str(run.conversation_id),
        "conversation": serialize_conversation(context.conversation).model_dump(mode="json")
        if context is not None and context.conversation is not None
        else None,
        "user_message": serialize_message(context.user_message).model_dump(mode="json")
        if context is not None and context.user_message is not None
        else None,
        "assistant_message_id": str(run.assistant_message_id) if run.assistant_message_id else None,
        "status": subscription.initial_status,
        "last_seq": subscription.initial_last_seq,
        "after_seq": cursor,
        "run": serialize_run(run).model_dump(mode="json"),
    }
    if (
        subscription.initial_status in TERMINAL
        and context is not None
        and context.assistant_message is not None
    ):
        meta["message"] = serialize_message(context.assistant_message).model_dump(mode="json")
    return _stream_response(_subscription_stream(manager, parsed_id, subscription, meta, cursor))


@router.post("/runs/{run_id}/cancel", summary="Cancel an agent run")
async def cancel_agent_run(
    run_id: str,
    request: Request,
    user: User = Depends(require_auth),
):
    """Explicit, idempotent cancellation. A terminal run returns its state."""
    request_id = request_id_var.get()
    try:
        parsed_id = uuid.UUID(run_id)
    except (TypeError, ValueError):
        return JSONResponse(
            status_code=404,
            content={"detail": "Agent run not found.", "code": "agent_run_not_found", "run_id": run_id},
        )
    manager = _run_manager(request)
    run = await manager.cancel(parsed_id, user.id)
    if run is None:
        return JSONResponse(
            status_code=404,
            content={
                "detail": "Agent run not found.",
                "code": "agent_run_not_found",
                "run_id": run_id,
                "request_id": request_id,
            },
        )
    return RunCancelOut(
        run_id=str(run.id),
        status=run.status,
        cancel_requested_at=run.cancel_requested_at,
    )


@router.post("/runs/{run_id}/answers", summary="Answer a question asked by an agent run")
async def answer_agent_run(
    run_id: str,
    body: AgentAnswerRequest,
    request: Request,
    user: User = Depends(require_auth),
):
    """Deliver one user answer to a run blocked in ``ask_user``."""

    request_id = request_id_var.get()
    try:
        parsed_id = uuid.UUID(run_id)
    except (TypeError, ValueError):
        return JSONResponse(
            status_code=404,
            content={
                "detail": "Agent run not found.",
                "code": "agent_run_not_found",
                "run_id": run_id,
                "request_id": request_id,
            },
        )

    manager = _run_manager(request)
    run = await asyncio.to_thread(manager.repo.get_run, parsed_id, user.id)
    if run is None:
        return JSONResponse(
            status_code=404,
            content=_error_payload(
                "agent_run_not_found",
                "Agent run not found.",
                request_id,
                run_id=run_id,
            ),
        )
    if run.status != "running":
        return JSONResponse(
            status_code=409,
            content=_error_payload(
                "agent_run_not_running",
                "This agent run is no longer running.",
                request_id,
                run.conversation_id,
                run_id=str(run.id),
            ),
        )

    try:
        accepted, code = await manager.answer_question(parsed_id, body.question_id, body.answer)
    except Exception:  # pragma: no cover - defensive
        logger.exception("Failed to deliver answer for run %s", run_id)
        accepted, code = False, "run_not_accepting_input"
    if not accepted:
        if code == "question_already_answered":
            return JSONResponse(
                status_code=409,
                content=_error_payload(
                    "agent_question_answered",
                    "This question has already been answered.",
                    request_id,
                    run.conversation_id,
                    run_id=str(run.id),
                    question_id=body.question_id,
                ),
            )
        if code in {"run_not_accepting_input", "question_not_pending"}:
            return JSONResponse(
                status_code=404,
                content=_error_payload(
                    "agent_question_not_found",
                    "This question is no longer waiting for an answer.",
                    request_id,
                    run.conversation_id,
                    run_id=str(run.id),
                    question_id=body.question_id,
                ),
            )
        return JSONResponse(
            status_code=409,
            content=_error_payload(
                "agent_question_unavailable",
                "The question is no longer available.",
                request_id,
                run.conversation_id,
                run_id=str(run.id),
                question_id=body.question_id,
            ),
        )

    return {"run_id": str(run.id), "question_id": body.question_id, "accepted": True}


# ---------------------------------------------------------------------------
# Legacy request-bound path (AGENT_RUN_BACKGROUND=false)


async def _legacy_invoke(
    request: Request,
    body: AgentInvokeRequest,
    user: User,
    history_limit: int,
    request_id: str,
):
    turn = _prepare_turn(body, user, history_limit)
    conversation_id = uuid.UUID(turn.conversation.id)
    assistant_id = uuid.uuid4()

    payload = {
        "conversation_id": str(conversation_id),
        "message_id": turn.user_message.id,
        "message": body.message,
        "history": turn.history,
        "user_id": str(user.id),
        "agent_id": body.agent_id or turn.conversation.agent_id,
        "run_id": body.run_id,
        "metadata": body.metadata,
    }

    if not body.stream:
        return await _complete_turn(user, turn, conversation_id, assistant_id, payload, request_id)

    generator = _event_stream(request, user.id, turn, conversation_id, assistant_id, payload, request_id)
    return _stream_response(generator)


async def _complete_turn(
    user: User,
    turn: PreparedTurn,
    conversation_id: uuid.UUID,
    assistant_id: uuid.UUID,
    payload: dict[str, Any],
    request_id: str,
):
    content_parts: list[str] = []
    metadata: dict[str, Any] = {}
    trace = ToolTrace()

    try:
        async for event in stream_agent(payload, request_id):
            if isinstance(event, AgentDelta):
                content_parts.append(event.delta)
            elif isinstance(event, AgentCompleted):
                metadata.update(event.metadata)
            elif isinstance(event, AgentToolCall):
                trace.start(
                    event.tool_call_id,
                    event.name,
                    iteration=event.iteration,
                    arguments=event.arguments,
                    raw_arguments=event.raw_arguments,
                    arguments_error=event.arguments_error,
                )
            elif isinstance(event, AgentToolResult):
                trace.finish(
                    event.tool_call_id,
                    event.name,
                    event.content,
                    is_error=event.is_error,
                    iteration=event.iteration,
                    status=event.status,
                    duration_ms=event.duration_ms,
                )
    except AgentLoopError as exc:
        trace.abort_open(f"run failed: {exc.detail}")
        attach_trace(trace, metadata)
        content = "".join(content_parts)
        if should_persist_reply(content, trace):
            await asyncio.to_thread(
                _save_assistant,
                conversation_id,
                user.id,
                assistant_id,
                content,
                "failed",
                metadata,
            )
        status_code = {
            "agent_timeout": 504,
            "agent_not_configured": 503,
        }.get(exc.code, 502)
        return JSONResponse(
            status_code=status_code,
            content=_error_payload(exc.code, exc.detail, request_id, conversation_id),
        )

    content = "".join(content_parts)
    trace.abort_open("run completed without a tool result")
    attach_trace(trace, metadata)
    if not content:
        if trace.has_entries():
            await asyncio.to_thread(
                _save_assistant,
                conversation_id,
                user.id,
                assistant_id,
                "",
                "failed",
                metadata,
            )
        return JSONResponse(
            status_code=502,
            content=_error_payload(
                "agent_protocol_error",
                "Agent executor produced no content.",
                request_id,
                conversation_id,
            ),
        )

    message = await asyncio.to_thread(
        _save_assistant,
        conversation_id,
        user.id,
        assistant_id,
        content,
        "completed",
        metadata,
    )
    if message is None:
        return JSONResponse(
            status_code=404,
            content=_error_payload("conversation_deleted", "Conversation not found.", request_id, conversation_id),
        )

    return AgentInvokeResponse(
        conversation=turn.conversation,
        user_message=turn.user_message,
        message=message,
    )


def _event_stream(
    request: Request,
    owner_id: uuid.UUID,
    turn: PreparedTurn,
    conversation_id: uuid.UUID,
    assistant_id: uuid.UUID,
    payload: dict[str, Any],
    request_id: str,
):
    meta = {
        "conversation": turn.conversation.model_dump(mode="json"),
        "user_message": turn.user_message.model_dump(mode="json"),
    }

    async def generator():
        content_parts: list[str] = []
        metadata: dict[str, Any] = {}
        trace = ToolTrace()
        persisted = False
        sequence = 0
        status = "failed"
        client_gone = False

        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

        async def pump() -> None:
            try:
                async for event in stream_agent(payload, request_id):
                    await queue.put(("event", event))
                await queue.put(("done", None))
            except AgentLoopError as exc:
                await queue.put(("error", exc))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                logging.exception("Agent executor crashed")
                await queue.put(("error", AgentLoopError("agent_upstream_error", str(exc))))

        task = asyncio.create_task(pump())

        try:
            yield sse_event("meta", meta)

            while True:
                if await request.is_disconnected():
                    status = "cancelled"
                    client_gone = True
                    break

                try:
                    kind, value = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                except TimeoutError:
                    yield sse_comment()
                    continue

                if kind == "event":
                    if isinstance(value, AgentDelta):
                        content_parts.append(value.delta)
                        yield sse_event(
                            "message.delta",
                            {
                                "conversation_id": str(conversation_id),
                                "message_id": str(assistant_id),
                                "delta": value.delta,
                            },
                            event_id=f"{assistant_id}:{sequence}",
                        )
                        sequence += 1
                    elif isinstance(value, AgentCompleted):
                        metadata.update(value.metadata)
                    elif isinstance(value, AgentToolCall):
                        entry = trace.start(
                            value.tool_call_id,
                            value.name,
                            iteration=value.iteration,
                            arguments=value.arguments,
                            raw_arguments=value.raw_arguments,
                            arguments_error=value.arguments_error,
                        )
                        yield sse_event(
                            "tool.call",
                            tool_call_payload(conversation_id, assistant_id, entry),
                        )
                    elif isinstance(value, AgentToolResult):
                        entry = trace.finish(
                            value.tool_call_id,
                            value.name,
                            value.content,
                            is_error=value.is_error,
                            iteration=value.iteration,
                            status=value.status,
                            duration_ms=value.duration_ms,
                        )
                        yield sse_event(
                            "tool.result",
                            tool_result_payload(conversation_id, assistant_id, entry),
                        )
                elif kind == "error":
                    status = "failed"
                    for entry in trace.abort_open(f"run failed: {value.detail}"):
                        yield sse_event(
                            "tool.result",
                            tool_result_payload(conversation_id, assistant_id, entry),
                        )
                    attach_trace(trace, metadata)
                    content = "".join(content_parts)
                    if should_persist_reply(content, trace):
                        saved = await asyncio.to_thread(
                            _save_assistant,
                            conversation_id,
                            owner_id,
                            assistant_id,
                            content,
                            "failed",
                            metadata,
                        )
                        persisted = saved is not None
                    yield sse_event(
                        "error",
                        {
                            "code": value.code,
                            "detail": value.detail,
                            "request_id": request_id,
                            "conversation_id": str(conversation_id),
                        },
                    )
                    break
                elif kind == "done":
                    status = "completed"
                    break

            if client_gone:
                return

            if status == "completed" and not persisted:
                trace.abort_open("run completed without a tool result")
                attach_trace(trace, metadata)
                content = "".join(content_parts)
                if should_persist_reply(content, trace):
                    saved = await asyncio.to_thread(
                        _save_assistant,
                        conversation_id,
                        owner_id,
                        assistant_id,
                        content,
                        "completed",
                        metadata,
                    )
                    if saved is None:
                        yield sse_event(
                            "error",
                            {
                                "code": "conversation_deleted",
                                "detail": "Conversation not found.",
                                "request_id": request_id,
                                "conversation_id": str(conversation_id),
                            },
                        )
                    else:
                        persisted = True
                        yield sse_event(
                            "message.completed",
                            {
                                "conversation_id": str(conversation_id),
                                "message": saved.model_dump(mode="json"),
                            },
                        )

            yield sse_event("done", {"conversation_id": str(conversation_id)})
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            if not persisted:
                trace.abort_open(f"run {'failed' if status == 'failed' else 'cancelled'}")
                attach_trace(trace, metadata)
                content = "".join(content_parts)
                if should_persist_reply(content, trace):
                    final_status = "completed" if status == "completed" else ("failed" if status == "failed" else "cancelled")
                    write = asyncio.to_thread(
                        _save_assistant,
                        conversation_id,
                        owner_id,
                        assistant_id,
                        content,
                        final_status,
                        metadata,
                    )
                    try:
                        await asyncio.shield(write)
                    except asyncio.CancelledError:
                        pass
                    except Exception:  # pragma: no cover - defensive
                        logging.exception("Failed to persist partial assistant message")

    return generator()
