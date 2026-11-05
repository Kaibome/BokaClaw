"""Allow memory run events and spans

Revision ID: 012
Revises: 011
Create Date: 2026-11-05

"""

from typing import Sequence, Union

from alembic import op

revision: str = "012"
down_revision: Union[str, None] = "011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


EVENT_TYPES_OLD = "'message.delta', 'tool.call', 'tool.result', 'message.completed', 'error'"
EVENT_TYPES_NEW = (
    "'message.delta', 'tool.call', 'tool.result', 'memory.recall', 'memory.save', 'message.completed', 'error'"
)
SPAN_TYPES_OLD = "'agent', 'llm', 'tool'"
SPAN_TYPES_NEW = "'agent', 'llm', 'tool', 'memory'"


def upgrade() -> None:
    op.drop_constraint("ck_agent_run_events_type", "agent_run_events", type_="check")
    op.create_check_constraint(
        "ck_agent_run_events_type",
        "agent_run_events",
        f"event_type IN ({EVENT_TYPES_NEW})",
    )
    op.drop_constraint("ck_agent_run_spans_type", "agent_run_spans", type_="check")
    op.create_check_constraint(
        "ck_agent_run_spans_type",
        "agent_run_spans",
        f"span_type IN ({SPAN_TYPES_NEW})",
    )


def downgrade() -> None:
    # Rows written under the wider constraints would violate the narrow ones.
    op.execute("DELETE FROM agent_run_events WHERE event_type IN ('memory.recall', 'memory.save')")
    op.execute("DELETE FROM agent_run_spans WHERE span_type = 'memory'")
    op.drop_constraint("ck_agent_run_events_type", "agent_run_events", type_="check")
    op.create_check_constraint(
        "ck_agent_run_events_type",
        "agent_run_events",
        f"event_type IN ({EVENT_TYPES_OLD})",
    )
    op.drop_constraint("ck_agent_run_spans_type", "agent_run_spans", type_="check")
    op.create_check_constraint(
        "ck_agent_run_spans_type",
        "agent_run_spans",
        f"span_type IN ({SPAN_TYPES_OLD})",
    )
