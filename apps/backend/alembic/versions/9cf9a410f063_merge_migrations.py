"""merge migrations

Revision ID: 9cf9a410f063
Revises: 8ce5bc73f974, d4e5f6a7b8c9
Create Date: 2026-05-17 11:50:26.829909

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '9cf9a410f063'
down_revision: Union[str, Sequence[str], None] = ('8ce5bc73f974', 'd4e5f6a7b8c9')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
