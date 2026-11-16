"""Add structured metadata to eval goldens

Revision ID: 017
Revises: 016
Create Date: 2026-11-13

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "017"
down_revision: Union[str, None] = "016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "eval_goldens",
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_check_constraint(
        "ck_eval_goldens_metadata",
        "eval_goldens",
        "jsonb_typeof(metadata) = 'object'",
    )


def downgrade() -> None:
    op.drop_constraint("ck_eval_goldens_metadata", "eval_goldens", type_="check")
    op.drop_column("eval_goldens", "metadata")
