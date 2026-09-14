"""add commitment_statuses table (Storage Commitment Push Model, Phase A3)

Revision ID: 6070910a1b2c
Revises: f6070910a1b2
Create Date: 2026-08-05

Storage Commitment (SOP Class 1.2.840.10008.1.20.1): a modality asks via
N-ACTION whether PAXS guarantees a set of instances is durably archived; the
verdict is delivered asynchronously in an N-EVENT-REPORT. This table records
each transaction and its per-instance verification results.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6070910a1b2c'
down_revision: Union[str, None] = 'g7081011b2c3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'commitment_statuses',
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
        sa.Column('transaction_uid', sa.String(length=128), nullable=False),
        sa.Column('ae_title', sa.String(length=64), nullable=True),
        sa.Column('status', sa.String(length=16), server_default='PENDING', nullable=False),
        sa.Column('payload_json', sa.Text(), nullable=False),
        sa.Column('sent_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('transaction_uid'),
    )
    op.create_index('ix_commit_ae_title', 'commitment_statuses', ['ae_title'])
    op.create_index('ix_commit_status', 'commitment_statuses', ['status'])


def downgrade() -> None:
    op.drop_index('ix_commit_status', table_name='commitment_statuses')
    op.drop_index('ix_commit_ae_title', table_name='commitment_statuses')
    op.drop_table('commitment_statuses')
