"""Add routing_rules table for DICOM auto-routing engine.

Revision ID: i9011213d4e5
Revises: h8091112c3d4
Create Date: 2026-08-05
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "i9011213d4e5"
down_revision = "h8091112c3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "routing_rules",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        sa.Column("description", sa.String(255), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("destination_node_id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), sa.ForeignKey("dicom_nodes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("modality", sa.String(16), nullable=True),
        sa.Column("body_part_examined", sa.String(64), nullable=True),
        sa.Column("source_aet", sa.String(64), nullable=True),
        sa.Column("study_description_pattern", sa.String(255), nullable=True),
        sa.Column("delay_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("routing_rules")
