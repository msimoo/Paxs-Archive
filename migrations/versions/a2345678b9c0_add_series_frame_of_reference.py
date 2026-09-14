"""Add frame_of_reference_uid to series — fusion (roadmap #29) spatial contract.

Revision ID: a2345678b9c0
Revises: t9013243n4o5
Create Date: 2026-08-12
"""

from alembic import op
import sqlalchemy as sa

revision = "a2345678b9c0"
down_revision = "t9013243n4o5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "series",
        sa.Column("frame_of_reference_uid", sa.String(128), nullable=True),
    )
    op.create_index(
        "ix_series_frame_of_reference_uid",
        "series",
        ["frame_of_reference_uid"],
    )


def downgrade() -> None:
    op.drop_index("ix_series_frame_of_reference_uid", table_name="series")
    op.drop_column("series", "frame_of_reference_uid")
