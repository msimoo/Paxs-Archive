"""add report_addenda (C06: finalized reports are immutable)

Revision ID: a3456789b0c1
Revises: c0d1e2f3a4b5
Create Date: 2026-09-23

A finalized radiology report is a signed clinical record, so its clinical fields
must never be rewritten. Follow-up text is stored as an append-only addendum
instead, attributed to the authenticated author.
"""

import sqlalchemy as sa

from alembic import op

revision = "a3456789b0c1"
down_revision = "c0d1e2f3a4b5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "report_addenda",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("report_id", sa.BigInteger(), nullable=False),
        sa.Column("author", sa.String(128), server_default="", nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["report_id"], ["reports.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_report_addenda_report_id", "report_addenda", ["report_id"])


def downgrade() -> None:
    op.drop_index("ix_report_addenda_report_id", table_name="report_addenda")
    op.drop_table("report_addenda")
