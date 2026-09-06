"""add session_version

Revision ID: d9658049e3ae
Revises: e1f2a3b4c5d6
Create Date: 2026-09-06 18:45:44.227966

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d9658049e3ae"
down_revision: Union[str, Sequence[str], None] = "e1f2a3b4c5d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "session_version",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.alter_column("users", "session_version", server_default=None)


def downgrade() -> None:
    op.drop_column("users", "session_version")
