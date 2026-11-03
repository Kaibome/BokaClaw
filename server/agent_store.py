import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from agent_schemas import ConversationMessageOut, ConversationOut
from models import Conversation, ConversationMessage


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


DEFAULT_TITLE = "New conversation"
MAX_TITLE_LENGTH = 80
MAX_HISTORY_CHARACTERS = 24_000


def derive_title(message: str) -> str:
    """Collapse whitespace and truncate to a short conversation title."""
    collapsed = " ".join(message.split())
    if len(collapsed) <= MAX_TITLE_LENGTH:
        return collapsed or DEFAULT_TITLE
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


def serialize_message(message: ConversationMessage) -> ConversationMessageOut:
    return ConversationMessageOut(
        id=str(message.id),
        conversation_id=str(message.conversation_id),
        role=message.role,
        content=message.content,
        status=message.status,
        metadata=message.meta or {},
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


def build_history(
    db: Session,
    conversation_id: uuid.UUID,
    *,
    limit: int = 40,
    character_budget: int = MAX_HISTORY_CHARACTERS,
) -> list[dict[str, str]]:
    """Return the most recent completed user/assistant turns, oldest first.

    History is capped by both message count and an approximate character
    budget so a long thread cannot blow up the upstream prompt."""
    recent = list(
        db.scalars(
            select(ConversationMessage)
            .where(
                ConversationMessage.conversation_id == conversation_id,
                ConversationMessage.role.in_(("user", "assistant")),
                ConversationMessage.status == "completed",
                ConversationMessage.content != "",
            )
            .order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc())
            .limit(limit)
        ).all()
    )

    selected: list[ConversationMessage] = []
    characters = 0
    for message in recent:
        characters += len(message.content)
        if selected and characters > character_budget:
            break
        selected.append(message)

    selected.reverse()
    return [{"role": message.role, "content": message.content} for message in selected]
