"""Allow live context-compression run events

Revision ID: 015
Revises: 014
Create Date: 2026-11-11

"""

from typing import Sequence, Union

from alembic import op

revision: str = "015"
down_revision: Union[str, None] = "014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


EVENT_TYPES_OLD = (
    "'message.delta', 'tool.call', 'tool.result', 'memory.recall', 'memory.save', "
    "'user.question', 'user.answer', 'message.completed', 'error'"
)
EVENT_TYPES_NEW = (
    "'message.delta', 'tool.call', 'tool.result', 'memory.recall', 'memory.save', "
    "'user.question', 'user.answer', 'context.compressed', 'message.completed', 'error'"
)


def upgrade() -> None:
    op.drop_constraint("ck_agent_run_events_type", "agent_run_events", type_="check")
    op.create_check_constraint(
        "ck_agent_run_events_type",
        "agent_run_events",
        f"event_type IN ({EVENT_TYPES_NEW})",
    )


def downgrade() -> None:
    # Rows written under the wider constraint would violate the narrow one.
    op.execute("DELETE FROM agent_run_events WHERE event_type = 'context.compressed'")
    op.drop_constraint("ck_agent_run_events_type", "agent_run_events", type_="check")
    op.create_check_constraint(
        "ck_agent_run_events_type",
        "agent_run_events",
        f"event_type IN ({EVENT_TYPES_OLD})",
    )
