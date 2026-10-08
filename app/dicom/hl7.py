"""Minimal HL7 v2 listener (hospital EMR/HIS integration) — the Interop
write-path (ADT + ORM).

Listens on a TCP port for:
  * ADT^A01/A04/A08/A31 (admit / register / update) — parses the PID
    segment and upserts the Patient row so studies arriving later from
    modalities can be matched to an existing patient.
  * ORM^O01 (order entry / imaging order) — turns each order into a
    SCHEDULED Modality Worklist step (create on NW, update on XO, cancel on
    CA/OC/DC/CR), the RIS -> PACS write-path that closes the Interop loop.

This is deliberately a *small, dependency-free* HL7 parser: it handles the
segments PAXS needs (MSH + PID + ORC + OBR, ZDS for radiology orders) and
ignores the rest. It speaks plain HL7 (lower-layer framing is simply
newline-delimited or MLLP).

Threading: runs in its own daemon thread; uses the sync DB engine (same as the
DICOM receiver / worker) because socket handlers are not async.
"""

from __future__ import annotations

import logging
import secrets
import socket
import threading
from datetime import date

from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.db.models import Patient, ScheduledProcedureStep
from app.db.session import SyncSessionLocal

from app.services.pix_pdq_service import (
    handle_adt_feed_patient_registration,
    handle_adt_feed_patient_merge,
    pix_query_by_identifier,
    pix_query_by_demographics,
    pdq_query_by_identifier,
    pdq_query_by_demographics,
)

log = logging.getLogger("paxs.hl7")

# Message types that carry patient registration/update information.
_HANDLED_TRIGGERS = {"A01", "A04", "A08", "A31", "A40"}

# Triggers that also *schedule* a default worklist step (admit / register).
# Update-only triggers (A08/A31/A40) enrich the patient but don't open new
# scheduled procedures.
_SCHEDULING_TRIGGERS = {"A01", "A04"}

# ORM order controls (ORC-1) that create/update a worklist step.
_ORDER_CREATE = {"NW", "XO"}   # NW = new order, XO = change order

# ORM order controls that cancel / discontinue an order.
_ORDER_CANCEL = {"CA", "CR", "OC", "DC"}


def _segment(seg: str, field: int, component: int = 1) -> str | None:
    parts = seg.split("|")
    if len(parts) <= field:
        return None
    value = parts[field]
    comps = value.split("^") if value else []
    if not comps:
        return None
    return comps[component - 1] if len(comps) >= component else None


def _name(pid_segment: str) -> str | None:
    """PID-5 is FAMILY^GIVEN^MIDDLE^SUFFIX — normalise to a display string.
    Uses the raw field (not a single component)."""
    parts = pid_segment.split("|")
    if len(parts) <= 5:
        return None
    name_parts = [p for p in parts[5].split("^") if p]
    return " ".join(name_parts) if name_parts else None


def _dt_parts(dtm: str | None) -> tuple[str | None, str | None]:
    """Split an HL7 DTM ('YYYYMMDDHHMMSS[.sss]') into DICOM date + time
    strings (start_date 'YYYYMMDD', start_time 'HHMMSS'), or (None, None)."""
    dtm = (dtm or "").strip()
    if len(dtm) >= 8 and dtm[:8].isdigit():
        date_part = dtm[:8]
        time_part = dtm[8:14]
        return date_part, time_part or None
    return None, None


def _xcn_name(seg: str, field: int) -> str | None:
    """XCN field (ID^FAMILY^GIVEN^...) -> 'FAMILY^GIVEN' display name."""
    parts = seg.split("|")
    if len(parts) <= field:
        return None
    comps = parts[field].split("^")
    if len(comps) >= 3 and (comps[1] or comps[2]):
        return "^".join(c for c in comps[1:3] if c)
    return comps[0] or None


def parse_adt_message(message: str) -> dict | None:
    """Parse an HL7 ADT message into a patient dict, or None if not handled."""
    segments = [s for s in message.replace("\r", "\n").split("\n") if s.strip()]
    if not segments:
        return None

    msh = segments[0]
    if not msh.startswith("MSH|"):
        log.debug("Ignoring non-HL7 payload (%.60r)", message[:60])
        return None

    # MSH-9 is MessageType: e.g. ADT^A01^ADT_A01
    msg_type = (msh.split("|")[8] if len(msh.split("|")) > 8 else "") or ""
    trigger = msg_type.split("^")[1] if "^" in msg_type else msg_type.split("^")[0]

    pid = next((s for s in segments if s.startswith("PID|")), None)
    if pid is None:
        log.debug("ADT %s message has no PID segment", trigger)
        return None

    return {
        "family": "ADT",
        "trigger": trigger,
        "patient_id": _segment(pid, 3, 1),      # PID-3.1: MRN
        "issuer": _segment(pid, 3, 4),          # PID-3.4: assigning authority
        "name": _name(pid),                     # PID-5: patient name
        "birth_date": _segment(pid, 7) or None, # PID-7: DOB (YYYYMMDD)
        "sex": _segment(pid, 8) or None,        # PID-8: sex
        "phone": _segment(pid, 13) or None,     # PID-13: home phone
    }


def parse_orm_message(message: str) -> dict | None:
    """Parse an HL7 v2 ORM^O01 (imaging order) message into an order dict, or
    None if it is not an ORM order or lacks a PID segment.

    ORM is the RIS -> PACS "place an order" write-path: it carries a PID
    (patient) plus ORC (Common Order) and OBR (Observation Request) segments
    describing the imaging request. PAXS turns each order into a SCHEDULED
    Modality Worklist step so the scanner sees it immediately.

    Field mapping (DICOM PS3.4 / IHE RAD TF-2):
      ORC-1                  order control (NW/XO/CA/OC/DC/CR)
      OBR-2 / ORC-2          placer order number  -> Requested Procedure ID
      OBR-3 / ORC-3          filler order number  -> Accession Number
      OBR-4.1                procedure code
      OBR-4.2                procedure description -> Requested Procedure
                                                        Description
      OBR-6/7                requested / observation date-time
      ORC-12 / OBR-16        ordering provider (XCN)
      ZDS-1                  modality (radiology schedule segment)
    """
    segments = [s for s in message.replace("\r", "\n").split("\n") if s.strip()]
    if not segments:
        return None

    msh = segments[0]
    if not msh.startswith("MSH|"):
        return None

    msg_type = (msh.split("|")[8] if len(msh.split("|")) > 8 else "") or ""
    if not msg_type.startswith("ORM^O01"):
        return None

    pid = next((s for s in segments if s.startswith("PID|")), None)
    if pid is None:
        log.debug("ORM^O01 message has no PID segment")
        return None
    orc = next((s for s in segments if s.startswith("ORC|")), None)
    obr = next((s for s in segments if s.startswith("OBR|")), None)
    zds = next((s for s in segments if s.startswith("ZDS|")), None)

    control = _segment(orc, 1) if orc else None
    # Placer order number (OBR-2 wins; ORC-2 falls back).
    placer = _segment(obr, 2) or (_segment(orc, 2) if orc else None)
    # Filler order number -> Accession Number.
    filler = _segment(obr, 3) or (_segment(orc, 3) if orc else None)
    # OBR-4 Universal Service ID: CODE^TEXT^coding system...
    service = (obr.split("|")[4] if obr and len(obr.split("|")) > 4 else "") or ""
    service_parts = service.split("^") if service else []
    procedure_code = service_parts[0] or None
    procedure_description = service_parts[1] or None
    # OBR-6 requested date/time; OBR-7 observation date/time as fallback.
    start_date, start_time = _dt_parts(
        _segment(obr, 6) or (_segment(obr, 7) if obr else None)
    )
    provider = _xcn_name(orc, 12) if orc else None
    if not provider and obr:
        provider = _xcn_name(obr, 16)
    # ZDS-1 modality (IHE RAD schedule segment).
    modality = _segment(zds, 1) if zds else None

    return {
        "family": "ORM",
        "control": (control or "").upper(),
        "patient_id": _segment(pid, 3, 1),      # PID-3.1: MRN
        "issuer": _segment(pid, 3, 4),          # PID-3.4: assigning authority
        "name": _name(pid),                     # PID-5: patient name
        "birth_date": _segment(pid, 7) or None, # PID-7: DOB (YYYYMMDD)
        "sex": _segment(pid, 8) or None,        # PID-8: sex
        "phone": _segment(pid, 13) or None,     # PID-13: home phone
        "placer_order": placer,
        "filler_order": filler,
        "procedure_code": procedure_code,
        "procedure_description": procedure_description,
        "modality": modality,
        "start_date": start_date,
        "start_time": start_time,
        "ordering_provider": provider,
    }


def _create_default_step(session, patient: Patient, data: dict) -> None:
    """Open a default SCHEDULED worklist step on admit/register.

    This is what makes an HL7-registered patient visible to modalities
    immediately, with zero manual entry. The station AE title comes from
    config (empty = matches any station) and modality is left open; the step is
    closed by the C-STORE/STOW path when the images actually arrive.
    """
    today = date.today().strftime("%Y%m%d")
    existing = (
        session.query(ScheduledProcedureStep)
        .filter(
            ScheduledProcedureStep.patient_id == patient.id,
            ScheduledProcedureStep.status == "SCHEDULED",
            ScheduledProcedureStep.start_date == today,
        )
        .first()
    )
    if existing is not None:
        return  # an open step for today already exists — don't duplicate
    # Known edge (accepted): two concurrent A01s for the same MRN can both pass
    # the guard above and open two near-identical steps. Harmless — sps_ids
    # differ, and on image arrival find_matching_step completes by accession
    # first, then the oldest open step by created_at.
    step = ScheduledProcedureStep(
        patient_id=patient.id,
        # ScheduledProcedureStepID is VR SH (max 16 chars) — keep it short.
        sps_id=f"SPS-{secrets.token_hex(4).upper()}",
        scheduled_station_aet=settings.MWL_DEFAULT_STATION_AET or None,
        start_date=today,
        status="SCHEDULED",
    )
    session.add(step)
    log.info("HL7 scheduled default worklist step %s for %s",
             step.sps_id, patient.patient_id)


def _ensure_patient(session, data: dict) -> Patient | None:
    """Find the Patient row for an HL7 message (ADT or ORM), creating it if
    needed. Race-safe: if a C-STORE/STOW for the same brand-new MRN wins the
    INSERT first (and we lose on the unique patient_id), we adopt the winner
    instead of crashing the listener thread and dropping the message."""
    # C10: the MRN is no longer globally unique, so this lookup is explicitly
    # limited to the legacy (unscoped) bucket — it can never adopt a patient
    # that belongs to a tenant.
    #
    # KNOWN GAP: the HL7 listener has no AE->tenant mapping (unlike the DICOM
    # receiver), so ADT/ORM registrations can only land in the legacy bucket.
    # A deployment that ingests DICOM from tenant-mapped AE titles must give the
    # listener the same mapping, otherwise HL7-registered patients will not
    # correlate with tenant-scoped DICOM patients.
    patient = (
        session.query(Patient)
        .filter(Patient.patient_id == data["patient_id"], Patient.tenant_id.is_(None))
        .one_or_none()
    )
    if patient is None:
        try:
            patient = Patient(patient_id=data["patient_id"], tenant_id=None)
            session.add(patient)
            log.info("HL7 new patient %s (%s)", data["patient_id"], data.get("name"))
            session.flush()
        except IntegrityError:
            # The modality's insert for this MRN committed first — adopt it.
            session.rollback()
            patient = (
                session.query(Patient)
                .filter(Patient.patient_id == data["patient_id"], Patient.tenant_id.is_(None))
                .one_or_none()
            )
    if patient is not None:
        _apply_demographics(patient, data)
    return patient


def _upsert_patient(data: dict) -> None:
    """Upsert the Patient row for an ADT message, marking it HIS-registered.

    Studies arriving later from modalities carry the same MRN in PatientID, so
    `get_or_create_patient` finds this row and the images auto-attach to it.
    On admit/register (A01/A04) a default Modality Worklist step is also
    opened so the patient appears on modality worklists.
    """
    if not data or not data.get("patient_id"):
        return
    schedule = data.get("trigger") in _SCHEDULING_TRIGGERS
    with SyncSessionLocal() as session:
        patient = _ensure_patient(session, data)
        if patient is None:
            return
        if schedule:
            _create_default_step(session, patient, data)
        session.commit()


def _find_open_order_step(session, patient: Patient, data: dict):
    """Locate the open (SCHEDULED / IN PROGRESS) worklist step an ORM order
    refers to, so re-orders and changes update it instead of duplicating.

    Match by filler order number (accession) first — the stable link a RIS
    re-uses — then by placer order number (Requested Procedure ID)."""
    filler = (data.get("filler_order") or "").strip()
    if filler:
        step = (
            session.query(ScheduledProcedureStep)
            .filter(
                ScheduledProcedureStep.patient_id == patient.id,
                ScheduledProcedureStep.accession_number == filler,
                ScheduledProcedureStep.status.in_(("SCHEDULED", "IN PROGRESS")),
            )
            .first()
        )
        if step is not None:
            return step
    placer = (data.get("placer_order") or "").strip()
    if placer:
        return (
            session.query(ScheduledProcedureStep)
            .filter(
                ScheduledProcedureStep.patient_id == patient.id,
                ScheduledProcedureStep.requested_procedure_id == placer,
                ScheduledProcedureStep.status.in_(("SCHEDULED", "IN PROGRESS")),
            )
            .first()
        )
    return None


def _upsert_order_step(session, patient: Patient, data: dict) -> None:
    """Create or update the scheduled worklist step for an ORM order (NW/XO)."""
    step = _find_open_order_step(session, patient, data)
    if step is None:
        step = ScheduledProcedureStep(
            patient_id=patient.id,
            # ScheduledProcedureStepID is VR SH (max 16 chars) — keep it short.
            sps_id=f"SPS-{secrets.token_hex(4).upper()}",
            status="SCHEDULED",
        )
        session.add(step)
        log.info("HL7 ORM %s scheduled worklist step %s for %s",
                 data.get("control"), step.sps_id, patient.patient_id)
    else:
        log.info("HL7 ORM %s updated worklist step %s for %s",
                 data.get("control"), step.sps_id, patient.patient_id)

    filler = (data.get("filler_order") or "").strip()
    if filler:
        step.accession_number = filler
    placer = (data.get("placer_order") or "").strip()
    if placer:
        step.requested_procedure_id = placer
    if data.get("procedure_code"):
        step.requested_procedure_id = step.requested_procedure_id or data["procedure_code"]
    if data.get("procedure_description"):
        step.requested_procedure_description = data["procedure_description"]
    if data.get("modality"):
        step.modality = data["modality"]
    if data.get("start_date"):
        step.start_date = data["start_date"]
    if data.get("start_time"):
        step.start_time = data["start_time"]
    if data.get("ordering_provider"):
        step.scheduled_performing_physician = data["ordering_provider"]
    if not step.scheduled_station_aet:
        # ORM doesn't carry a station; the config default applies (empty =
        # visible to every station querying the worklist).
        step.scheduled_station_aet = settings.MWL_DEFAULT_STATION_AET or None


def _cancel_order_steps(session, patient: Patient, data: dict) -> None:
    """Mark the open worklist step(s) for a canceled/discontinued ORM order as
    CANCELED (CA/OC/DC/CR). Matching by accession, else placer order number."""
    filler = (data.get("filler_order") or "").strip()
    placer = (data.get("placer_order") or "").strip()
    for step in (
        session.query(ScheduledProcedureStep)
        .filter(
            ScheduledProcedureStep.patient_id == patient.id,
            ScheduledProcedureStep.status.in_(("SCHEDULED", "IN PROGRESS")),
        )
        .all()
    ):
        matches = (filler and step.accession_number == filler) or (
            placer and step.requested_procedure_id == placer
        )
        if matches:
            step.status = "CANCELED"
            log.info("HL7 ORM %s canceled worklist step %s for %s",
                     data.get("control"), step.sps_id, patient.patient_id)


def _upsert_order(data: dict) -> None:
    """Apply an HL7 ORM order: create/update/cancel the patient's scheduled
    worklist step so the modality sees the order on the worklist.

    Order controls:
      NW (new)                 -> create a SCHEDULED step
      XO (change)              -> update the matching open step
      CA/OC/DC/CR (cancel)     -> mark the matching open step CANCELED

    The patient is upserted exactly like ADT (source=his, HIS demographics
    authoritative, race-safe against a simultaneous C-STORE)."""
    if not data or not data.get("patient_id"):
        return
    control = data.get("control") or ""
    with SyncSessionLocal() as session:
        patient = _ensure_patient(session, data)
        if patient is None:
            return
        if control in _ORDER_CANCEL:
            _cancel_order_steps(session, patient, data)
        elif control in _ORDER_CREATE:
            _upsert_order_step(session, patient, data)
        else:
            log.debug("HL7 ORM: unhandled order control %r — ignoring", control)
            return
        session.commit()


def _apply_demographics(patient: Patient, data: dict) -> None:
    """Stamp HIS provenance and enrich demographics from the HL7 message."""
    patient.source = "his"
    if data.get("name"):
        patient.patient_name = data["name"]
    if data.get("birth_date"):
        patient.patient_birth_date = data["birth_date"]
    if data.get("sex"):
        patient.patient_sex = data["sex"]
    if data.get("phone"):
        patient.patient_phone = data["phone"]


def _handle_message(raw: bytes) -> None:
    try:
        text = raw.decode("utf-8", errors="replace").strip("\x0b\x1c\x0d\r\n ")
    except Exception:
        return
    if not text:
        return
    segments = [s for s in text.replace("\r", "\n").split("\n") if s.strip()]
    if not segments or not segments[0].startswith("MSH|"):
        return
    msg_type = (segments[0].split("|")[8] if len(segments[0].split("|")) > 8 else "") or ""
    if msg_type.startswith("ORM^O01"):
        data = parse_orm_message(text)
        if data:
            _upsert_order(data)
        return
    data = parse_adt_message(text)
    if data and data["trigger"] in ("A04", "A08", "A40"):  # PIX Patient Identity Feed
        if data["trigger"] == "A40":
            handle_adt_feed_patient_merge(data)
        else:
            handle_adt_feed_patient_registration(data)
        return
    if msg_type.startswith("QBP^Q23"):  # PIX Query
        _handle_pix_query(text)
        return
    if msg_type.startswith("QBP^Q22"):  # PDQ Query
        _handle_pdq_query(text)
        return
    if data and data["trigger"] in _HANDLED_TRIGGERS:
        _upsert_patient(data)


# --------------------------------------------------------------------------- #
# PIX Query (QBP^Q23) + Response (RSP^K23)
# --------------------------------------------------------------------------- #
def _parse_qpd_segment(seg: str) -> dict:
    """Parse QPD (Query Parameter Definition) segment.
    
    QPD-1 = Query Tag (e.g., "QRY_123")
    QPD-2 = Query/Response Profile (e.g., "IHE PIX Query", "IHE PDQ Query")
    QPD-3+ = query parameters (varies by profile)
    """
    parts = seg.split("|")
    if len(parts) < 3:
        return {}
    return {
        "query_tag": parts[1] if len(parts) > 1 else "",
        "profile": parts[2] if len(parts) > 2 else "",
        "params": parts[3:] if len(parts) > 3 else [],
    }


def _extract_pix_query_params(qpd: dict) -> dict:
    """Extract PIX query parameters from QPD segment.
    
    IHE PIX Query (QBP^Q23) QPD fields:
      QPD-3: Patient ID List (PID-3) - ID^AA^ID_Type
      QPD-4: Patient Name (optional)
      QPD-5: Mother's Maiden Name (optional)
      QPD-6: DOB (optional)
      QPD-7: Sex (optional)
      QPD-8: Address (optional)
      QPD-9: Phone (optional)
    """
    params = qpd.get("params", [])
    result = {"query_tag": qpd.get("query_tag", "")}
    
    # QPD-3: Patient ID List (can be multiple, pipe-separated or repeatable)
    if len(params) >= 1 and params[0]:
        # Format: ID^AA^ID_Type or just ID^AA
        id_parts = params[0].split("^")
        result["patient_id"] = id_parts[0] if len(id_parts) > 0 else None
        result["issuer"] = id_parts[1] if len(id_parts) > 1 else None
        result["id_type"] = id_parts[2] if len(id_parts) > 2 else "MR"
    
    # QPD-4: Patient Name
    if len(params) >= 2 and params[1]:
        result["patient_name"] = params[1].replace("^", " ")
    
    # QPD-6: DOB
    if len(params) >= 4 and params[3]:
        result["birth_date"] = params[3]
    
    # QPD-7: Sex
    if len(params) >= 5 and params[4]:
        result["sex"] = params[4]
    
    # QPD-9: Phone
    if len(params) >= 7 and params[6]:
        result["phone"] = params[6]
    
    return result


def _extract_pdq_query_params(qpd: dict) -> dict:
    """Extract PDQ query parameters from QPD segment.
    
    IHE PDQ Query (QBP^Q22) QPD fields:
      QPD-3: Patient ID List (optional)
      QPD-4: Patient Name (optional)
      QPD-5: Mother's Maiden Name (optional)
      QPD-6: DOB (optional)
      QPD-7: Sex (optional)
      QPD-8: Address (optional)
      QPD-9: Phone (optional)
      QPD-10: SSN (optional)
      QPD-11: Driver's License (optional)
      QPD-12: Assigning Authority (optional) - filter by AA
    """
    params = qpd.get("params", [])
    result = {"query_tag": qpd.get("query_tag", "")}
    
    # QPD-3: Patient ID List
    if len(params) >= 1 and params[0]:
        id_parts = params[0].split("^")
        result["patient_id"] = id_parts[0] if len(id_parts) > 0 else None
        result["issuer"] = id_parts[1] if len(id_parts) > 1 else None
    
    # QPD-4: Patient Name
    if len(params) >= 2 and params[1]:
        result["patient_name"] = params[1].replace("^", " ")
    
    # QPD-6: DOB
    if len(params) >= 4 and params[3]:
        result["birth_date"] = params[3]
    
    # QPD-7: Sex
    if len(params) >= 5 and params[4]:
        result["sex"] = params[4]
    
    # QPD-9: Phone
    if len(params) >= 7 and params[6]:
        result["phone"] = params[6]
    
    # QPD-12: Assigning Authority filter
    if len(params) >= 10 and params[9]:
        result["assigning_authority_oid"] = params[9]
    
    return result


def _handle_pix_query(text: str) -> None:
    """Process a PIX Query (QBP^Q23) and send RSP^K23 response."""
    segments = [s for s in text.replace("\r", "\n").split("\n") if s.strip()]
    qpd = next((s for s in segments if s.startswith("QPD|")), None)
    if not qpd:
        log.warning("PIX Query missing QPD segment")
        return
    
    qpd_data = _parse_qpd_segment(qpd)
    params = _extract_pix_query_params(qpd_data)
    query_tag = params.get("query_tag", "")
    
    # Execute query
    results = []
    if params.get("patient_id") and params.get("issuer"):
        results = pix_query_by_identifier(params["issuer"], params["patient_id"])
    elif params.get("patient_name") or params.get("birth_date") or params.get("sex"):
        results = pix_query_by_demographics(
            patient_name=params.get("patient_name"),
            birth_date=params.get("birth_date"),
            sex=params.get("sex"),
        )
    
    # Build and send response
    response = _build_pix_response(query_tag, results)
    log.info("PIX Query %s: %d result(s)", query_tag, len(results))
    # TODO: Send response back over MLLP connection
    # For now, log the response
    log.debug("PIX Response:\n%s", response)


def _handle_pdq_query(text: str) -> None:
    """Process a PDQ Query (QBP^Q22) and send RSP^K22 response."""
    segments = [s for s in text.replace("\r", "\n").split("\n") if s.strip()]
    qpd = next((s for s in segments if s.startswith("QPD|")), None)
    if not qpd:
        log.warning("PDQ Query missing QPD segment")
        return
    
    qpd_data = _parse_qpd_segment(qpd)
    params = _extract_pdq_query_params(qpd_data)
    query_tag = params.get("query_tag", "")
    
    # Execute query
    results = []
    if params.get("patient_id") and params.get("issuer"):
        results = pdq_query_by_identifier(params["issuer"], params["patient_id"])
    elif params.get("patient_name") or params.get("birth_date") or params.get("sex"):
        results = pdq_query_by_demographics(
            patient_name=params.get("patient_name"),
            birth_date=params.get("birth_date"),
            sex=params.get("sex"),
            assigning_authority_oid=params.get("assigning_authority_oid"),
        )
    
    # Build and send response
    response = _build_pdq_response(query_tag, results)
    log.info("PDQ Query %s: %d result(s)", query_tag, len(results))
    log.debug("PDQ Response:\n%s", response)


def _build_pix_response(query_tag: str, results: list) -> str:
    """Build HL7 RSP^K23 (PIX Query Response) message."""
    from datetime import datetime
    
    msh = f"MSH|^~\\&|PAXS^PIX^|PAXS|QRY|{datetime.now().strftime('%Y%m%d%H%M%S')}||RSP^K23|{query_tag}|P|2.5"
    msa = f"MSA|AA|{query_tag}"
    qak = f"QAK|{query_tag}|OK|IHE PIX Query"
    
    segments = [msh, msa, qak]
    
    for result in results:
        # PID segment with the identity from each AA
        pid_parts = [
            "PID",
            "1",  # PID-1: Set ID
            f"{result.identifier}^^^^{result.assigning_authority_oid}^PI",  # PID-3
            "",  # PID-4: Patient ID List (deprecated)
            "",  # PID-5: Patient Name (will add if available)
        ]
        segments.append("|".join(pid_parts))
    
    return "\r".join(segments) + "\r"


def _build_pdq_response(query_tag: str, results: list) -> str:
    """Build HL7 RSP^K22 (PDQ Query Response) message."""
    from datetime import datetime
    
    msh = f"MSH|^~\\&|PAXS^PDQ^|PAXS|PAXS|QRY|{datetime.now().strftime('%Y%m%d%H%M%S')}||RSP^K22|{query_tag}|P|2.5"
    msa = f"MSA|AA|{query_tag}"
    qak = f"QAK|{query_tag}|OK|IHE PDQ Query"
    
    segments = [msh, msa, qak]
    
    for demo in results:
        # PID segment with all identities
        for ident in demo.identities:
            pid_parts = [
                "PID",
                "1",
                f"{ident.identifier}^^^^{ident.assigning_authority_oid}^PI",
                "",
                demo.patient_name or "",
                "",
                demo.birth_date or "",
                demo.sex or "",
            ]
            segments.append("|".join(pid_parts))
    
    return "\r".join(segments) + "\r"


class HL7Listener(threading.Thread):
    def __init__(self, host: str | None = None, port: int | None = None):
        super().__init__(name="paxs-hl7-listener", daemon=True)
        self.host = host or settings.HL7_HOST
        self.port = port or settings.HL7_PORT
        self._stop = threading.Event()
        self._sock: socket.socket | None = None

    def stop(self) -> None:
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass

    def run(self) -> None:
        log.info("HL7 ADT listener starting on %s:%s", self.host, self.port)
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind((self.host, self.port))
            self._sock.listen(5)
            self._sock.settimeout(1.0)
        except OSError as exc:
            log.error("HL7 listener failed to bind %s:%s — %s", self.host, self.port, exc)
            return

        while not self._stop.is_set():
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            # One thread per connection so a slow/idle EMR never stalls the
            # accept loop for every other sender.
            threading.Thread(
                target=self._handle_connection,
                args=(conn, addr),
                daemon=True,
            ).start()

        log.info("HL7 ADT listener stopped")

    def _handle_connection(self, conn: socket.socket, addr) -> None:
        conn.settimeout(5.0)
        buffer = b""
        try:
            while not self._stop.is_set():
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buffer += chunk
                # MLLP framing: <0x0B> message <0x1C><0x0D>
                while b"\x1c\x0d" in buffer:
                    msg, _, buffer = buffer.partition(b"\x1c\x0d")
                    _handle_message(msg)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


_listener: HL7Listener | None = None


def start_hl7_listener() -> HL7Listener | None:
    global _listener
    if _listener is not None and _listener.is_alive():
        return _listener
    if not settings.HL7_ENABLED:
        return None
    _listener = HL7Listener()
    _listener.start()
    return _listener


def shutdown_hl7() -> None:
    global _listener
    if _listener is not None:
        _listener.stop()
        _listener.join(timeout=3)
        _listener = None
