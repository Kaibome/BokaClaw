import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.agent.schemas import AttachmentOut, ConversationMessageOut, ConversationOut
from app.models import Conversation, ConversationMessage


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


DEFAULT_TITLE = "New conversation"
MAX_TITLE_LENGTH = 80
MAX_HISTORY_CHARACTERS = 24_000
DEFAULT_HISTORY_TOOL_RESULT_MAX_CHARS = 2_000
DEFAULT_HISTORY_TOOL_ARGUMENT_MAX_CHARS = 1_000
DEFAULT_HISTORY_MAX_IMAGES = 4
HISTORY_IMAGE_CHAR_COST = 4_000
HISTORY_TRUNCATION_MARKER = "\n... [truncated] ...\n"
_HISTORY_TOOL_TRACE_ENV = "AGENT_LOOP_HISTORY_TOOL_TRACE"
_HISTORY_TOOL_RESULT_MAX_CHARS_ENV = "AGENT_LOOP_HISTORY_TOOL_RESULT_MAX_CHARS"
_HISTORY_TOOL_ARGUMENT_MAX_CHARS_ENV = "AGENT_LOOP_HISTORY_TOOL_ARGUMENT_MAX_CHARS"
_HISTORY_MAX_IMAGES_ENV = "AGENT_HISTORY_MAX_IMAGES"
_FALSE_VALUES = {"0", "false", "no", "off"}


def _history_tool_trace_enabled() -> bool:
    raw = (os.environ.get(_HISTORY_TOOL_TRACE_ENV) or "").strip().lower()
    return raw not in _FALSE_VALUES


def _env_bounded_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(0, value)


def derive_title(message: str, fallback: str = DEFAULT_TITLE) -> str:
    """Collapse whitespace and truncate to a short conversation title."""
    collapsed = " ".join(message.split())
    if len(collapsed) <= MAX_TITLE_LENGTH:
        return collapsed or fallback
    return f"{collapsed[:MAX_TITLE_LENGTH]}..."


def parse_conversation_id(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        raise HTTPException(status_code=404, detail="Conversation not found.")


def get_owned_conversation(db: Session, conversation_id: uuid.UUID | str, owner_id: uuid.UUID) -> Conversation:
    """Load a conversation by id, scoped to its owner. Missing and foreign
    conversations both return 404 so callers cannot probe for existence."""
    parsed = parse_conversation_id(str(conversation_id))
    conversation = db.scalar(select(Conversation).where(Conversation.id == parsed, Conversation.created_by == owner_id))
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return conversation


def serialize_conversation(conversation: Conversation) -> ConversationOut:
    return ConversationOut(
        id=str(conversation.id),
        title=conversation.title,
        agent_id=conversation.agent_id,
        metadata=conversation.meta or {},
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
    )


def _attachment_descriptors(meta: Any) -> list[dict[str, Any]]:
    """Return well-formed attachment descriptors from a message metadata object."""

    if not isinstance(meta, dict):
        return []
    raw = meta.get("attachments")
    if not isinstance(raw, list):
        return []
    descriptors: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        attachment_id = item.get("id")
        if not isinstance(attachment_id, str) or not attachment_id:
            continue
        try:
            descriptor = {
                "id": attachment_id,
                "name": str(item.get("name") or attachment_id)[:120],
                "mime_type": str(item.get("mime_type") or "image/jpeg"),
                "bytes": max(0, int(item.get("bytes") or 0)),
                "width": max(0, int(item.get("width") or 0)),
                "height": max(0, int(item.get("height") or 0)),
            }
        except (TypeError, ValueError):
            continue
        descriptors.append(descriptor)
    return descriptors


def serialize_message(message: ConversationMessage) -> ConversationMessageOut:
    attachments = [AttachmentOut(**descriptor) for descriptor in _attachment_descriptors(message.meta)]
    return ConversationMessageOut(
        id=str(message.id),
        conversation_id=str(message.conversation_id),
        role=message.role,
        content=message.content,
        status=message.status,
        metadata=message.meta or {},
        attachments=attachments,
        created_at=message.created_at,
    )


def create_conversation(
    db: Session,
    owner_id: uuid.UUID,
    *,
    title: str = DEFAULT_TITLE,
    agent_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Conversation:
    conversation = Conversation(
        created_by=owner_id,
        title=title,
        agent_id=agent_id,
        meta=metadata or {},
    )
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation


def append_message(
    db: Session,
    conversation: Conversation,
    *,
    role: str,
    content: str,
    status: str = "completed",
    metadata: dict[str, Any] | None = None,
    message_id: uuid.UUID | None = None,
) -> ConversationMessage:
    message = ConversationMessage(
        id=message_id or uuid.uuid4(),
        conversation_id=conversation.id,
        role=role,
        content=content,
        status=status,
        meta=metadata or {},
    )
    db.add(message)
    conversation.updated_at = _utcnow()
    db.commit()
    db.refresh(message)
    return message


def list_conversations(db: Session, owner_id: uuid.UUID, limit: int = 50, offset: int = 0) -> list[Conversation]:
    return list(
        db.scalars(
            select(Conversation)
            .where(Conversation.created_by == owner_id)
            .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
            .limit(limit)
            .offset(offset)
        ).all()
    )


def list_messages(
    db: Session,
    conversation_id: uuid.UUID,
    limit: int = 100,
    offset: int = 0,
) -> list[ConversationMessage]:
    return list(
        db.scalars(
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation_id)
            .order_by(ConversationMessage.created_at.asc(), ConversationMessage.id.asc())
            .limit(limit)
            .offset(offset)
        ).all()
    )


def _cap_history_text(text: str, max_chars: int) -> str:
    """Keep the head and tail of text that exceeds the history budget."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    if max_chars <= len(HISTORY_TRUNCATION_MARKER):
        return HISTORY_TRUNCATION_MARKER[:max_chars]

    preserved = max_chars - len(HISTORY_TRUNCATION_MARKER)
    head_len = preserved // 2
    tail_len = preserved - head_len
    tail = text[-tail_len:] if tail_len else ""
    return f"{text[:head_len]}{HISTORY_TRUNCATION_MARKER}{tail}"


def _tool_call_arguments(entry: dict[str, Any], max_chars: int) -> str:
    """Return valid JSON arguments from a persisted trace entry."""
    raw = entry.get("raw_arguments")
    if isinstance(raw, str) and raw.strip():
        try:
            json.loads(raw)
        except (TypeError, ValueError):
            pass
        else:
            return _cap_history_text(raw, max_chars)

    arguments = entry.get("arguments")
    if isinstance(arguments, dict):
        return _cap_history_text(json.dumps(arguments, ensure_ascii=False), max_chars)
    return "{}"


def _tool_result_content(entry: dict[str, Any], max_chars: int) -> str:
    content = entry.get("content")
    if not isinstance(content, str) or not content:
        content = json.dumps(
            {"status": entry.get("status") or "completed", "truncated": bool(entry.get("truncated"))},
            ensure_ascii=False,
        )
    return _cap_history_text(content, max_chars)


def _render_history_message(
    message: ConversationMessage,
    *,
    include_tool_trace: bool,
    tool_result_max_chars: int,
    tool_argument_max_chars: int,
    keep_image_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Render one stored message as the wire-format messages it represents.

    An assistant turn that used tools becomes ``assistant(tool_calls)`` followed
    by one ``tool`` message per call and, when the run produced an answer, a
    final assistant message. The model therefore sees what earlier runs already
    fetched instead of starting from a blank slate.

    ``keep_image_ids`` bounds how many historical images reach the model view;
    images not in the set become a text placeholder so the turn is still
    understandable without another multi-megabyte request.
    """
    content = message.content or ""
    if message.role == "user":
        if message.status != "completed":
            return []
        descriptors = _attachment_descriptors(message.meta)
        kept = [
            descriptor
            for descriptor in descriptors
            if keep_image_ids is None or descriptor["id"] in keep_image_ids
        ]
        dropped = len(descriptors) - len(kept)
        if dropped and content:
            content = f"{content}\n[Earlier image omitted]"
        elif dropped:
            content = "[Earlier image omitted]"
        if not content and not kept:
            return []
        rendered: dict[str, Any] = {"role": "user", "content": content}
        if kept:
            rendered["attachments"] = kept
        return [rendered]
    if message.role != "assistant":
        return []

    entries = message.meta.get("tool_trace") if isinstance(message.meta, dict) else None
    if not include_tool_trace or not isinstance(entries, list):
        entries = []

    tool_calls: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("status") == "running":
            continue
        tool_call_id = entry.get("tool_call_id")
        name = entry.get("name")
        if not isinstance(tool_call_id, str) or not tool_call_id or not isinstance(name, str) or not name:
            continue
        tool_calls.append(
            {
                "id": tool_call_id,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": _tool_call_arguments(entry, tool_argument_max_chars),
                },
            }
        )
        tool_results.append(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": _tool_result_content(entry, tool_result_max_chars),
            }
        )

    if tool_calls:
        block: list[dict[str, Any]] = [
            {"role": "assistant", "content": "", "tool_calls": tool_calls},
            *tool_results,
        ]
        if content and message.status == "completed":
            block.append({"role": "assistant", "content": content})
        return block

    if message.status != "completed" or not content:
        return []
    return [{"role": "assistant", "content": content}]


def _history_block_cost(block: list[dict[str, Any]]) -> int:
    cost = 0
    for message in block:
        content = message.get("content")
        if isinstance(content, str):
            cost += len(content)
        attachments = message.get("attachments")
        if isinstance(attachments, list):
            cost += len(attachments) * HISTORY_IMAGE_CHAR_COST
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            cost += len(function.get("name") or "") + len(function.get("arguments") or "")
        cost += 8
    return cost


def build_history(
    db: Session,
    conversation_id: uuid.UUID,
    *,
    limit: int = 40,
    character_budget: int = MAX_HISTORY_CHARACTERS,
    include_tool_trace: bool | None = None,
    tool_result_max_chars: int | None = None,
    tool_argument_max_chars: int | None = None,
) -> list[dict[str, Any]]:
    """Return prior turns in OpenAI wire format, oldest first.

    Completed user/assistant text is included as before. Assistant messages
    also carry the tool trace persisted for their run, which is rendered as
    ``assistant(tool_calls)`` + ``tool`` results so the next run can build on
    pages and commands it already executed. Failed or cancelled runs are
    included only through that trace, never as a fabricated answer.

    History is capped by message count and an approximate character budget so a
    long thread cannot blow up the upstream prompt. Tool results and arguments
    are capped individually before the budget is applied.
    """
    if include_tool_trace is None:
        include_tool_trace = _history_tool_trace_enabled()
    if tool_result_max_chars is None:
        tool_result_max_chars = _env_bounded_int(
            _HISTORY_TOOL_RESULT_MAX_CHARS_ENV,
            DEFAULT_HISTORY_TOOL_RESULT_MAX_CHARS,
        )
    if tool_argument_max_chars is None:
        tool_argument_max_chars = _env_bounded_int(
            _HISTORY_TOOL_ARGUMENT_MAX_CHARS_ENV,
            DEFAULT_HISTORY_TOOL_ARGUMENT_MAX_CHARS,
        )

    recent = list(
        db.scalars(
            select(ConversationMessage)
            .where(
                ConversationMessage.conversation_id == conversation_id,
                ConversationMessage.role.in_(("user", "assistant")),
                or_(
                    ConversationMessage.status == "completed",
                    and_(
                        ConversationMessage.role == "assistant",
                        ConversationMessage.meta.has_key("tool_trace"),
                    ),
                ),
            )
            .order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc())
            .limit(limit)
        ).all()
    )

    max_images = _env_bounded_int(_HISTORY_MAX_IMAGES_ENV, DEFAULT_HISTORY_MAX_IMAGES)
    images_kept = 0

    def render(message: ConversationMessage) -> list[dict[str, Any]]:
        nonlocal images_kept
        keep_image_ids: set[str] | None = None
        descriptors = _attachment_descriptors(message.meta)
        if message.role == "user" and descriptors:
            remaining = max(0, max_images - images_kept)
            keep = descriptors[:remaining]
            keep_image_ids = {descriptor["id"] for descriptor in keep}
            images_kept += len(keep)
        return _render_history_message(
            message,
            include_tool_trace=include_tool_trace,
            tool_result_max_chars=tool_result_max_chars,
            tool_argument_max_chars=tool_argument_max_chars,
            keep_image_ids=keep_image_ids,
        )

    selected: list[list[dict[str, Any]]] = []
    characters = 0
    index = 0
    while index < len(recent):
        message = recent[index]
        block = render(message)
        if not block:
            index += 1
            continue

        cost = _history_block_cost(block)
        paired: list[dict[str, Any]] | None = None
        if message.role == "assistant" and index + 1 < len(recent) and recent[index + 1].role == "user":
            user_block = render(recent[index + 1])
            if user_block:
                paired = user_block
                cost += _history_block_cost(user_block)

        if selected and characters + cost > character_budget:
            break

        selected.append(block)
        characters += cost
        if paired is not None:
            selected.append(paired)
            index += 2
        else:
            index += 1

    selected.reverse()
    return [message for block in selected for message in block]
