"""Clinical and workflow entities: reports, presentation states, addenda.

These tables sit on top of the imaging hierarchy in
:mod:`app.db.models.study` and carry the *clinical record* rather than the
images. The distinction matters legally: a signed report is a medical document,
while the pixels it describes are data that may be lawfully purged when the
retention window closes.

**C-19 (fixed here).** ``reports.study_id`` used to be ``ON DELETE CASCADE``.
Purging a study — an operation an operator runs precisely because the *images*
have aged out — therefore deleted the signed report along with it, silently and
irreversibly. The foreign key is now ``ON DELETE RESTRICT`` and neither side of
the relationship carries a delete cascade, so a study that has a report cannot
be deleted by accident: the delete is refused and the caller must deal with the
report first (the API answers **409** with an actionable message).

Two consequences that are deliberate, not oversights:

* ``tenant_id`` is **not** a column here. A report's tenant is the tenant of its
  study, derived through ``study_id``; denormalising it would create a second
  copy of the scope that could disagree with the first — the exact drift the
  single-predicate rule (FA-1) exists to prevent. Reports are tenant-scoped by
  joining ``studies``.
* ``status`` is a ``String(16)``, not a database ``ENUM``. The shipped values are
  ``DRAFT`` and ``FINALIZED``; ``PRELIMINARY`` and ``AMENDED`` are not produced
  by any code path today (an amendment is a :class:`ReportAddendum` row, which
  keeps the signed original immutable — C06). A native ``ENUM`` would also not
  exist on SQLite and would make every future value addition a migration.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import true
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
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
from app.db.models.study import Study

__all__ = [
    "ClinicalProtocol",
    "CriticalResult",
    "HangingProtocol",
    "KeyImageNote",
    "OrganizationSettings",
    "PeerReviewCase",
    "PresentationState",
    "RadiationDoseReport",
    "RadiologyReport",
    "ReportAddendum",
    "ReportTemplate",
    "StudyViewState",
    "XDSDocument",
]

#: Statuses a report may hold. ``PRELIMINARY`` / ``AMENDED`` are declared for
#: completeness of the clinical vocabulary; nothing writes them yet.
REPORT_STATUSES = ("DRAFT", "PRELIMINARY", "FINALIZED", "AMENDED")


class PresentationState(Base):
    """A DICOM Grayscale Softcopy Presentation State (GSPS) object.

    SOP Class UID: 1.2.840.10008.5.1.4.1.1.11.1 — Modality: PR.

    GSPS objects arrive via C-STORE or STOW-RS just like images. The raw DICOM
    file is stored in the normal instance hierarchy; this table indexes the
    *semantic* content so the viewer and the WADO-RS ``/rendered`` endpoint can
    find and apply presentation states quickly.

    The link to the imaging hierarchy is by **UID string**, not by foreign key:
    a GSPS may reference instances across several series of one study (and the
    referenced SOP Instance UIDs are a multi-valued attribute), so a real FK
    would need a join table and would have to be re-pointed whenever an object
    moves tiers. ``study_instance_uid`` / ``series_instance_uid`` are indexed for
    the lookups the viewer actually performs.
    """

    __tablename__ = "presentation_states"

    id: Mapped[int] = mapped_column(
        AutoBigIntPK, primary_key=True, autoincrement=True
    )
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
    rotation: Mapped[int | None] = mapped_column(Integer)  # 0, 90, 180, 270
    horizontal_flip: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    # ---------- Referenced image SOP Instance UIDs (comma-separated) --------
    # For fast lookup; the full sequence detail is in the raw DICOM file.
    referenced_sop_instance_uids: Mapped[str | None] = mapped_column(Text)
    # ---------- File location (same key as instances.file_path) -------------
    file_path: Mapped[str] = mapped_column(String(768), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


class RadiologyReport(Base):
    """A radiology report attached to a study (E2 — HL7 ORU write-back to the EMR).

    One report per study (unique ``study_id``): the radiologist drafts it, then
    FINALIZES it — which stamps ``finalized_at`` and (when the EMR MLLP channel
    is enabled) sends an ORU^R01 observation message carrying the findings and
    impression to the hospital EMR. Delivery state lives on the row
    (``oru_status`` / ``oru_sent_at`` / ``oru_error``) so a failed delivery stays
    visible and can be re-sent.

    The narrative is split into ``clinical_history`` / ``findings`` /
    ``impression`` rather than one free-text blob because those are the three
    separately-addressed sections of both the DICOM SR template and the HL7 ORU
    segment structure; the report editor and the ORU builder both address them
    individually.

    **C-19:** ``study_id`` is ``ON DELETE RESTRICT`` and neither relationship
    cascades a delete. A signed report outlives the images it describes.
    """

    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(
        AutoBigIntPK, primary_key=True, autoincrement=True
    )
    # C-19: RESTRICT, never CASCADE. A study that has a report cannot be
    # deleted until the report is dealt with — the failure is loud and the API
    # turns it into an actionable 409 rather than a silent loss of the signed
    # document.
    study_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("studies.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    # Denormalized for dashboard rows / HL7 PID without joining every time.
    study_instance_uid: Mapped[str] = mapped_column(
        String(128), index=True, nullable=False
    )
    patient_id: Mapped[str | None] = mapped_column(String(64), index=True)
    # DRAFT (editable) / FINALIZED (locked + ORU-worthy). See REPORT_STATUSES
    # for the full clinical vocabulary.
    status: Mapped[str] = mapped_column(
        String(16), default="DRAFT", server_default="DRAFT", nullable=False, index=True
    )
    clinical_history: Mapped[str | None] = mapped_column(Text)
    findings: Mapped[str | None] = mapped_column(Text)
    impression: Mapped[str | None] = mapped_column(Text)
    # The authenticated actor recorded as signer (never a client-supplied name).
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

    # C-19: an explicitly empty cascade set — no save-update, no merge and
    # above all no delete. Writers set `study_id` directly (which is what
    # report_service does); they must not rely on `study.reports.append(...)`
    # to persist a new report, because with `cascade=""` it will not.
    study: Mapped[Study] = relationship(back_populates="reports", cascade="")

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

    id: Mapped[int] = mapped_column(
        AutoBigIntPK, primary_key=True, autoincrement=True
    )
    report_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("reports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The authenticated actor who added it (never a client-supplied name).
    author: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    text: Mapped[str] = mapped_column(Text, nullable=False)

    # An addendum is a child of its report and carries no independent legal
    # weight, so it *does* cascade with the report — that is the one delete
    # cascade in this module, and it is deliberate.
    report: Mapped[RadiologyReport] = relationship()


# --------------------------------------------------------------------------- #
# FA-3: clinical entities moved out of the package ``__init__``.
#
# Declared here rather than in ``__init__`` so the package facade stays a pure
# re-export; ``__all__`` above is extended accordingly.
# --------------------------------------------------------------------------- #
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
        Boolean, default=True, server_default=true(), nullable=False
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


class XDSDocument(Base):
    """An IHE XDS-I.b (Cross-Enterprise Document Sharing for Imaging) Document Registry Entry.

    Tracks ebXML metadata (entry_uuid, unique_id, class_code, format_code, availability_status)
    enabling health information exchange (HIE) document registry queries [ITI-18] and retrieval [ITI-43].
    """

    __tablename__ = "xds_documents"

    id: Mapped[int] = pk()
    tenant_id: Mapped[int | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True, index=True
    )
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


class StudyViewState(Base):
    """D4: one saved 3D view per (study, user) — disposable UI state, not a record.

    ``ON DELETE CASCADE`` on ``study_id`` is deliberate and the *opposite* of
    :class:`RadiologyReport`: a saved camera is not a medical document, so purging
    an aged-out study must take it along instead of blocking the purge (409) over a
    camera angle. As with reports there is no ``tenant_id`` column — the tenant is
    the tenant of the study, resolved through ``study_id`` (FA-1: one scope, one
    predicate), and every query joins ``studies``.

    ``state`` holds the viewer's own versioned JSON document (bounded at the API
    boundary); the server does not interpret its fields, so the 3D view can gain a
    setting without a migration.
    """

    __tablename__ = "study_view_states"

    id: Mapped[int] = mapped_column(
        AutoBigIntPK, primary_key=True, autoincrement=True
    )
    study_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("studies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    state: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("study_id", "user_id", name="uq_study_view_state"),
    )
