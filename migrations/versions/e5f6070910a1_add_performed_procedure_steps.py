"""add performed_procedure_steps table (MPPS)

Revision ID: e5f6070910a1
Revises: d4e5f6070910
Create Date: 2026-08-05 17:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5f6070910a1'
down_revision: Union[str, None] = 'd4e5f6070910'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'performed_procedure_steps',
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
        sa.Column('sop_instance_uid', sa.String(length=128), nullable=False),
        sa.Column('patient_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=True),
        sa.Column('scheduled_step_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=True),
        sa.Column('status', sa.String(length=16), server_default='IN PROGRESS', nullable=False),
        sa.Column('pps_id', sa.String(length=64), nullable=True),
        sa.Column('pps_description', sa.String(length=255), nullable=True),
        sa.Column('modality', sa.String(length=16), nullable=True),
        sa.Column('performed_station_aet', sa.String(length=64), nullable=True),
        sa.Column('accession_number', sa.String(length=64), nullable=True),
        sa.Column('start_date', sa.String(length=8), nullable=True),
        sa.Column('start_time', sa.String(length=16), nullable=True),
        sa.Column('end_date', sa.String(length=8), nullable=True),
        sa.Column('end_time', sa.String(length=16), nullable=True),
        sa.Column('study_instance_uid', sa.String(length=128), nullable=True),
        sa.Column('series_instance_uid', sa.String(length=128), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['patient_id'], ['patients.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['scheduled_step_id'], ['scheduled_procedure_steps.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('sop_instance_uid'),
    )
    op.create_index('ix_pps_patient_id', 'performed_procedure_steps', ['patient_id'])
    op.create_index('ix_pps_scheduled_step_id', 'performed_procedure_steps', ['scheduled_step_id'])
    op.create_index('ix_pps_status', 'performed_procedure_steps', ['status'])
    op.create_index('ix_pps_accession', 'performed_procedure_steps', ['accession_number'])
    op.create_index('ix_pps_study', 'performed_procedure_steps', ['study_instance_uid'])


def downgrade() -> None:
    op.drop_index('ix_pps_study', table_name='performed_procedure_steps')
    op.drop_index('ix_pps_accession', table_name='performed_procedure_steps')
    op.drop_index('ix_pps_status', table_name='performed_procedure_steps')
    op.drop_index('ix_pps_scheduled_step_id', table_name='performed_procedure_steps')
    op.drop_index('ix_pps_patient_id', table_name='performed_procedure_steps')
    op.drop_table('performed_procedure_steps')
