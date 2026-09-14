"""add triage flags

Revision ID: 87b28a09bb37
Revises: 56d9f882c2e4
Create Date: 2026-08-05 12:09:17.669216

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '87b28a09bb37'
down_revision: Union[str, Sequence[str], None] = '56d9f882c2e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the triage_flags table (AI/algorithmic findings)."""
    op.create_table(
        'triage_flags',
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
        sa.Column('study_instance_uid', sa.String(length=128), nullable=False),
        sa.Column('series_instance_uid', sa.String(length=128), nullable=True),
        sa.Column('sop_instance_uid', sa.String(length=128), nullable=True),
        sa.Column('finding', sa.String(length=32), nullable=False),
        sa.Column('severity', sa.String(length=8), nullable=False),
        sa.Column('confidence', sa.Float(), nullable=False),
        sa.Column('summary', sa.String(length=512), nullable=True),
        sa.Column('reviewed', sa.Boolean(), nullable=False),
        sa.Column('detector', sa.String(length=64), nullable=False),
        # CURRENT_TIMESTAMP (not now()) keeps the migration portable to SQLite.
        sa.Column('created_at', sa.DateTime(),
                  server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_triage_flags_study_instance_uid'),
        'triage_flags',
        ['study_instance_uid'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_triage_flags_study_instance_uid'), table_name='triage_flags')
    op.drop_table('triage_flags')
