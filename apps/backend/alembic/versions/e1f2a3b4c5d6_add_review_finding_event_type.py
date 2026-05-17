"""add review_finding event type

Revision ID: e1f2a3b4c5d6
Revises: fdbb53631cb3
Create Date: 2026-05-17 12:40:00.000000
"""
from typing import Sequence, Union

from alembic import op

revision: str = "e1f2a3b4c5d6"
down_revision: Union[str, Sequence[str], None] = "fdbb53631cb3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE event_type ADD VALUE IF NOT EXISTS 'review_finding'")


def downgrade() -> None:
    # PostgreSQL cannot drop an enum value in-place.
    pass
