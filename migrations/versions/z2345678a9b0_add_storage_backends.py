"""add multi-storage: storage_backends + backend columns

Revision ID: z2345678a9b0
Revises: y1234567z8a0
Create Date: 2026-08-06

Multi-storage support: a `storage_backends` registry of named backends
(local filesystem roots and S3-compatible stores). Studies/instances record
which backend holds their objects; NULL means the environment default.
"""

import sqlalchemy as sa

from alembic import op

revision = "z2345678a9b0"
down_revision = "y1234567z8a0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "storage_backends",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.Column("root_path", sa.String(512), nullable=True),
        sa.Column("endpoint_url", sa.String(512), nullable=True),
        sa.Column("region", sa.String(64), nullable=True),
        sa.Column("bucket", sa.String(255), nullable=True),
        sa.Column("access_key", sa.String(255), nullable=True),
        sa.Column("secret_key", sa.String(255), nullable=True),
        sa.Column("presign_expires", sa.Integer(), server_default="900", nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("name"),
    )
    op.add_column(
        "studies",
        sa.Column("storage_backend", sa.String(64), nullable=True),
    )
    op.add_column(
        "instances",
        sa.Column("storage_backend", sa.String(64), nullable=True),
    )
    op.create_index("ix_instances_storage_backend", "instances", ["storage_backend"])


def downgrade() -> None:
    op.drop_index("ix_instances_storage_backend", table_name="instances")
    op.drop_column("instances", "storage_backend")
    op.drop_column("studies", "storage_backend")
    op.drop_table("storage_backends")
