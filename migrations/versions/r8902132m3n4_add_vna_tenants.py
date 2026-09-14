"""VNA multi-tenancy: tenants + AE-title mappings + study/user tenant columns.

Revision ID: r8902132m3n4
Revises: q7892021l2m3
Create Date: 2026-08-06
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "r8902132m3n4"
down_revision: str | None = "q7892021l2m3"
branch_labels = None
depends_on = None

_BIG = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", _BIG, nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
        sa.UniqueConstraint("code"),
    )

    op.create_table(
        "tenant_ae_titles",
        sa.Column("id", _BIG, nullable=False),
        sa.Column("tenant_id", _BIG, nullable=False),
        sa.Column("ae_title", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ae_title"),
    )
    op.create_index("ix_tenant_ae_titles_tenant_id", "tenant_ae_titles", ["tenant_id"])

    # Studies get an owning tenant (nullable = legacy unscoped scope). Batch
    # mode is required for SQLite (no ALTER support for FK constraints).
    with op.batch_alter_table("studies") as batch_op:
        batch_op.add_column(sa.Column("tenant_id", _BIG, nullable=True))
        batch_op.create_foreign_key(
            "fk_studies_tenant_id_tenants", "tenants",
            ["tenant_id"], ["id"], ondelete="SET NULL",
        )
        batch_op.create_index("ix_studies_tenant_id", ["tenant_id"])

    # Users belong to a tenant (nullable = legacy scope); admins see all.
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("tenant_id", _BIG, nullable=True))
        batch_op.create_foreign_key(
            "fk_users_tenant_id_tenants", "tenants",
            ["tenant_id"], ["id"], ondelete="SET NULL",
        )
        batch_op.create_index("ix_users_tenant_id", ["tenant_id"])


def downgrade() -> None:
    # MySQL refuses to drop an index that a FK constraint still depends on
    # (errno 1553), so constraints must go before the supporting indexes.
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_constraint("fk_users_tenant_id_tenants", type_="foreignkey")
        batch_op.drop_index("ix_users_tenant_id")
        batch_op.drop_column("tenant_id")

    with op.batch_alter_table("studies") as batch_op:
        batch_op.drop_constraint("fk_studies_tenant_id_tenants", type_="foreignkey")
        batch_op.drop_index("ix_studies_tenant_id")
        batch_op.drop_column("tenant_id")

    # No other table references tenant_ae_titles, so dropping the table also
    # removes its FK constraint — MySQL refuses to drop the supporting index
    # while the FK exists (errno 1553), and the FK name is backend-generated.
    op.drop_table("tenant_ae_titles")
    op.drop_table("tenants")
