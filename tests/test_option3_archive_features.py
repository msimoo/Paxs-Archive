"""Tests for Option 3: DICOM Tag Editor, Scheduled Backups/Verify, and HL7/MWL UI."""

from __future__ import annotations

import io
import time
from pathlib import Path

import pytest
from pydicom import dcmread
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage, generate_uid

from app.core.config import settings
from app.services import db_admin, storage_service, tag_editor_service


def _make_dcm_bytes() -> bytes:
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset("edit.dcm", {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID = "OLD-ID"
    ds.PatientName = "OLD^NAME"
    ds.StudyInstanceUID = generate_uid()
    ds.SeriesInstanceUID = generate_uid()
    ds.SOPInstanceUID = str(meta.MediaStorageSOPInstanceUID)
    ds.SOPClassUID = SecondaryCaptureImageStorage
    ds.Modality = "CT"
    buf = io.BytesIO()
    ds.save_as(buf, enforce_file_format=True)
    return buf.getvalue()


def test_validate_tag_patch_rejects_bad_dates_and_sex() -> None:
    with pytest.raises(ValueError, match="YYYYMMDD"):
        tag_editor_service.validate_tag_patch(tag_editor_service.TagPatch(study_date="2026-11-23"))
    with pytest.raises(ValueError, match="patient_sex"):
        tag_editor_service.validate_tag_patch(tag_editor_service.TagPatch(patient_sex="X"))
    with pytest.raises(ValueError, match="patient_id"):
        tag_editor_service.validate_tag_patch(tag_editor_service.TagPatch(patient_id="   "))

    clean = tag_editor_service.validate_tag_patch(
        tag_editor_service.TagPatch(
            patient_id="MRN-123",
            patient_name="DOE^JANE",
            patient_birth_date="19900115",
            patient_sex="f",
            accession_number="ACC-9",
            study_description="CT CHEST",
        )
    )
    assert clean["patient_sex"] == "F"
    assert clean["patient_id"] == "MRN-123"


def test_rewrite_dicom_object_tags_updates_file_and_returns_json(tmp_path: Path, monkeypatch) -> None:
    stored = tmp_path / "obj.dcm"
    stored.write_bytes(_make_dcm_bytes())

    monkeypatch.setattr(storage_service, "read_dataset", lambda key, backend_name=None: dcmread(stored))
    monkeypatch.setattr(storage_service, "save_bytes", lambda key, data, backend_name=None: stored.write_bytes(data))

    meta_json = tag_editor_service.rewrite_dicom_object_tags(
        "s/ser/sop.dcm",
        None,
        {"patient_name": "CORRECTED^PATIENT", "patient_id": "NEW-777", "accession_number": "ACC-55"},
    )
    reloaded = dcmread(stored)
    assert str(reloaded.PatientName) == "CORRECTED^PATIENT"
    assert str(reloaded.PatientID) == "NEW-777"
    assert str(reloaded.AccessionNumber) == "ACC-55"
    assert "NEW-777" in meta_json


def test_backup_verification_and_retention_pruning(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "BACKUP_DIR", str(tmp_path))

    good_sql = tmp_path / "paxs-postgresql-20261123-010000.sql"
    good_sql.write_text("-- PAXS dump\nBEGIN;\nSELECT 1;\nCOMMIT;\n", encoding="utf-8")
    res_sql = db_admin.verify_backup(good_sql.name)
    assert res_sql["ok"] is True
    assert res_sql["format"] == "sql"
    assert len(res_sql["sha256"]) == 64

    bad_sql = tmp_path / "paxs-postgresql-20261123-020000.sql"
    bad_sql.write_text("truncated file", encoding="utf-8")
    assert db_admin.verify_backup(bad_sql.name)["ok"] is False

    bad_dump = tmp_path / "paxs-postgresql-20261123-030000.dump"
    bad_dump.write_bytes(b"NOTPGDMP")
    assert db_admin.verify_backup(bad_dump.name)["ok"] is False

    # Touch timestamps and verify retention pruning keeps newest 2
    for idx, f in enumerate((good_sql, bad_sql, bad_dump)):
        t = time.time() - (3 - idx) * 60
        f.touch()
        import os
        os.utime(f, (t, t))
    removed = db_admin.prune_old_backups(2)
    assert removed == 1
    assert len(list(tmp_path.glob("paxs-*"))) == 2


def test_settings_ui_wires_archive_tools_tab() -> None:
    root = Path(__file__).resolve().parents[1]
    html = (root / "app" / "static" / "settings" / "index.html").read_text(encoding="utf-8")
    js = (root / "app" / "static" / "settings" / "archive-tools.js").read_text(encoding="utf-8")
    assert 'data-tab="archive-tools-tab"' in html
    assert 'id="archive-tools-tab"' in html
    assert "/static/settings/archive-tools.js" in html
    assert "/api/v1/studies/" in js
    assert "/api/v1/db/backup-schedule" in js
    assert "/api/v1/worklist/hl7-status" in js
