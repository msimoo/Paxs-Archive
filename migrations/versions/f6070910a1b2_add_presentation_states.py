"""Add presentation_states table for GSPS support.

Revision ID: f6070910a1b2
Revises: e5f6070910a1
Create Date: 2026-08-05
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "f6070910a1b2"
down_revision = "e5f6070910a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "presentation_states",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True),
        sa.Column("sop_instance_uid", sa.String(128), nullable=False, unique=True),
        sa.Column("study_instance_uid", sa.String(128), nullable=False, index=True),
        sa.Column("series_instance_uid", sa.String(128), nullable=False, index=True),
        sa.Column("presentation_label", sa.String(64), nullable=True),
        sa.Column("presentation_description", sa.String(255), nullable=True),
        sa.Column("presentation_creation_date", sa.String(8), nullable=True),
        sa.Column("presentation_creation_time", sa.String(16), nullable=True),
        sa.Column("presentation_creator", sa.String(192), nullable=True),
        sa.Column("window_center", sa.String(64), nullable=True),
        sa.Column("window_width", sa.String(64), nullable=True),
        sa.Column("rotation", sa.Integer(), nullable=True),
        sa.Column("horizontal_flip", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("referenced_sop_instance_uids", sa.Text(), nullable=True),
        sa.Column("file_path", sa.String(768), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("presentation_states")
