"""Allow dedicated context-compression spans

Revision ID: 016
Revises: 015
Create Date: 2026-11-11

"""

from typing import Sequence, Union

from alembic import op

revision: str = "016"
down_revision: Union[str, None] = "015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SPAN_TYPES_OLD = "'agent', 'llm', 'tool', 'memory'"
SPAN_TYPES_NEW = "'agent', 'llm', 'tool', 'memory', 'compression'"


def upgrade() -> None:
    op.drop_constraint("ck_agent_run_spans_type", "agent_run_spans", type_="check")
    op.create_check_constraint(
        "ck_agent_run_spans_type",
        "agent_run_spans",
        f"span_type IN ({SPAN_TYPES_NEW})",
    )


def downgrade() -> None:
    # Rows written under the wider constraint would violate the narrow one.
    op.execute("DELETE FROM agent_run_spans WHERE span_type = 'compression'")
    op.drop_constraint("ck_agent_run_spans_type", "agent_run_spans", type_="check")
    op.create_check_constraint(
        "ck_agent_run_spans_type",
        "agent_run_spans",
        f"span_type IN ({SPAN_TYPES_OLD})",
    )
