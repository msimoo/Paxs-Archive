"""Add ian_notifications table.

Revision ID: l2341516g7h8
Revises: k1231415f6a7
Create Date: 2026-08-05
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "l2341516g7h8"
down_revision = "k1231415f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ian_notifications",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True),
        sa.Column("study_instance_uid", sa.String(128), nullable=False, index=True),
        sa.Column("target_ae_title", sa.String(64), nullable=False, index=True),
        sa.Column("instance_availability", sa.String(16), nullable=False, server_default="ONLINE"),
        sa.Column("status", sa.String(16), nullable=False, server_default="PENDING", index=True),
        sa.Column("payload_json", sa.Text(), nullable=False),  # TEXT cannot carry a literal default on MySQL 8 (errno 1101); app always sets it
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("ian_notifications")
