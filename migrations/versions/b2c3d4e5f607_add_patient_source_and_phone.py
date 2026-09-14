"""add patient source (HIS provenance) and phone columns

Revision ID: b2c3d4e5f607
Revises: a1b2c3d4e5f6
Create Date: 2026-08-05 13:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f607'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'patients',
        sa.Column('patient_phone', sa.String(length=32), nullable=True),
    )
    op.add_column(
        'patients',
        sa.Column('source', sa.String(length=8), nullable=False,
                  server_default='dicom'),
    )
    op.create_index(op.f('ix_patients_source'), 'patients', ['source'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_patients_source'), table_name='patients')
    op.drop_column('patients', 'source')
    op.drop_column('patients', 'patient_phone')
