"""Live Admin Services Control & DCMTK Diagnostics API (/api/v1/services/*).

Backs the upgraded Admin -> Services tab (http://localhost:8000/#admin):
  GET  /api/v1/services/overview          — live DICOM SCP, DCMTK, HL7 & Web layers
  POST /api/v1/services/dicom/{action}    — live start / stop / restart DIMSE SCP
  POST /api/v1/services/hl7/{action}      — live start / stop / restart HL7 MLLP listener
  POST /api/v1/services/dcmtk/detect      — scan local system for native OFFIS DCMTK
  POST /api/v1/services/dcmtk/echo        — run live C-ECHO (DCMTK or pynetdicom)
  POST /api/v1/services/dcmtk/find        — run live study-level C-FIND diagnostic
  POST /api/v1/services/emr/test          — test TCP reachability to configured HL7 EMR
"""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
import time
from collections.abc import Callable
from typing import Any, Literal, TypeVar

_T = TypeVar("_T")

from fastapi import APIRouter, Depends, HTTPException
from pydicom.dataset import Dataset
from pydantic import BaseModel, Field

from app.api.v1.deps import require_admin, require_csrf
from app.core.config import settings
from app.db.models import User
from app.dicom import dcmtk, hl7, receiver
from app.services import dicom_health_service, storage_service

log = logging.getLogger("paxs.api.services_control")

# Serialize live listener lifecycle actions so double-clicks cannot race socket
# bind/teardown, and cap concurrent diagnostic DCMTK subprocesses.
_dicom_control_lock = threading.Lock()
_hl7_control_lock = threading.Lock()
_dcmtk_probe_sem = threading.BoundedSemaphore(4)

router = APIRouter(
    prefix="/api/v1/services",
    tags=["services-control"],
    dependencies=[Depends(require_csrf)],
)


class EchoTestRequest(BaseModel):
    host: str = Field(default="127.0.0.1", min_length=1, max_length=255)
    port: int = Field(default=10400, ge=1, le=65535)
    ae_title: str = Field(default="PAXS_SCP", min_length=1, max_length=16)
    engine: Literal["auto", "dcmtk", "pynetdicom"] = "auto"


class FindTestRequest(BaseModel):
    host: str = Field(default="127.0.0.1", min_length=1, max_length=255)
    port: int = Field(default=10400, ge=1, le=65535)
    ae_title: str = Field(default="PAXS_SCP", min_length=1, max_length=16)
    patient_id: str = Field(default="", max_length=64)
    study_date: str = Field(default="", max_length=17)
    modality: str = Field(default="", max_length=16)
    max_results: int = Field(default=20, ge=1, le=100)


def _pynetdicom_echo(host: str, port: int, ae_title: str) -> dict[str, Any]:
    from pynetdicom import AE
    from pynetdicom.sop_class import Verification

    started = time.perf_counter()
    ae = AE(ae_title="PAXS_SCU")
    ae.acse_timeout = 5
    ae.dimse_timeout = 5
    ae.network_timeout = 5
    ae.add_requested_context(Verification)
    assoc = ae.associate(host, port, ae_title=ae_title)
    if not assoc.is_established:
        return {"engine": "pynetdicom", "ok": False, "status": None, "reason": "Association rejected or peer unreachable."}
    try:
        st = assoc.send_c_echo()
        code = int(getattr(st, "Status", -1)) if st is not None else None
        ok = code == 0
        return {
            "engine": "pynetdicom",
            "ok": ok,
            "status": code,
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 1),
            "reason": None if ok else "C-ECHO returned non-zero status.",
        }
    finally:
        assoc.release()


def _run_echo_probe(req: EchoTestRequest) -> dict[str, Any]:
    use_dcmtk = req.engine == "dcmtk" or (req.engine == "auto" and dcmtk.enabled_for())
    if use_dcmtk:
        started = time.perf_counter()
        res = dcmtk.send_echo(req.host, req.port, req.ae_title)
        res["latency_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
        return res
    return _pynetdicom_echo(req.host, req.port, req.ae_title)


def _run_find_probe(req: FindTestRequest) -> dict[str, Any]:
    query = Dataset()
    query.QueryRetrieveLevel = "STUDY"
    query.PatientID = req.patient_id.strip()
    query.PatientName = ""
    query.StudyDate = req.study_date.strip()
    query.StudyDescription = ""
    query.StudyInstanceUID = ""
    query.ModalitiesInStudy = req.modality.strip().upper()
    started = time.perf_counter()
    res = dcmtk.send_find(
        req.host, req.port, req.ae_title, query, max_results=req.max_results
    )
    elapsed = round((time.perf_counter() - started) * 1000.0, 1)
    if not res.get("ok"):
        return {
            "ok": False,
            "engine": "dcmtk",
            "latency_ms": elapsed,
            "reason": res.get("reason") or "DCMTK C-FIND failed.",
            "matches": [],
        }
    rows = [
        {
            "study_uid": str(getattr(ds, "StudyInstanceUID", "") or ""),
            "patient_id": str(getattr(ds, "PatientID", "") or ""),
            "study_date": str(getattr(ds, "StudyDate", "") or ""),
            "modality": str(getattr(ds, "ModalitiesInStudy", "") or ""),
            "description": str(getattr(ds, "StudyDescription", "") or ""),
        }
        for ds in res.get("matches", [])[:20]
    ]
    return {
        "ok": True,
        "engine": "dcmtk",
        "latency_ms": elapsed,
        "count": len(res.get("matches", [])),
        "truncated": bool(res.get("truncated")),
        "matches": rows,
    }


def _probe_tcp(host: str, port: int, timeout: float = 3.0) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return {"ok": True, "host": host, "port": int(port), "latency_ms": round((time.perf_counter() - started) * 1000.0, 1)}
    except OSError as exc:
        return {"ok": False, "host": host, "port": int(port), "error": type(exc).__name__}


@router.get("/overview")
async def services_overview(_user: User = Depends(require_admin)) -> dict[str, Any]:
    """Return live status for DICOM SCP, DCMTK binaries, HL7 MLLP, and Web layers."""
    dicom_probe, dcmtk_info, detected, disk_status = await asyncio.gather(
        asyncio.to_thread(dicom_health_service.probe_receiver),
        asyncio.to_thread(dcmtk.detailed_status),
        asyncio.to_thread(dcmtk.detect_installations),
        asyncio.to_thread(storage_service.get_local_disk_status),
    )
    hl7_live = getattr(hl7, "_listener", None) is not None
    return {
        "dicom": {
            "live": bool(dicom_probe.get("live")),
            "configured": bool(settings.DICOM_RECEIVER_ENABLED),
            "ae_title": settings.DICOM_AE_TITLE,
            "host": settings.DICOM_HOST,
            "port": int(settings.DICOM_PORT),
            "max_associations": int(settings.DICOM_MAX_ASSOCIATIONS),
            "max_pdu_size": int(settings.DICOM_MAX_PDU_SIZE),
            "network_timeout_seconds": int(settings.DICOM_NETWORK_TIMEOUT_SECONDS),
            "dimse_timeout_seconds": int(settings.DICOM_DIMSE_TIMEOUT_SECONDS),
            "tls_enabled": bool(settings.DICOM_TLS_ENABLED),
            "allowed_calling_aes": settings.DICOM_ALLOWED_CALLING_AES,
            "mwl_default_station_aet": settings.MWL_DEFAULT_STATION_AET,
            "queue": receiver.receiver_queue_snapshot(),
        },
        "storage_disk": disk_status,
        "dcmtk": dcmtk_info,
        "dcmtk_detected": detected,
        "hl7": {
            "live": hl7_live,
            "enabled": bool(settings.HL7_ENABLED),
            "host": settings.HL7_HOST,
            "port": int(settings.HL7_PORT),
            "tls_enabled": bool(settings.HL7_TLS_ENABLED),
            "allowed_source_ips": settings.HL7_ALLOWED_SOURCE_IPS,
            "allowed_senders": settings.HL7_ALLOWED_SENDING_FACILITIES,
            "emr_enabled": bool(settings.HL7_EMR_ENABLED),
            "emr_host": settings.HL7_EMR_HOST,
            "emr_port": int(settings.HL7_EMR_PORT),
            "emr_application": settings.HL7_EMR_APPLICATION,
            "emr_facility": settings.HL7_EMR_FACILITY,
        },
        "web": {
            "dicomweb_public_read": bool(settings.DICOMWEB_PUBLIC_READ),
            "dicomweb_public_write": bool(settings.DICOMWEB_PUBLIC_WRITE),
            "dicomweb_client_allow_private": bool(settings.DICOMWEB_CLIENT_ALLOW_PRIVATE),
            "fhir_enabled": bool(settings.FHIR_ENABLED),
            "fusion_enabled": bool(settings.FUSION_ENABLED),
            "transfer_accelerator_enabled": bool(settings.TRANSFER_ACCELERATOR_ENABLED),
            "transfer_accelerator_workers": int(settings.TRANSFER_ACCELERATOR_WORKERS),
            "webhook_urls": settings.WEBHOOK_URLS,
            "paxs_viewer_dir": settings.PAXS_VIEWER_DIR,
        },
    }


@router.post("/dicom/{action}")
async def control_dicom_receiver(
    action: Literal["start", "stop", "restart"],
    _user: User = Depends(require_admin),
) -> dict[str, Any]:
    """Start, stop, or restart the in-process DIMSE SCP listener."""
    def _apply() -> dict[str, Any]:
        with _dicom_control_lock:
            if action in ("stop", "restart"):
                receiver.shutdown_receiver()
            if action in ("start", "restart"):
                receiver.invalidate_calling_ae_cache()
                receiver.start_receiver_in_thread()
                time.sleep(0.4)
            probe = dicom_health_service.probe_receiver()
            return {"ok": True, "action": action, "live": bool(probe.get("live")), "probe": probe}

    return await asyncio.to_thread(_apply)


@router.post("/hl7/{action}")
async def control_hl7_listener(
    action: Literal["start", "stop", "restart"],
    _user: User = Depends(require_admin),
) -> dict[str, Any]:
    """Start, stop, or restart the in-process HL7 MLLP listener."""
    def _apply() -> dict[str, Any]:
        with _hl7_control_lock:
            if action in ("stop", "restart"):
                hl7.shutdown_hl7()
            if action in ("start", "restart"):
                hl7.start_hl7_listener()
                time.sleep(0.2)
            return {"ok": True, "action": action, "live": getattr(hl7, "_listener", None) is not None}

    try:
        return await asyncio.to_thread(_apply)
    except Exception as exc:
        log.warning("hl7_control_failed action=%s error_type=%s", action, type(exc).__name__)
        raise HTTPException(400, f"HL7 listener {action} failed ({type(exc).__name__})") from None


@router.post("/dcmtk/detect")
async def detect_dcmtk(_user: User = Depends(require_admin)) -> dict[str, Any]:
    """Scan this host for genuine OFFIS DCMTK installations."""
    found = await asyncio.to_thread(dcmtk.detect_installations)
    return {"found": found, "count": len(found)}


def _run_bounded_probe(fn: Callable[[_T], dict[str, Any]], body: _T) -> dict[str, Any]:
    if not _dcmtk_probe_sem.acquire(blocking=False):
        raise HTTPException(429, "Too many concurrent DICOM diagnostic probes; retry shortly.")
    try:
        return fn(body)
    finally:
        _dcmtk_probe_sem.release()


@router.post("/dcmtk/echo")
async def test_dcmtk_echo(
    body: EchoTestRequest, _user: User = Depends(require_admin)
) -> dict[str, Any]:
    """Run a live C-ECHO probe against a local or remote DICOM peer."""
    return await asyncio.to_thread(_run_bounded_probe, _run_echo_probe, body)


@router.post("/dcmtk/find")
async def test_dcmtk_find(
    body: FindTestRequest, _user: User = Depends(require_admin)
) -> dict[str, Any]:
    """Run a live DCMTK C-FIND study probe against a DICOM peer."""
    return await asyncio.to_thread(_run_bounded_probe, _run_find_probe, body)


@router.post("/emr/test")
async def test_emr_connection(_user: User = Depends(require_admin)) -> dict[str, Any]:
    """Verify TCP reachability to the configured outbound HL7 EMR endpoint."""
    return await asyncio.to_thread(_probe_tcp, settings.HL7_EMR_HOST, settings.HL7_EMR_PORT)
