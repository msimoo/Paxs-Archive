"""add storage_quotas.used_bytes (H08: atomic quota reservation)

Revision ID: d6789012e3f4
Revises: c5678901d2e3
Create Date: 2026-09-23

Quota enforcement was a check-then-act over `SUM(instances.file_size_bytes)`,
which two concurrent ingests could both pass. Enforcement is now a single atomic
`UPDATE storage_quotas SET used_bytes = used_bytes + :size WHERE ... AND
used_bytes + :size <= max_bytes`, so this migration adds the counter and
backfills it from the currently indexed bytes (so an existing installation
starts from an accurate figure rather than zero).
"""

import sqlalchemy as sa

from alembic import op

revision = "d6789012e3f4"
down_revision = "c5678901d2e3"
branch_labels = None
depends_on = None

_TENANT_USAGE = """
SELECT COALESCE(SUM(i.file_size_bytes), 0)
FROM instances i
JOIN series s ON i.series_id = s.id
JOIN studies st ON s.study_id = st.id
WHERE st.tenant_id = :tenant_id
"""

_BACKEND_USAGE = """
SELECT COALESCE(SUM(i.file_size_bytes), 0)
FROM instances i
WHERE {predicate}
"""


def upgrade() -> None:
    bind = op.get_bind()
    columns = {c["name"] for c in sa.inspect(bind).get_columns("storage_quotas")}
    if "used_bytes" not in columns:
        op.add_column(
            "storage_quotas",
            sa.Column(
                "used_bytes", sa.BigInteger(), server_default=sa.text("0"), nullable=False
            ),
        )

    # Backfill the counter from the indexed bytes.
    rows = bind.execute(
        sa.text("SELECT id, tenant_id, storage_backend_name FROM storage_quotas")
    ).fetchall()
    for row in rows:
        quota_id, tenant_id, backend = row[0], row[1], row[2]
        if tenant_id is not None:
            params = {"tenant_id": tenant_id}
            sql = _TENANT_USAGE
            if backend:
                sql += " AND i.storage_backend = :backend"
                params["backend"] = backend
            total = bind.execute(sa.text(sql), params).scalar()
        else:
            if backend:
                sql = _BACKEND_USAGE.format(predicate="i.storage_backend = :backend")
                total = bind.execute(sa.text(sql), {"backend": backend}).scalar()
            else:
                sql = _BACKEND_USAGE.format(predicate="i.storage_backend IS NULL")
                total = bind.execute(sa.text(sql)).scalar()
        bind.execute(
            sa.text("UPDATE storage_quotas SET used_bytes = :used WHERE id = :qid"),
            {"used": int(total or 0), "qid": quota_id},
        )


def downgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("storage_quotas")}
    if "used_bytes" in columns:
        with op.batch_alter_table("storage_quotas") as batch:
            batch.drop_column("used_bytes")
