"""Modality worklist, MPPS and instance-availability notifications.

``ScheduledProcedureStep`` is the MWL row a modality queries;
``PerformedProcedureStep`` is the MPPS echo of what it actually did;
``InstanceAvailabilityNotification`` records the IAN N-CREATE we sent to a RIS.
``TriageFlag`` is the reading worklist's "look here first" marker — a workflow
hint, never a diagnosis.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.models.base import Base, pk

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from app.db.models.patient import Patient

__all__ = [
    "InstanceAvailabilityNotification",
    "PerformedProcedureStep",
    "ScheduledProcedureStep",
    "TriageFlag",
]


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
        ForeignKey("patients.id", ondelete="CASCADE"), nullable=False
    )
    sps_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)  # (0040,0009)
    accession_number: Mapped[str | None] = mapped_column(String(64))
    modality: Mapped[str | None] = mapped_column(String(16))
    scheduled_station_aet: Mapped[str | None] = mapped_column(String(64))
    start_date: Mapped[str | None] = mapped_column(String(8))  # (0040,0002)
    start_time: Mapped[str | None] = mapped_column(String(16))  # (0040,0003)
    # SCHEDULED / COMPLETED / CANCELED
    status: Mapped[str] = mapped_column(
        String(16), default="SCHEDULED", server_default="SCHEDULED", nullable=False
    )
    requested_procedure_id: Mapped[str | None] = mapped_column(String(64))
    requested_procedure_description: Mapped[str | None] = mapped_column(String(255))
    scheduled_performing_physician: Mapped[str | None] = mapped_column(String(192))
    # Closed loop: set when the images for this step actually arrive.
    study_instance_uid: Mapped[str | None] = mapped_column(String(128))
    performed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    patient: Mapped[Patient] = relationship()

    __table_args__ = (
        Index("ix_sps_patient_id", "patient_id"),
        Index("ix_sps_accession", "accession_number"),
        Index("ix_sps_modality", "modality"),
        Index("ix_sps_station", "scheduled_station_aet"),
        Index("ix_sps_start_date", "start_date"),
        Index("ix_sps_study", "study_instance_uid"),
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
    tenant_id: Mapped[int | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True, index=True
    )
    patient_id: Mapped[int | None] = mapped_column(
        ForeignKey("patients.id", ondelete="SET NULL"), nullable=True
    )
    scheduled_step_id: Mapped[int | None] = mapped_column(
        ForeignKey("scheduled_procedure_steps.id", ondelete="SET NULL"),
        nullable=True,
    )
    # IN PROGRESS / COMPLETED / DISCONTINUED
    status: Mapped[str] = mapped_column(
        String(16), default="IN PROGRESS", server_default="IN PROGRESS",
        nullable=False,
    )
    pps_id: Mapped[str | None] = mapped_column(String(64))          # (0040,0253)
    pps_description: Mapped[str | None] = mapped_column(String(255))  # (0040,0254)
    modality: Mapped[str | None] = mapped_column(String(16))
    performed_station_aet: Mapped[str | None] = mapped_column(String(64))
    accession_number: Mapped[str | None] = mapped_column(String(64))
    start_date: Mapped[str | None] = mapped_column(String(8))
    start_time: Mapped[str | None] = mapped_column(String(16))
    end_date: Mapped[str | None] = mapped_column(String(8))
    end_time: Mapped[str | None] = mapped_column(String(16))
    study_instance_uid: Mapped[str | None] = mapped_column(String(128))
    series_instance_uid: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    patient: Mapped[Patient | None] = relationship()
    scheduled_step: Mapped[ScheduledProcedureStep | None] = relationship()

    __table_args__ = (
        Index("ix_pps_patient_id", "patient_id"),
        Index("ix_pps_scheduled_step_id", "scheduled_step_id"),
        Index("ix_pps_status", "status"),
        Index("ix_pps_accession", "accession_number"),
        Index("ix_pps_study", "study_instance_uid"),
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
