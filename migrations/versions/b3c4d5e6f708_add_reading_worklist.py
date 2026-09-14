"""add reading worklist columns to studies (roadmap #30)

Revision ID: b3c4d5e6f708
Revises: a1234567b8c1
Create Date: 2026-08-07
"""

from alembic import op
import sqlalchemy as sa


revision = "b3c4d5e6f708"
down_revision = "a1234567b8c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "studies",
        sa.Column("stat_flag", sa.Boolean(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "studies",
        sa.Column("assigned_radiologist", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "studies",
        sa.Column("read_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "studies",
        sa.Column("read_by", sa.String(length=128), nullable=True),
    )
    op.create_index(
        "ix_studies_assigned_radiologist", "studies", ["assigned_radiologist"]
    )


def downgrade() -> None:
    op.drop_index("ix_studies_assigned_radiologist", table_name="studies")
    op.drop_column("studies", "read_by")
    op.drop_column("studies", "read_at")
    op.drop_column("studies", "assigned_radiologist")
    op.drop_column("studies", "stat_flag")
