"""Tests for Phase 1–3 scalability, stability, and standalone .exe fixes."""

from __future__ import annotations

import asyncio
import io
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage, generate_uid
from sqlalchemy.pool import NullPool

from app.api import ratelimit
from app.core.config import settings
from app.db import session as db_session
from app.dicom import receiver
from app.services import anonymizer_service, dicomdir_service, disc_burn_service, redis_service, storage_service
import paxs_desktop


def _make_dicom_bytes(study_uid: str, series_uid: str, sop_uid: str) -> bytes:
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    meta.MediaStorageSOPInstanceUID = sop_uid
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset("test.dcm", {}, file_meta=meta, preamble=b"\0" * 128)
    ds.is_little_endian = True
    ds.is_implicit_VR = False
    ds.PatientName = "DOE^JOHN"
    ds.PatientID = "MRN-999"
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.SOPInstanceUID = sop_uid
    ds.SOPClassUID = SecondaryCaptureImageStorage
    ds.Modality = "OT"
    buf = io.BytesIO()
    ds.save_as(buf, enforce_file_format=True)
    return buf.getvalue()


def test_anonymize_files_to_zip_file_writes_directly_to_disk(tmp_path: Path, monkeypatch) -> None:
    study_uid, series_uid, sop_uid = generate_uid(), generate_uid(), generate_uid()
    dcm_path = tmp_path / "source.dcm"
    dcm_path.write_bytes(_make_dicom_bytes(study_uid, series_uid, sop_uid))

    from pydicom import dcmread

    monkeypatch.setattr(
        storage_service,
        "read_dataset",
        lambda key, backend_name=None: dcmread(dcm_path),
    )

    out_zip = tmp_path / "anon.zip"
    result = anonymizer_service.anonymize_files_to_zip_file(["study/series/sop.dcm"], out_zip)
    assert result == out_zip
    assert out_zip.is_file() and out_zip.stat().st_size > 0
    with zipfile.ZipFile(out_zip, "r") as zf:
        members = zf.namelist()
        assert len(members) == 1
        assert study_uid not in members[0]
        ds = dcmread(io.BytesIO(zf.read(members[0])))
        assert getattr(ds, "PatientIdentityRemoved", "") == "YES"
        assert "PatientName" not in ds


def test_dicomdir_write_streams_from_storage_to_disk(tmp_path: Path, monkeypatch) -> None:
    study_uid, series_uid, sop_uid = generate_uid(), generate_uid(), generate_uid()
    raw = _make_dicom_bytes(study_uid, series_uid, sop_uid)

    monkeypatch.setattr(storage_service, "exists", lambda key, backend_name=None: True)
    monkeypatch.setattr(storage_service, "read_stream", lambda key, backend_name=None: io.BytesIO(raw))

    structure = [{
        "patient_id": "P001",
        "patient_name": "TEST^PATIENT",
        "study_instance_uid": study_uid,
        "series_list": [{
            "series_instance_uid": series_uid,
            "modality": "OT",
            "series_number": 1,
            "instances": [{
                "sop_instance_uid": sop_uid,
                "sop_class_uid": str(SecondaryCaptureImageStorage),
                "relative_file_id": ["IMAGES", "IMG00001"],
            }],
        }],
    }]
    out_zip = tmp_path / "dicomdir.zip"
    written = dicomdir_service._write_dicomdir_zip(
        out_zip, structure, [("k/1.dcm", ["IMAGES", "IMG00001"], None)]
    )
    assert written == 1
    with zipfile.ZipFile(out_zip, "r") as zf:
        names = set(zf.namelist())
        assert "DICOMDIR" in names
        assert "IMAGES/IMG00001" in names
        assert zf.read("IMAGES/IMG00001") == raw


def test_rate_limit_skips_blocking_pool_when_redis_disabled(monkeypatch) -> None:
    ratelimit.reset_rate_limits()
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(settings, "REDIS_CACHE_ENABLED", False)

    called = False

    async def fail_if_called(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("run_blocking must not be called when Redis is disabled")

    monkeypatch.setattr(ratelimit, "run_blocking", fail_if_called)
    dep = ratelimit.rate_limit("test_fastpath", 5, 60)
    req = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})
    asyncio.run(dep(req))
    assert not called


def test_is_redis_ready_fast_respects_open_circuit(monkeypatch) -> None:
    monkeypatch.setattr(settings, "REDIS_CACHE_ENABLED", True)
    monkeypatch.setattr(settings, "REDIS_URL", "redis://127.0.0.1:6379/0")
    redis_service.reset_redis_circuit()
    assert redis_service.is_redis_ready_fast() is True

    for _ in range(settings.REDIS_CIRCUIT_FAILURE_THRESHOLD):
        with pytest.raises(OSError):
            redis_service.redis_circuit_call(lambda: (_ for _ in ()).throw(OSError("down")))
    assert redis_service.is_redis_ready_fast() is False
    redis_service.reset_redis_circuit()


def test_dimse_engine_is_pooled_and_reuses_single_loop() -> None:
    assert not isinstance(db_session.dimse_async_engine.sync_engine.pool, NullPool)

    async def current_loop_id() -> int:
        return id(asyncio.get_running_loop())

    loop_a = db_session.run_dimse_coro(current_loop_id())
    loop_b = db_session.run_dimse_coro(current_loop_id())
    assert loop_a == loop_b


def test_dimse_association_cache_reuses_and_clears(monkeypatch) -> None:
    tenant_calls = 0
    backend_calls = 0

    def fake_tenant(ae):
        nonlocal tenant_calls
        tenant_calls += 1
        return 7

    def fake_backend(uid):
        nonlocal backend_calls
        backend_calls += 1
        return "s3-main"

    monkeypatch.setattr(receiver, "_resolve_tenant", fake_tenant)
    monkeypatch.setattr(storage_service, "resolve_study_backend", fake_backend)

    assoc = SimpleNamespace(is_released=True)
    assert receiver._resolve_tenant_for_assoc(assoc, "CT_01") == 7
    assert receiver._resolve_tenant_for_assoc(assoc, "CT_01") == 7
    assert tenant_calls == 1

    assert receiver._resolve_backend_for_assoc(assoc, "1.2.3") == "s3-main"
    assert receiver._resolve_backend_for_assoc(assoc, "1.2.3") == "s3-main"
    assert backend_calls == 1

    receiver._track_association_end(SimpleNamespace(assoc=assoc, event=receiver.evt.EVT_RELEASED))
    assert id(assoc) not in receiver._assoc_cache


def test_desktop_rotating_log_stream_caps_file_size(tmp_path: Path) -> None:
    log_path = tmp_path / "paxs-desktop.log"
    stream = paxs_desktop._RotatingLogStream(log_path, max_bytes=1024, backup_count=2)
    try:
        for i in range(60):
            stream.write(f"line-{i:03d}-" + ("x" * 60) + "\n")
    finally:
        stream._stream.close()

    assert log_path.exists()
    assert (tmp_path / "paxs-desktop.log.1").exists()
    assert (tmp_path / "paxs-desktop.log.2").exists()
    assert not (tmp_path / "paxs-desktop.log.3").exists()


def test_desktop_window_api_tracks_active_local_jobs() -> None:
    api = paxs_desktop.WindowApi(base_url="http://127.0.0.1:8000")
    assert api.has_active_local_jobs() is False
    api._local_jobs["j1"] = {"phase": "writing"}
    assert api.has_active_local_jobs() is True
    api._local_jobs["j1"]["phase"] = "verified"
    assert api.has_active_local_jobs() is False


def test_write_zip_to_target_direct_staging_and_validation(tmp_path: Path) -> None:
    zip_path = tmp_path / "pkg.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("DICOMDIR", b"DICM-HEADER")
        zf.writestr("IMAGES/IMG00001", b"PIXEL-DATA")

    dest_parent = tmp_path / "usb_root"
    dest_parent.mkdir()
    phases: list[str] = []
    out_dir = disc_burn_service.write_zip_to_target(
        zip_path, dest_parent, phases.append, folder_name="Study_A"
    )
    out = Path(out_dir)
    assert out.is_dir()
    assert (out / "DICOMDIR").read_bytes() == b"DICM-HEADER"
    assert (out / "IMAGES" / "IMG00001").read_bytes() == b"PIXEL-DATA"
    assert phases == ["preparing", "writing", "verifying"]
    # No leftover .paxs-stage-* directories remain.
    assert not list(dest_parent.glob(".paxs-stage-*"))

    bad_zip = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad_zip, "w") as zf:
        zf.writestr("../escape.txt", b"nope")
    with pytest.raises(disc_burn_service.DiscBurnError):
        disc_burn_service.write_zip_to_target(bad_zip, dest_parent, MagicMock(), folder_name="Bad")


def test_packaging_and_ops_scripts_hardening() -> None:
    root = Path(__file__).resolve().parents[1]
    build_bat = (root / "build_standalone.bat").read_text(encoding="utf-8")
    assert 'if exist ".env" copy /y ".env"' not in build_bat
    assert "--keep-env" in build_bat

    install_bat = (root / "scripts" / "install_service.bat").read_text(encoding="utf-8")
    assert 'if exist "%~dp0PAXS-Server.exe"' in install_bat
    assert "AppRotateBytes 10485760" in install_bat
    assert "AppExit Default Restart" in install_bat
    assert "http://localhost:8000" in install_bat

    health_bat = (root / "health_monitor.bat").read_text(encoding="utf-8")
    assert "PAXS-Archive.exe" in health_bat
    assert ":5432" in health_bat

    haproxy = (root / "deploy" / "haproxy.cfg").read_text(encoding="utf-8")
    assert "timeout server 600s" in haproxy
