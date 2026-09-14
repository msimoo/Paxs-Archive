"""Anonymizer unit + API tests: PHI stripped, UIDs remapped consistently."""

import io
import zipfile

import pydicom

from app.services import anonymizer
from tests.helpers import make_instance
from tests.test_dicomweb import _upload_study


def _make_phi_dataset():
    ds = make_instance(patient_id="P-SECRET", patient_name="Confidential^Patient")
    ds.InstitutionName = "St Secret Hospital"
    ds.AccessionNumber = "ACC12345"
    ds.StudyDate = "20250115"
    ds.PatientAddress = "1 Hidden Lane"
    ds.OperatorsName = "Dr^X"
    return ds


def test_anonymize_dataset_strips_phi():
    ds = _make_phi_dataset()
    clean = anonymizer.anonymize_dataset(ds, {})

    for kw in ("PatientName", "PatientID", "PatientAddress", "InstitutionName",
               "AccessionNumber", "OperatorsName", "StudyDate"):
        assert kw not in clean, f"{kw} should be removed"
    assert clean.SOPInstanceUID != ds.SOPInstanceUID
    assert clean.StudyInstanceUID != ds.StudyInstanceUID
    # Pixel data survives (it's the point of an imaging export).
    assert clean.PixelData == ds.PixelData


def test_anonymize_remaps_uids_consistently():
    ds1 = make_instance(study_uid="1.2.3.11", series_uid="1.2.3.11.1", sop_uid="1.2.3.11.1.1")
    ds2 = make_instance(study_uid="1.2.3.11", series_uid="1.2.3.11.1", sop_uid="1.2.3.11.1.2")
    uid_map = {}
    c1 = anonymizer.anonymize_dataset(ds1, uid_map)
    c2 = anonymizer.anonymize_dataset(ds2, uid_map)

    # Same original study -> same new study UID across files.
    assert c1.StudyInstanceUID == c2.StudyInstanceUID
    assert c1.StudyInstanceUID != "1.2.3.11"
    # Same original series -> same new series UID.
    assert c1.SeriesInstanceUID == c2.SeriesInstanceUID
    # Different SOPs -> different new SOP UIDs.
    assert c1.SOPInstanceUID != c2.SOPInstanceUID


def test_export_requires_auth(client):
    study_uid, _ = _upload_study(client, n=1)
    res = client.get(f"/api/v1/export/{study_uid}/anonymized")
    assert res.status_code == 401


def test_export_returns_anonymized_zip(client):
    study_uid, _ = _upload_study(client, n=2)
    res = client.post("/api/v1/auth/register", json={
        "username": "researcher", "password": "secret1",
    })
    token = res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    res = client.get(f"/api/v1/export/{study_uid}/anonymized", headers=headers)
    assert res.status_code == 200, res.text
    assert res.headers["content-type"] == "application/zip"
    assert res.content[:2] == b"PK"  # zip magic

    zf = zipfile.ZipFile(io.BytesIO(res.content))
    names = zf.namelist()
    assert len(names) == 2
    assert all(n.endswith(".dcm") for n in names)

    first = pydicom.dcmread(io.BytesIO(zf.read(names[0])))
    assert "PatientName" not in first
    assert "PatientID" not in first
    assert "InstitutionName" not in first
    assert first.StudyInstanceUID != study_uid
    # Multi-file consistency inside the zip.
    second = pydicom.dcmread(io.BytesIO(zf.read(names[1])))
    assert first.StudyInstanceUID == second.StudyInstanceUID


def test_export_missing_study_404(client):
    res = client.post("/api/v1/auth/register", json={
        "username": "researcher2", "password": "secret1",
    })
    token = res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    res = client.get("/api/v1/export/1.2.3.999/anonymized", headers=headers)
    assert res.status_code == 404
