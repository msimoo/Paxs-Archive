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

import logging
import secrets
import socket
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.db.models import Patient, RadiologyReport, ReportAddendum, Study
from app.db.session import SyncSessionLocal

log = logging.getLogger("paxs.report")

VALID_STATUSES = ("DRAFT", "FINALIZED")

# LOINC observation identifiers used in OBX-3.
LOINC_FINDINGS = "50005-5"      # Radiology - Study observation (findings)
LOINC_IMPRESSION = "50122-5"    # Impression

_ESC_MAP = {"\\": "\\E\\", "|": "\\F\\", "^": "\\S\\", "&": "\\T\\", "~": "\\R\\"}


def _esc(value: str | None) -> str:
    """HL7-escape user text (\\ | ^ & ~) so it can't corrupt the message."""
    if not value:
        return ""
    return "".join(_ESC_MAP.get(ch, ch) for ch in str(value))


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------- #
# Async CRUD (used directly by the REST API)
# --------------------------------------------------------------------------- #
async def create_report(session, data: dict, performed_by: str) -> RadiologyReport:
    """Create a DRAFT report for a study. Raises ValueError on conflicts."""
    study_uid = (data.get("study_instance_uid") or "").strip()
    if not study_uid:
        raise ValueError("study_instance_uid is required")
    study = (
        await session.execute(
            select(Study)
            .options(selectinload(Study.patient))
            .where(Study.study_instance_uid == study_uid)
        )
    ).scalar_one_or_none()
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
        clinical_history=data.get("clinical_history"),
        findings=data.get("findings"),
        impression=data.get("impression"),
        radiologist=data.get("radiologist") or performed_by,
    )
    session.add(report)
    await session.flush()
    return report


async def get_report(session, report_id: int) -> RadiologyReport | None:
    return await session.get(RadiologyReport, report_id)


async def list_reports(session, status: str | None = None,
                       study_uid: str | None = None) -> list[RadiologyReport]:
    stmt = select(RadiologyReport).order_by(RadiologyReport.created_at.desc())
    if status:
        stmt = stmt.where(RadiologyReport.status == status.upper())
    if study_uid:
        stmt = stmt.where(RadiologyReport.study_instance_uid == study_uid)
    return list((await session.execute(stmt)).scalars().all())


async def update_report(session, report: RadiologyReport, data: dict) -> RadiologyReport:
    """Edit a DRAFT report's clinical fields. Raises ValueError if finalized."""
    if report.status != "DRAFT":
        raise ValueError("Only DRAFT reports can be edited")
    for field in ("clinical_history", "findings", "impression", "radiologist"):
        if field in data and data[field] is not None:
            setattr(report, field, data[field])
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


async def list_addenda(session, report_id: int) -> list[ReportAddendum]:
    """Addenda for a report, oldest first (the order they were signed in)."""
    rows = (
        await session.execute(
            select(ReportAddendum)
            .where(ReportAddendum.report_id == report_id)
            .order_by(ReportAddendum.created_at, ReportAddendum.id)
        )
    ).scalars().all()
    return list(rows)


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


def build_oru_message(report: RadiologyReport, study: Study,
                      patient: Patient | None = None) -> str:
    """Build an HL7 v2.3 ORU^R01 message for a finalized report.

    Segment layout (standard radiology result message):
      MSH — header (PAXS -> EMR, ORU^R01^ORU_R01, P, 2.3)
      PID — patient (MRN with assigning authority, name, DOB, sex)
      PV1 — minimal visit
      ORC — order control RE (results), placer/filler order numbers
      OBR — observation request (procedure, study datetime, ordering provider,
            service section RAD, result status F)
      OBX — findings (LOINC 50005-5) + impression (LOINC 50122-5), status F

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
    obr[24] = "F"    # result status: final
    while obr and not obr[-1]:
        obr.pop()

    obx_lines = []
    set_id = 1
    for identifier, text in (
        (f"{LOINC_FINDINGS}^FINDINGS^LN", report.findings),
        (f"{LOINC_IMPRESSION}^IMPRESSION^LN", report.impression),
    ):
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
        log.warning("ORU MLLP send to %s:%s failed: %s", host, port, exc)
        return {"ok": False, "ack": "", "reason": str(exc)}

    ack_text = ack.decode("utf-8", errors="replace").strip("\x0b\x1c\x0d")
    rejected = ("MSA|AE" in ack_text) or ("MSA|AR" in ack_text)
    if rejected:
        return {"ok": False, "ack": ack_text,
                "reason": "EMR rejected the message (MSA|AE/AR)"}
    return {"ok": True, "ack": ack_text, "reason": None}


# --------------------------------------------------------------------------- #
# Finalize / resend (sync core — MLLP blocks)
# --------------------------------------------------------------------------- #
def _deliver(session: Session, report: RadiologyReport) -> dict:
    """(Re)send the ORU for a finalized report and record the outcome."""
    message = build_oru_message(report, report.study, report.study.patient)
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


def finalize_report(report_id: int, performed_by: str = "report") -> dict:
    """Lock the report as FINALIZED and (when enabled) deliver ORU^R01."""
    with SyncSessionLocal() as session:
        report = session.get(RadiologyReport, report_id)
        if report is None:
            return {"ok": False, "error": "Report not found"}
        already_sent = report.status == "FINALIZED" and report.oru_status == "SENT"
        if report.status != "FINALIZED":
            report.status = "FINALIZED"
            report.finalized_at = _now()
            report.radiologist = report.radiologist or performed_by

        if already_sent:
            # Idempotent: a second finalize must not duplicate the ORU in the EMR.
            session.commit()
            return {"ok": True, "report_id": report.id, "status": report.status,
                    "delivery": {"status": "SENT", "sent_at": report.oru_sent_at.isoformat() if report.oru_sent_at else None}}

        delivery: dict = {"status": "PENDING"}
        if settings.HL7_EMR_ENABLED:
            delivery = _deliver(session, report)
        else:
            report.oru_status = "PENDING"
            report.oru_error = None
        session.commit()
        return {
            "ok": True,
            "report_id": report.id,
            "status": report.status,
            "delivery": delivery,
        }


def resend_report(report_id: int) -> dict:
    """Re-send the ORU^R01 for a previously finalized report."""
    with SyncSessionLocal() as session:
        report = session.get(RadiologyReport, report_id)
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
from app.db.models import OrganizationSettings, ReportTemplate  # noqa: E402


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
        stmt = stmt.where((ReportTemplate.modality == modality.upper()) | (ReportTemplate.modality == None))  # noqa: E711
    return list((await session.execute(stmt)).scalars().all())


async def create_report_template(session, data: dict) -> ReportTemplate:
    """Create a new report template."""
    tmpl = ReportTemplate(
        title=data.get("title", "Untitled Template"),
        modality=data.get("modality", "").upper() or None,
        body_findings_template=data.get("body_findings_template", ""),
        default_impression_template=data.get("default_impression_template", ""),
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


async def get_study_report_metadata(session, study_uid: str) -> dict:
    """Fetch existing report or prepare pre-filled DICOM metadata for a study."""
    study = (
        await session.execute(
            select(Study)
            .options(selectinload(Study.patient), selectinload(Study.series_list))
            .where(Study.study_instance_uid == study_uid)
        )
    ).scalar_one_or_none()
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
        except Exception:
            pass

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
        "report": {
            "id": report.id,
            "status": report.status,
            "clinical_history": report.clinical_history,
            "findings": report.findings,
            "impression": report.impression,
            "radiologist": report.radiologist,
            "finalized_at": report.finalized_at.isoformat() if report.finalized_at else None,
            "created_at": report.created_at.isoformat() if report.created_at else None,
        } if report else None
    }
    return metadata

