"""Add migration_jobs table for bulk DICOM migration tracking.

Revision ID: g7081011b2c3
Revises: f6070910a1b2
Create Date: 2026-08-05
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "g7081011b2c3"
down_revision = "f6070910a1b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "migration_jobs",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True),
        sa.Column("job_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="PENDING", index=True),
        sa.Column("source_info", sa.String(512), nullable=False),
        sa.Column("total_items", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("processed_items", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_items", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("migration_jobs")
