from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from agent_schemas import (
    ConversationCreate,
    ConversationDetailOut,
    ConversationMessageOut,
    ConversationOut,
    ConversationUpdate,
)
from agent_store import (
    DEFAULT_TITLE,
    create_conversation,
    get_owned_conversation,
    list_conversations,
    list_messages,
    serialize_conversation,
    serialize_message,
)
from auth import require_auth
from db import get_db
from models import User
from schemas import MessageResponse

router = APIRouter(prefix="/conversations", tags=["conversations"])

MAX_DETAIL_MESSAGES = 500


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@router.get("", response_model=list[ConversationOut])
def get_conversations(
    user: User = Depends(require_auth),
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    return [serialize_conversation(item) for item in list_conversations(db, user.id, limit=limit, offset=offset)]


@router.post("", response_model=ConversationOut, status_code=201)
def post_conversation(
    body: ConversationCreate,
    user: User = Depends(require_auth),
    db: Session = Depends(get_db),
):
    conversation = create_conversation(
        db,
        user.id,
        title=body.title or DEFAULT_TITLE,
        agent_id=body.agent_id,
        metadata=body.metadata,
    )
    return serialize_conversation(conversation)


@router.get("/{conversation_id}", response_model=ConversationDetailOut)
def get_conversation(
    conversation_id: str,
    user: User = Depends(require_auth),
    db: Session = Depends(get_db),
):
    conversation = get_owned_conversation(db, conversation_id, user.id)
    messages = list_messages(db, conversation.id, limit=MAX_DETAIL_MESSAGES)
    detail = serialize_conversation(conversation)
    return ConversationDetailOut(
        **detail.model_dump(),
        messages=[serialize_message(message) for message in messages],
    )


@router.put("/{conversation_id}", response_model=ConversationOut)
def put_conversation(
    conversation_id: str,
    body: ConversationUpdate,
    user: User = Depends(require_auth),
    db: Session = Depends(get_db),
):
    conversation = get_owned_conversation(db, conversation_id, user.id)
    conversation.title = body.title
    conversation.updated_at = _utcnow()
    db.commit()
    db.refresh(conversation)
    return serialize_conversation(conversation)


@router.delete("/{conversation_id}", response_model=MessageResponse)
def delete_conversation(
    conversation_id: str,
    user: User = Depends(require_auth),
    db: Session = Depends(get_db),
):
    conversation = get_owned_conversation(db, conversation_id, user.id)
    db.delete(conversation)
    db.commit()
    return MessageResponse(message="Conversation deleted.")


@router.get("/{conversation_id}/messages", response_model=list[ConversationMessageOut])
def get_conversation_messages(
    conversation_id: str,
    user: User = Depends(require_auth),
    db: Session = Depends(get_db),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    conversation = get_owned_conversation(db, conversation_id, user.id)
    messages = list_messages(db, conversation.id, limit=limit, offset=offset)
    return [serialize_message(message) for message in messages]
