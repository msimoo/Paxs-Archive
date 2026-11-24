"""Standalone PAXS Media Exporter launcher & live progress tracking API.

Bridges ``http://localhost:8000/studies-ui`` (and ``PAXS-Archive.exe``) with
the standalone ``PAXS-MediaExporter.exe`` desktop studio:

  GET  /api/v1/export/launcher/status                      — tool & MicroDicom availability
  POST /api/v1/export/launcher/open                        — create study-scoped session & spawn local .exe
  GET  /api/v1/export/launcher/sessions/{sid}              — poll live progress from studies-ui
  POST /api/v1/export/launcher/sessions/{sid}/progress     — live progress heartbeat from .exe
  GET  /api/v1/export/launcher/sessions/{sid}/study-info   — study metadata for .exe hero card
  GET  /api/v1/export/launcher/sessions/{sid}/download/{f} — stream zip / dicom-zip / anonymized / iso
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.api.v1.deps import get_current_user_any, require_csrf, require_study_access
from app.core.concurrency import run_blocking
from app.core.response_admission import AdmittedStreamingResponse, release_response_bytes, reserve_response_bytes
from app.db.models import AuditLog, Instance, Series, Study, User
from app.db.session import get_db
from app.services import anonymizer_service, client_media_service, microdicom_autorun

log = logging.getLogger("paxs.api.export_launcher")

router = APIRouter(prefix="/api/v1/export/launcher", tags=["Media Exporter Launcher"])

LauncherMode = Literal["media", "dicom-zip"]
DownloadFormat = Literal["zip", "dicom-zip", "iso", "anonymized"]

_SESSION_TTL_SECONDS = 45 * 60
_MAX_SESSIONS = 64
_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}


@dataclass
class LauncherSession:
    session_id: str
    transfer_token: str
    owner_id: int
    username: str
    tenant_id: int | None
    study_uid: str
    study_id: int
    mode: LauncherMode
    study_summary: dict[str, Any]
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    phase: str = "launching"
    percent: int = 0
    message: str = "Launching PAXS Media Exporter…"
    target_label: str = ""
    output_path: str | None = None
    files_done: int = 0
    files_total: int = 0
    bytes_written: int = 0
    speed_mbps: float = 0.0
    microdicom_verified: bool = False
    error: str | None = None

    def is_expired(self, now: float) -> bool:
        return (now - self.updated_at) > _SESSION_TTL_SECONDS

    def public_snapshot(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "study_uid": self.study_uid,
            "mode": self.mode,
            "phase": self.phase,
            "percent": self.percent,
            "message": self.message,
            "target_label": self.target_label,
            "output_path": self.output_path,
            "files_done": self.files_done,
            "files_total": self.files_total,
            "bytes_written": self.bytes_written,
            "speed_mbps": self.speed_mbps,
            "microdicom_verified": self.microdicom_verified,
            "error": self.error,
            "updated_at": self.updated_at,
        }


_sessions_lock = threading.Lock()
_sessions: dict[str, LauncherSession] = {}


def _prune_sessions_locked() -> None:
    now = time.time()
    for sid in [k for k, s in _sessions.items() if s.is_expired(now)]:
        _sessions.pop(sid, None)
    while len(_sessions) >= _MAX_SESSIONS:
        oldest = min(_sessions, key=lambda k: _sessions[k].updated_at)
        _sessions.pop(oldest, None)


def find_exporter_command() -> list[str] | None:
    """Locate PAXS-MediaExporter.exe (or dev Python entry point) on this host."""
    if os.name != "nt":
        return None
    repo_root = Path(__file__).resolve().parents[3]
    exe_candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        here = Path(sys.executable).resolve().parent
        exe_candidates.extend([
            here / "PAXS-MediaExporter.exe",
            here / "PAXS-MediaExporter" / "PAXS-MediaExporter.exe",
            here.parent / "PAXS-MediaExporter" / "PAXS-MediaExporter.exe",
        ])
    exe_candidates.extend([
        repo_root / "dist" / "PAXS-MediaExporter" / "PAXS-MediaExporter.exe",
        repo_root / "dist" / "PAXS-Server" / "PAXS-MediaExporter.exe",
    ])
    for exe in exe_candidates:
        if exe.is_file():
            return [str(exe)]
    script = repo_root / "paxs_media_exporter.py"
    if script.is_file():
        return [sys.executable, str(script)]
    return None


def _is_loopback_request(request: Request) -> bool:
    client_host = (request.client.host if request.client else "").strip().lower()
    return client_host in _LOCAL_HOSTS


def _require_transfer_session(session_id: str, token: str | None) -> LauncherSession:
    with _sessions_lock:
        _prune_sessions_locked()
        sess = _sessions.get(session_id)
    if sess is None or not token or not hmac.compare_digest(sess.transfer_token, token):
        raise HTTPException(401, "Invalid or expired exporter session token.")
    return sess


def _build_study_summary(study: Study, instances: list[Any]) -> dict[str, Any]:
    modalities = sorted({str(s.modality) for s in (study.series_list or []) if getattr(s, "modality", None)})
    patient = study.patient
    total_bytes = sum(max(0, int(getattr(inst, "file_size_bytes", 0) or 0)) for inst in instances)
    return {
        "study_uid": study.study_instance_uid,
        "patient_name": getattr(patient, "patient_name", "") or "Anonymous",
        "patient_id": getattr(patient, "patient_id", "") or "UNKNOWN",
        "study_date": getattr(study, "study_date", "") or "",
        "study_time": getattr(study, "study_time", "") or "",
        "description": getattr(study, "study_description", "") or "",
        "accession": getattr(study, "accession_number", "") or "",
        "modalities": modalities or ["OT"],
        "modality": ", ".join(modalities) if modalities else "OT",
        "n_series": len(study.series_list or []),
        "n_instances": len(instances),
        "total_bytes": total_bytes,
    }


def _spawn_local_exporter(
    cmd_base: list[str], server_url: str, session: LauncherSession
) -> bool:
    args = [
        *cmd_base,
        "--server", server_url,
        "--session", session.session_id,
        "--token", session.transfer_token,
        "--mode", session.mode,
    ]
    try:
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        subprocess.Popen(args, creationflags=flags, close_fds=True)  # noqa: S603 - trusted local executable
        return True
    except OSError as exc:
        log.warning("exporter_spawn_failed error_type=%s", type(exc).__name__)
        return False


class OpenLauncherRequest(BaseModel):
    study_uid: str = Field(..., min_length=1, max_length=128)
    mode: LauncherMode = "media"


class ProgressUpdateRequest(BaseModel):
    phase: str = Field(..., min_length=1, max_length=32)
    percent: int = Field(default=0, ge=0, le=100)
    message: str = Field(default="", max_length=300)
    target_label: str = Field(default="", max_length=120)
    output_path: str | None = Field(default=None, max_length=512)
    files_done: int = Field(default=0, ge=0)
    files_total: int = Field(default=0, ge=0)
    bytes_written: int = Field(default=0, ge=0)
    speed_mbps: float = Field(default=0.0, ge=0.0)
    microdicom_verified: bool = False
    error: str | None = Field(default=None, max_length=300)


@router.get("/status")
async def launcher_status(
    request: Request,
    _user: User = Depends(get_current_user_any),
) -> dict[str, Any]:
    """Report whether the standalone PAXS-MediaExporter.exe & MicroDicom are available."""
    cmd = find_exporter_command()
    return {
        "local_exe_available": cmd is not None and _is_loopback_request(request),
        "exporter_found": cmd is not None,
        "is_loopback": _is_loopback_request(request),
        "protocol_scheme": "paxs-exporter://",
        "microdicom": microdicom_autorun.microdicom_status(),
    }


@router.post("/open", dependencies=[Depends(require_csrf)])
async def open_media_exporter(
    req: OpenLauncherRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user_any),
) -> dict[str, Any]:
    """Create a study-scoped transfer session and launch PAXS-MediaExporter.exe."""
    study = await require_study_access(db, req.study_uid, user)
    full_study = (
        await db.execute(
            select(Study)
            .options(joinedload(Study.patient), joinedload(Study.series_list))
            .where(Study.id == study.id)
        )
    ).unique().scalar_one()
    instances = (
        await db.execute(
            select(Instance)
            .join(Series, Instance.series_id == Series.id)
            .where(Series.study_id == study.id)
            .order_by(Instance.id)
        )
    ).scalars().all()
    if not instances:
        raise HTTPException(404, "This study has no stored DICOM instances.")

    session_id = secrets.token_hex(12)
    transfer_token = secrets.token_urlsafe(32)
    summary = _build_study_summary(full_study, list(instances))
    sess = LauncherSession(
        session_id=session_id,
        transfer_token=transfer_token,
        owner_id=user.id,
        username=user.username,
        tenant_id=user.tenant_id,
        study_uid=req.study_uid,
        study_id=study.id,
        mode=req.mode,
        study_summary=summary,
        files_total=len(instances),
    )
    with _sessions_lock:
        _prune_sessions_locked()
        _sessions[session_id] = sess

    db.add(
        AuditLog(
            study_instance_uid=req.study_uid,
            action=f"EXPORTER_LAUNCH_{req.mode.upper().replace('-', '_')}",
            performed_by=user.username,
        )
    )
    await db.commit()

    server_url = str(request.base_url).rstrip("/")
    qs = urllib.parse.urlencode({
        "server": server_url,
        "session": session_id,
        "token": transfer_token,
        "mode": req.mode,
    })
    protocol_uri = f"paxs-exporter://open?{qs}"

    cmd = find_exporter_command() if _is_loopback_request(request) else None
    launched = _spawn_local_exporter(cmd, server_url, sess) if cmd else False
    return {
        "launched": launched,
        "method": "local-exe" if launched else "protocol",
        "session_id": session_id,
        "protocol_uri": protocol_uri,
        "study": summary,
        "microdicom": microdicom_autorun.microdicom_status(),
    }


@router.get("/sessions/{session_id}")
async def get_launcher_session(
    session_id: str,
    user: User = Depends(get_current_user_any),
) -> dict[str, Any]:
    """Poll live progress of a running PAXS-MediaExporter.exe export."""
    with _sessions_lock:
        _prune_sessions_locked()
        sess = _sessions.get(session_id)
    if sess is None or sess.owner_id != user.id:
        raise HTTPException(404, "Exporter session not found.")
    return sess.public_snapshot()


@router.post("/sessions/{session_id}/progress")
async def update_launcher_progress(
    session_id: str,
    body: ProgressUpdateRequest,
    x_exporter_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Receive live progress telemetry from PAXS-MediaExporter.exe."""
    sess = _require_transfer_session(session_id, x_exporter_token)
    with _sessions_lock:
        sess.phase = body.phase
        sess.percent = body.percent
        sess.message = body.message
        if body.target_label:
            sess.target_label = body.target_label
        if body.output_path is not None:
            sess.output_path = body.output_path
        sess.files_done = body.files_done
        if body.files_total:
            sess.files_total = body.files_total
        sess.bytes_written = body.bytes_written
        sess.speed_mbps = body.speed_mbps
        sess.microdicom_verified = body.microdicom_verified
        sess.error = body.error
        sess.updated_at = time.time()
    return {"ok": True}


@router.get("/sessions/{session_id}/study-info")
async def get_session_study_info(
    session_id: str,
    x_exporter_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Return study metadata + MicroDicom status to PAXS-MediaExporter.exe."""
    sess = _require_transfer_session(session_id, x_exporter_token)
    return {
        "session_id": sess.session_id,
        "mode": sess.mode,
        "study": sess.study_summary,
        "microdicom": microdicom_autorun.microdicom_status(),
    }


def _build_anonymized_for_session(
    workspace: Path, study_uid: str, instances: list[Any], tenant_id: int | None
) -> Path:
    dest = workspace / f"PAXS_ANONYMIZED_{study_uid}.zip"
    paths = [i.path for i in instances]
    backends = {i.path: i.backend for i in instances}
    salt = anonymizer_service.derive_tenant_date_salt(tenant_id)
    anonymizer_service.anonymize_files_to_zip_file(
        paths, dest, zip_name=dest.name, backends=backends, date_shift_salt=salt
    )
    return dest


@router.get("/sessions/{session_id}/download/{media_format}")
async def download_session_package(
    session_id: str,
    media_format: DownloadFormat,
    x_exporter_token: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> AdmittedStreamingResponse:
    """Stream the authorized study package directly to PAXS-MediaExporter.exe."""
    sess = _require_transfer_session(session_id, x_exporter_token)
    instances = await client_media_service.load_instances(db, sess.study_id)
    from tempfile import TemporaryDirectory

    temp_dir = TemporaryDirectory(prefix="paxs-exporter-dl-")
    workspace = Path(temp_dir.name)
    try:
        if media_format == "anonymized":
            pkg = await run_blocking(
                _build_anonymized_for_session, workspace, sess.study_uid, instances, sess.tenant_id
            )
            media_type = "application/zip"
        else:
            pkg = await run_blocking(
                client_media_service.build_client_media,
                workspace, sess.study_uid, instances, media_format,
            )
            media_type = "application/x-iso9660-image" if media_format == "iso" else "application/zip"
        pkg_size = pkg.stat().st_size
    except Exception:
        temp_dir.cleanup()
        raise

    reserve_response_bytes(pkg_size)

    def _stream_and_cleanup():
        try:
            yield from client_media_service.stream_package(pkg)
        finally:
            temp_dir.cleanup()

    try:
        return AdmittedStreamingResponse(
            _stream_and_cleanup(),
            reserved_bytes=pkg_size,
            media_type=media_type,
            headers={
                "Cache-Control": "private, no-store",
                "Content-Disposition": f'attachment; filename="{pkg.name}"',
                "Content-Length": str(pkg_size),
            },
        )
    except BaseException:
        release_response_bytes(pkg_size)
        temp_dir.cleanup()
        raise
