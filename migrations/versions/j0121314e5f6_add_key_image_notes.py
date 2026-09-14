"""Add key_image_notes table for KIN support.

Revision ID: j0121314e5f6
Revises: i9011213d4e5
Create Date: 2026-08-05
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "j0121314e5f6"
down_revision = "i9011213d4e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "key_image_notes",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True),
        sa.Column("sop_instance_uid", sa.String(128), nullable=False, unique=True),
        sa.Column("study_instance_uid", sa.String(128), nullable=False, index=True),
        sa.Column("series_instance_uid", sa.String(128), nullable=False, index=True),
        sa.Column("title_concept_code", sa.String(64), nullable=True),
        sa.Column("title_concept_text", sa.String(255), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("author_name", sa.String(192), nullable=True),
        sa.Column("referenced_sop_instance_uids", sa.Text(), nullable=True),
        sa.Column("file_path", sa.String(768), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("key_image_notes")
