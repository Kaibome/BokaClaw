"""Create the workspace-Markdown memory index

Revision ID: 014
Revises: 013
Create Date: 2026-11-08

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.memory.models import HalfVec

revision: str = "014"
down_revision: Union[str, None] = "013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "memory_index_entries",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=True),
        sa.Column("run_id", sa.Text(), nullable=True),
        sa.Column("kind", sa.String(32), nullable=False, server_default="operational"),
        sa.Column("source", sa.String(32), nullable=False, server_default="manual"),
        sa.Column("trust", sa.String(32), nullable=False, server_default="owner"),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("importance", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("project", sa.Text(), nullable=True),
        sa.Column("question_key", sa.String(64), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_path", sa.Text(), nullable=False, server_default=""),
        sa.Column("source_ordinal", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("embedding_model", sa.Text(), nullable=True),
        sa.Column("embedding_dims", sa.Integer(), nullable=True),
        sa.Column("embedding_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("embedding_error", sa.Text(), nullable=True),
        sa.Column("embedding", HalfVec(3072), nullable=True),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column(
            "search_tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('simple', coalesce(search_text, content))", persisted=True),
        ),
    )
    op.create_index(
        "ix_memory_entries_user_kind",
        "memory_index_entries",
        ["user_id", "kind"],
    )
    op.create_index(
        "ix_memory_entries_user_question",
        "memory_index_entries",
        ["user_id", "question_key"],
    )
    op.create_index(
        "ix_memory_entries_user_updated",
        "memory_index_entries",
        ["user_id", "updated_at"],
    )
    op.create_index(
        "ix_memory_entries_expires",
        "memory_index_entries",
        ["expires_at"],
        postgresql_where=sa.text("expires_at IS NOT NULL"),
    )
    op.create_index(
        "ix_memory_entries_search_tsv",
        "memory_index_entries",
        ["search_tsv"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_memory_entries_content_trgm",
        "memory_index_entries",
        ["content"],
        postgresql_using="gin",
        postgresql_ops={"content": "gin_trgm_ops"},
    )
    op.execute(
        "CREATE INDEX ix_memory_entries_embedding_hnsw "
        "ON memory_index_entries USING hnsw (embedding halfvec_cosine_ops)"
    )

    op.create_table(
        "memory_index_files",
        sa.Column("user_id", sa.Text(), primary_key=True),
        sa.Column("path", sa.Text(), primary_key=True),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("mtime", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "memory_history",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("memory_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=True),
        sa.Column("event", sa.String(32), nullable=False, server_default="ADD"),
        sa.Column("old_memory", sa.Text(), nullable=True),
        sa.Column("new_memory", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("actor_id", sa.Text(), nullable=True),
        sa.Column("role", sa.Text(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.create_index(
        "ix_memory_history_memory_created",
        "memory_history",
        ["memory_id", "created_at"],
    )

    op.create_table(
        "memory_embedding_cache",
        sa.Column("model", sa.Text(), primary_key=True),
        sa.Column("dims", sa.Integer(), primary_key=True),
        sa.Column("content_hash", sa.String(64), primary_key=True),
        sa.Column("embedding", HalfVec(3072), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "memory_index_state",
        sa.Column("user_id", sa.Text(), primary_key=True),
        sa.Column("dirty", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("last_reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("reembed_required", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("memory_index_state")
    op.drop_table("memory_embedding_cache")
    op.drop_index("ix_memory_history_memory_created", table_name="memory_history")
    op.drop_table("memory_history")
    op.drop_table("memory_index_files")
    op.drop_index("ix_memory_entries_embedding_hnsw", table_name="memory_index_entries")
    op.drop_index("ix_memory_entries_content_trgm", table_name="memory_index_entries")
    op.drop_index("ix_memory_entries_search_tsv", table_name="memory_index_entries")
    op.drop_index("ix_memory_entries_expires", table_name="memory_index_entries")
    op.drop_index("ix_memory_entries_user_updated", table_name="memory_index_entries")
    op.drop_index("ix_memory_entries_user_question", table_name="memory_index_entries")
    op.drop_index("ix_memory_entries_user_kind", table_name="memory_index_entries")
    op.drop_table("memory_index_entries")
