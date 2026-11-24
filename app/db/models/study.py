"""Core DICOM IOD entities: Study -> Series -> Instance.

This is the spine of the archive: every other table hangs off these rows
(reports, shares, triage flags, GSPS, worklist steps, audit rows), so their
column definitions are a **public contract** — they are created by migrations
under ``migrations/versions/``, not by ``create_all``, and renaming or retyping
a column here changes nothing in a running system (AGENTS.md N-7).

``Patient`` lives in :mod:`app.db.models.patient` (it is the identity root, not
a study entity). The relationship targets below are string-based, so the mapper
only needs the class imported before ``configure_mappers()`` — which the package
``__init__`` guarantees.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import false
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.models.base import AutoBigIntPK, Base

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from app.db.models.clinical import RadiologyReport
    from app.db.models.patient import Patient

__all__ = [
    "Instance",
    "Series",
    "Study",
]


class Study(Base):
    """A DICOM Study — one imaging exam, belonging to exactly one patient.

    ``created_at`` is declared explicitly rather than taken from
    :class:`TimestampMixin` because the shipped column is **indexed** (newest-first
    browsing), and a mixin cannot carry that index without also adding one to
    every other model that uses it.
    """

    __tablename__ = "studies"

    id: Mapped[int] = mapped_column(
        AutoBigIntPK, primary_key=True, autoincrement=True
    )
    # ondelete is CASCADE, not RESTRICT: the archive's own delete paths remove a
    # study (and its series/instances) as one unit, and A-10 guarantees a patient
    # merge never deletes studies. A RESTRICT FK here would need a data migration
    # and would break the documented study-delete flow.
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
    # stat_flag: manual urgent flag (auto-bucketed with pending critical results
    # + unreviewed HIGH triage); assigned_radiologist: who is reading it;
    # read_at/read_by: when a radiologist opened it (auto-derived state: no
    # report + not read = UNREAD, DRAFT report = DICTATED, FINALIZED report or
    # read = READ).
    stat_flag: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    assigned_radiologist: Mapped[str | None] = mapped_column(String(128), index=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime)
    read_by: Mapped[str | None] = mapped_column(String(128))
    # Reading claim (plans/09 C1): who has the study open for reporting and the
    # time of their last heartbeat. A claim older than READING_CLAIM_TTL_SECONDS
    # is stale and free to take. Advisory: it warns a second reader, it never
    # blocks image access (see app/services/reading_claim_service.py).
    claimed_by: Mapped[str | None] = mapped_column(String(128))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    patient: Mapped[Patient] = relationship(back_populates="studies")
    series_list: Mapped[list[Series]] = relationship(
        back_populates="study", cascade="all, delete-orphan"
    )
    # C-19: clinical reports hang off the study but must OUTLIVE it. The empty
    # cascade set is deliberate — no delete, no delete-orphan, and no
    # save-update either, so nothing here can ever remove a signed document as a
    # side effect of purging images. The database enforces the same rule with
    # `reports.study_id ON DELETE RESTRICT`.
    reports: Mapped[list[RadiologyReport]] = relationship(
        back_populates="study", cascade=""
    )

    __table_args__ = (
        # Tenant-scoped study browsing is the hottest dashboard/QIDO query
        # (scope + date range + newest-first): composite covers it in one index
        # instead of AND-ing two single-column scans.
        Index("ix_studies_tenant_created", "tenant_id", "created_at"),
        Index("ix_studies_tenant_date", "tenant_id", "study_date"),
        # Lifecycle sweeps (archive/restore/purge) scan by state + age.
        Index("ix_studies_state_created", "lifecycle_state", "created_at"),
        # Worklist & radiologist assignment filtering
        Index("ix_studies_assigned_rad", "assigned_radiologist", "stat_flag"),
        Index("ix_studies_stat_flag", "stat_flag"),
    )


class Series(Base):
    """A DICOM Series — one acquisition within a study.

    ``ModalitiesInStudy`` and ``NumberOfInstances`` are **not** columns here:
    they are per-study aggregates, derived by query (see
    ``app/services/query_service.py``). Storing them would need a migration, a
    trigger or a maintained counter, and would be a denormalisation with a drift
    risk for a value the study browser already computes.
    """

    __tablename__ = "series"

    id: Mapped[int] = mapped_column(
        AutoBigIntPK, primary_key=True, autoincrement=True
    )
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
    frame_of_reference_uid: Mapped[str | None] = mapped_column(String(128), index=True)
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
    instances: Mapped[list[Instance]] = relationship(
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
    """A DICOM Instance (SOP Instance) — one stored object on a storage tier.

    ``storage_backend`` is the tier: it names the backend that holds the object
    (NULL = the environment-configured default), and it is what
    ``lifecycle_service`` moves between hot and cold. ``file_size_bytes`` is the
    stored size, used by quota accounting and the S3 usage scan.
    """

    __tablename__ = "instances"

    id: Mapped[int] = mapped_column(
        AutoBigIntPK, primary_key=True, autoincrement=True
    )
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
    # WADO-RS metadata (PS3.18 F) for this instance, already serialised as one
    # compact DICOM JSON object. Written once at ingest, where the dataset is
    # already parsed, so `GET …/metadata` never re-reads or re-parses the object.
    # NULL on rows ingested before this column existed (and on the rewriting
    # paths until they refresh it): the endpoint falls back to reading the file,
    # which is slower but identical, and the backfill fills them in.
    # PostgreSQL TEXT is unbounded (up to 1 GB) — instances with
    # large private sequences exist (≈12 KB here), and a truncated value would be
    # invalid JSON rather than a slow answer.
    # `deferred`: this column is ~12 KB per row, so loading it on every
    # `select(Instance)` would make a frame fetch or a delete plan transfer the
    # whole study's metadata. Only the WADO-RS /metadata route undefer()s it.
    metadata_json: Mapped[str | None] = mapped_column(
        Text(), nullable=True, deferred=True
    )
    compressed_at: Mapped[datetime | None] = mapped_column(DateTime)
    # C-04: monotonically increasing version of the *stored bytes* behind this
    # SOP Instance UID. Every path that rewrites the object under the same UID
    # (re-ingest, compression, lossy transcode, split/merge) bumps it. HTTP
    # ETags and every frame/render/thumbnail cache key include it, so a client
    # or cache can never serve pre-rewrite pixels as current.
    content_version: Mapped[int] = mapped_column(
        Integer, default=1, server_default=text("1"), nullable=False
    )
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
