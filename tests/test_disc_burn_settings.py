from __future__ import annotations

import io
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.fileset import FileSet
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

from app.api.v1 import dicomdir as api
from app.services import disc_burn_service as burn


def make_instance(study_uid, series_uid):
    sop_uid = generate_uid()
    meta = FileMetaDataset()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    meta.MediaStorageSOPClassUID = CTImageStorage
    meta.MediaStorageSOPInstanceUID = sop_uid
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.StudyInstanceUID, ds.SeriesInstanceUID = study_uid, series_uid
    ds.SOPClassUID, ds.SOPInstanceUID = CTImageStorage, sop_uid
    ds.PatientID, ds.PatientName = "TEST", "Test^Patient"
    ds.StudyDate, ds.StudyTime = "20260101", "120000"
    ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "CT", 1, 1
    ds.BitsAllocated = 16
    ds.PixelData = b"\x12\x34"
    out = io.BytesIO()
    ds.save_as(out, enforce_file_format=True)
    raw = out.getvalue()
    return burn.DiscInstance(sop_uid, series_uid, sop_uid, "test-store", len(raw)), raw


def test_stages_all_series_with_original_bytes_and_verifies_readback(tmp_path, monkeypatch):
    study_uid = generate_uid()
    records = dict(make_instance(study_uid, generate_uid()) for _ in range(3))
    paths = {source.path: raw for source, raw in records.items()}

    def read_stream(path, backend_name):
        assert backend_name == "test-store"
        return io.BytesIO(paths[path])

    monkeypatch.setattr(burn.storage_service, "read_stream", read_stream)
    media, manifest = burn.stage_study(tmp_path, study_uid, list(records), 2**20)
    catalog = FileSet(media / "DICOMDIR")
    assert len(catalog) == 3
    assert {instance.SOPInstanceUID for instance in catalog} == paths.keys()
    for instance in catalog:
        assert (media / instance.FileID).read_bytes() == paths[instance.SOPInstanceUID]
    burn.verify_disc(media, manifest)
    (media / next(iter(catalog)).FileID).write_bytes(b"corrupt")
    with pytest.raises(burn.DiscBurnError, match="verification failed"):
        burn.verify_disc(media, manifest)


@pytest.mark.parametrize("problem, expected", [
    ("missing", "could not be read"),
    ("truncated", "unexpected size"),
    ("other-study", "does not match"),
    ("capacity", "does not fit"),
])
def test_does_not_build_disc_with_partial_or_wrong_study(tmp_path, monkeypatch, problem, expected):
    study_uid = generate_uid()
    source, raw = make_instance(generate_uid() if problem == "other-study" else study_uid, generate_uid())

    def read_stream(*args, **kwargs):
        if problem == "missing":
            raise FileNotFoundError
        return io.BytesIO(raw[:200] if problem == "truncated" else raw)

    monkeypatch.setattr(burn.storage_service, "read_stream", read_stream)
    with pytest.raises(burn.DiscBurnError, match=expected):
        burn.stage_study(tmp_path, study_uid, [source], 100 if problem == "capacity" else 2**20)
    assert not (tmp_path / "media" / "DICOMDIR").exists()


def test_burn_api_guards_access_and_reports_verified_job(monkeypatch):
    app = FastAPI()
    app.include_router(api.router)
    settings = SimpleNamespace(DISC_BURN_ENABLED=False, DISC_BURN_DRIVE="D:", DISC_BURN_VOLUME_LABEL="PAXS_EXPORT")
    user = SimpleNamespace(id=1, username="reader")
    study_uid = generate_uid()
    source, _ = make_instance(study_uid, generate_uid())
    row = (source.sop_uid, source.series_uid, source.path, source.backend, source.size)
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(all=lambda: [row])),
        commit=AsyncMock(),
    )
    audit = []
    writes = []

    async def access(db, uid, caller):
        if uid != study_uid or caller.id != 1:
            raise HTTPException(404, "Study not found")
        return SimpleNamespace(id=1)

    async def require_user():
        return user

    class AuditSession:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *args):
            pass

    def simulate_burn(drive, uid, instances, set_phase, *, volume_label):
        writes.append((drive, uid, instances, volume_label))
        set_phase("verifying")

    app.dependency_overrides[api.get_db] = lambda: session
    app.dependency_overrides[api.get_current_user_any] = require_user
    app.dependency_overrides[api.get_current_user] = require_user
    monkeypatch.setattr(api, "_disc_jobs", {})
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    monkeypatch.setattr(api, "require_study_access", access)
    monkeypatch.setattr(api, "AsyncSessionLocal", AuditSession)
    monkeypatch.setattr(api.audit_service, "add_audit", lambda db, sop, action, **kwargs: audit.append(action))
    monkeypatch.setattr(burn, "burn_study", simulate_burn)

    path = f"/api/v1/export/dicomdir/disc/{uuid4().hex}"
    with TestClient(app) as client:
        body = {"study_uid": study_uid}
        assert client.post(path, json=body).status_code == 403
        settings.DISC_BURN_ENABLED = True
        assert client.post(path, json={"study_uid": generate_uid()}).status_code == 404
        assert client.post(path, json=body).status_code == 202
        assert client.get(path).json() == {"phase": "verified", "count": 1, "error": None}
        assert client.post(path, json=body).status_code == 202
        user.id = 2
        assert client.get(path).status_code == 404

    assert writes == [("D:", study_uid, [source], "PAXS_EXPORT")]
    assert audit == ["DISC_BURN_REQUESTED", "DISC_BURN_VERIFIED"]


@pytest.mark.parametrize("output", [
    ["D:", "9", "0"],
    ["D:", "81", "2000"],
    ["D:", "9", "-1"],
    ["bad-drive", "9", "2000"],
])
def test_preflight_rejects_unusable_media_status(monkeypatch, tmp_path, output):
    monkeypatch.setattr(burn, "_imapi", lambda *args: output)
    with pytest.raises(burn.DiscBurnError):
        burn._check_media(tmp_path / "check.vbs", "D:")


def test_admin_optical_diagnostics_and_activity(monkeypatch):
    app = FastAPI()
    app.include_router(api.router)
    user = SimpleNamespace(role="READER")
    app.dependency_overrides[api.get_current_user_any] = lambda: user
    jobs = {}
    monkeypatch.setattr(api, "_disc_jobs", jobs)
    monkeypatch.setattr(burn, "_imapi", lambda script, mode, drive="": (
        ["F:\tTest DVD writer", "D:\tTest CD writer"] if mode == "list"
        else [drive, "9", "2300000"]
    ))

    root = "/api/v1/export/dicomdir/disc"
    with TestClient(app) as client:
        for tool in ("/recorders", "/media?drive=F:", "/activity"):
            assert client.get(root + tool).status_code == 403
        user.role = "ADMIN"
        assert client.get(root + "/recorders").json() == {"recorders": [
            {"drive": "D:", "model": "Test CD writer"}, {"drive": "F:", "model": "Test DVD writer"},
        ]}
        assert client.get(root + "/media?drive=bad").status_code == 422
        media = client.get(root + "/media?drive=F:").json()
        assert media["drive"] == "F:" and media["media_type"] == "DVD-R"
        assert media["available_bytes"] == 2300000 * 2048
        jobs[uuid4()] = api.DiscBurnJob(1, "study-uid", 3, "F:", phase="writing")
        assert client.get(root + "/media?drive=F:").status_code == 409
        activity = client.get(root + "/activity").json()
        assert activity["active"] is True
        assert activity["jobs"][0]["phase"] == "writing"
        assert "study-uid" not in str(activity)
