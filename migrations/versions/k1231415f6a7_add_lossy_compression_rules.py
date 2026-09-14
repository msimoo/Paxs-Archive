"""Add lossy_compression_rules table.

Revision ID: k1231415f6a7
Revises: j0121314e5f6
Create Date: 2026-08-05
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "k1231415f6a7"
down_revision = "j0121314e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "lossy_compression_rules",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        sa.Column("description", sa.String(255), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("min_age_days", sa.Integer(), nullable=False, server_default="365"),
        sa.Column("modality", sa.String(16), nullable=True),
        sa.Column("body_part_examined", sa.String(64), nullable=True),
        sa.Column("transfer_syntax_uid", sa.String(128), nullable=False, server_default="1.2.840.10008.1.2.4.50"),
        sa.Column("quality_factor", sa.Integer(), nullable=False, server_default="85"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("lossy_compression_rules")
