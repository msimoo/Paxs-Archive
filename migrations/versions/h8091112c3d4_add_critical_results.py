"""Add critical_results table for critical clinical finding tracking.

Revision ID: h8091112c3d4
Revises: g7081011b2c3
Create Date: 2026-08-05
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "h8091112c3d4"
down_revision = "g7081011b2c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "critical_results",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True),
        sa.Column("study_instance_uid", sa.String(128), nullable=False, index=True),
        sa.Column("series_instance_uid", sa.String(128), nullable=True),
        sa.Column("sop_instance_uid", sa.String(128), nullable=True),
        sa.Column("patient_id", sa.String(64), nullable=True, index=True),
        sa.Column("finding_level", sa.String(32), nullable=False, server_default="CRITICAL_RED", index=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("reported_by", sa.String(128), nullable=False),
        sa.Column("communicated_to", sa.String(192), nullable=True),
        sa.Column("communication_channel", sa.String(32), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="PENDING_ACK", index=True),
        sa.Column("acknowledged_by", sa.String(128), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(), nullable=True),
        sa.Column("acknowledgement_notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("critical_results")
