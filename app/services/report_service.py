"""Radiology report service (Phase E2 — HL7 ORU write-back to the EMR).

A radiologist drafts a report for a study (findings + impression), then
FINALIZES it: the report is locked and — when the EMR MLLP channel is enabled
(``HL7_EMR_ENABLED``) — an HL7 v2 ORU^R01 observation message is sent to the
EMR over MLLP carrying the patient (PID), the order (ORC/OBR) and the report
text (OBX, LOINC-coded). Delivery state lives on the report row so a failed
delivery stays visible and can be re-sent.

Deliberately dependency-free, mirroring ``app/dicom/hl7.py``: the ORU message
is built by hand (MSH/PID/PV1/ORC/OBR/OBX) with proper HL7 escaping, and the
MLLP transport is a plain blocking socket — no external HL7 library.

The MLLP send is *sync* (blocking socket) so the async REST layer wraps it in
``asyncio.to_thread`` exactly like the DIMSE SCU helpers.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import socket
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.core.roles import DESTRUCTIVE_ROLES
from app.db.models import (
    AuditLog,
    OrganizationSettings,
    Patient,
    RadiologyReport,
    ReportAddendum,
    ReportTemplate,
    Study,
)
from app.db.session import SyncSessionLocal
from app.services import query_service, reading_claim_service
from app.services.report_html import html_to_plain_text, sanitize_stored_field

log = logging.getLogger("paxs.report")

VALID_STATUSES = ("DRAFT", "FINALIZED")

# LOINC observation identifiers used in OBX-3.
LOINC_FINDINGS = "50005-5"      # Radiology - Study observation (findings)
LOINC_IMPRESSION = "50122-5"    # Impression

_ESC_MAP = {
    "\\": "\\E\\",
    "|": "\\F\\",
    "^": "\\S\\",
    "&": "\\T\\",
    "~": "\\R\\",
    "\r": "\\X0D\\",
    "\n": "\\X0A\\",
}


def escape_hl7_text(value: str | None) -> str:
    """Encode HL7 delimiters and line breaks using v2 escape sequences."""
    if not value:
        return ""
    return "".join(_ESC_MAP.get(ch, ch) for ch in str(value))


def _esc(value: str | None) -> str:
    """Backward-compatible internal alias for the HL7 text encoder."""
    return escape_hl7_text(value)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


#: The clinical fields a finalize request may carry. ``radiologist`` is
#: deliberately absent: the signer is always the authenticated actor, never a
#: value from the request body.
CLINICAL_FIELDS: tuple[str, ...] = ("clinical_history", "findings", "impression")


class ReportConflict(ValueError):
    """A request would violate the signed-record contract.

    Raised when a caller tries to change the content of an already-FINALIZED
    report. Subclasses :class:`ValueError` so existing ``except ValueError``
    call sites keep working, while remaining a distinct, greppable type that
    the API layer maps to HTTP 409.
    """


class ReportNotFound(LookupError):
    """The study or the report row does not exist (API maps this to 404)."""


class ReportForbidden(PermissionError):
    """The authenticated role may not perform this report transition (403)."""


def _text(value: object) -> str:
    """Normalise a clinical field for comparison (``None`` and ``""`` agree)."""
    if value is None:
        return ""
    return (value if isinstance(value, str) else str(value)).strip()


def _submitted_edits(edits: dict | None) -> dict[str, str]:
    """The clinical fields the caller actually supplied.

    A field the request omits is *not* an instruction to clear it — an omitted
    field means "leave it alone", which is what makes a bare
    ``status=FINALIZED`` replay idempotent against a report that already has
    content.
    """
    if not edits:
        return {}
    return {f: edits[f] for f in CLINICAL_FIELDS if edits.get(f) is not None}


def _deliver_or_pend(
    session: Session,
    report: RadiologyReport,
    *,
    addenda: list[ReportAddendum] | None = None,
    result_status: str = "F",
) -> dict:
    """Deliver the ORU when the EMR channel is on, else leave it PENDING."""
    if settings.HL7_EMR_ENABLED:
        if addenda is None and result_status == "F":
            return _deliver(session, report)
        return _deliver(
            session, report, addenda=addenda, result_status=result_status
        )
    report.oru_status = "PENDING"
    report.oru_error = None
    return {"status": "PENDING"}


def _finalize_payload(report: RadiologyReport, delivery: dict,
                      *, idempotent: bool) -> dict:
    """The shape returned by every finalize path (router + dedicated endpoint)."""
    return {
        "ok": True,
        "report_id": report.id,
        "status": report.status,
        "signer": report.radiologist,
        "oru_status": report.oru_status,
        "finalized_at": report.finalized_at.isoformat() if report.finalized_at else None,
        "idempotent": idempotent,
        "delivery": delivery,
    }


# --------------------------------------------------------------------------- #
# Async CRUD (used directly by the REST API)
# --------------------------------------------------------------------------- #
async def create_report(
    session: AsyncSession, data: dict, performed_by: str, *,
    tenant_id: int | None = None, include_all: bool = False,
) -> RadiologyReport:
    """Create a DRAFT report for a study. Raises ValueError on conflicts."""
    study_uid = (data.get("study_instance_uid") or "").strip()
    if not study_uid:
        raise ValueError("study_instance_uid is required")
    statement = select(Study).options(selectinload(Study.patient)).where(
        Study.study_instance_uid == study_uid
    )
    statement = query_service.apply_study_tenant_scope(statement, tenant_id, include_all)
    study = (await session.execute(statement)).scalar_one_or_none()
    if study is None:
        raise ValueError("Study not found")

    existing = (
        await session.execute(
            select(RadiologyReport).where(RadiologyReport.study_id == study.id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ValueError("A report already exists for this study")

    report = RadiologyReport(
        study_id=study.id,
        study_instance_uid=study_uid,
        patient_id=data.get("patient_id") or (study.patient.patient_id if study.patient else None),
        status="DRAFT",
        clinical_history=sanitize_stored_field(data.get("clinical_history")),
        findings=sanitize_stored_field(data.get("findings")),
        impression=sanitize_stored_field(data.get("impression")),
        # The authenticated caller is the only valid signer; never trust
        # client-provided radiologist/signer fields in ``data``.
        radiologist=performed_by,
    )
    session.add(report)
    await session.flush()
    return report


async def get_report(
    session: AsyncSession, report_id: int, *,
    tenant_id: int | None = None, include_all: bool = False,
) -> RadiologyReport | None:
    """Fetch one report only through its owning study's tenant boundary."""
    statement = (
        select(RadiologyReport)
        .join(Study, Study.id == RadiologyReport.study_id)
        .where(RadiologyReport.id == report_id)
    )
    statement = query_service.apply_study_tenant_scope(statement, tenant_id, include_all)
    return (await session.execute(statement)).scalar_one_or_none()


async def list_reports(
    session: AsyncSession, status: str | None = None, study_uid: str | None = None, *,
    tenant_id: int | None = None, include_all: bool = False,
) -> list[RadiologyReport]:
    """List reports through the owning study's tenant scope."""
    stmt = (
        select(RadiologyReport)
        .join(Study, Study.id == RadiologyReport.study_id)
        .order_by(RadiologyReport.created_at.desc())
    )
    stmt = query_service.apply_study_tenant_scope(stmt, tenant_id, include_all)
    if status:
        stmt = stmt.where(RadiologyReport.status == status.upper())
    if study_uid:
        stmt = stmt.where(RadiologyReport.study_instance_uid == study_uid)
    return list((await session.execute(stmt)).scalars().all())


async def update_report(session, report: RadiologyReport, data: dict) -> RadiologyReport:
    """Edit a DRAFT report's clinical fields. Raises ValueError if finalized."""
    if report.status != "DRAFT":
        raise ValueError("Only DRAFT reports can be edited")
    for field in ("clinical_history", "findings", "impression"):
        if field in data and data[field] is not None:
            setattr(report, field, sanitize_stored_field(data[field]))
    await session.flush()
    return report


# --------------------------------------------------------------------------- #
# Addenda (C06)
#
# A FINALIZED report is the signed record: its clinical fields are never
# rewritten. Follow-up text goes into an append-only addendum, attributed to the
# authenticated author, so the original stays intact and auditable.
# --------------------------------------------------------------------------- #
async def add_addendum(session, report: RadiologyReport, text: str,
                       author: str) -> ReportAddendum:
    """Append an addendum to a FINALIZED report. Raises ValueError otherwise."""
    body = (text or "").strip()
    if not body:
        raise ValueError("Addendum text is required")
    if report.status != "FINALIZED":
        raise ValueError(
            "Addenda can only be added to FINALIZED reports; edit the draft instead"
        )
    addendum = ReportAddendum(report_id=report.id, author=author or "", text=body)
    session.add(addendum)
    await session.flush()
    return addendum


async def list_addenda(
    session: AsyncSession, report_id: int, *,
    tenant_id: int | None = None, include_all: bool = False,
) -> list[ReportAddendum]:
    """Addenda for a report, oldest first (the order they were signed in)."""
    statement = (
        select(ReportAddendum)
        .join(RadiologyReport, RadiologyReport.id == ReportAddendum.report_id)
        .join(Study, Study.id == RadiologyReport.study_id)
        .where(ReportAddendum.report_id == report_id)
        .order_by(ReportAddendum.created_at, ReportAddendum.id)
    )
    statement = query_service.apply_study_tenant_scope(statement, tenant_id, include_all)
    rows = (await session.execute(statement)).scalars().all()
    return list(rows)


def add_report_addendum(
    report_id: int, text: str, author_user: str, *,
    tenant_id: int | None = None, include_all: bool = False,
) -> dict:
    """Append, audit, and dispatch an amended ORU for a finalized report.

    The signed report columns are never changed. The addendum row, audit row,
    ORU delivery state, and outbound message are handled from one service path
    so callers cannot accidentally persist text without recording the amendment.
    The caller runs this synchronous MLLP workflow in a bounded worker thread.
    """
    body = (text or "").strip()
    if not body:
        raise ValueError("Addendum text is required")
    actor = (author_user or "").strip()
    if not actor:
        raise ValueError("Authenticated addendum author is required")

    with SyncSessionLocal() as session:
        statement = (
            select(RadiologyReport)
            .join(Study, Study.id == RadiologyReport.study_id)
            .options(
                selectinload(RadiologyReport.study).selectinload(Study.patient),
                selectinload(RadiologyReport.study).selectinload(Study.series_list),
            )
            .where(RadiologyReport.id == report_id)
            .with_for_update()
        )
        statement = query_service.apply_study_tenant_scope(
            statement, tenant_id, include_all
        )
        report = session.execute(statement).scalar_one_or_none()
        if report is None:
            return {"ok": False, "error": "Report not found"}
        if report.status != "FINALIZED":
            raise ReportConflict("Addenda can only be added to FINALIZED reports")

        addendum = ReportAddendum(report_id=report.id, author=actor, text=body)
        session.add(addendum)
        session.flush()
        addenda = list(
            session.execute(
                select(ReportAddendum)
                .where(ReportAddendum.report_id == report.id)
                .order_by(ReportAddendum.created_at, ReportAddendum.id)
            ).scalars().all()
        )
        session.add(
            AuditLog(
                study_instance_uid=report.study_instance_uid,
                action="REPORT_ADDENDUM",
                performed_by=actor,
            )
        )
        delivery = _deliver_or_pend(
            session, report, addenda=addenda, result_status="A"
        )
        session.commit()
        return {
            "ok": True,
            "report_id": report.id,
            "status": report.status,
            "oru_status": report.oru_status,
            "delivery": delivery,
            "addendum": {
                "id": addendum.id,
                "report_id": addendum.report_id,
                "author": addendum.author,
                "text": addendum.text,
                "created_at": addendum.created_at.isoformat()
                if addendum.created_at else None,
            },
        }


# --------------------------------------------------------------------------- #
# ORU^R01 message builder (pure — easy to unit-test)
# --------------------------------------------------------------------------- #
def _dtm(study_date: str | None, study_time: str | None) -> str:
    """OBR-7 observation datetime: YYYYMMDDHHMMSS from DICOM date/time parts."""
    date_part = (study_date or "").strip()
    time_part = (study_time or "").strip()
    if not date_part:
        return ""
    if time_part:
        return f"{date_part}{time_part[:6]}"
    return f"{date_part}000000"


def build_oru_message(
    report: RadiologyReport,
    study: Study,
    patient: Patient | None = None,
    *,
    addenda: list[ReportAddendum] | None = None,
    result_status: str = "F",
) -> str:
    """Build an HL7 v2.3 ORU^R01 message for a finalized report.

    Segment layout (standard radiology result message):
      MSH — header (PAXS -> EMR, ORU^R01^ORU_R01, P, 2.3)
      PID — patient (MRN with assigning authority, name, DOB, sex)
      PV1 — minimal visit
      ORC — order control RE (results), placer/filler order numbers
      OBR — observation request (procedure, study datetime, ordering provider,
            service section RAD, result status F/A/C)
      OBX — findings, impression, and any append-only addenda

    Returns the message with segments joined by ``\\r`` (HL7 standard) and a
    trailing ``\\r`` — ready for MLLP framing.
    """
    patient = patient or study.patient
    now = _now().strftime("%Y%m%d%H%M%S")
    control_id = f"PAXS-{secrets.token_hex(4).upper()}"

    accession = (study.accession_number or "").strip()
    placer = (study.study_id or "").strip() or f"PAXS-{study.id}"
    procedure_code = ""
    if study.series_list:
        procedure_code = study.series_list[0].modality or ""
    procedure_desc = (study.study_description or "").strip()

    # PID
    mrn = (patient.patient_id if patient else "") or ""
    mrn_parts = mrn.split("^") if patient else []
    issuer = (mrn_parts[3] if len(mrn_parts) > 3
              else (mrn_parts[1] if len(mrn_parts) > 1 else "")) or ""
    pid = "PID|1|" + (
        f"{_esc(mrn)}^^^{_esc(issuer)}^MR"
        if mrn else ""
    ) + f"|{_esc(patient.patient_name if patient else '')}" \
        f"||{_esc(patient.patient_birth_date if patient else '')}" \
        f"|{_esc(patient.patient_sex if patient else '')}"

    # OBR (26 fields; trailing empties trimmed).
    obr = [""] * 26
    obr[0] = "1"
    obr[1] = _esc(placer)
    obr[2] = _esc(accession)
    obr[3] = f"{_esc(procedure_code)}^{_esc(procedure_desc)}"
    obr[6] = _dtm(study.study_date, study.study_time)
    obr[15] = _esc(study.referring_physician or "")
    obr[23] = "RAD"  # diagnostic service section
    obr[24] = result_status if result_status in {"F", "A", "C"} else "F"
    while obr and not obr[-1]:
        obr.pop()

    obx_lines = []
    set_id = 1
    observations = [
        (f"{LOINC_FINDINGS}^FINDINGS^LN", html_to_plain_text(report.findings)),
        (f"{LOINC_IMPRESSION}^IMPRESSION^LN", html_to_plain_text(report.impression)),
        *(
            ("ADDENDUM^REPORT ADDENDUM^99PAXS", html_to_plain_text(addendum.text))
            for addendum in (addenda or [])
        ),
    ]
    for identifier, text in observations:
        if not (text or "").strip():
            continue
        obx = [""] * 11
        obx[0] = str(set_id)
        obx[1] = "TX"
        obx[2] = identifier
        obx[4] = _esc(text)
        obx[10] = "F"  # OBX-11 observation result status
        while obx and not obx[-1]:
            obx.pop()
        obx_lines.append("OBX|" + "|".join(obx))
        set_id += 1

    segments = [
        "MSH|^~\\&"
        f"|{settings.HL7_SENDING_APPLICATION}|{settings.HL7_SENDING_FACILITY}"
        f"|{settings.HL7_EMR_APPLICATION}|{settings.HL7_EMR_FACILITY}"
        f"|{now}||ORU^R01^ORU_R01|{control_id}|P|2.3",
        pid,
        "PV1|1|O",
        f"ORC|RE|{_esc(placer)}|{_esc(accession)}",
        "OBR|" + "|".join(obr),
        *obx_lines,
    ]
    return "\r".join(segments) + "\r"


# --------------------------------------------------------------------------- #
# MLLP transport (sync — blocking socket)
# --------------------------------------------------------------------------- #
def send_oru_to_emr(message: str, host: str | None = None,
                    port: int | None = None) -> dict:
    """Send an HL7 message to the EMR over MLLP and read the ACK.

    Framing: ``<0x0B> message <0x1C><0x0D>`` (HL7 lower-layer protocol). The
    receiver is expected to answer with an ACK message; a missing/empty ack is
    treated as delivered (best-effort receivers), while an explicit
    ``MSA|AE`` / ``MSA|AR`` (application/commit error) is a failure.

    Returns ``{"ok", "ack", "reason"?}``.
    """
    host = host or settings.HL7_EMR_HOST
    port = port or settings.HL7_EMR_PORT
    payload = b"\x0b" + message.encode("utf-8") + b"\x1c\x0d"
    try:
        with socket.create_connection((host, port), timeout=5) as sock:
            sock.settimeout(5)
            sock.sendall(payload)
            ack = b""
            while b"\x1c\x0d" not in ack:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                ack += chunk
                if len(ack) > 1_000_000:  # safety against a chatty peer
                    break
    except OSError as exc:
        log.warning("ORU MLLP send failed error_type=%s", type(exc).__name__)
        return {"ok": False, "ack": "", "reason": "EMR connection failed"}

    ack_text = ack.decode("utf-8", errors="replace").strip("\x0b\x1c\x0d")
    rejected = ("MSA|AE" in ack_text) or ("MSA|AR" in ack_text)
    if rejected:
        return {"ok": False, "ack": ack_text,
                "reason": "EMR rejected the message (MSA|AE/AR)"}
    return {"ok": True, "ack": ack_text, "reason": None}


# --------------------------------------------------------------------------- #
# Finalize / resend (sync core — MLLP blocks)
# --------------------------------------------------------------------------- #
def _deliver(
    session: Session,
    report: RadiologyReport,
    *,
    addenda: list[ReportAddendum] | None = None,
    result_status: str = "F",
) -> dict:
    """(Re)send an ORU and record the delivery outcome on the report."""
    message = build_oru_message(
        report,
        report.study,
        report.study.patient,
        addenda=addenda,
        result_status=result_status,
    )
    result = send_oru_to_emr(message)
    now = _now()
    if result["ok"]:
        report.oru_status = "SENT"
        report.oru_sent_at = now
        report.oru_error = None
        delivery = {"status": "SENT", "sent_at": now.isoformat(),
                    "ack": (result.get("ack") or "")[:200]}
        log.info("ORU^R01 for report %s delivered to EMR", report.id)
    else:
        report.oru_status = "FAILED"
        report.oru_error = result.get("reason")
        delivery = {"status": "FAILED", "error": result.get("reason")}
    return delivery


def finalize_report(
    report_id: int, performed_by: str = "report", edits: dict | None = None, *,
    tenant_id: int | None = None, include_all: bool = False,
) -> dict:
    """Finalize a report, applying ``edits`` first — **one transaction** (A-7).

    Before this, ``POST /reports/save`` skipped the draft update whenever the
    request asked to finalize, so the *stored* draft text was signed and the
    edits in the request body were silently discarded (and the draft author,
    not the finalizer, was recorded as the signer). Everything below happens in
    a single transaction:

    a. the submitted clinical fields are applied to the DRAFT;
    b. the status transitions to FINALIZED (stamping ``finalized_at``);
    c. ``performed_by`` — the authenticated actor — is recorded as the signer.

    Idempotency: re-finalizing with the same content (or with no content at all)
    returns the existing outcome and **does not** re-send the ORU. Changing the
    content of a FINALIZED report raises :class:`ReportConflict` — the signed
    record is immutable; follow-up text belongs in an addendum.

    Runs synchronously (MLLP blocks) — call it via ``asyncio.to_thread``.
    """
    submitted = _submitted_edits(edits)

    with SyncSessionLocal() as session:
        # SELECT ... FOR UPDATE where the engine supports it (MySQL); SQLite
        # omits the clause, so the write below is serialised by its own lock.
        statement = (
            select(RadiologyReport)
            .join(Study, Study.id == RadiologyReport.study_id)
            .where(RadiologyReport.id == report_id)
            .with_for_update()
        )
        statement = query_service.apply_study_tenant_scope(
            statement, tenant_id, include_all
        )
        report = session.execute(statement).scalar_one_or_none()
        if report is None:
            return {"ok": False, "error": "Report not found"}

        if report.status == "FINALIZED":
            changed = [f for f, v in submitted.items()
                       if _text(getattr(report, f, None)) != _text(sanitize_stored_field(v))]
            if changed:
                raise ReportConflict(
                    "Report is FINALIZED and cannot be edited ("
                    + ", ".join(sorted(changed))
                    + "); add follow-up text with an addendum instead"
                )
            # Idempotent replay: identical content (or a bare finalize) changes
            # nothing and must not duplicate the ORU in the EMR.
            session.commit()
            delivery = {
                "status": report.oru_status,
                "sent_at": report.oru_sent_at.isoformat() if report.oru_sent_at else None,
            }
            return _finalize_payload(report, delivery, idempotent=True)

        # (a) apply the submitted edits to the DRAFT in this same transaction
        for field, value in submitted.items():
            setattr(report, field, sanitize_stored_field(value))
        # (b) transition, and (c) the authenticated actor signs
        report.status = "FINALIZED"
        report.finalized_at = _now()
        report.radiologist = performed_by or report.radiologist or ""
        # The signed report ends the read: free the study's reading claim (C1).
        reading_claim_service.release_on_finalize(session, report.study_id)
        session.flush()

        # The ORU is built from the row *after* the edits, so the EMR receives
        # the text that was actually signed.
        delivery = _deliver_or_pend(session, report)
        session.commit()
        return _finalize_payload(report, delivery, idempotent=False)



def resend_report(
    report_id: int, *, tenant_id: int | None = None, include_all: bool = False,
) -> dict:
    """Re-send the ORU^R01 for a previously finalized report."""
    with SyncSessionLocal() as session:
        statement = (
            select(RadiologyReport)
            .join(Study, Study.id == RadiologyReport.study_id)
            .where(RadiologyReport.id == report_id)
        )
        statement = query_service.apply_study_tenant_scope(
            statement, tenant_id, include_all
        )
        report = session.execute(statement).scalar_one_or_none()
        if report is None:
            return {"ok": False, "error": "Report not found"}
        if report.status != "FINALIZED":
            return {"ok": False, "error": "Only FINALIZED reports can be re-sent"}
        if not settings.HL7_EMR_ENABLED:
            return {"ok": False,
                    "error": "HL7 EMR channel is disabled (set HL7_EMR_ENABLED)"}
        delivery = _deliver(session, report)
        session.commit()
        return {"ok": True, "report_id": report.id, "delivery": delivery}


# --------------------------------------------------------------------------- #
# Organization Settings & Report Templates Service Functions
# --------------------------------------------------------------------------- #

_PACKAGED_ORG_LOGO = "/viewer/assets/logomain.png"
_APP_DIR = Path(__file__).resolve().parents[1]


def resolve_org_logo_path(logo_path: str | None) -> str:
    """Return an existing same-origin logo path or the packaged PAXS logo.

    Uploaded assets are not guaranteed to survive a deployment unless the
    uploads directory is persisted. Returning the fallback in settings data
    prevents the browser from requesting a stale file.
    """
    if not logo_path:
        return _PACKAGED_ORG_LOGO

    parsed = urlsplit(logo_path)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        return _PACKAGED_ORG_LOGO
    relative = PurePosixPath(parsed.path.lstrip("/"))
    if relative.is_absolute() or ".." in relative.parts:
        return _PACKAGED_ORG_LOGO
    if not (relative.parts[:2] == ("static", "uploads")
            or relative.parts[:1] == ("viewer",)):
        return _PACKAGED_ORG_LOGO

    candidate = (_APP_DIR / Path(*relative.parts)).resolve()
    try:
        candidate.relative_to(_APP_DIR.resolve())
    except ValueError:
        return _PACKAGED_ORG_LOGO
    return logo_path if candidate.is_file() else _PACKAGED_ORG_LOGO


async def get_or_create_org_settings(session) -> OrganizationSettings:
    """Fetch the single OrganizationSettings instance (id=1) or create defaults."""
    org = await session.get(OrganizationSettings, 1)
    if org is None:
        org = OrganizationSettings(
            id=1,
            org_name_ar="مستشفى PAXS الأرشيفي",
            org_name_en="PAXS Archive Diagnostic Center",
            logo_path="/static/uploads/logo.png",
            header_html_text="<div style='text-align:center;'><h2>PAXS Archive Medical Center</h2><p>Department of Radiology & Nuclear Medicine</p></div>",
            footer_html_text="<div style='text-align:center;'><p>Electronic Diagnostic Report · PAXS Archive Platform</p></div>",
            phone="+1 (800) 555-PAXS",
            email="reports@paxs-archive.org",
            address="Building 4, Healthcare Innovation District",
        )
        session.add(org)
        await session.flush()
    return org


async def update_org_settings(session, data: dict, logo_path: str | None = None) -> OrganizationSettings:
    """Update OrganizationSettings properties."""
    org = await get_or_create_org_settings(session)
    for field in ("org_name_ar", "org_name_en", "header_html_text", "footer_html_text", "phone", "email", "address"):
        if field in data and data[field] is not None:
            setattr(org, field, data[field])
    if logo_path:
        org.logo_path = logo_path
    await session.flush()
    return org


async def list_report_templates(session, modality: str | None = None) -> list[ReportTemplate]:
    """List report templates, optionally filtered by modality. Auto-seeds defaults if empty."""
    existing = list((await session.execute(select(ReportTemplate))).scalars().all())
    if not existing:
        defaults = [
            ReportTemplate(
                title="Normal CT Brain",
                modality="CT",
                body_findings_template="CT Brain without IV contrast:\n- Brain parenchyma demonstrates normal attenuation and architecture.\n- No evidence of acute intra- or extra-axial hemorrhage, mass effect, or midline shift.\n- Ventricles and basal cisterns are unremarkable for age.\n- Calvarium and skull base intact.",
                default_impression_template="Unremarkable non-contrast CT brain. No acute intracranial abnormality.",
                is_default=True,
            ),
            ReportTemplate(
                title="Routine Chest X-Ray Normal",
                modality="XR",
                body_findings_template="Chest Radiograph (PA & Lateral):\n- Lungs are clear without focal consolidation, pneumothorax, or pleural effusion.\n- Cardiomediastinal silhouette is within normal limits for size and contour.\n- Osseous structures and soft tissues are unremarkable.",
                default_impression_template="Normal PA and lateral chest radiograph.",
                is_default=True,
            ),
            ReportTemplate(
                title="Abdominal Ultrasound Normal",
                modality="US",
                body_findings_template="Transabdominal Sonogram:\n- Liver exhibits normal echogenicity and homogenous architecture without focal lesions.\n- Gallbladder and biliary tree are unremarkable.\n- Kidneys show normal parenchymal thickness and no hydronephrosis.\n- Spleen and pancreas within normal limits.",
                default_impression_template="Normal abdominal ultrasound examination.",
                is_default=True,
            ),
        ]
        for d in defaults:
            session.add(d)
        await session.flush()

    stmt = select(ReportTemplate).order_by(ReportTemplate.is_default.desc(), ReportTemplate.title.asc())
    if modality:
        stmt = stmt.where(
        (ReportTemplate.modality == modality.upper()) | ReportTemplate.modality.is_(None)
    )
    return list((await session.execute(stmt)).scalars().all())


async def create_report_template(session, data: dict) -> ReportTemplate:
    """Create a new report template."""
    tmpl = ReportTemplate(
        title=data.get("title", "Untitled Template"),
        modality=data.get("modality", "").upper() or None,
        body_findings_template=sanitize_stored_field(data.get("body_findings_template", "")),
        default_impression_template=sanitize_stored_field(data.get("default_impression_template", "")),
        is_default=bool(data.get("is_default", False)),
    )
    session.add(tmpl)
    await session.flush()
    return tmpl


async def get_report_template(session, template_id: int) -> ReportTemplate | None:
    return await session.get(ReportTemplate, template_id)


async def update_report_template(session, template_id: int, data: dict) -> ReportTemplate | None:
    tmpl = await get_report_template(session, template_id)
    if tmpl is None:
        return None
    for field in ("title", "modality", "body_findings_template", "default_impression_template", "is_default"):
        if field in data and data[field] is not None:
            if field == "modality":
                setattr(tmpl, field, str(data[field]).upper() or None)
            elif field in ("body_findings_template", "default_impression_template"):
                setattr(tmpl, field, sanitize_stored_field(data[field]))
            else:
                setattr(tmpl, field, data[field])
    await session.flush()
    return tmpl


async def delete_report_template(session, template_id: int) -> bool:
    tmpl = await get_report_template(session, template_id)
    if tmpl is None:
        return False
    await session.delete(tmpl)
    await session.flush()
    return True


async def get_study_report_metadata(
    session: AsyncSession, study_uid: str, *,
    tenant_id: int | None = None, include_all: bool = False,
) -> dict:
    """Fetch existing report or prepare pre-filled DICOM metadata for a study."""
    statement = (
        select(Study)
        .options(selectinload(Study.patient), selectinload(Study.series_list))
        .where(Study.study_instance_uid == study_uid)
    )
    statement = query_service.apply_study_tenant_scope(statement, tenant_id, include_all)
    study = (await session.execute(statement)).scalar_one_or_none()
    if study is None:
        raise ValueError("Study not found")

    report = (
        await session.execute(
            select(RadiologyReport).where(RadiologyReport.study_id == study.id)
        )
    ).scalar_one_or_none()

    patient = study.patient
    modality = study.series_list[0].modality if study.series_list else "CT"
    patient_name = patient.patient_name if patient else "UNKNOWN"
    patient_id = patient.patient_id if patient else ""
    patient_sex = patient.patient_sex if patient else ""
    patient_birth_date = patient.patient_birth_date if patient else ""

    # Derive age from birth date if available
    age = ""
    if patient_birth_date and len(patient_birth_date) == 8:
        try:
            b_year = int(patient_birth_date[:4])
            c_year = datetime.now().year
            age = f"{c_year - b_year}Y"
        except (TypeError, ValueError, AttributeError) as exc:  # age is a convenience field
            # A non-numeric birth date simply leaves the age blank. Type only:
            # the value is patient data (N-2).
            log.debug("report_patient_age_underivable error_type=%s", type(exc).__name__)

    report_data = None
    if report is not None:
        addenda = await list_addenda(
            session, report.id, tenant_id=tenant_id, include_all=include_all
        )
        report_data = {
            "id": report.id,
            "status": report.status,
            "clinical_history": report.clinical_history,
            "findings": report.findings,
            "impression": report.impression,
            "radiologist": report.radiologist,
            "finalized_at": report.finalized_at.isoformat() if report.finalized_at else None,
            "created_at": report.created_at.isoformat() if report.created_at else None,
            "addenda": [
                {
                    "id": addendum.id,
                    "author": addendum.author,
                    "text": addendum.text,
                    "created_at": addendum.created_at.isoformat()
                    if addendum.created_at else None,
                }
                for addendum in addenda
            ],
        }

    metadata = {
        "study_instance_uid": study.study_instance_uid,
        "accession_number": study.accession_number or "",
        "study_description": study.study_description or "Routine Diagnostic Imaging",
        "study_date": study.study_date or "",
        "modality": modality,
        "referring_physician": study.referring_physician or "",
        "patient_name": patient_name,
        "patient_id": patient_id,
        "patient_sex": patient_sex,
        "patient_birth_date": patient_birth_date,
        "patient_age": age,
        "report": report_data
    }
    return metadata


async def save_or_finalize_report(
    session: AsyncSession,
    *,
    actor: str,
    role: str,
    study_uid: str,
    edits: dict,
    wants_final: bool,
    tenant_id: int | None = None,
    include_all: bool = False,
) -> tuple[RadiologyReport, dict | None]:
    """Create/edit a DRAFT report, or finalize it — the UI's Save/Finalize path.

    Moved out of ``reports.save_report`` (Q-3/FA-2, Q-6/FA-4) so the router is
    only HTTP shape. Behaviour is unchanged, including the two properties that
    matter clinically:

    * the signer is always ``actor`` — the authenticated user, never the body;
    * finalizing applies the submitted edits **and** the signature in a single
      transaction via :func:`finalize_report`, so the signed text is the text
      the radiologist just wrote.

    Returns ``(report, delivery)``; ``delivery`` is ``None`` for a draft save.
    Raises :class:`ReportForbidden` (403), :class:`ReportNotFound` (404) or
    :class:`ReportConflict` (409).
    """
    if wants_final and role not in DESTRUCTIVE_ROLES:
        raise ReportForbidden("Finalizing a report requires a radiologist or admin role")

    try:
        meta = await get_study_report_metadata(
            session, study_uid, tenant_id=tenant_id, include_all=include_all
        )
    except ValueError as exc:
        raise ReportNotFound(str(exc)) from exc
    existing = meta.get("report")

    # ``radiologist`` is the authenticated actor, never a request-body value.
    fields = {**edits, "radiologist": actor}

    if not existing:
        try:
            report = await create_report(
                session, {"study_instance_uid": study_uid, **fields}, performed_by=actor,
                tenant_id=tenant_id, include_all=include_all,
            )
        except ValueError as exc:
            raise ReportConflict(str(exc)) from exc
        await session.commit()
        await session.refresh(report)
    else:
        report = await get_report(
            session, existing["id"], tenant_id=tenant_id, include_all=include_all
        )
        if report is None:
            raise ReportNotFound("Report not found")
        if not wants_final:
            try:
                report = await update_report(session, report, fields)
            except ValueError as exc:
                raise ReportConflict(str(exc)) from exc
            await session.commit()
            await session.refresh(report)

    if not wants_final:
        return report, None

    # Capture the id before committing: a commit expires the ORM instance, and
    # reading an attribute afterwards would trigger a lazy load (MissingGreenlet
    # on an async session).
    report_id = report.id
    # Close the async transaction first, so the refresh below cannot read a
    # stale snapshot of the row the sync worker is about to write.
    await session.commit()

    # A-7: one transaction applies the edits, flips the status and records the
    # authenticated actor as the signer, then delivers the ORU idempotently.
    result = await asyncio.to_thread(
        finalize_report, report_id, performed_by=actor, edits=fields,
        tenant_id=tenant_id, include_all=include_all,
    )
    if not result.get("ok"):
        raise ReportNotFound(result.get("error", "Report not found"))
    await session.refresh(report)
    return report, result.get("delivery")

