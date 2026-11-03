import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select

from agent_loop import (
    AgentCompleted,
    AgentDelta,
    AgentLoopError,
    is_configured,
    load_config,
    sse_comment,
    sse_event,
    stream_agent,
)
from agent_schemas import AgentInvokeRequest, AgentInvokeResponse, ConversationMessageOut, ConversationOut
from agent_store import (
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
from auth import require_auth
from db import SessionLocal
from errors import request_id_var
from models import Conversation, User
from rate_limit import limiter

router = APIRouter(prefix="/agent", tags=["agent"])

NOT_CONFIGURED_DETAIL = (
    "Agent execution is not configured. Set AGENT_MODEL and AGENT_API_KEY (or OPENAI_API_KEY), "
    "or set AGENT_LOOP_URL for an external executor."
)
HEARTBEAT_SECONDS = 15


@dataclass
class PreparedTurn:
    conversation: ConversationOut
    user_message: ConversationMessageOut
    history: list[dict[str, str]]


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


def _error_payload(code: str, detail: str, request_id: str, conversation_id: uuid.UUID) -> dict[str, Any]:
    return {
        "detail": detail,
        "code": code,
        "request_id": request_id,
        "conversation_id": str(conversation_id),
    }


@router.post("", summary="Invoke the agent for one turn")
@limiter.limit("30/minute")
async def invoke_agent(
    request: Request,
    body: AgentInvokeRequest,
    user: User = Depends(require_auth),
):
    """Run one agent turn and persist both sides of the conversation.

    With `stream=true` the response is `text/event-stream`; otherwise the
    completed assistant message is returned as JSON. The turn runs in-process
    with the `bokaclaw-agent` runtime unless `AGENT_LOOP_URL` points at an
    external executor."""
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

    turn = _prepare_turn(body, user, config.history_messages)
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
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


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

    try:
        async for event in stream_agent(payload, request_id):
            if isinstance(event, AgentDelta):
                content_parts.append(event.delta)
            elif isinstance(event, AgentCompleted):
                metadata.update(event.metadata)
    except AgentLoopError as exc:
        content = "".join(content_parts)
        if content:
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
    if not content:
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
                elif kind == "error":
                    status = "failed"
                    content = "".join(content_parts)
                    if content:
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
                # The partial reply is persisted in the finally block below;
                # writing to a disconnected socket would only raise.
                return

            if status == "completed" and not persisted:
                content = "".join(content_parts)
                if content:
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
            if not persisted and content_parts:
                # Shield the write: when the client aborts, the generator is
                # cancelled and a bare await here would be cancelled too.
                write = asyncio.to_thread(
                    _save_assistant,
                    conversation_id,
                    owner_id,
                    assistant_id,
                    "".join(content_parts),
                    "cancelled" if status != "failed" else "failed",
                    metadata,
                )
                try:
                    await asyncio.shield(write)
                except asyncio.CancelledError:
                    pass
                except Exception:  # pragma: no cover - defensive
                    logging.exception("Failed to persist partial assistant message")

    return generator()
