"""C10: tenant-scoped patient identity (PIX tables, tenant_id, conflict quarantine)

Revision ID: b4567890c1d2
Revises: a3456789b0c1
Create Date: 2026-09-23

Patient identity was global: `patients.patient_id` was unique across the whole
installation, so two institutions using the same MRN shared one patient row and
each ingest overwrote the other's demographics.

This migration scopes identity to a tenant:

* creates `assigning_authorities` and `patient_identities` — the PIX tables were
  present in the models and used by the PIX service, but **no migration ever
  created them**, so they were missing on every migrated installation;
* adds `patients.tenant_id` and `patient_identities.tenant_id`;
* replaces the global unique constraint on `patients.patient_id` with a
  tenant-scoped one;
* adds `patient_identity_conflicts` for quarantined demographic mismatches.

**Forward-only.** Legacy rows keep `tenant_id IS NULL` and resolve exactly as
before. Rows that are now ambiguous (referenced by studies from more than one
tenant) are *reported* for human reconciliation — nothing is split or merged
automatically, because an already-interleaved history cannot be untangled
mechanically.
"""

import sqlalchemy as sa

from alembic import op

revision = "b4567890c1d2"
down_revision = "a3456789b0c1"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    bind = op.get_bind()
    return name in sa.inspect(bind).get_table_names()


def _legacy_patients_table() -> sa.Table:
    """The pre-C10 `patients` columns, used as the batch *source* schema.

    It deliberately omits the old `UNIQUE (patient_id)`: batch mode rebuilds the
    table from this definition plus the operations in `upgrade()`, which is how
    SQLite's un-droppable unnamed constraint goes away. The existing indexes are
    listed so the rebuild keeps them.
    """
    meta = sa.MetaData()
    return sa.Table(
        "patients",
        meta,
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("patient_id", sa.String(64), nullable=False),
        sa.Column("patient_name", sa.String(192), nullable=True),
        sa.Column("patient_birth_date", sa.String(8), nullable=True),
        sa.Column("patient_sex", sa.String(16), nullable=True),
        sa.Column("patient_phone", sa.String(32), nullable=True),
        sa.Column("source", sa.String(8), nullable=False, server_default="dicom"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Index("ix_patients_patient_name", "patient_name"),
        sa.Index("ix_patients_source", "source"),
    )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # --- PIX tables (never migrated before) --------------------------------
    if not _has_table("assigning_authorities"):
        op.create_table(
            "assigning_authorities",
            sa.Column("id", sa.BigInteger(), primary_key=True),
            sa.Column("oid", sa.String(128), nullable=False),
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column("is_authoritative", sa.Boolean(), server_default=sa.text("0"), nullable=False),
            sa.Column("description", sa.String(255), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("oid", name="uq_assigning_authorities_oid"),
        )

    if not _has_table("patient_identities"):
        op.create_table(
            "patient_identities",
            sa.Column("id", sa.BigInteger(), primary_key=True),
            sa.Column("tenant_id", sa.BigInteger(), nullable=True),
            sa.Column("patient_id", sa.BigInteger(), nullable=False),
            sa.Column("assigning_authority_id", sa.BigInteger(), nullable=False),
            sa.Column("identifier", sa.String(64), nullable=False),
            sa.Column("id_type", sa.String(16), server_default="MR", nullable=False),
            sa.Column("is_primary", sa.Boolean(), server_default=sa.text("1"), nullable=False),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["patient_id"], ["patients.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["assigning_authority_id"], ["assigning_authorities.id"], ondelete="CASCADE"
            ),
        )
        op.create_index("ix_patient_identities_tenant_id", "patient_identities", ["tenant_id"])
        op.create_index("ix_patient_identities_patient_id", "patient_identities", ["patient_id"])
        op.create_index(
            "ix_patient_identities_assigning_authority_id",
            "patient_identities",
            ["assigning_authority_id"],
        )
        op.create_index(
            "ix_patient_identities_patient_aa", "patient_identities", ["patient_id", "assigning_authority_id"]
        )
        op.create_index(
            "ix_patient_identities_tenant_aa_identifier",
            "patient_identities",
            ["tenant_id", "assigning_authority_id", "identifier"],
            unique=True,
        )

    # --- conflict quarantine ----------------------------------------------
    if not _has_table("patient_identity_conflicts"):
        op.create_table(
            "patient_identity_conflicts",
            sa.Column("id", sa.BigInteger(), primary_key=True),
            sa.Column("tenant_id", sa.BigInteger(), nullable=True),
            sa.Column("patient_id", sa.BigInteger(), nullable=False),
            sa.Column("study_instance_uid", sa.String(128), nullable=True),
            sa.Column("identifier", sa.String(64), nullable=False),
            sa.Column("assigning_authority", sa.String(128), nullable=False),
            sa.Column("detail", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("source", sa.String(128), nullable=True),
            sa.Column("resolved", sa.Boolean(), server_default=sa.text("0"), nullable=False),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["patient_id"], ["patients.id"], ondelete="CASCADE"),
        )
        op.create_index("ix_patient_identity_conflicts_tenant_id", "patient_identity_conflicts", ["tenant_id"])
        op.create_index("ix_patient_identity_conflicts_patient_id", "patient_identity_conflicts", ["patient_id"])
        op.create_index("ix_patient_identity_conflicts_study", "patient_identity_conflicts", ["study_instance_uid"])
        op.create_index("ix_patient_identity_conflicts_resolved", "patient_identity_conflicts", ["resolved"])

    # --- patients.tenant_id + tenant-scoped uniqueness ---------------------
    # Rebuilt from an explicit definition rather than add_column + drop_constraint:
    # the old UNIQUE(patient_id) is unnamed on SQLite and cannot be dropped, and
    # batch-mode constraint drops raise at flush time (outside a try/except).
    # Foreign keys referencing `patients` are by name, so they survive the rebuild.
    with op.batch_alter_table("patients", copy_from=_legacy_patients_table()) as batch:
        batch.add_column(sa.Column("tenant_id", sa.BigInteger(), nullable=True))
        batch.create_index("ix_patients_tenant_id", ["tenant_id"])
        batch.create_unique_constraint(
            "uq_patients_tenant_patient_id", ["tenant_id", "patient_id"]
        )

    _report_ambiguous_patients()


def _report_ambiguous_patients() -> None:
    """List patients whose studies now span more than one tenant.

    These are the rows where identity was already conflated before this
    migration. They are reported for human reconciliation and deliberately left
    alone: assigning them to one tenant would silently move another
    institution's studies.
    """
    bind = op.get_bind()
    try:
        rows = bind.execute(
            sa.text(
                """
                SELECT p.patient_id AS mrn,
                       COUNT(DISTINCT s.tenant_id) AS tenant_count,
                       GROUP_CONCAT(DISTINCT COALESCE(CAST(s.tenant_id AS CHAR), 'NULL')) AS tenants,
                       COUNT(s.id) AS studies
                FROM patients p
                JOIN studies s ON s.patient_id = p.id
                GROUP BY p.id, p.patient_id
                HAVING COUNT(DISTINCT s.tenant_id) > 1
                ORDER BY tenant_count DESC
                """
            )
        ).fetchall()
    except Exception:  # noqa: BLE001 - reporting must never block the migration
        print("[C10] ambiguous-patient report skipped (query failed)")
        return

    if not rows:
        print("[C10] no ambiguous patients detected — every patient maps to a single tenant")
        return

    print("")
    print("[C10] AMBIGUOUS PATIENTS — review before relying on tenant isolation:")
    print(f"[C10]   {len(rows)} patient(s) have studies from more than one tenant.")
    print("[C10]   Nothing was split or re-assigned; reconcile manually.")
    for mrn, tenant_count, tenants, studies in rows:
        print(f"[C10]   MRN={mrn!r} tenants={tenants} studies={studies}")
    print("")


def downgrade() -> None:
    # Restore the pre-C10 shape: no tenant_id, global UNIQUE(patient_id).
    current = sa.Table(
        "patients",
        sa.MetaData(),
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("tenant_id", sa.BigInteger(), nullable=True),
        sa.Column("patient_id", sa.String(64), nullable=False),
        sa.Column("patient_name", sa.String(192), nullable=True),
        sa.Column("patient_birth_date", sa.String(8), nullable=True),
        sa.Column("patient_sex", sa.String(16), nullable=True),
        sa.Column("patient_phone", sa.String(32), nullable=True),
        sa.Column("source", sa.String(8), nullable=False, server_default="dicom"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Index("ix_patients_patient_name", "patient_name"),
        sa.Index("ix_patients_source", "source"),
        sa.Index("ix_patients_tenant_id", "tenant_id"),
    )
    op.drop_table("patient_identity_conflicts")
    op.drop_table("patient_identities")
    op.drop_table("assigning_authorities")
    with op.batch_alter_table("patients", copy_from=current) as batch:
        batch.drop_column("tenant_id")
        batch.create_unique_constraint("uq_patients_patient_id", ["patient_id"])
