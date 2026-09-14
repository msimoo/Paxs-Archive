"""add series triage_status column

Revision ID: a1b2c3d4e5f6
Revises: 87b28a09bb37
Create Date: 2026-08-05 12:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '87b28a09bb37'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Triage must run exactly once per series, even when no finding exists —
    so Series tracks PENDING -> RUNNING -> DONE explicitly."""
    op.add_column(
        'series',
        sa.Column('triage_status', sa.String(length=16), nullable=False,
                  server_default='PENDING'),
    )


def downgrade() -> None:
    op.drop_column('series', 'triage_status')
