"""HL7 v2 MLLP listener for ADT/ORM feeds and tenant-scoped PIX/PDQ queries.

Listens on a TCP port for:
  * ADT^A01/A04/A08/A31 (admit / register / update) — parses the PID
    segment and upserts the Patient row so studies arriving later from
    modalities can be matched to an existing patient.
  * ORM^O01 (order entry / imaging order) — turns each order into a
    SCHEDULED Modality Worklist step (create on NW, update on XO, cancel on
    CA/OC/DC/CR), the RIS -> PACS write-path that closes the Interop loop.
  * QBP^Q23/Q22 — delegates to ``hl7_qbp`` and returns a tenant-scoped RSP.

This is deliberately a *small, dependency-free* HL7 parser: it handles the
segments PAXS needs (MSH + PID + ORC + OBR, ZDS for radiology orders) and
ignores the rest. It speaks plain HL7 (lower-layer framing is simply
newline-delimited or MLLP).

Threading: runs in its own daemon thread; uses a bounded connection worker
pool and the sync DB engine (same as the DICOM receiver / worker). Every socket
has an explicit timeout and every MLLP frame has a hard byte limit.
"""

from __future__ import annotations

import contextlib
import ipaddress
import logging
import secrets
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.db.models import Patient, ScheduledProcedureStep
from app.db.session import SyncSessionLocal
from app.dicom.hl7_qbp import handle_qbp_message
from app.services import query_service
from app.services.hl7_pix_pdq_service import hl7_sender_tenant
from app.services.pix_pdq_service import (
    handle_adt_feed_patient_merge,
    handle_adt_feed_patient_registration,
)

log = logging.getLogger("paxs.hl7")


def is_hl7_source_allowed(peer_ip: str, allowed: str) -> bool:
    """True when ``peer_ip`` may talk to the MLLP listener (H-06, fail closed).

    ``allowed`` is the comma-separated ``HL7_ALLOWED_SOURCE_IPS`` value. Empty
    admits loopback peers only. An unparseable peer address is refused.
    """
    try:
        address = ipaddress.ip_address(str(peer_ip).split("%", 1)[0])
    except ValueError:
        # Fail closed (N-8), but observably: a socket peer that is not an IP
        # address is never expected, so say so rather than refusing silently.
        log.warning("hl7_peer_address_unparseable; connection refused")
        return False
    if getattr(address, "ipv4_mapped", None) is not None:
        address = address.ipv4_mapped
    entries = [part.strip() for part in (allowed or "").split(",") if part.strip()]
    if not entries:
        return address.is_loopback
    for entry in entries:
        try:
            if address in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:  # validated at settings load; skip defensively
            log.warning("hl7_allowlist_entry_invalid")
    return False

def _hl7_id(value: str) -> str:
    """The namespace ID of an HD field (``APP^OID^ISO`` -> ``APP``), upper-cased."""
    return str(value or "").split("^", 1)[0].strip().upper()


def is_hl7_sender_allowed(application: str, facility: str, allowed: str) -> bool:
    """True when MSH-3/MSH-4 name a sender in ``HL7_ALLOWED_SENDING_FACILITIES`` (H-06).

    Empty ``allowed`` disables the check (the IP allowlist still applies).
    Matching uses the HD namespace ID, case-insensitively, because HL7 senders
    vary the universal-ID components and the case between interface engines.
    """
    entries = [part.strip() for part in (allowed or "").split(",") if part.strip()]
    if not entries:
        return True
    sender = (_hl7_id(application), _hl7_id(facility))
    if not all(sender):
        return False
    for entry in entries:
        app, _sep, fac = entry.partition("|")
        if (_hl7_id(app), _hl7_id(fac)) == sender:
            return True
    return False


class HL7TlsConfigurationError(RuntimeError):
    """HL7_TLS_ENABLED is set but no usable server certificate is configured."""


def hl7_server_ssl_context() -> ssl.SSLContext | None:
    """Server TLS context for MLLP (H-06), or None when TLS is off.

    Raises :class:`HL7TlsConfigurationError` rather than ever falling back to
    plaintext (N-8). ``HL7_TLS_CA`` turns on mutual TLS: the client must
    present a certificate signed by that CA.
    """
    if not settings.HL7_TLS_ENABLED:
        return None
    cert, key = settings.HL7_TLS_CERT, settings.HL7_TLS_KEY
    if not cert or not key:
        raise HL7TlsConfigurationError("HL7_TLS_CERT and HL7_TLS_KEY are required")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        context.load_cert_chain(certfile=cert, keyfile=key)
        if settings.HL7_TLS_CA:
            context.load_verify_locations(cafile=settings.HL7_TLS_CA)
            context.verify_mode = ssl.CERT_REQUIRED
    except (OSError, ssl.SSLError) as exc:
        raise HL7TlsConfigurationError(
            f"HL7 TLS certificate could not be loaded ({type(exc).__name__})"
        ) from exc
    return context


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
        log.debug("hl7_non_hl7_payload_ignored")
        return None

    # MSH-9 is MessageType: e.g. ADT^A01^ADT_A01
    msg_type = (msh.split("|")[8] if len(msh.split("|")) > 8 else "") or ""
    trigger = msg_type.split("^")[1] if "^" in msg_type else msg_type.split("^")[0]

    pid = next((s for s in segments if s.startswith("PID|")), None)
    if pid is None:
        log.debug("hl7_adt_missing_pid")
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
    log.info("hl7_worklist_default_scheduled")


def _ensure_patient(session, data: dict) -> Patient | None:
    """Find the Patient row for an HL7 message (ADT or ORM), creating it if
    needed. Race-safe: if a C-STORE/STOW for the same brand-new MRN wins the
    INSERT first (and we lose on the unique patient_id), we adopt the winner
    instead of crashing the listener thread and dropping the message."""
    tenant_id = data.get("tenant_id")
    patient_scope = query_service.tenant_predicate(
        Patient.tenant_id, tenant_id, False
    )
    patient = (
        session.query(Patient)
        .filter(
            Patient.patient_id == data["patient_id"],
            patient_scope,
        )
        .one_or_none()
    )
    if patient is None:
        try:
            patient = Patient(patient_id=data["patient_id"], tenant_id=tenant_id)
            session.add(patient)
            log.info("hl7_patient_create")
            session.flush()
        except IntegrityError as exc:
            log.warning("hl7_patient_insert_conflict error_type=%s", type(exc).__name__)
            # The modality's insert for this MRN committed first — adopt it.
            session.rollback()
            patient = (
                session.query(Patient)
                .filter(
                    Patient.patient_id == data["patient_id"],
                    patient_scope,
                )
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
        log.info("hl7_orm_worklist_scheduled")
    else:
        log.info("hl7_orm_worklist_updated")

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
            log.info("hl7_orm_worklist_canceled")


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
            log.debug("hl7_orm_unhandled_control_ignored")
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


def _msh_field(value: str) -> str:
    """Keep inbound MSH values safe for reuse in a response segment."""
    return "".join(char for char in value if char not in "|\r\n\x0b\x1c")[:128]


def _build_ack(text: str, ack_code: str, error_text: str | None = None) -> bytes:
    """Build a standard MLLP-framed HL7 ACK for an inbound message."""
    segments = [segment for segment in text.replace("\r", "\n").split("\n") if segment]
    fields = segments[0].split("|") if segments and segments[0].startswith("MSH|") else []
    control_id = _msh_field(fields[9]) if len(fields) > 9 else ""
    receiving_app = _msh_field(fields[2]) if len(fields) > 2 else ""
    receiving_facility = _msh_field(fields[3]) if len(fields) > 3 else ""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S+0000")
    response_id = secrets.token_hex(8).upper()
    msh = (
        f"MSH|^~\\&|PAXS|PAXS|{receiving_app}|{receiving_facility}|"
        f"{timestamp}||ACK|{response_id}|P|2.5"
    )
    msa = f"MSA|{ack_code}|{control_id}"
    ack_segments = [msh, msa]
    if error_text:
        safe_error = _msh_field(error_text)
        ack_segments.append(f"ERR|||{safe_error}")
    payload = ("\r".join(ack_segments) + "\r").encode("utf-8")
    return b"\x0b" + payload + b"\x1c\x0d"


def _resolve_ingest_tenant(text: str, source_ip: str | None) -> tuple[int | None, bool]:
    """Map exact TCP peer/MSH identity; unmapped senders retain the NULL bucket."""
    msh = text.replace("\n", "\r").split("\r", 1)[0].split("|")
    if not source_ip or len(msh) <= 3:
        return None, True
    sender_key = f"{source_ip}|{msh[2]}|{msh[3]}"
    if sender_key not in settings.HL7_SENDER_TENANT_MAP:
        return None, True
    try:
        tenant_id = hl7_sender_tenant(sender_key, settings.HL7_SENDER_TENANT_MAP)
    except Exception as exc:  # noqa: BLE001 - boundary guard: fail closed on mapping/database faults
        log.error("hl7_ingest_tenant_resolution_failed error_type=%s", type(exc).__name__)
        return None, False
    if tenant_id is None:
        log.error("hl7_ingest_tenant_mapping_invalid")
        return None, False
    return tenant_id, True


def _handle_message(raw: bytes, source_ip: str | None = None) -> bytes:
    """Process one HL7 payload and return its MLLP ACK, NAK or QBP RSP frame."""
    text = raw.decode("utf-8", errors="replace").strip("\x0b\x1c\x0d\r\n ")
    segments = [segment for segment in text.replace("\r", "\n").split("\n") if segment.strip()]
    if not segments or not segments[0].startswith("MSH|"):
        log.warning("hl7_message_rejected reason=missing_msh")
        return _build_ack(text, "AR", "MSH segment missing or malformed")

    msh_fields = segments[0].split("|")
    if len(msh_fields) <= 9 or not msh_fields[9].strip():
        log.warning("hl7_message_rejected reason=missing_control_id")
        return _build_ack(text, "AR", "Message control ID missing")

    # H-06: a permitted host must not impersonate another feed. Checked before
    # any message-type dispatch, so a refused sender never reaches the DB.
    if not is_hl7_sender_allowed(msh_fields[2], msh_fields[3],
                                 settings.HL7_ALLOWED_SENDING_FACILITIES):
        # MSH-3/MSH-4 are interface identifiers, not patient data (N-2).
        log.warning("hl7_message_rejected reason=sender_not_allowed app=%s facility=%s",
                    _hl7_id(msh_fields[2]), _hl7_id(msh_fields[3]))
        return _build_ack(text, "AR", "Sending application/facility not authorized")

    msg_type = msh_fields[8] if len(msh_fields) > 8 else ""
    if msg_type.startswith(("QBP^Q23", "QBP^Q22")) and not any(
        segment.startswith("QPD|") for segment in segments
    ):
        return _build_ack(text, "AE", "Query message is missing QPD")

    if msg_type.startswith("QBP^Q23"):
        return handle_qbp_message(text, source_ip)
    if msg_type.startswith("QBP^Q22"):
        return handle_qbp_message(text, source_ip)
    if not msg_type.startswith(("ADT^", "ORM^O01")):
        return _build_ack(text, "AR", "Unsupported message type")

    tenant_id, scope_valid = _resolve_ingest_tenant(text, source_ip)
    if not scope_valid:
        return _build_ack(text, "AE", "Sender tenant scope could not be verified")

    try:
        if msg_type.startswith("ORM^O01"):
            data = parse_orm_message(text)
            if data is None:
                return _build_ack(text, "AE", "ORM message could not be parsed")
            data["tenant_id"] = tenant_id
            _upsert_order(data)
            return _build_ack(text, "AA")

        if msg_type.startswith("ADT^"):
            data = parse_adt_message(text)
            if data is None:
                return _build_ack(text, "AE", "ADT message could not be parsed")
        else:
            data = parse_adt_message(text)
        if data is not None:
            data["tenant_id"] = tenant_id

        if data and data["trigger"] in ("A04", "A08", "A40"):
            if data["trigger"] == "A40":
                # The merge service applies the same mapped or legacy scope.
                handle_adt_feed_patient_merge(data)
            else:
                handle_adt_feed_patient_registration(data)
            return _build_ack(text, "AA")
        if data and data["trigger"] in _HANDLED_TRIGGERS:
            _upsert_patient(data)
        elif data:
            return _build_ack(text, "AR", "Unsupported ADT trigger")
        else:
            return _build_ack(text, "AR", "Unsupported message type")
        return _build_ack(text, "AA")
    except Exception as exc:
        log.error("hl7_message_processing_failed error_type=%s", type(exc).__name__)
        return _build_ack(text, "AE", "Message processing failed")


def _send_ack(conn, ack: bytes) -> bool:
    """Write an MLLP ACK frame; return False when the peer cannot be written to.

    A reply failure is never fatal - the inbound message has already been
    processed - so transport problems are logged and reported, not raised.
    """
    try:
        conn.sendall(ack)
        return True
    except OSError as exc:
        log.debug("hl7_ack_send_failed error_type=%s", type(exc).__name__)
        return False
    except Exception as exc:  # noqa: BLE001 - boundary guard: isolate one client
        log.warning("hl7_ack_send_unsupported error_type=%s", type(exc).__name__)
        return False


class HL7Listener(threading.Thread):
    """Bounded MLLP listener with strict framing and socket limits."""

    def __init__(self, host: str | None = None, port: int | None = None):
        super().__init__(name="paxs-hl7-listener", daemon=True)
        self.host = host or settings.HL7_HOST
        self.port = port or settings.HL7_PORT
        self.max_connections = max(1, int(getattr(settings, "HL7_MAX_CONCURRENT_CONNECTIONS", 20)))
        self.max_message_bytes = max(1, int(getattr(settings, "HL7_MAX_MESSAGE_BYTES", 1024 * 1024)))
        self.socket_timeout = max(1.0, float(getattr(settings, "HL7_SOCKET_TIMEOUT_SECONDS", 15.0)))
        # Keep Thread._stop callable so listener.join() can finish shutdown.
        self._stop_event = threading.Event()
        self._sock: socket.socket | None = None
        self._pool = ThreadPoolExecutor(
            max_workers=self.max_connections,
            thread_name_prefix="paxs-hl7-client",
        )
        self._slots = threading.BoundedSemaphore(self.max_connections)
        self._reject_log_lock = threading.Lock()
        self._last_reject_log = 0.0
        self._ssl_context: ssl.SSLContext | None = None

    def stop(self) -> None:
        self._stop_event.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError as exc:
                log.error("hl7_listener_stop_close_failed error_type=%s", type(exc).__name__)
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _reject_connection(self, conn: socket.socket) -> None:
        """Close an over-capacity socket and rate-limit the warning."""
        with contextlib.suppress(OSError):
            conn.close()
        now = time.monotonic()
        with self._reject_log_lock:
            if now - self._last_reject_log >= 1.0:
                self._last_reject_log = now
                log.warning("hl7_connection_limit_reached")

    def _release_slot(self, _future) -> None:
        self._slots.release()

    def run(self) -> None:
        """Accept connections until shutdown using a bounded worker pool.

        ``socket.timeout`` is the expected idle-poll sentinel: the listening
        socket carries a 1s timeout so shutdown is noticed without blocking,
        and the resulting timeout is normal operation, not an I/O failure.
        """
        log.info("hl7_listener_starting")
        try:
            # H-06: resolved before binding, so a TLS misconfiguration leaves
            # the port closed instead of silently serving plaintext.
            self._ssl_context = hl7_server_ssl_context()
        except HL7TlsConfigurationError as exc:
            # The cause type only (no raw exception text in logs); the operator
            # checks HL7_TLS_CERT / HL7_TLS_KEY / HL7_TLS_CA.
            cause = type(exc.__cause__).__name__ if exc.__cause__ else "missing_cert_or_key"
            log.error("hl7_listener_tls_misconfigured cause=%s; listener not started "
                      "(check HL7_TLS_CERT / HL7_TLS_KEY / HL7_TLS_CA)", cause)
            self._pool.shutdown(wait=False, cancel_futures=True)
            return
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind((self.host, self.port))
            self._sock.listen(self.max_connections)
            self._sock.settimeout(1.0)
        except OSError as exc:
            log.error("hl7_listener_bind_failed error_type=%s", type(exc).__name__)
            self._pool.shutdown(wait=False, cancel_futures=True)
            return

        while not self._stop_event.is_set():
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:  # expected idle-poll sentinel — not an I/O failure
                continue
            except OSError as exc:
                if self._stop_event.is_set():
                    # Shutdown closed the listening socket under us: the loop is
                    # ending, so this is expected rather than a failure.
                    log.debug("hl7_listener_accept_stopped error_type=%s",
                              type(exc).__name__)
                else:
                    log.error("hl7_listener_accept_failed error_type=%s",
                              type(exc).__name__)
                break
            # H-06: an unlisted peer is dropped before any byte is read.
            if not is_hl7_source_allowed(str(addr[0]), settings.HL7_ALLOWED_SOURCE_IPS):
                log.warning("hl7_connection_refused_source peer_ip=%s", addr[0])
                self._reject_connection(conn)
                continue
            if not self._slots.acquire(blocking=False):
                self._reject_connection(conn)
                continue
            try:
                future = self._pool.submit(self._handle_connection, conn, addr)
                future.add_done_callback(self._release_slot)
            except RuntimeError as exc:  # the pool was shut down mid-accept
                # Shutdown raced an accepted connection: give the slot back and
                # drop the peer cleanly rather than leaking a worker slot.
                log.warning("hl7_pool_unavailable_on_accept error_type=%s",
                            type(exc).__name__)
                self._slots.release()
                self._reject_connection(conn)

        log.info("hl7_listener_stopped")

    def _handle_connection(self, conn: socket.socket, addr) -> None:
        buffer = bytearray()
        try:
            conn.settimeout(self.socket_timeout)
            if self._ssl_context is not None:
                # H-06: the handshake runs on this worker thread, bounded by the
                # socket timeout, so a stalled client cannot block accept().
                try:
                    conn = self._ssl_context.wrap_socket(conn, server_side=True)
                except (ssl.SSLError, OSError) as exc:
                    log.warning("hl7_tls_handshake_failed error_type=%s", type(exc).__name__)
                    return
            while not self._stop_event.is_set():
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buffer.extend(chunk)
                # Process complete MLLP frames before enforcing the cap on the
                # still-open frame. This permits multiple bounded messages per
                # connection without retaining earlier payloads.
                while True:
                    trailer_at = buffer.find(b"\x1c\x0d")
                    if trailer_at < 0:
                        break
                    frame = bytes(buffer[:trailer_at])
                    del buffer[:trailer_at + 2]
                    payload = frame[1:] if frame.startswith(b"\x0b") else frame
                    if len(payload) > self.max_message_bytes:
                        return
                    ack: bytes | None
                    try:
                        ack = _handle_message(frame, source_ip=str(addr[0]))
                    except Exception as exc:  # noqa: BLE001 - boundary guard: isolate one client
                        # Never `log.exception` here: the traceback can carry the
                        # message body, which is patient data (N-2).
                        log.warning(
                            "hl7_ack_generation_failed error_type=%s", type(exc).__name__
                        )
                        ack = _build_ack("", "AR", "Message could not be processed")
                    # The message has been processed; replying is best effort so a
                    # read-only or already-closed peer never aborts the read loop.
                    if ack:
                        if not _send_ack(conn, ack):
                            return
                if len(buffer) > self.max_message_bytes:
                    log.warning("hl7_message_size_limit_reached")
                    return
        except (OSError, TimeoutError):
            log.debug("hl7_connection_closed")
        finally:
            buffer.clear()
            try:
                conn.close()
            except OSError as exc:
                log.error("hl7_connection_close_failed error_type=%s", type(exc).__name__)


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
