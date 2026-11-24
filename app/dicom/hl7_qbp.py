"""HL7 QBP PIX/PDQ parsing and MLLP RSP serialization."""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from typing import Any

from app.core.config import settings
from app.services.hl7_pix_pdq_service import (
    hl7_pdq_query,
    hl7_pix_query,
    hl7_sender_tenant,
)
from app.services.pix_pdq_service import PatientDemographics, PatientIdentityResult

log = logging.getLogger("paxs.hl7.qbp")
_PIX_PROFILE = "IHE PIX Query"
_PDQ_PROFILE = "IHE PDQ Query"


def _parse_qpd(segment: str) -> dict[str, Any]:
    """Parse the query name, tag and parameter fields from QPD."""
    parts = segment.split("|")
    if len(parts) < 3:
        return {}
    return {"query_name": parts[1], "query_tag": parts[2], "params": parts[3:]}


def _authority_from_cx(components: list[str]) -> str | None:
    """Extract the universal identifier from a CX assigning-authority HD."""
    authority = components[3] if len(components) > 3 else None
    authority = authority or (components[1] if len(components) > 1 else None)
    authority = authority or (components[0] if components else None)
    parts = (authority or "").split("&")
    return parts[1] if len(parts) > 1 and parts[1] else parts[0] or None


def _query_filters(qpd: dict[str, Any]) -> dict[str, str | None]:
    """Extract the supported identifier/demographic criteria from QPD."""
    params = qpd.get("params", [])
    cx = params[0].split("^") if params and params[0] else []
    authority = _authority_from_cx(cx) if cx else None
    name = params[1] if len(params) > 1 and params[1] else None
    birth = params[3] if len(params) > 3 and params[3] else None
    sex = params[4] if len(params) > 4 and params[4] else None
    phone = params[6] if len(params) > 6 and params[6] else None
    if qpd.get("query_name") == _PDQ_PROFILE and len(params) > 9 and params[9]:
        authority = _authority_from_cx(params[9].split("^")) or authority
    return {
        "identifier": cx[0] if cx and cx[0] else None,
        "assigning_authority_oid": authority,
        "patient_name": name,
        "birth_date": birth,
        "sex": sex,
        "phone": phone,
    }


def _sender_tenant(text: str, source_ip: str | None) -> int | None:
    """Resolve actual TCP peer plus exact MSH-3/MSH-4 using explicit config."""
    fields = text.replace("\n", "\r").split("\r", 1)[0].split("|")
    if not source_ip or len(fields) <= 3:
        return None
    sender_key = f"{source_ip}|{fields[2]}|{fields[3]}"
    try:
        return hl7_sender_tenant(sender_key, settings.HL7_SENDER_TENANT_MAP)
    except Exception as exc:  # Boundary guard: DB failure denies QBP instead of querying globally.
        log.error("hl7_qbp_tenant_resolution_failed error_type=%s", type(exc).__name__)
        return None


def _escape(value: str | None) -> str:
    """Escape HL7 delimiters before serializing stored patient values."""
    escaped = "".join(char for char in (value or "") if ord(char) >= 32 and ord(char) != 127)
    for char, code in (("\\", "E"), ("|", "F"), ("^", "S"), ("~", "R"), ("&", "T")):
        escaped = escaped.replace(char, f"\\{code}\\")
    return escaped


def _response_header(
    text: str, qpd: dict[str, Any], response_type: str, profile: str,
    ack_code: str, query_status: str, error: str | None = None,
) -> list[str]:
    """Build an RSP header and echo the QPD parameter fields."""
    request = text.replace("\n", "\r").split("\r", 1)[0].split("|")
    control_id = _escape(request[9] if len(request) > 9 else "")
    receiver_app = _escape(request[2] if len(request) > 2 else "")
    receiver_facility = _escape(request[3] if len(request) > 3 else "")
    query_tag = _escape(str(qpd.get("query_tag") or ""))
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S+0000")
    response_id = secrets.token_hex(8).upper()
    msh = (
        f"MSH|^~\\&|{_escape(settings.HL7_SENDING_APPLICATION)}|"
        f"{_escape(settings.HL7_SENDING_FACILITY)}|{receiver_app}|{receiver_facility}|"
        f"{timestamp}||{response_type}|{response_id}|P|2.5"
    )
    msa = f"MSA|{ack_code}|{control_id}"
    if error:
        msa += f"|{_escape(error)}"
    query_params = [
        "".join(char for char in str(value) if ord(char) >= 32 and ord(char) != 127)
        for value in qpd.get("params", [])
    ]
    qpd_segment = "|".join(("QPD", _escape(profile), query_tag, *query_params))
    return [msh, msa, f"QAK|{query_tag}|{query_status}|{_escape(profile)}", qpd_segment]


def _pid_for_pix(identities: list[PatientIdentityResult]) -> str:
    """Build one PIX PID with tenant-local identifiers as repetitions."""
    ids = "~".join(
        "^".join((_escape(item.identifier), "", "", _escape(item.assigning_authority_oid),
                  _escape(item.id_type)))
        for item in identities
    )
    return f"PID|1||{ids}"


def _pid_for_pdq(patient: PatientDemographics) -> str:
    """Build one PDQ PID with escaped demographics and tenant-local IDs."""
    ids = "~".join(
        "^".join((_escape(item.identifier), "", "", _escape(item.assigning_authority_oid),
                  _escape(item.id_type)))
        for item in patient.identities
    )
    name = "^".join(_escape(part) for part in (patient.patient_name or "").split("^"))
    return f"PID|1||{ids}||{name}||{_escape(patient.birth_date)}|{_escape(patient.sex)}"


def _frame(segments: list[str]) -> bytes:
    """Encode and MLLP-frame a response payload."""
    return b"\x0b" + ("\r".join(segments) + "\r").encode("utf-8") + b"\x1c\x0d"


def handle_qbp_message(text: str, source_ip: str | None) -> bytes:
    """Return a scoped PIX/PDQ RSP; unknown peers receive an error response."""
    message_type = text.replace("\n", "\r").split("\r", 1)[0].split("|")
    is_pix = len(message_type) > 8 and message_type[8].startswith("QBP^Q23")
    profile = _PIX_PROFILE if is_pix else _PDQ_PROFILE
    response_type = "RSP^K23" if is_pix else "RSP^K22"
    qpd_segment = next((line for line in text.replace("\r", "\n").split("\n")
                        if line.startswith("QPD|")), "")
    qpd = _parse_qpd(qpd_segment)
    if not qpd or not qpd.get("query_tag"):
        return _frame(_response_header(text, qpd, response_type, profile, "AE", "AE",
                                       "Query definition or tag is missing"))
    if qpd.get("query_name") != profile:
        return _frame(_response_header(text, qpd, response_type, profile, "AR", "AE",
                                       "Unsupported query profile"))
    tenant_id = _sender_tenant(text, source_ip)
    if tenant_id is None:
        return _frame(_response_header(text, qpd, response_type, profile, "AR", "AE",
                                       "HL7 sender tenant scope is not configured"))
    return _execute_query(text, qpd, profile, response_type, tenant_id, is_pix)


def _execute_query(
    text: str, qpd: dict[str, Any], profile: str, response_type: str,
    tenant_id: int, is_pix: bool,
) -> bytes:
    """Run a bounded tenant query and build success, no-match or error RSP."""
    try:
        filters = _query_filters(qpd)
        query = hl7_pix_query if is_pix else hl7_pdq_query
        results = query(tenant_id=tenant_id, **filters)
        segments = _response_header(text, qpd, response_type, profile,
                                    "AA", "OK" if results else "NF")
        segments.extend(_pid_for_pix(row) for row in results) if is_pix else segments.extend(
            _pid_for_pdq(row) for row in results
        )
        return _frame(segments)
    except Exception as exc:  # Boundary guard: return only type-safe, PHI-free errors.
        log.error("hl7_qbp_query_failed error_type=%s", type(exc).__name__)
        return _frame(_response_header(text, qpd, response_type, profile, "AE", "AE",
                                       "Query could not be completed"))
