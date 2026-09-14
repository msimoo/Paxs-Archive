"""Merge the storage-quotas head with the clinical-protocols head.

A parallel track created `clinical_protocols` (d5e6f70819a2) directly off
c4d5e6f70819, forking the graph into two heads and breaking `alembic upgrade
head` for fresh installations (the DB-admin "Initialize new installation"
flow). The branches touch disjoint tables (clinical_protocols vs. the storage
quotas feature), so a plain merge revision is safe.

Revision ID: t9013243n4o5
Revises: a12345678901, d5e6f70819a2
Create Date: 2026-08-12
"""

from alembic import op
import sqlalchemy as sa  # noqa: F401


# revision identifiers, used by Alembic.
revision: str = "t9013243n4o5"
down_revision: tuple[str, str] = ("a12345678901", "d5e6f70819a2")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
