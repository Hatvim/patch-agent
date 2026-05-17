"""add code_chunks table for vector code search

Revision ID: d4e5f6a7b8c9
Revises: 8ce5bc73f974
Create Date: 2026-05-16 10:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector


revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = "8ce5bc73f974"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "code_chunks",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "repository_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("commit_sha", sa.String(length=64), nullable=True),
        sa.Column("file_path", sa.String(length=1024), nullable=False),
        sa.Column("language", sa.String(length=64), nullable=True),
        sa.Column("symbol_name", sa.String(length=255), nullable=True),
        sa.Column("symbol_type", sa.String(length=64), nullable=True),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("embedding", Vector(1536), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_index(
        "ix_code_chunks_repo_file_lines",
        "code_chunks",
        ["repository_id", "file_path", "start_line", "end_line"],
    )
    op.create_index(
        "uq_code_chunks_repo_path_start_hash",
        "code_chunks",
        ["repository_id", "file_path", "start_line", "content_hash"],
        unique=True,
    )

    op.execute(
        """
        CREATE INDEX ix_code_chunks_embedding
        ON code_chunks
        USING ivfflat (embedding vector_cosine_ops)
        WITH (lists = 100)
        """
    )


def downgrade() -> None:
    op.drop_index("ix_code_chunks_embedding", table_name="code_chunks")
    op.drop_index("uq_code_chunks_repo_path_start_hash", table_name="code_chunks")
    op.drop_index("ix_code_chunks_repo_file_lines", table_name="code_chunks")
    op.drop_table("code_chunks")
