"""Track replica content generations and renew legacy retry budgets."""

import sqlalchemy as sa
from alembic import op

revision = "pg0002_replication_generation"
down_revision = "pg0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("replication_records", sa.Column(
        "generation", sa.BigInteger(), nullable=False, server_default="0"
    ))
    op.execute(
        "UPDATE replication_records SET attempts = 0, first_attempted_at = NULL "
        "WHERE status = 'REPLICATED' OR (status = 'PENDING' AND replicated_at IS NOT NULL)"
    )


def downgrade() -> None:
    op.drop_column("replication_records", "generation")
