"""Add hanging_protocols table.

Revision ID: p6781920k1l2
Revises: o5671819j0k1
Create Date: 2026-08-05
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "p6781920k1l2"
down_revision = "o5671819j0k1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hanging_protocols",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        sa.Column("description", sa.String(255), nullable=True),
        sa.Column("modality", sa.String(16), nullable=True, index=True),
        sa.Column("body_part_examined", sa.String(64), nullable=True, index=True),
        sa.Column("user_id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("layout_rows", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("layout_columns", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("configuration_json", sa.Text(), nullable=False),  # TEXT cannot carry a literal default on MySQL 8 (errno 1101); app always sets it
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("hanging_protocols")
