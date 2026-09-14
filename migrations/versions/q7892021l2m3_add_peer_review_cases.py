"""Add peer_review_cases table.

Revision ID: q7892021l2m3
Revises: p6781920k1l2
Create Date: 2026-08-06
"""

from alembic import op
import sqlalchemy as sa

revision = "q7892021l2m3"
down_revision = "p6781920k1l2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "peer_review_cases",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True),
        sa.Column("study_instance_uid", sa.String(128), nullable=False, index=True),
        sa.Column("patient_id", sa.String(64), nullable=True, index=True),
        sa.Column("modality", sa.String(16), nullable=True, index=True),
        sa.Column("body_part_examined", sa.String(64), nullable=True),
        sa.Column("original_reader", sa.String(128), nullable=False),
        sa.Column("reviewer", sa.String(128), nullable=True, index=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="PENDING", index=True),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("comments", sa.Text(), nullable=True),
        sa.Column("assigned_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("peer_review_cases")
