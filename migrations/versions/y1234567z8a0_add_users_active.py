"""add is_active to users

Revision ID: y1234567z8a0
Revises: x1234567y8z9
Create Date: 2026-08-06

User management (Admin -> Users): accounts can be disabled without deletion.
Existing rows default to active.
"""

import sqlalchemy as sa

from alembic import op

revision = "y1234567z8a0"
down_revision = "x1234567y8z9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("1"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("users", "is_active")
