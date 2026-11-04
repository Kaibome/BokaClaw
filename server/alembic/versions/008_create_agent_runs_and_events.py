"""Create agent_runs and agent_run_events tables

Revision ID: 008
Revises: 007
Create Date: 2026-11-04

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "008"
down_revision: Union[str, None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.Uuid(),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_message_id",
            sa.Uuid(),
            sa.ForeignKey("conversation_messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Deliberately no FK: the assistant message row is written at terminal
        # state, after this run row already exists.
        sa.Column("assistant_message_id", sa.Uuid(), nullable=True),
        sa.Column(
            "request",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("client_run_id", sa.String(255), nullable=True),
        sa.Column("agent_id", sa.String(255), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("partial_content", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "partial_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("last_seq", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("worker_id", sa.Text(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'cancelled', 'interrupted')",
            name="ck_agent_runs_status",
        ),
    )
    op.create_index(
        "uq_agent_runs_active_conversation",
        "agent_runs",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_index(
        "ix_agent_runs_created_by_created",
        "agent_runs",
        ["created_by", "created_at"],
    )
    op.create_index(
        "ix_agent_runs_status_heartbeat",
        "agent_runs",
        ["status", "heartbeat_at"],
    )

    op.create_table(
        "agent_run_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("run_id", "seq", name="uq_agent_run_events_run_seq"),
        sa.CheckConstraint(
            "event_type IN ('message.delta', 'tool.call', 'tool.result', 'message.completed', 'error')",
            name="ck_agent_run_events_type",
        ),
    )
    op.create_index("ix_agent_run_events_run_seq", "agent_run_events", ["run_id", "seq"])


def downgrade() -> None:
    op.drop_index("ix_agent_run_events_run_seq", table_name="agent_run_events")
    op.drop_table("agent_run_events")
    op.drop_index("ix_agent_runs_status_heartbeat", table_name="agent_runs")
    op.drop_index("ix_agent_runs_created_by_created", table_name="agent_runs")
    op.drop_index("uq_agent_runs_active_conversation", table_name="agent_runs")
    op.drop_table("agent_runs")
