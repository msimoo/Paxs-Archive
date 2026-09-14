"""Tests for Radiation Dose Monitoring (RDSR) feature."""

from __future__ import annotations

import pytest
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian, generate_uid
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AuditLog, RadiationDoseReport
from app.db.session import AsyncSessionLocal, sync_engine
from app.services import dose_service, indexing_service, storage_service

RDSR_SOP_CLASS = "1.2.840.10008.5.1.4.1.1.88.67"


def _make_rdsr(
    *,
    patient_id: str = "DOSE_PAT1",
    study_uid: str | None = None,
    sop_uid: str | None = None,
    dlp: float = 850.5,
    ctdivol: float = 22.4,
    events_count: int = 3,
) -> Dataset:
    """Build a synthetic Radiation Dose Structured Report (RDSR) DICOM dataset."""
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = RDSR_SOP_CLASS
    sop = sop_uid or generate_uid()
    file_meta.MediaStorageSOPInstanceUID = sop
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

    ds = FileDataset("rdsr.dcm", {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.SOPClassUID = RDSR_SOP_CLASS
    ds.SOPInstanceUID = sop
    ds.Modality = "SR"
    ds.PatientID = patient_id
    ds.PatientName = "Dose^Test"
    ds.StudyInstanceUID = study_uid or generate_uid()
    ds.SeriesInstanceUID = generate_uid()
    ds.Manufacturer = "Siemens"
    ds.ManufacturerModelName = "SOMATOM Definition"

    # Root Content Sequence
    content_seq = []

    # 1. Total DLP
    dlp_item = Dataset()
    dlp_code = Dataset()
    dlp_code.CodeValue = "113813"
    dlp_code.CodeMeaning = "Dose Length Product"
    dlp_item.ConceptNameCodeSequence = Sequence([dlp_code])
    dlp_item.ValueType = "NUMERIC"
    meas_dlp = Dataset()
    meas_dlp.NumericValue = str(dlp)
    dlp_item.MeasuredValueSequence = Sequence([meas_dlp])
    content_seq.append(dlp_item)

    # 2. Mean CTDIvol
    ctdi_item = Dataset()
    ctdi_code = Dataset()
    ctdi_code.CodeValue = "113830"
    ctdi_code.CodeMeaning = "Mean CTDIvol"
    ctdi_item.ConceptNameCodeSequence = Sequence([ctdi_code])
    ctdi_item.ValueType = "NUMERIC"
    meas_ctdi = Dataset()
    meas_ctdi.NumericValue = str(ctdivol)
    ctdi_item.MeasuredValueSequence = Sequence([meas_ctdi])
    content_seq.append(ctdi_item)

    # 3. Irradiation Events
    for i in range(events_count):
        ev_item = Dataset()
        ev_code = Dataset()
        ev_code.CodeValue = "113706"
        ev_code.CodeMeaning = "Irradiation Event"
        ev_item.ConceptNameCodeSequence = Sequence([ev_code])
        ev_item.ValueType = "CONTAINER"
        content_seq.append(ev_item)

    ds.ContentSequence = Sequence(content_seq)
    return ds


@pytest.mark.anyio
async def test_parse_and_index_rdsr_service():
    """Verify parsing and auto-indexing of Radiation Dose SR datasets."""
    study_uid = generate_uid()
    rdsr_ds = _make_rdsr(patient_id="DOSE_PAT_SERVICE", study_uid=study_uid, dlp=920.0, ctdivol=25.0, events_count=4)

    # Ingest RDSR dataset
    async with AsyncSessionLocal() as session:
        async with session.begin():
            key, sha = storage_service.save_dataset(rdsr_ds)
            from app.dicom.parser import parse_metadata
            await indexing_service.index_new_instance(session, parse_metadata(rdsr_ds), key, sha)

    # Query DB
    with Session(sync_engine) as session:
        reports = session.execute(
            select(RadiationDoseReport).where(RadiationDoseReport.study_instance_uid == study_uid)
        ).scalars().all()

        assert len(reports) == 1
        r = reports[0]
        assert r.patient_id == "DOSE_PAT_SERVICE"
        assert r.total_dlp_mgy_cm == 920.0
        assert r.total_ctdivol_mgy == 25.0
        assert r.irradiation_events_count == 4
        assert r.manufacturer == "Siemens"

        # Audit Log check
        audits = session.execute(
            select(AuditLog).where(AuditLog.sop_instance_uid == rdsr_ds.SOPInstanceUID)
        ).scalars().all()
        actions = {a.action for a in audits}
        assert "DOSE_REPORT_INDEXED" in actions


@pytest.mark.anyio
async def test_patient_cumulative_dose_service():
    """Verify cumulative radiation dose calculation across multiple studies for a patient."""
    pid = "CUMULATIVE_DOSE_PAT"
    ds1 = _make_rdsr(patient_id=pid, dlp=400.0, ctdivol=10.0, events_count=2)
    ds2 = _make_rdsr(patient_id=pid, dlp=600.0, ctdivol=15.0, events_count=3)

    async with AsyncSessionLocal() as session:
        async with session.begin():
            k1, s1 = storage_service.save_dataset(ds1)
            k2, s2 = storage_service.save_dataset(ds2)
            from app.dicom.parser import parse_metadata
            await indexing_service.index_new_instance(session, parse_metadata(ds1), k1, s1)
            await indexing_service.index_new_instance(session, parse_metadata(ds2), k2, s2)

    async with AsyncSessionLocal() as session:
        summary = await dose_service.get_patient_cumulative_dose(session, pid)
        assert summary["patient_id"] == pid
        assert summary["total_reports_count"] == 2
        assert summary["cumulative_dlp_mgy_cm"] == 1000.0
        assert summary["cumulative_ctdivol_mgy"] == 25.0
        assert summary["total_irradiation_events"] == 5


def _get_auth_headers(client, username="admin_dose"):
    reg = client.post(
        "/api/v1/auth/register",
        json={"username": username, "password": "adminpassword", "full_name": "Admin Dose"},
    )
    if reg.status_code == 201:
        token = reg.json()["access_token"]
    else:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": username, "password": "adminpassword"},
        )
        token = login.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_dose_api(client):
    """Test Radiation Dose Monitoring REST API endpoints."""
    headers = _get_auth_headers(client, "admin_dose_api")

    pid = "DOSE_API_P1"
    study_uid = generate_uid()
    rdsr_ds = _make_rdsr(patient_id=pid, study_uid=study_uid, dlp=750.0, ctdivol=18.0)
    sop_uid = rdsr_ds.SOPInstanceUID

    from tests.helpers import build_stow_body, ds_bytes, stow_headers
    body = build_stow_body([ds_bytes(rdsr_ds)])
    client.post(f"/dicom-web/studies/{study_uid}", content=body, headers=stow_headers())

    # 1. GET /api/v1/dose/study/{study_uid}
    resp_st = client.get(f"/api/v1/dose/study/{study_uid}", headers=headers)
    assert resp_st.status_code == 200
    st_data = resp_st.json()
    assert len(st_data) == 1
    assert st_data[0]["total_dlp_mgy_cm"] == 750.0

    # 2. GET /api/v1/dose/patient/{patient_id}
    resp_pat = client.get(f"/api/v1/dose/patient/{pid}", headers=headers)
    assert resp_pat.status_code == 200
    pat_data = resp_pat.json()
    assert pat_data["cumulative_dlp_mgy_cm"] == 750.0

    # 3. GET /api/v1/dose/analytics
    resp_ana = client.get("/api/v1/dose/analytics", headers=headers)
    assert resp_ana.status_code == 200
    assert "total_rdsr_reports" in resp_ana.json()

    # 4. GET /api/v1/dose/report/{sop_uid}
    resp_rep = client.get(f"/api/v1/dose/report/{sop_uid}", headers=headers)
    assert resp_rep.status_code == 200
    assert resp_rep.json()["sop_instance_uid"] == sop_uid
