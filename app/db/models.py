"""ORM models mirroring the DICOM IOD hierarchy: Patient -> Study -> Series -> Instance,
plus supporting tables (users, shares, audit log, compression jobs).

Datetimes are naive UTC. DICOM dates are stored as YYYYMMDD strings to survive
messy real-world tags losslessly.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# PKs: BIGINT on MySQL (matches the DDL docs) but plain INTEGER on SQLite,
# because SQLite only auto-increments INTEGER PRIMARY KEY columns.
BigIntPK = BigInteger().with_variant(Integer, "sqlite")


def pk():
    return mapped_column(BigIntPK, primary_key=True)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Tenant(Base):
    """A VNA (Vendor Neutral Archive) tenant — an institution, department or
    customer whose imaging data is isolated from every other tenant.

    Tenant isolation is applied at three boundaries:
      * ingest — instances stored via a DICOM AE title mapped to this tenant
        (or via STOW by a user assigned to it) are tagged with tenant_id;
      * query  — C-FIND / C-MOVE / C-GET from a mapped AE title and REST/QIDO
        browsing by an assigned user only see that tenant's studies;
      * admin  — tenant admins manage mappings; unmapped traffic and
        non-assigned users keep the legacy "unscoped" scope (tenant_id NULL),
        so pre-VNA archives keep working unchanged.
    """

    __tablename__ = "tenants"

    id: Mapped[int] = pk()
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    # Short stable slug used in URLs / labels, e.g. "rad-a".
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="1", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class TenantAeTitle(Base):
    """Maps a DICOM AE title to a tenant — the heart of "isolate per AE".

    Any association whose calling AE title is listed here (C-STORE ingest,
    C-FIND / C-MOVE / C-GET queries) operates inside that tenant's scope.
    Unmapped AE titles stay in the legacy unscoped scope.
    """

    __tablename__ = "tenant_ae_titles"

    id: Mapped[int] = pk()
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ae_title: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = pk()
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(192))
    role: Mapped[str] = mapped_column(String(16), default="PHYSICIAN", nullable=False)
    # Disabled accounts can no longer log in or hold API tokens (checked in
    # get_current_user). Defaults to active for every pre-existing row.
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("1"), nullable=False
    )
    # Brute-force lockout (auth plan §4.1): failed_attempts increments on bad
    # logins; when locked_until is in the future the account is locked and
    # login is rejected with the remaining minutes. Both reset on success.
    failed_attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    locked_until: Mapped[datetime | None] = mapped_column(DateTime)
    last_login: Mapped[datetime | None] = mapped_column(DateTime)
    last_login_ip: Mapped[str | None] = mapped_column(String(45))
    # VNA: the tenant this user belongs to. NULL = legacy scope (unscoped data).
    # Admins see every tenant regardless of this value.
    tenant_id: Mapped[int | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


class Session(Base):
    """A login session for refresh-token rotation (auth plan §3.2).

    One row per refresh token: the token itself is only ever stored as a
    SHA-256 hash, and every use rotates it (new hash, same family_id). If a
    token is ever presented that does not match the stored hash, it has been
    reused after rotation — a theft signal — and the entire session family is
    revoked (plan §4.3).
    """

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # UUIDv4
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    refresh_token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )  # SHA-256 hex of the opaque refresh token
    family_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    is_revoked: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("0"), nullable=False
    )
    revoke_reason: Mapped[str | None] = mapped_column(String(100))


class AuthAuditLog(Base):
    """Auth event trail (auth plan §3.3): login/refresh/theft/lockout/roles.

    Written on login_success, login_failed, account_locked, logout,
    token_refresh, token_theft_detected, password_change, role_changed.
    details holds small JSON context (attempt counts, reasons) — never PHI.
    """

    __tablename__ = "auth_audit_log"

    id: Mapped[int] = pk()
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    action: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False, index=True
    )
    details: Mapped[str | None] = mapped_column(Text)  # JSON string, small


class Patient(Base):
    __tablename__ = "patients"

    id: Mapped[int] = pk()
    # C10: the external MRN is only unique *within* a tenant. The composite key
    # below replaces the old global unique constraint, so two institutions can
    # both hold MRN "12345" as separate patients. NULL = legacy/unscoped row
    # (pre-VNA data), which keeps resolving exactly as before.
    tenant_id: Mapped[int | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True, index=True
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_patients_name_id", "patient_name", "patient_id"),
        # C10: one row per (tenant, MRN). Legacy rows (tenant NULL) are matched
        # explicitly by the resolver, which is what keeps their behaviour
        # unchanged — see resolve_patient_identity().
        UniqueConstraint("tenant_id", "patient_id", name="uq_patients_tenant_patient_id"),
    )

    studies: Mapped[list["Study"]] = relationship(
        back_populates="patient", cascade="all, delete-orphan"
    )

    # Patient identities in this domain (PIX): one Patient can carry multiple
    # identifiers across assigning authorities. Declared here — inside the
    # class body — so the Mapped[list["PatientIdentity"]] annotation is
    # captured in __annotations__ and SQLAlchemy can resolve the forward ref.
    identities: Mapped[list["PatientIdentity"]] = relationship(
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
        Boolean, default=False, server_default=text("0"), nullable=False
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
    identities: Mapped[list["PatientIdentity"]] = relationship(
        back_populates="assigning_authority", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_assigning_authorities_oid", "oid"),
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
        ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True, index=True
    )
    patient_id: Mapped[int] = mapped_column(
        ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    assigning_authority_id: Mapped[int] = mapped_column(
        ForeignKey("assigning_authorities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The actual identifier value (e.g. "MRN12345")
    identifier: Mapped[str] = mapped_column(String(64), nullable=False)
    # Identifier type code (e.g. "MR" = Medical Record Number, "NI" = National ID)
    id_type: Mapped[str] = mapped_column(String(16), default="MR", nullable=False)
    # If True, this is the primary/preferred ID for this assigning authority
    is_primary: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("1"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    patient: Mapped["Patient"] = relationship(back_populates="identities")
    assigning_authority: Mapped["AssigningAuthority"] = relationship(back_populates="identities")

    __table_args__ = (
        # A patient can have at most one primary ID per assigning authority
        Index("ix_patient_identities_patient_aa", "patient_id", "assigning_authority_id"),
        # The core PIX lookup: find the patient for (tenant, AA, identifier).
        Index(
            "ix_patient_identities_tenant_aa_identifier",
            "tenant_id",
            "assigning_authority_id",
            "identifier",
            unique=True,
        ),
    )


class Study(Base):
    __tablename__ = "studies"

    id: Mapped[int] = pk()
    patient_id: Mapped[int] = mapped_column(
        ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # VNA: owning tenant (NULL = legacy unscoped scope). Set on first ingest
    # from the source AE title's tenant; never rewritten on re-import.
    tenant_id: Mapped[int | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True, index=True
    )
    study_instance_uid: Mapped[str] = mapped_column(
        String(128), unique=True, nullable=False
    )
    study_date: Mapped[str | None] = mapped_column(String(8), index=True)
    study_time: Mapped[str | None] = mapped_column(String(16))
    accession_number: Mapped[str | None] = mapped_column(String(64), index=True)
    study_id: Mapped[str | None] = mapped_column(String(64))
    study_description: Mapped[str | None] = mapped_column(String(512))
    referring_physician: Mapped[str | None] = mapped_column(String(192))
    # Study Lifecycle Management (item 14): ONLINE (hot) / ARCHIVED (cold
    # tier — objects moved under the `.archive/` prefix) / (deleted = PURGED).
    lifecycle_state: Mapped[str] = mapped_column(
        String(16), default="ONLINE", server_default="ONLINE", nullable=False, index=True
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Named storage backend holding this study's objects (NULL = env default).
    storage_backend: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Radiologist reading worklist (roadmap #30 — Unread / Stat / Dictated).
    # stat_flag: manual urgent flag (auto-bucketed with pending critical
    # results + unreviewed HIGH triage); assigned_radiologist: who is reading
    # it; read_at/read_by: when a radiologist opened it (auto-derived state:
    # no report + not read = UNREAD, DRAFT report = DICTATED, FINALIZED
    # report or read = READ).
    stat_flag: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("0"), nullable=False
    )
    assigned_radiologist: Mapped[str | None] = mapped_column(String(128), index=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime)
    read_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    patient: Mapped[Patient] = relationship(back_populates="studies")
    series_list: Mapped[list["Series"]] = relationship(
        back_populates="study", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # Tenant-scoped study browsing is the hottest dashboard/QIDO query
        # (scope + date range + newest-first): composite covers it in one
        # index instead of AND-ing two single-column scans.
        Index("ix_studies_tenant_created", "tenant_id", "created_at"),
        Index("ix_studies_tenant_date", "tenant_id", "study_date"),
        # Lifecycle sweeps (archive/restore/purge) scan by state + age.
        Index("ix_studies_state_created", "lifecycle_state", "created_at"),
        # Worklist & radiologist assignment filtering
        Index("ix_studies_assigned_rad", "assigned_radiologist", "stat_flag"),
        Index("ix_studies_stat_flag", "stat_flag"),
    )


class Series(Base):
    __tablename__ = "series"

    id: Mapped[int] = pk()
    study_id: Mapped[int] = mapped_column(
        ForeignKey("studies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    series_instance_uid: Mapped[str] = mapped_column(
        String(128), unique=True, nullable=False
    )
    modality: Mapped[str | None] = mapped_column(String(16), index=True)
    series_number: Mapped[int | None] = mapped_column(Integer)
    series_description: Mapped[str | None] = mapped_column(String(255))
    body_part_examined: Mapped[str | None] = mapped_column(String(64))
    laterality: Mapped[str | None] = mapped_column(String(16))
    # Fusion (roadmap #29): series acquired in the SAME patient coordinate
    # system share a FrameOfReferenceUID — the spatial contract that lets a
    # viewer overlay PET on CT / MRI on US. NULL when absent from the source.
    frame_of_reference_uid: Mapped[str | None] = mapped_column(
        String(128), index=True
    )
    # PENDING -> claimed RUNNING -> DONE (triage must run exactly once, even
    # when a series produces no findings). server_default keeps the NOT NULL
    # constraint satisfied for raw-SQL inserts (MySQL) and matches the migration.
    triage_status: Mapped[str] = mapped_column(
        String(16), default="PENDING", server_default="PENDING", nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    study: Mapped[Study] = relationship(back_populates="series_list")
    instances: Mapped[list["Instance"]] = relationship(
        back_populates="series", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # Viewer + study-detail load every series of a study ordered by
        # series_number — one index serves both the FK filter and the sort.
        Index("ix_series_study_number", "study_id", "series_number"),
        # Triage worker claims PENDING series per study (and modality filters
        # in QIDO series queries).
        Index("ix_series_study_modality", "study_id", "modality"),
    )


class Instance(Base):
    __tablename__ = "instances"

    id: Mapped[int] = pk()
    series_id: Mapped[int] = mapped_column(
        ForeignKey("series.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sop_instance_uid: Mapped[str] = mapped_column(
        String(128), unique=True, nullable=False
    )
    sop_class_uid: Mapped[str | None] = mapped_column(String(128))
    instance_number: Mapped[int | None] = mapped_column(Integer)
    file_path: Mapped[str] = mapped_column(String(768), nullable=False)
    # Named storage backend that holds this instance's object (NULL = the
    # environment-configured default backend). Multi-storage: see
    # StorageBackendConfig / app.services.storage.
    storage_backend: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    transfer_syntax_uid: Mapped[str | None] = mapped_column(String(128))
    rows: Mapped[int | None] = mapped_column(Integer)
    columns: Mapped[int | None] = mapped_column(Integer)
    bits_allocated: Mapped[int | None] = mapped_column(Integer)
    photometric_interpretation: Mapped[str | None] = mapped_column(String(32))
    slice_thickness: Mapped[str | None] = mapped_column(String(16))
    compressed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    series: Mapped[Series] = relationship(back_populates="instances")

    __table_args__ = (
        # Viewer instance list + WADO-RS fetch both filter by series and sort
        # by instance number: composite index serves the whole read path.
        Index("ix_instances_series_number", "series_id", "instance_number"),
        # Storage-backend moves / DR replication scan by backend.
        Index("ix_instances_backend_created", "storage_backend", "created_at"),
    )


class PresentationState(Base):
    """A DICOM Grayscale Softcopy Presentation State (GSPS) object linking
    display parameters (window/level, annotations, shutters, spatial
    transforms) to one or more referenced image instances.

    SOP Class UID: 1.2.840.10008.5.1.4.1.1.11.1
    Modality: PR

    GSPS objects arrive via C-STORE or STOW-RS just like images.  The raw
    DICOM file is stored in the normal instance hierarchy; this table indexes
    the *semantic* content so the viewer and WADO-RS /rendered endpoint can
    quickly find and apply presentation states.
    """

    __tablename__ = "presentation_states"

    id: Mapped[int] = pk()
    # The PR instance's own SOP Instance UID (also in instances.sop_instance_uid).
    sop_instance_uid: Mapped[str] = mapped_column(
        String(128), unique=True, nullable=False
    )
    # The study this PR belongs to.
    study_instance_uid: Mapped[str] = mapped_column(
        String(128), index=True, nullable=False
    )
    # The series this PR belongs to (modality=PR).
    series_instance_uid: Mapped[str] = mapped_column(
        String(128), index=True, nullable=False
    )
    # ---------- GSPS display parameters (cached from the DICOM object) ------
    # Presentation Label (0070,0080) — short user-visible label.
    presentation_label: Mapped[str | None] = mapped_column(String(64))
    # Presentation Description (0070,0081).
    presentation_description: Mapped[str | None] = mapped_column(String(255))
    # Presentation Creation Date/Time.
    presentation_creation_date: Mapped[str | None] = mapped_column(String(8))
    presentation_creation_time: Mapped[str | None] = mapped_column(String(16))
    # Presentation Creator's Name (0070,0084).
    presentation_creator: Mapped[str | None] = mapped_column(String(192))
    # ---------- Softcopy VOI LUT (window/level from the PR) -----------------
    window_center: Mapped[str | None] = mapped_column(String(64))
    window_width: Mapped[str | None] = mapped_column(String(64))
    # ---------- Spatial transform -----------------------------------------
    rotation: Mapped[int | None] = mapped_column(Integer)       # 0, 90, 180, 270
    horizontal_flip: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # ---------- Referenced image SOP Instance UIDs (comma-separated) --------
    # For fast lookup; the full sequence detail is in the raw DICOM file.
    referenced_sop_instance_uids: Mapped[str | None] = mapped_column(Text)
    # ---------- File location (same key as instances.file_path) -------------
    file_path: Mapped[str] = mapped_column(String(768), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
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


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = pk()
    sop_instance_uid: Mapped[str | None] = mapped_column(String(128), index=True)
    study_instance_uid: Mapped[str | None] = mapped_column(String(128), index=True)
    sha256_hash: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(32), nullable=False, index=True)  # STORED/RETRIEVED/COMPRESSED/DELETED/SHARED
    performed_by: Mapped[str | None] = mapped_column(String(128))
    performed_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False, index=True
    )


class TriageFlag(Base):
    """An AI/algorithmic finding attached to a study (emergency triage).

    Detectors run in the background worker and write findings here; the
    dashboard surfaces unreviewed HIGH findings to radiologists.
    """

    __tablename__ = "triage_flags"

    id: Mapped[int] = pk()
    study_instance_uid: Mapped[str] = mapped_column(
        String(128), index=True, nullable=False
    )
    series_instance_uid: Mapped[str | None] = mapped_column(String(128))
    sop_instance_uid: Mapped[str | None] = mapped_column(String(128))
    finding: Mapped[str] = mapped_column(String(32), nullable=False)  # PNEUMOTHORAX, …
    severity: Mapped[str] = mapped_column(String(8), default="MEDIUM", nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    summary: Mapped[str | None] = mapped_column(String(512))
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    detector: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # Emergency queue: unreviewed HIGH findings first.
        Index("ix_triage_reviewed_severity", "reviewed", "severity"),
    )


class ScheduledProcedureStep(Base):
    """A scheduled imaging procedure served to modalities via the Modality
    Worklist SOP Class (1.2.840.10008.5.1.4.31).

    Patient demographics are NOT copied here — MWL responses JOIN `patients`,
    so worklist entries always carry the authoritative name/DOB/sex (HIS wins
    for HL7-registered patients). The step is closed (COMPLETED + linked to
    the study) when images for it arrive on the C-STORE/STOW path.
    """

    __tablename__ = "scheduled_procedure_steps"

    id: Mapped[int] = pk()
    patient_id: Mapped[int] = mapped_column(
        ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sps_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)  # (0040,0009)
    accession_number: Mapped[str | None] = mapped_column(String(64), index=True)
    modality: Mapped[str | None] = mapped_column(String(16), index=True)
    scheduled_station_aet: Mapped[str | None] = mapped_column(String(64), index=True)
    start_date: Mapped[str | None] = mapped_column(String(8), index=True)  # (0040,0002)
    start_time: Mapped[str | None] = mapped_column(String(16))  # (0040,0003)
    # SCHEDULED / COMPLETED / CANCELED
    status: Mapped[str] = mapped_column(
        String(16), default="SCHEDULED", server_default="SCHEDULED", nullable=False
    )
    requested_procedure_id: Mapped[str | None] = mapped_column(String(64))
    requested_procedure_description: Mapped[str | None] = mapped_column(String(255))
    scheduled_performing_physician: Mapped[str | None] = mapped_column(String(192))
    # Closed loop: set when the images for this step actually arrive.
    study_instance_uid: Mapped[str | None] = mapped_column(String(128), index=True)
    performed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    patient: Mapped[Patient] = relationship()

    __table_args__ = (
        # The worklist C-FIND query filters by status + station (+ modality);
        # the dashboard counts by status alone — both served by this prefix.
        Index("ix_sps_status_station", "status", "scheduled_station_aet"),
        Index("ix_sps_status_modality", "status", "modality"),
    )


class PerformedProcedureStep(Base):
    """A Modality Performed Procedure Step (MPPS) instance — the modality's
    own report of an exam lifecycle (N-CREATE = IN PROGRESS, N-SET =
    COMPLETED/DISCONTINUED).

    Ties the A1 scheduled procedure step (worklist) to the stored study: when
    the modality reports COMPLETED it carries the Performed Series Sequence
    with the Study/Series UIDs, closing the scheduled -> performed -> stored
    chain with the scanner as source of truth (no C-STORE inference needed).
    """

    __tablename__ = "performed_procedure_steps"

    id: Mapped[int] = pk()
    sop_instance_uid: Mapped[str] = mapped_column(
        String(128), unique=True, nullable=False
    )
    patient_id: Mapped[int | None] = mapped_column(
        ForeignKey("patients.id", ondelete="SET NULL"), nullable=True, index=True
    )
    scheduled_step_id: Mapped[int | None] = mapped_column(
        ForeignKey("scheduled_procedure_steps.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # IN PROGRESS / COMPLETED / DISCONTINUED
    status: Mapped[str] = mapped_column(
        String(16), default="IN PROGRESS", server_default="IN PROGRESS",
        nullable=False, index=True,
    )
    pps_id: Mapped[str | None] = mapped_column(String(64))          # (0040,0253)
    pps_description: Mapped[str | None] = mapped_column(String(255))  # (0040,0254)
    modality: Mapped[str | None] = mapped_column(String(16))
    performed_station_aet: Mapped[str | None] = mapped_column(String(64))
    accession_number: Mapped[str | None] = mapped_column(String(64), index=True)
    start_date: Mapped[str | None] = mapped_column(String(8))
    start_time: Mapped[str | None] = mapped_column(String(16))
    end_date: Mapped[str | None] = mapped_column(String(8))
    end_time: Mapped[str | None] = mapped_column(String(16))
    study_instance_uid: Mapped[str | None] = mapped_column(String(128), index=True)
    series_instance_uid: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    patient: Mapped[Patient | None] = relationship()
    scheduled_step: Mapped[ScheduledProcedureStep | None] = relationship()


class StorageCommitRequest(Base):
    """A Storage Commitment Push Model transaction (SOP Class 1.2.840.10008.1.20.1).

    A modality asks "will you guarantee these instances are archived?" via
    N-ACTION; PAXS verifies every referenced instance against the active
    storage backend and answers *asynchronously* with an N-EVENT-REPORT
    (EventTypeID=1). `payload_json` keeps the request + per-instance results
    so reports can be re-sent (retry queue) and the dashboard can show exactly
    why each instance passed or failed.

    status: PENDING (received, verification/report outstanding) / COMPLETED
    (report delivered) / FAILED (report could not be delivered) / EXPIRED
    (retention follow-up, unused in this slice).
    """

    __tablename__ = "commitment_statuses"

    id: Mapped[int] = pk()
    transaction_uid: Mapped[str] = mapped_column(
        String(128), unique=True, nullable=False
    )
    # AE title of the requesting modality.
    ae_title: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(
        String(16), default="PENDING", server_default="PENDING", nullable=False, index=True
    )
    # JSON: {ae_title, requested: [{sop_class_uid, sop_instance_uid}],
    #        results: [{sop_instance_uid, ok, failure_reason?}]}
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class DicomNode(Base):
    """A registered DICOM peer (destination AE) used to resolve C-MOVE
    destinations and as a forwarding target for auto-routing.

    C-MOVE requests name their destination by AE title; this registry maps
    that title to a concrete host/port so the SCP can open the C-STORE
    sub-operation association.

    DICOMweb (REST) transport: when ``web_url`` is set (the peer's DICOMweb
    service base, e.g. ``https://peer.example/dicom-web``) the node can also
    be queried and retrieved over QIDO-RS / WADO-RS instead of DIMSE — the
    dashboard Nodes panel exposes Web Query / Web Retrieve actions. Optional
    HTTP Basic credentials (``web_username`` / ``web_password``) are sent
    when configured; the client enforces SSRF guards before connecting.
    """

    __tablename__ = "dicom_nodes"

    id: Mapped[int] = pk()
    ae_title: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str | None] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # DICOMweb REST base URL (optional). When empty the node is DIMSE-only.
    web_url: Mapped[str | None] = mapped_column(String(512))
    web_username: Mapped[str | None] = mapped_column(String(128))
    web_password: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class CompressionJob(Base):
    __tablename__ = "compression_jobs"

    id: Mapped[int] = pk()
    instance_id: Mapped[int] = mapped_column(
        ForeignKey("instances.id", ondelete="CASCADE"), nullable=False
    )
    method: Mapped[str] = mapped_column(String(16), default="RLE", nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default="PENDING", index=True, nullable=False
    )  # PENDING / RUNNING / DONE / FAILED
    original_size: Mapped[int | None] = mapped_column(BigInteger)
    compressed_size: Mapped[int | None] = mapped_column(BigInteger)
    ratio_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)

    instance: Mapped[Instance] = relationship()


class MigrationJob(Base):
    """A bulk migration job tracking either folder watching or remote C-MOVE batch retrieval.

    job_type: FOLDER_WATCH / CMOVE_BATCH
    status: PENDING / RUNNING / COMPLETED / FAILED / STOPPED
    source_info: Folder path for FOLDER_WATCH or AE Title / filter JSON for CMOVE_BATCH
    """

    __tablename__ = "migration_jobs"

    id: Mapped[int] = pk()
    job_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default="PENDING", server_default="PENDING", index=True, nullable=False
    )
    source_info: Mapped[str] = mapped_column(String(512), nullable=False)
    total_items: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    processed_items: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_items: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class CriticalResult(Base):
    """A critical or urgent clinical finding attached to a study.

    Tracks creation by radiologist/AI, severity level (CRITICAL_RED / URGENT_ORANGE / ACTIONABLE_YELLOW),
    notification target, and mandatory legal/compliance acknowledgement by referring physicians.
    """

    __tablename__ = "critical_results"

    id: Mapped[int] = pk()
    study_instance_uid: Mapped[str] = mapped_column(
        String(128), index=True, nullable=False
    )
    series_instance_uid: Mapped[str | None] = mapped_column(String(128))
    sop_instance_uid: Mapped[str | None] = mapped_column(String(128))
    patient_id: Mapped[str | None] = mapped_column(String(64), index=True)
    # CRITICAL_RED / URGENT_ORANGE / ACTIONABLE_YELLOW
    finding_level: Mapped[str] = mapped_column(
        String(32), default="CRITICAL_RED", server_default="CRITICAL_RED", nullable=False, index=True
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    reported_by: Mapped[str] = mapped_column(String(128), nullable=False)
    communicated_to: Mapped[str | None] = mapped_column(String(192))
    communication_channel: Mapped[str | None] = mapped_column(String(32))  # PHONE / WEBSOCKET / SMS / EMAIL
    # PENDING_ACK / ACKNOWLEDGED / ESCALATED / EXPIRED
    status: Mapped[str] = mapped_column(
        String(32), default="PENDING_ACK", server_default="PENDING_ACK", nullable=False, index=True
    )
    acknowledged_by: Mapped[str | None] = mapped_column(String(128))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime)
    acknowledgement_notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        # Dashboard + STAT worklist scan pending results per study.
        Index("ix_critical_status_study", "status", "study_instance_uid"),
        Index("ix_critical_created", "created_at"),
    )


class RoutingRule(Base):
    """An auto-routing rule evaluated when new DICOM studies arrive.

    If incoming DICOM study metadata matches the specified filters (modality,
    body_part, source_aet, study_description_pattern), PAXS automatically
    dispatches C-STORE requests to the destination DICOM node.
    """

    __tablename__ = "routing_rules"

    id: Mapped[int] = pk()
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    destination_node_id: Mapped[int] = mapped_column(
        ForeignKey("dicom_nodes.id", ondelete="CASCADE"), nullable=False
    )
    modality: Mapped[str | None] = mapped_column(String(16))
    body_part_examined: Mapped[str | None] = mapped_column(String(64))
    source_aet: Mapped[str | None] = mapped_column(String(64))
    study_description_pattern: Mapped[str | None] = mapped_column(String(255))
    delay_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    destination_node: Mapped[DicomNode] = relationship()


class RoutingQueueItem(Base):
    """A durable auto-routing dispatch job (Phase C1 send queue).

    When a routing rule matches a study, PAXS enqueues one item per
    (study, destination node) instead of dispatching fire-and-forget. The
    background worker claims PENDING items, C-STORE's the study to the
    destination node and marks COMPLETED; failed sends are retried with
    exponential backoff up to `max_attempts`, then flagged FAILED for a
    human (admin panel) to review and retry.

    status: PENDING (due) / SENDING (claimed by worker) / COMPLETED /
    FAILED (exhausted retries).
    """

    __tablename__ = "routing_queue"

    id: Mapped[int] = pk()
    study_instance_uid: Mapped[str] = mapped_column(
        String(128), index=True, nullable=False
    )
    rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("routing_rules.id", ondelete="SET NULL"), nullable=True
    )
    node_id: Mapped[int] = mapped_column(
        ForeignKey("dicom_nodes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(16), default="PENDING", server_default="PENDING", nullable=False, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    last_error: Mapped[str | None] = mapped_column(Text)
    sent_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    rule: Mapped[RoutingRule | None] = relationship()
    node: Mapped[DicomNode] = relationship()


class KeyImageNote(Base):
    """A DICOM Key Object Selection Document (KIN) object (SOP Class 1.2.840.10008.5.1.4.1.1.88.22).

    Modality: KO
    Bookmarks significant images with standardized concept titles (e.g. Of Interest,
    For Quality Control, For Teaching) and radiologist notes.
    """

    __tablename__ = "key_image_notes"

    id: Mapped[int] = pk()
    sop_instance_uid: Mapped[str] = mapped_column(
        String(128), unique=True, nullable=False
    )
    study_instance_uid: Mapped[str] = mapped_column(
        String(128), index=True, nullable=False
    )
    series_instance_uid: Mapped[str] = mapped_column(
        String(128), index=True, nullable=False
    )
    title_concept_code: Mapped[str | None] = mapped_column(String(64))
    title_concept_text: Mapped[str | None] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text)
    author_name: Mapped[str | None] = mapped_column(String(192))
    referenced_sop_instance_uids: Mapped[str | None] = mapped_column(Text)
    file_path: Mapped[str] = mapped_column(String(768), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


class StudyLifecycleRule(Base):
    """A Study Lifecycle Management rule (auto-archive / restore / purge).

    Evaluated on a schedule (background worker) and on demand: studies whose
    age exceeds ``min_age_days`` and whose modality / body part match are
    moved through the lifecycle:

      * ARCHIVE — migrate objects to the cold tier (``.archive/`` prefix) and
        mark the study ARCHIVED (availability OFFLINE/NEARLINE semantics).
      * RESTORE — bring an ARCHIVED study back to the hot tier.
      * PURGE   — delete the study's objects and metadata (retention expiry).
    """

    __tablename__ = "study_lifecycle_rules"

    id: Mapped[int] = pk()
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    # ARCHIVE / RESTORE / PURGE
    action: Mapped[str] = mapped_column(String(16), default="ARCHIVE", nullable=False)
    min_age_days: Mapped[int] = mapped_column(Integer, default=365, nullable=False)
    modality: Mapped[str | None] = mapped_column(String(16))
    body_part_examined: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class LossyCompressionRule(Base):
    """A rule specifying lossy compression policy for aging PACS studies.

    Transcodes stored studies exceeding min_age_days to JPEG/JPEG-LS lossy
    formats with quality_factor Q, reducing archive storage consumption.
    """

    __tablename__ = "lossy_compression_rules"

    id: Mapped[int] = pk()
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    min_age_days: Mapped[int] = mapped_column(Integer, default=365, nullable=False)
    modality: Mapped[str | None] = mapped_column(String(16))
    body_part_examined: Mapped[str | None] = mapped_column(String(64))
    transfer_syntax_uid: Mapped[str] = mapped_column(
        # Default to JPEG-LS Near-Lossless (1.2.840.10008.1.2.4.81): pydicom
        # implements no JPEG Baseline (…4.50) encoder, so a baseline default
        # could only ever fail. Both supported targets preserve 16-bit samples.
        String(128), default="1.2.840.10008.1.2.4.81", nullable=False
    )
    quality_factor: Mapped[int] = mapped_column(Integer, default=85, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class LosslessCompressionRule(Base):
    """A rule specifying lossless compression policy for PACS studies.

    Transcodes stored studies exceeding min_age_days to lossless formats
    (RLE, JPEG-LS, JPEG2000, HTJ2K) for storage optimization without
    diagnostic quality loss.
    """

    __tablename__ = "lossless_compression_rules"

    id: Mapped[int] = pk()
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    min_age_days: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    modality: Mapped[str | None] = mapped_column(String(16))
    body_part_examined: Mapped[str | None] = mapped_column(String(64))
    method: Mapped[str] = mapped_column(
        String(16), default="JPEG2000", nullable=False
    )  # RLE, JPEG-LS, JPEG2000, HTJ2K
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class StorageBackendConfig(Base):
    """A named storage backend for DICOM objects (multi-storage).

    ``kind`` is ``local`` (filesystem under ``root_path``) or ``s3`` (an
    S3-compatible object store — AWS S3 / MinIO). At most one row is the
    default; when no default row exists, the environment-configured backend
    (``STORAGE_BACKEND`` / ``S3_*``) remains the active default.

    ``instances.storage_backend`` records which backend holds each object;
    NULL means the environment default. Studies can be moved between backends
    with the Admin → Storage → Move action (cross-backend copy + delete).
    """

    __tablename__ = "storage_backends"

    id: Mapped[int] = pk()
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)  # local | s3
    is_default: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("0"), nullable=False
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("1"), nullable=False
    )
    # --- local ---
    root_path: Mapped[str | None] = mapped_column(String(512))
    # --- s3 ---
    endpoint_url: Mapped[str | None] = mapped_column(String(512))
    region: Mapped[str | None] = mapped_column(String(64))
    bucket: Mapped[str | None] = mapped_column(String(255))
    access_key: Mapped[str | None] = mapped_column(String(255))
    secret_key: Mapped[str | None] = mapped_column(String(255))
    presign_expires: Mapped[int] = mapped_column(Integer, default=900, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


class StorageQuota(Base):
    """Storage quota limits for tenants and/or storage backends.

    Enforces maximum storage consumption at two granularities:
      * tenant-scoped: limits total bytes across all backends for a tenant
      * backend-scoped: limits total bytes on a specific backend (all tenants)

    When both apply, the stricter limit wins. A quota of 0 or NULL means
    unlimited. The `warning_threshold_pct` triggers advisory alerts (logged
    and exposed via API) when usage crosses the percentage but does not
    block writes.
    """

    __tablename__ = "storage_quotas"

    id: Mapped[int] = pk()
    # Scope: exactly one of tenant_id or storage_backend_name must be set
    tenant_id: Mapped[int | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True, index=True
    )
    storage_backend_name: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    max_bytes: Mapped[int] = mapped_column(
        BigInteger, default=0, nullable=False
    )
    # H08: reserved+committed bytes for this scope. A denormalised counter is
    # what makes check-and-reserve a single atomic UPDATE, closing the
    # check-then-act race. `recompute_used_bytes()` reconciles it if it drifts.
    used_bytes: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )
    warning_threshold_pct: Mapped[int] = mapped_column(
        Integer, default=80, nullable=False
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("1"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    tenant: Mapped["Tenant | None"] = relationship()

    __table_args__ = (
        Index("ix_storage_quotas_tenant", "tenant_id"),
        Index("ix_storage_quotas_backend", "storage_backend_name"),
    )


class InstanceAvailabilityNotification(Base):
    """A DICOM Instance Availability Notification (IAN) transaction (SOP Class 1.2.840.10008.5.1.4.33).

    Sent via N-CREATE DIMSE to listening RIS/EMR systems when new studies finish arriving at PAXS.
    Tracks target AE title, availability status (ONLINE/NEARLINE/OFFLINE), payload JSON, and delivery status.
    """

    __tablename__ = "ian_notifications"

    id: Mapped[int] = pk()
    study_instance_uid: Mapped[str] = mapped_column(
        String(128), index=True, nullable=False
    )
    target_ae_title: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    instance_availability: Mapped[str] = mapped_column(
        String(16), default="ONLINE", server_default="ONLINE", nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), default="PENDING", server_default="PENDING", index=True, nullable=False
    )
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    error_message: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class PrefetchRule(Base):
    """An automated prefetching rule triggered when Modality Worklist steps or ADT triggers occur.

    Queries remote DICOM archive nodes for historical prior studies matching Patient ID,
    Modality, and Body Part, automatically triggering background C-MOVE requests so priors
    are ready in local PAXS storage before radiologist interpretation.
    """

    __tablename__ = "prefetch_rules"

    id: Mapped[int] = pk()
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    modality: Mapped[str | None] = mapped_column(String(16))
    body_part_examined: Mapped[str | None] = mapped_column(String(64))
    max_prior_age_days: Mapped[int] = mapped_column(Integer, default=730, nullable=False)
    max_priors_count: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    source_node_id: Mapped[int] = mapped_column(
        ForeignKey("dicom_nodes.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    source_node: Mapped[DicomNode] = relationship()


class PrefetchJob(Base):
    """A log & status tracking record for automated prior study prefetch requests."""

    __tablename__ = "prefetch_jobs"

    id: Mapped[int] = pk()
    patient_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    worklist_step_id: Mapped[int | None] = mapped_column(
        ForeignKey("scheduled_procedure_steps.id", ondelete="SET NULL")
    )
    source_node_id: Mapped[int] = mapped_column(
        ForeignKey("dicom_nodes.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), default="PENDING", server_default="PENDING", index=True, nullable=False
    )
    priors_found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    priors_retrieved: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    source_node: Mapped[DicomNode] = relationship()
    worklist_step: Mapped[ScheduledProcedureStep | None] = relationship()


class RadiationDoseReport(Base):
    """A DICOM Radiation Dose Structured Report (RDSR) (SOP Class 1.2.840.10008.5.1.4.1.1.88.67).

    Stores total DLP (mGy·cm), CTDIvol (mGy), Dose RP (mGy), irradiation event count, and scanner info
    for radiation dose monitoring, patient cumulative exposure tracking, and regulatory audit compliance.
    """

    __tablename__ = "radiation_dose_reports"

    id: Mapped[int] = pk()
    sop_instance_uid: Mapped[str] = mapped_column(
        String(128), unique=True, nullable=False
    )
    study_instance_uid: Mapped[str] = mapped_column(
        String(128), index=True, nullable=False
    )
    series_instance_uid: Mapped[str] = mapped_column(
        String(128), index=True, nullable=False
    )
    patient_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    modality: Mapped[str] = mapped_column(String(16), default="CT", nullable=False)
    total_dlp_mgy_cm: Mapped[float | None] = mapped_column(Float)
    total_ctdivol_mgy: Mapped[float | None] = mapped_column(Float)
    total_dose_rp_mgy: Mapped[float | None] = mapped_column(Float)
    irradiation_events_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    manufacturer: Mapped[str | None] = mapped_column(String(128))
    device_model: Mapped[str | None] = mapped_column(String(128))
    raw_payload_json: Mapped[str | None] = mapped_column(Text)
    file_path: Mapped[str] = mapped_column(String(768), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


class XDSDocument(Base):
    """An IHE XDS-I.b (Cross-Enterprise Document Sharing for Imaging) Document Registry Entry.

    Tracks ebXML metadata (entry_uuid, unique_id, class_code, format_code, availability_status)
    enabling health information exchange (HIE) document registry queries [ITI-18] and retrieval [ITI-43].
    """

    __tablename__ = "xds_documents"

    id: Mapped[int] = pk()
    entry_uuid: Mapped[str] = mapped_column(
        String(128), unique=True, nullable=False, index=True
    )
    unique_id: Mapped[str] = mapped_column(
        String(128), unique=True, nullable=False, index=True
    )
    patient_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    study_instance_uid: Mapped[str | None] = mapped_column(String(128), index=True)
    document_title: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(64), default="application/dicom", nullable=False)
    class_code: Mapped[str] = mapped_column(String(64), default="IMAGES", nullable=False)
    type_code: Mapped[str] = mapped_column(String(64), default="RADIOLOGY", nullable=False)
    format_code: Mapped[str] = mapped_column(String(128), default="urn:ihe:rad:TEXT:2011", nullable=False)
    repository_unique_id: Mapped[str] = mapped_column(
        String(128), default="1.2.826.0.1.3680043.8.498.REPO", nullable=False
    )
    availability_status: Mapped[str] = mapped_column(
        String(32), default="APPROVED", server_default="APPROVED", index=True, nullable=False
    )
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sha256_hash: Mapped[str | None] = mapped_column(String(64))
    file_path: Mapped[str] = mapped_column(String(768), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class HangingProtocol(Base):
    """A DICOM / Viewer Hanging Protocol defining viewport layout rules for medical image viewers.

    Controls viewport grid structure (1x1, 1x2, 2x2), window/level presets, and series ordering
    matching modality, body part examined, or specific radiologist preferences.
    """

    __tablename__ = "hanging_protocols"

    id: Mapped[int] = pk()
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(255))
    modality: Mapped[str | None] = mapped_column(String(16), index=True)
    body_part_examined: Mapped[str | None] = mapped_column(String(64), index=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    priority: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    layout_rows: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    layout_columns: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    configuration_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ClinicalProtocol(Base):
    """Clinical Protocols Library (roadmap #31).

    Maps study descriptions / modalities / body parts to a standardized
    clinical protocol (a normalized procedure definition with a local RIS
    code). The archive auto-suggests the best protocol for an incoming study
    (and the dashboard lets admins curate the library + test matches).

    ``study_description_pattern`` is a case-insensitive substring (or comma-
    separated alternatives) matched against Study.study_description; when it
    matches, the protocol is a candidate regardless of modality/body part.
    ``priority`` breaks ties (lower = stronger).
    """

    __tablename__ = "clinical_protocols"

    id: Mapped[int] = pk()
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(512))
    # Standardized protocol name (e.g. "CT CHEST W CONTRAST").
    standardized_name: Mapped[str | None] = mapped_column(String(255), index=True)
    # Local RIS code the hospital uses for this protocol.
    ris_code: Mapped[str | None] = mapped_column(String(64), index=True)
    modality: Mapped[str | None] = mapped_column(String(16), index=True)
    body_part_examined: Mapped[str | None] = mapped_column(String(64), index=True)
    # Case-insensitive substring / comma-separated alternatives matched
    # against study descriptions (e.g. "CHEST,THORAX").
    study_description_pattern: Mapped[str | None] = mapped_column(String(255))
    priority: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("1"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        # Auto-suggestion queries match on modality + body part together.
        Index("ix_clinical_protocols_modality_bodypart", "modality", "body_part_examined"),
        Index("ix_clinical_protocols_enabled_priority", "enabled", "priority"),
    )


class RadiologyReport(Base):
    """A finalized radiology report attached to a study (E2 — HL7 ORU
    write-back to the EMR).

    One report per study (unique study_id): the radiologist drafts it,
    then FINALIZES it — which stamps ``finalized_at`` and (when the EMR
    MLLP channel is enabled) sends an ORU^R01 observation message carrying
    the findings + impression to the hospital EMR. Delivery state lives on
    the row (``oru_status`` / ``oru_sent_at`` / ``oru_error``) so failed
    deliveries stay visible and can be re-sent.
    """

    __tablename__ = "reports"

    id: Mapped[int] = pk()
    study_id: Mapped[int] = mapped_column(
        ForeignKey("studies.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    # Denormalized for dashboard rows / HL7 PID without joining every time.
    study_instance_uid: Mapped[str] = mapped_column(
        String(128), index=True, nullable=False
    )
    patient_id: Mapped[str | None] = mapped_column(String(64), index=True)
    # DRAFT (editable) / FINALIZED (locked + ORU-worthy)
    status: Mapped[str] = mapped_column(
        String(16), default="DRAFT", server_default="DRAFT", nullable=False, index=True
    )
    clinical_history: Mapped[str | None] = mapped_column(Text)
    findings: Mapped[str | None] = mapped_column(Text)
    impression: Mapped[str | None] = mapped_column(Text)
    radiologist: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime)
    # ORU delivery state: PENDING (finalized, not yet sent) / SENT / FAILED
    oru_status: Mapped[str] = mapped_column(
        String(16), default="PENDING", server_default="PENDING", nullable=False, index=True
    )
    oru_sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    oru_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    study: Mapped[Study] = relationship()

    __table_args__ = (
        # Reading-worklist + EMR counters filter by status and ORU delivery.
        Index("ix_reports_status_oru", "status", "oru_status"),
    )


class ReportAddendum(Base, TimestampMixin):
    """An append-only addendum to a FINALIZED report (C06).

    A finalized report is immutable: its ``clinical_history`` / ``findings`` /
    ``impression`` are the signed record and must never be rewritten. Follow-up
    observations, corrections and clarifications are recorded here instead, so
    the original text is preserved verbatim alongside the addition and both stay
    attributable to the authenticated author.
    """

    __tablename__ = "report_addenda"

    id: Mapped[int] = pk()
    report_id: Mapped[int] = mapped_column(
        ForeignKey("reports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The authenticated actor who added it (never a client-supplied name).
    author: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    text: Mapped[str] = mapped_column(Text, nullable=False)

    report: Mapped[RadiologyReport] = relationship()


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
        ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True, index=True
    )
    patient_id: Mapped[int] = mapped_column(
        ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    study_instance_uid: Mapped[str | None] = mapped_column(String(128), index=True)
    identifier: Mapped[str] = mapped_column(String(64), nullable=False)
    assigning_authority: Mapped[str] = mapped_column(String(128), nullable=False)
    # JSON: {field: {"existing": ..., "incoming": ...}} for each mismatch.
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    # Where the conflicting values came from (AE title / user / "stow-rs").
    source: Mapped[str | None] = mapped_column(String(128))
    resolved: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("0"), nullable=False, index=True
    )


class PeerReviewCase(Base):
    """A Peer Review / QA case for RADPEER scoring.

    Studies are assigned (anonymized) to a reviewing radiologist who was *not*
    the original reader.  The reviewer scores the original interpretation using
    the ACR RADPEER scale (1-4) and may add free-text comments.

    Scale:
      1 — Concur with interpretation
      2 — Discrepancy / not clinically significant
      3 — Discrepancy / clinically significant – unlikely to result in adverse outcome
      4 — Discrepancy / clinically significant – likely to result in adverse outcome
    """

    __tablename__ = "peer_review_cases"

    id: Mapped[int] = pk()
    study_instance_uid: Mapped[str] = mapped_column(
        String(128), index=True, nullable=False
    )
    patient_id: Mapped[str | None] = mapped_column(String(64), index=True)
    modality: Mapped[str | None] = mapped_column(String(16), index=True)
    body_part_examined: Mapped[str | None] = mapped_column(String(64))
    # Original reader & reviewer
    original_reader: Mapped[str] = mapped_column(String(128), nullable=False)
    reviewer: Mapped[str | None] = mapped_column(String(128), index=True)
    # Status lifecycle: PENDING -> ASSIGNED -> COMPLETED / CANCELLED
    status: Mapped[str] = mapped_column(
        String(16), default="PENDING", server_default="PENDING",
        nullable=False, index=True,
    )
    # RADPEER score (1-4), NULL until reviewed
    score: Mapped[int | None] = mapped_column(Integer)
    comments: Mapped[str | None] = mapped_column(Text)
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ReplicationRecord(Base):
    """DR replication state for one study (roadmap #49).

    The worker (and the Admin → Replication panel) copies every object of a
    study to an off-site S3/Azure-Blob-compatible target backend and records
    the outcome here. ``study_instance_uid`` is unique — re-enqueueing an
    already-REPLICATED study is a no-op until its objects change.
    """

    __tablename__ = "replication_records"

    id: Mapped[int] = pk()
    study_instance_uid: Mapped[str] = mapped_column(
        String(128), unique=True, index=True, nullable=False
    )
    target: Mapped[str] = mapped_column(String(128), nullable=False)  # backend name or "env-s3"
    status: Mapped[str] = mapped_column(
        String(16), default="PENDING", server_default="PENDING", nullable=False
    )  # PENDING / REPLICATED / FAILED
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    object_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    bytes_replicated: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0", nullable=False
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    first_attempted_at: Mapped[datetime | None] = mapped_column(DateTime)
    replicated_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        # Worker sweep claims PENDING replicas (and retries FAILED).
        Index("ix_replication_status_attempts", "status", "attempts"),
    )


class ServerSetting(Base):
    """A runtime-editable server configuration override (admin Settings UI).

    The Settings view in the dashboard reads the live value of every editable
    key as ``DB override (this table) ?? env default (app.core.config.settings)``
    and writes changes here. On startup PAXS applies all stored overrides to
    the in-process ``settings`` object, so persisted changes survive restarts
    for the keys that are consumed at boot (AE title, ports, toggles). Keys
    whose consumers bind at import time (routers, listeners) carry a
    "restart required" hint in the UI registry.
    """

    __tablename__ = "server_settings"

    id: Mapped[int] = pk()
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class OrganizationSettings(Base):
    """Institutional branding, header/footer HTML, logo path and contact info for reports."""

    __tablename__ = "organization_settings"

    id: Mapped[int] = pk()
    org_name_ar: Mapped[str | None] = mapped_column(String(255))
    org_name_en: Mapped[str | None] = mapped_column(String(255))
    logo_path: Mapped[str | None] = mapped_column(String(512))
    header_html_text: Mapped[str | None] = mapped_column(Text)
    footer_html_text: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(String(64))
    email: Mapped[str | None] = mapped_column(String(128))
    address: Mapped[str | None] = mapped_column(String(512))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ReportTemplate(Base):
    """Reusable diagnostic report template with modality and Jinja2 placeholders."""

    __tablename__ = "report_templates"

    id: Mapped[int] = pk()
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    modality: Mapped[str | None] = mapped_column(String(32), index=True)
    body_findings_template: Mapped[str | None] = mapped_column(Text)
    default_impression_template: Mapped[str | None] = mapped_column(Text)
    is_default: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

