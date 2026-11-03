import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

ChatRole = Literal["user", "assistant", "system", "tool"]
MessageStatus = Literal["completed", "cancelled", "failed"]


class ConversationCreate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=200)
    agent_id: str | None = Field(None, max_length=255)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("title")
    @classmethod
    def _strip_title(cls, value: str | None) -> str | None:
        if value is None:
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("title must not be blank")
        return stripped


class ConversationUpdate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)

    @field_validator("title")
    @classmethod
    def _strip_title(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("title must not be blank")
        return stripped


class ConversationOut(BaseModel):
    id: str
    title: str
    agent_id: str | None
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class ConversationMessageOut(BaseModel):
    id: str
    conversation_id: str
    role: ChatRole
    content: str
    status: MessageStatus
    metadata: dict[str, Any]
    created_at: datetime


class ConversationDetailOut(ConversationOut):
    messages: list[ConversationMessageOut]


class AgentInvokeRequest(BaseModel):
    conversation_id: uuid.UUID | None = None
    message: str = Field(..., min_length=1, max_length=32_000)
    agent_id: str | None = Field(None, max_length=255)
    run_id: str | None = Field(None, max_length=255)
    metadata: dict[str, Any] = Field(default_factory=dict)
    stream: bool = False

    @field_validator("message")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("message must not be blank")
        return stripped


class AgentInvokeResponse(BaseModel):
    conversation: ConversationOut
    user_message: ConversationMessageOut
    message: ConversationMessageOut
