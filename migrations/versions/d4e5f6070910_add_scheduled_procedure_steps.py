"""add scheduled_procedure_steps table (Modality Worklist)

Revision ID: d4e5f6070910
Revises: c3d4e5f60709
Create Date: 2026-08-05 15:45:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4e5f6070910'
down_revision: Union[str, None] = 'c3d4e5f60709'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'scheduled_procedure_steps',
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
        sa.Column('patient_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
        sa.Column('sps_id', sa.String(length=64), nullable=False),
        sa.Column('accession_number', sa.String(length=64), nullable=True),
        sa.Column('modality', sa.String(length=16), nullable=True),
        sa.Column('scheduled_station_aet', sa.String(length=64), nullable=True),
        sa.Column('start_date', sa.String(length=8), nullable=True),
        sa.Column('start_time', sa.String(length=16), nullable=True),
        sa.Column('status', sa.String(length=16), server_default='SCHEDULED', nullable=False),
        sa.Column('requested_procedure_id', sa.String(length=64), nullable=True),
        sa.Column('requested_procedure_description', sa.String(length=255), nullable=True),
        sa.Column('scheduled_performing_physician', sa.String(length=192), nullable=True),
        sa.Column('study_instance_uid', sa.String(length=128), nullable=True),
        sa.Column('performed_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['patient_id'], ['patients.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('sps_id'),
    )
    op.create_index('ix_sps_patient_id', 'scheduled_procedure_steps', ['patient_id'])
    op.create_index('ix_sps_accession', 'scheduled_procedure_steps', ['accession_number'])
    op.create_index('ix_sps_station', 'scheduled_procedure_steps', ['scheduled_station_aet'])
    op.create_index('ix_sps_start_date', 'scheduled_procedure_steps', ['start_date'])
    op.create_index('ix_sps_modality', 'scheduled_procedure_steps', ['modality'])
    op.create_index('ix_sps_study', 'scheduled_procedure_steps', ['study_instance_uid'])


def downgrade() -> None:
    op.drop_index('ix_sps_study', table_name='scheduled_procedure_steps')
    op.drop_index('ix_sps_modality', table_name='scheduled_procedure_steps')
    op.drop_index('ix_sps_start_date', table_name='scheduled_procedure_steps')
    op.drop_index('ix_sps_station', table_name='scheduled_procedure_steps')
    op.drop_index('ix_sps_accession', table_name='scheduled_procedure_steps')
    op.drop_index('ix_sps_patient_id', table_name='scheduled_procedure_steps')
    op.drop_table('scheduled_procedure_steps')
