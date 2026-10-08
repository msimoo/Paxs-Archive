"""sync the migration chain with the models (H11)

Revision ID: c5678901d2e3
Revises: b4567890c1d2
Create Date: 2026-09-23

Running the chain end-to-end on an empty database (the H11 parity check) showed
four objects that the models declare but no migration ever created — a fresh,
migration-built installation would be missing them:

* `lossless_compression_rules` — the lossy-compression rule table
* `organization_settings` — the reporting/branding settings row
* `report_templates` — the report template library
* `report_addenda.updated_at` — omitted by the C10 migration (b4567890c1d2),
  which created the table without the `TimestampMixin` column

The tables are created from the model metadata rather than transcribed by hand,
so they match `Base.metadata` exactly at this revision. `checkfirst=True` keeps
the migration safe on databases where `create_all` had already added them.
"""

import sqlalchemy as sa

from alembic import op

revision = "c5678901d2e3"
down_revision = "b4567890c1d2"
branch_labels = None
depends_on = None

_MISSING_TABLES = (
    "lossless_compression_rules",
    "organization_settings",
    "report_templates",
)


def upgrade() -> None:
    from app.db.models import Base

    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    for name in _MISSING_TABLES:
        if name in existing:
            continue
        # Create from the model definition so columns, defaults, foreign keys
        # and indexes all match `Base.metadata`.
        Base.metadata.tables[name].create(bind, checkfirst=True)

    # b4567890c1d2 created report_addenda without the mixin's updated_at.
    columns = {c["name"] for c in sa.inspect(bind).get_columns("report_addenda")}
    if "updated_at" not in columns:
        op.add_column(
            "report_addenda",
            sa.Column(
                "updated_at",
                sa.DateTime(),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    columns = {c["name"] for c in sa.inspect(bind).get_columns("report_addenda")}
    if "updated_at" in columns:
        with op.batch_alter_table("report_addenda") as batch:
            batch.drop_column("updated_at")

    existing = set(sa.inspect(bind).get_table_names())
    for name in _MISSING_TABLES:
        if name in existing:
            op.drop_table(name)
