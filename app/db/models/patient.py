"""Patient identity: the MRN, its assigning authorities and cross-references.

``patients.tenant_key`` is a stored generated column and the MRN uniqueness
constraint is keyed on it — **not** on ``tenant_id``. ``tenant_id IS NULL`` is
the legacy/unscoped bucket (pre-VNA data); SQL treats every NULL as distinct, so
``UNIQUE (tenant_id, patient_id)`` leaves that bucket unconstrained and two
concurrent first-time ingests can insert two patients for one MRN, after which
``resolve_patient_identity`` raises ``MultipleResultsFound`` — an unhandled 500
on the ingest path (A-8, fixed 2026-10-12). ``tenant_key = COALESCE(tenant_id,
0)`` collapses the NULL bucket onto the unused sentinel tenant 0, so one
composite constraint covers every row. ``tenant_id`` itself stays nullable: NULL
is a *meaning* (the legacy scope the resolver matches explicitly), not a missing
value.

Identity here is PIX/PDQ-facing: ``AssigningAuthority`` names an identity
domain, ``PatientIdentity`` maps an MRN to one, and ``PatientIdentityConflict``
records a merge a human must resolve. None of these tables may be widened
without a migration (AGENTS.md N-7).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import false, true
from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.models.base import AutoBigIntPK, Base, TimestampMixin, pk

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from app.db.models.study import Study

__all__ = [
    "AssigningAuthority",
    "Patient",
    "PatientIdentity",
    "PatientIdentityConflict",
    "PatientShare",
]


class Patient(Base, TimestampMixin):
    """A patient, identified by an MRN that is unique **within a tenant**.

    ``created_at`` / ``updated_at`` come from :class:`TimestampMixin`. The
    remaining columns are declared explicitly because their types, indexes and
    defaults are part of the shipped schema.
    """

    __tablename__ = "patients"

    id: Mapped[int] = mapped_column(
        AutoBigIntPK, primary_key=True, autoincrement=True
    )
    # C10: the external MRN is only unique *within* a tenant, so two
    # institutions can both hold MRN "12345" as separate patients. NULL =
    # legacy/unscoped row (pre-VNA data), which keeps resolving exactly as
    # before — see the module docstring.
    tenant_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "tenants.id",
            ondelete="SET NULL",
            name="fk_patients_tenant_id_tenants",
        ),
        nullable=True,
        index=True,
    )
    # C10 (A-8): the uniqueness sentinel. A plain UNIQUE(tenant_id, patient_id)
    # does **not** constrain the legacy NULL bucket, because SQL treats every
    # NULL as distinct. This stored generated column collapses NULL to the
    # unused sentinel tenant 0 (autoincrement starts at 1), so
    # UNIQUE(tenant_key, patient_id) covers every row.
    #
    # A stored generated column (PostgreSQL ``GENERATED ALWAYS AS ... STORED``)
    # rather than a functional index: being database-generated means it cannot
    # drift from tenant_id — the DB rejects writes to it. COALESCE is total, so
    # the value is never NULL in practice.
    #
    tenant_key: Mapped[int | None] = mapped_column(
        BigInteger,
        Computed("COALESCE(tenant_id, 0)", persisted=True),
        nullable=True,
    )
    patient_id: Mapped[str] = mapped_column(String(64), nullable=False)
    patient_name: Mapped[str | None] = mapped_column(String(192), index=True)
    patient_birth_date: Mapped[str | None] = mapped_column(String(8))
    patient_sex: Mapped[str | None] = mapped_column(String(16))
    patient_phone: Mapped[str | None] = mapped_column(String(32))
    # Provenance: "dicom" (arrived from a modality) or "his" (registered by the
    # HL7 ADT listener). HIS data is authoritative for HIS-registered patients.
    source: Mapped[str] = mapped_column(
        String(8), default="dicom", server_default="dicom", nullable=False, index=True
    )

    __table_args__ = (
        Index("ix_patients_name_id", "patient_name", "patient_id"),
        # C10 (A-8): one row per (tenant, MRN) — **including** the legacy
        # NULL-tenant bucket, which the previous UNIQUE(tenant_id, patient_id)
        # left unconstrained because NULL never equals NULL. Legacy rows are
        # still *matched* explicitly by the resolver (see
        # resolve_patient_identity()), so their behaviour is unchanged; they are
        # simply no longer allowed to duplicate.
        UniqueConstraint("tenant_key", "patient_id", name="uq_patients_tenant_key_mrn"),
    )

    studies: Mapped[list[Study]] = relationship(
        back_populates="patient", cascade="all, delete-orphan"
    )

    # Patient identities in this domain (PIX): one Patient can carry multiple
    # identifiers across assigning authorities. Declared inside the class body
    # so the Mapped[list[...]] annotation is captured and SQLAlchemy can resolve
    # the forward reference to the class defined later in the package.
    identities: Mapped[list[PatientIdentity]] = relationship(
        back_populates="patient", cascade="all, delete-orphan"
    )


class AssigningAuthority(Base):
    """An Assigning Authority (AA) / Identity Domain for patient identifiers.

    In IHE PIX/PDQ, every Patient ID (PID-3) is qualified by an Assigning
    Authority (OID or namespace). This model registers the authorities this
    PIX Manager / PDQ Supplier knows about and whether it acts as the
    authoritative source for that domain.
    """

    __tablename__ = "assigning_authorities"

    id: Mapped[int] = pk()
    # OID or URI identifier for this assigning authority (e.g. 1.2.3.4.5)
    oid: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    # Human-readable name (e.g. "Hospital MRN", "National Health ID")
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # If True, this PAXS instance is the authoritative source for IDs in this
    # domain (Patient Identity Feed consumer). If False, we only map/query.
    is_authoritative: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    # Optional description / notes
    description: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Patient identities in this domain
    identities: Mapped[list[PatientIdentity]] = relationship(
        back_populates="assigning_authority", cascade="all, delete-orphan"
    )

class PatientIdentity(Base):
    """A patient identifier in a specific Assigning Authority domain.

    This implements the core PIX cross-referencing: one local Patient row can
    have multiple PatientIdentity rows, each with a different assigning
    authority (OID) and identifier value. The combination (assigning_authority_id,
    identifier) is unique. Queries for a patient by any known ID return all
    linked IDs across all domains.
    """

    __tablename__ = "patient_identities"

    id: Mapped[int] = pk()
    # C10: identities are scoped to the tenant that owns them, so the same
    # (authority, identifier) pair can exist in two institutions.
    tenant_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("tenants.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # A-8: same NULL-collapsing sentinel as `patients.tenant_key`. The PIX
    # lookup key is (tenant, assigning authority, identifier); without this the
    # unique index below did not constrain the legacy bucket, so two concurrent
    # first-time ingests could both insert an identity for the same MRN — after
    # which `resolve_patient_identity`'s `scalar_one_or_none()` raised
    # MultipleResultsFound (an unhandled 500 on the ingest path). Nullable for
    # the same MariaDB reason as the `patients` column.
    tenant_key: Mapped[int | None] = mapped_column(
        BigInteger,
        Computed("COALESCE(tenant_id, 0)", persisted=True),
        nullable=True,
    )
    patient_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("patients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    assigning_authority_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("assigning_authorities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # The actual identifier value (e.g. "MRN12345")
    identifier: Mapped[str] = mapped_column(String(64), nullable=False)
    # Identifier type code (e.g. "MR" = Medical Record Number, "NI" = National ID)
    id_type: Mapped[str] = mapped_column(String(16), default="MR", nullable=False)
    # If True, this is the primary/preferred ID for this assigning authority
    is_primary: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    patient: Mapped[Patient] = relationship(back_populates="identities")
    assigning_authority: Mapped[AssigningAuthority] = relationship(back_populates="identities")

    __table_args__ = (
        # A patient can have at most one primary ID per assigning authority
        Index("ix_patient_identities_patient_aa", "patient_id", "assigning_authority_id"),
        # The core PIX lookup: find the patient for (tenant, AA, identifier).
        # Keyed on `tenant_key`, not `tenant_id`, so the legacy/unscoped bucket
        # is constrained too (A-8) — see the column note above.
        Index(
            "ix_patient_identities_tenant_key_aa_identifier",
            "tenant_key",
            "assigning_authority_id",
            "identifier",
            unique=True,
        ),
    )


class PatientShare(Base):
    __tablename__ = "patient_shares"

    id: Mapped[int] = pk()
    study_instance_uid: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    share_token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    phone: Mapped[str | None] = mapped_column(String(20))
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    accessed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


class PatientIdentityConflict(Base, TimestampMixin):
    """A quarantined demographic conflict (C10).

    When an ingest resolves to an existing (tenant, assigning authority, MRN)
    but carries *different* demographics, the existing validated row is left
    untouched and the discrepancy is recorded here for human reconciliation.
    Nothing is auto-merged and no clinical field is overwritten.
    """

    __tablename__ = "patient_identity_conflicts"

    id: Mapped[int] = pk()
    tenant_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("tenants.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    patient_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("patients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    study_instance_uid: Mapped[str | None] = mapped_column(String(128))
    identifier: Mapped[str] = mapped_column(String(64), nullable=False)
    assigning_authority: Mapped[str] = mapped_column(String(128), nullable=False)
    # JSON: {field: {"existing": ..., "incoming": ...}} for each mismatch.
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    # Where the conflicting values came from (AE title / user / "stow-rs").
    source: Mapped[str | None] = mapped_column(String(128))
    resolved: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False, index=True
    )

    __table_args__ = (
        Index("ix_patient_identity_conflicts_study", "study_instance_uid"),
    )
