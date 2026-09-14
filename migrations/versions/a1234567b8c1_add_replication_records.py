"""add replication_records (DR replication, roadmap #49)

Revision ID: a1234567b8c1
Revises: z2345678a9b0
Create Date: 2026-08-07
"""

from alembic import op
import sqlalchemy as sa


revision = "a1234567b8c1"
down_revision = "z2345678a9b0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "replication_records",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("study_instance_uid", sa.String(length=128), nullable=False),
        sa.Column("target", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="PENDING", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("object_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("bytes_replicated", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("first_attempted_at", sa.DateTime(), nullable=True),
        sa.Column("replicated_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("study_instance_uid"),
    )
    op.create_index(
        "ix_replication_records_study_instance_uid",
        "replication_records",
        ["study_instance_uid"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_replication_records_study_instance_uid", table_name="replication_records")
    op.drop_table("replication_records")
