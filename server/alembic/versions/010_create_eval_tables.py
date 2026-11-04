"""Create eval_goldens and eval_runs tables

Revision ID: 010
Revises: 009
Create Date: 2026-11-05

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "010"
down_revision: Union[str, None] = "009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "eval_goldens",
        sa.Column("eval_case_id", sa.String(255), primary_key=True),
        sa.Column("input", sa.Text(), nullable=False),
        sa.Column("expected_output", sa.Text(), nullable=True),
        sa.Column(
            "expected_tools",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "context",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("jsonb_typeof(expected_tools) = 'array'", name="ck_eval_goldens_tools"),
        sa.CheckConstraint("jsonb_typeof(context) = 'array'", name="ck_eval_goldens_context"),
    )

    op.create_table(
        "eval_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("eval_case_id", sa.String(255), nullable=False),
        sa.Column(
            "golden_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("golden_hash", sa.String(64), nullable=False),
        sa.Column("use_llm_judge", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column(
            "metrics",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "warnings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("raw_result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("overall_success", sa.Boolean(), nullable=True),
        sa.Column("judge_model", sa.String(128), nullable=True),
        sa.Column("token_cost", sa.Float(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("worker_id", sa.Text(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'cancelled')",
            name="ck_eval_runs_status",
        ),
    )
    op.create_index(
        "uq_eval_runs_active_target",
        "eval_runs",
        ["run_id", "eval_case_id", "use_llm_judge"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_index("ix_eval_runs_status_heartbeat", "eval_runs", ["status", "heartbeat_at"])
    op.create_index("ix_eval_runs_run_created", "eval_runs", ["run_id", "created_at"])
    op.create_index("ix_eval_runs_created_at", "eval_runs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_eval_runs_created_at", table_name="eval_runs")
    op.drop_index("ix_eval_runs_run_created", table_name="eval_runs")
    op.drop_index("ix_eval_runs_status_heartbeat", table_name="eval_runs")
    op.drop_index("uq_eval_runs_active_target", table_name="eval_runs")
    op.drop_table("eval_runs")
    op.drop_table("eval_goldens")
