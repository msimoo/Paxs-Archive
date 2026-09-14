"""Add study lifecycle management (item 14) — rules table + study state columns.

Revision ID: t0123456q6r7
Revises: s9013243n4o5
Create Date: 2026-08-06
"""

from alembic import op
import sqlalchemy as sa

revision = "t0123456q6r7"
down_revision = "s9013243n4o5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "study_lifecycle_rules",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        sa.Column("description", sa.String(255), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("10")),
        sa.Column("action", sa.String(16), nullable=False, server_default="ARCHIVE"),
        sa.Column("min_age_days", sa.Integer(), nullable=False, server_default=sa.text("365")),
        sa.Column("modality", sa.String(16), nullable=True),
        sa.Column("body_part_examined", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.add_column("studies", sa.Column("lifecycle_state", sa.String(16),
                                       nullable=False, server_default="ONLINE"))
    op.add_column("studies", sa.Column("archived_at", sa.DateTime(), nullable=True))
    op.create_index("ix_studies_lifecycle_state", "studies", ["lifecycle_state"])


def downgrade() -> None:
    op.drop_index("ix_studies_lifecycle_state", table_name="studies")
    op.drop_column("studies", "archived_at")
    op.drop_column("studies", "lifecycle_state")
    op.drop_table("study_lifecycle_rules")
