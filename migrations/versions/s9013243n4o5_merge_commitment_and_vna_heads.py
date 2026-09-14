"""Merge the Storage-Commitment branch head with the VNA head.

A parallel track created `commitment_statuses` (6070910a1b2c) directly off
g7081011b2c3, forking the graph into two heads and breaking `alembic upgrade
head`. The branches touch disjoint tables (commitment_statuses vs. the VNA
tenants feature), so a plain merge revision is safe.

Revision ID: s9013243n4o5
Revises: 6070910a1b2c, r8902132m3n4
Create Date: 2026-08-06
"""

from alembic import op
import sqlalchemy as sa  # noqa: F401


# revision identifiers, used by Alembic.
revision: str = "s9013243n4o5"
down_revision: tuple[str, str] = ("6070910a1b2c", "r8902132m3n4")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
