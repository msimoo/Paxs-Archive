"""Outbound work: DIMSE destinations, routing, compression, migration, prefetch.

Every table here is a queue or a rule that drives one. The queue rows share a
contract that must not drift: a claim is a guarded ``UPDATE … WHERE
status='PENDING'`` plus a ``rowcount`` check, never a read-then-write
(AGENTS.md N-6) — the column names below are what that statement targets.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.models.base import Base, pk

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from app.db.models.study import Instance
    from app.db.models.worklist import ScheduledProcedureStep

__all__ = [
    "CompressionJob",
    "DicomNode",
    "LosslessCompressionRule",
    "LossyCompressionRule",
    "MigrationJob",
    "PrefetchJob",
    "PrefetchRule",
    "ReplicationRecord",
    "StorageCommitRequest",
    "StudyLifecycleRule",
    "TransferJob",
]


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
    __table_args__ = (
        Index("ix_commit_ae_title", "ae_title"),
        Index("ix_commit_status", "status"),
    )

    id: Mapped[int] = pk()
    tenant_id: Mapped[int | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True, index=True
    )
    transaction_uid: Mapped[str] = mapped_column(
        String(128), unique=True, nullable=False
    )
    # AE title of the requesting modality.
    ae_title: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(
        String(16), default="PENDING", server_default="PENDING", nullable=False
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
    # B-16: filesystem path to this peer's certificate (PEM), used as the SCU's
    # pinned trust anchor when no global DICOM_TLS_CA is configured. Without one
    # of the two, a production SCU refuses to open a TLS association rather than
    # encrypting to an unverified peer.
    trust_cert: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class CompressionJob(Base):
    __tablename__ = "compression_jobs"
    __table_args__ = (
        Index("ix_compression_jobs_status_lease_until", "status", "lease_until"),
    )

    id: Mapped[int] = pk()
    instance_id: Mapped[int] = mapped_column(
        ForeignKey("instances.id", ondelete="CASCADE"), nullable=False
    )
    method: Mapped[str] = mapped_column(String(16), default="RLE", nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default="PENDING", index=True, nullable=False
    )  # PENDING / RUNNING / DONE / FAILED
    owner_id: Mapped[str | None] = mapped_column(String(64))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime)
    attempt: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
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


class TransferJob(Base):
    """Durable one-shot C-STORE send or remote study retrieval request (FT-1)."""

    __tablename__ = "transfer_jobs"
    __table_args__ = (
        Index("ix_transfer_jobs_tenant_status_created", "tenant_id", "status", "created_at"),
    )

    id: Mapped[int] = pk()
    tenant_id: Mapped[int | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=True
    )
    requested_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    node_ae_title: Mapped[str] = mapped_column(String(64), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    transport: Mapped[str] = mapped_column(String(16), nullable=False)
    our_ae_title: Mapped[str] = mapped_column(String(16), nullable=False)
    study_uids_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default="PENDING", server_default="PENDING", index=True, nullable=False
    )
    include_all: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    total_items: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    completed_items: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    failed_items: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    error_code: Mapped[str | None] = mapped_column(String(32))
    error_message: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


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
    __table_args__ = (
        Index("ix_prefetch_jobs_tenant_scope_id", "tenant_id", "is_archive_wide", "id"),
    )

    id: Mapped[int] = pk()
    tenant_id: Mapped[int | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True
    )
    is_archive_wide: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
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
    generation: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0", nullable=False)
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
