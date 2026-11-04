"""Create agent_run_spans table

Revision ID: 009
Revises: 008
Create Date: 2026-11-04

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "009"
down_revision: Union[str, None] = "008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_run_spans",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("span_id", sa.String(128), nullable=False),
        sa.Column("parent_span_id", sa.String(128), nullable=True),
        sa.Column("span_type", sa.String(16), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="running"),
        sa.Column("iteration", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column(
            "input",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "output",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "attributes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.UniqueConstraint("run_id", "span_id", name="uq_agent_run_spans_run_span"),
        sa.CheckConstraint(
            "span_type IN ('agent', 'llm', 'tool')",
            name="ck_agent_run_spans_type",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'failed', 'aborted', 'interrupted')",
            name="ck_agent_run_spans_status",
        ),
    )
    op.create_index("ix_agent_run_spans_run_started", "agent_run_spans", ["run_id", "started_at"])


def downgrade() -> None:
    op.drop_index("ix_agent_run_spans_run_started", table_name="agent_run_spans")
    op.drop_table("agent_run_spans")
