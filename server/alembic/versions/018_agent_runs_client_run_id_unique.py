"""Make client_run_id a per-user idempotency key

Revision ID: 018
Revises: 017
Create Date: 2026-11-16

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "018"
down_revision: Union[str, None] = "017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing duplicates would make the unique index fail. Keep the earliest
    # run per (user, client_run_id) and null out the key on the rest; those runs
    # keep working, they just stop being replay targets.
    op.execute(
        """
        UPDATE agent_runs AS runs
        SET client_run_id = NULL
        FROM (
            SELECT id, row_number() OVER (
                PARTITION BY created_by, client_run_id
                ORDER BY created_at, id
            ) AS position
            FROM agent_runs
            WHERE client_run_id IS NOT NULL
        ) AS ranked
        WHERE runs.id = ranked.id AND ranked.position > 1
        """
    )
    op.create_index(
        "uq_agent_runs_client_run_id",
        "agent_runs",
        ["created_by", "client_run_id"],
        unique=True,
        postgresql_where=sa.text("client_run_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_agent_runs_client_run_id", table_name="agent_runs")
