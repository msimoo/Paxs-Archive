"""add storage quotas table

Revision ID: a12345678901
Revises: z2345678a9b0
Create Date: 2026-08-12

Storage quota limits for tenants and/or storage backends.
Enforces maximum storage consumption at two granularities:
  * tenant-scoped: limits total bytes across all backends for a tenant
  * backend-scoped: limits total bytes on a specific backend (all tenants)
"""

import sqlalchemy as sa

from alembic import op

revision = "a12345678901"
down_revision = "z2345678a9b0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "storage_quotas",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("tenant_id", sa.BigInteger(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True, index=True),
        sa.Column("storage_backend_name", sa.String(64), nullable=True, index=True),
        sa.Column("max_bytes", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("warning_threshold_pct", sa.Integer(), server_default="80", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
    )
    op.create_index("ix_storage_quotas_tenant", "storage_quotas", ["tenant_id"])
    op.create_index("ix_storage_quotas_backend", "storage_quotas", ["storage_backend_name"])


def downgrade() -> None:
    op.drop_index("ix_storage_quotas_backend", table_name="storage_quotas")
    op.drop_index("ix_storage_quotas_tenant", table_name="storage_quotas")
    op.drop_table("storage_quotas")
