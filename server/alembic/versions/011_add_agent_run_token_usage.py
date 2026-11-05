"""Add per-run model token usage to agent_runs

Revision ID: 011
Revises: 010
Create Date: 2026-11-05

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "011"
down_revision: Union[str, None] = "010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("input_tokens", sa.BigInteger(), nullable=True))
    op.add_column("agent_runs", sa.Column("output_tokens", sa.BigInteger(), nullable=True))
    op.add_column("agent_runs", sa.Column("total_tokens", sa.BigInteger(), nullable=True))
    op.add_column(
        "agent_runs",
        sa.Column("model_calls", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "agent_runs",
        sa.Column("unknown_usage_calls", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "agent_runs",
        sa.Column(
            "usage_breakdown",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )

    # Legacy runs stored the SDK aggregate on partial_metadata.usage. Lift it
    # into the new columns so old runs report an aggregate instead of nothing.
    op.execute(
        """
        UPDATE agent_runs
        SET input_tokens = COALESCE((partial_metadata->'usage'->>'prompt_tokens')::bigint, 0),
            output_tokens = COALESCE((partial_metadata->'usage'->>'completion_tokens')::bigint, 0),
            total_tokens = COALESCE(
                (partial_metadata->'usage'->>'total_tokens')::bigint,
                COALESCE((partial_metadata->'usage'->>'prompt_tokens')::bigint, 0)
                + COALESCE((partial_metadata->'usage'->>'completion_tokens')::bigint, 0)
            ),
            model_calls = 1,
            unknown_usage_calls = 0,
            usage_breakdown = jsonb_build_object(
                'coverage', 'aggregate',
                'transport', 'legacy',
                'model_calls', 1,
                'unknown_calls', 0,
                'sources', jsonb_build_object(
                    'agent_loop', jsonb_build_object(
                        'input_tokens', COALESCE((partial_metadata->'usage'->>'prompt_tokens')::bigint, 0),
                        'output_tokens', COALESCE((partial_metadata->'usage'->>'completion_tokens')::bigint, 0),
                        'total_tokens', COALESCE(
                            (partial_metadata->'usage'->>'total_tokens')::bigint,
                            COALESCE((partial_metadata->'usage'->>'prompt_tokens')::bigint, 0)
                            + COALESCE((partial_metadata->'usage'->>'completion_tokens')::bigint, 0)
                        ),
                        'calls', 1,
                        'unknown_calls', 0
                    )
                )
            )
        WHERE status IN ('completed', 'failed', 'cancelled', 'interrupted')
          AND partial_metadata->'usage' ? 'prompt_tokens'
        """
    )


def downgrade() -> None:
    op.drop_column("agent_runs", "usage_breakdown")
    op.drop_column("agent_runs", "unknown_usage_calls")
    op.drop_column("agent_runs", "model_calls")
    op.drop_column("agent_runs", "total_tokens")
    op.drop_column("agent_runs", "output_tokens")
    op.drop_column("agent_runs", "input_tokens")
