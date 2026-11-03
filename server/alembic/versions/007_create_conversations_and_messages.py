"""Create conversations and conversation_messages tables

Revision ID: 007
Revises: 006
Create Date: 2026-11-03

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("agent_id", sa.String(255), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_conversations_created_by_updated",
        "conversations",
        ["created_by", "updated_at"],
    )

    op.create_table(
        "conversation_messages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.Uuid(),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(20), nullable=False, server_default="completed"),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "role IN ('user', 'assistant', 'system', 'tool')",
            name="ck_conversation_messages_role",
        ),
        sa.CheckConstraint(
            "status IN ('completed', 'cancelled', 'failed')",
            name="ck_conversation_messages_status",
        ),
    )
    op.create_index(
        "ix_conversation_messages_conversation_created",
        "conversation_messages",
        ["conversation_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_conversation_messages_conversation_created",
        table_name="conversation_messages",
    )
    op.drop_table("conversation_messages")
    op.drop_index("ix_conversations_created_by_updated", table_name="conversations")
    op.drop_table("conversations")
