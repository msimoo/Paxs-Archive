"""Tests for portable CD/DVD & USB media export (stage_study, ISO, USB/folder verification)."""

from pathlib import Path
from zipfile import ZIP_STORED, ZipFile

import pytest
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage, generate_uid

from app.services import client_media_service, disc_burn_service, storage_service


def _write_test_dicom(
    path: Path, *, study_uid: str, series_uid: str, sop_uid: str, omit_date_time: bool = False,
) -> int:
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    meta.MediaStorageSOPInstanceUID = sop_uid
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(str(path), {}, file_meta=meta, preamble=b"\0" * 128)
    ds.is_little_endian = True
    ds.is_implicit_VR = False
    ds.PatientID = "PAT-USB-1"
    ds.PatientName = "Doe^Media"
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.SOPInstanceUID = sop_uid
    ds.SOPClassUID = SecondaryCaptureImageStorage
    ds.Modality = "OT"
    if not omit_date_time:
        ds.StudyDate = "20261123"
        ds.StudyTime = "120000"
    ds.save_as(str(path), enforce_file_format=True)
    return path.stat().st_size


def test_stage_study_and_iso_succeed_when_dicom_lacks_study_date_time(tmp_path: Path, monkeypatch):
    study_uid = generate_uid()
    series_uid = generate_uid()
    sop_uid = generate_uid()
    src = tmp_path / "source.dcm"
    size = _write_test_dicom(src, study_uid=study_uid, series_uid=series_uid, sop_uid=sop_uid, omit_date_time=True)

    monkeypatch.setattr(storage_service, "read_stream", lambda key, backend_name=None: Path(key).open("rb"))
    inst = disc_burn_service.DiscInstance(sop_uid, series_uid, str(src), None, size + 999)

    iso_path = client_media_service.build_client_media(tmp_path / "iso_ws", study_uid, [inst], "iso")
    assert iso_path.is_file()
    assert iso_path.stat().st_size > 2048

    zip_path = client_media_service.build_client_media(tmp_path / "zip_ws", study_uid, [inst], "zip")
    assert zip_path.is_file()
    with ZipFile(zip_path, "r") as zf:
        names = set(zf.namelist())
        assert "DICOMDIR" in names
        assert len(names) >= 2


def test_write_zip_to_target_extracts_and_verifies_dicomdir(tmp_path: Path, monkeypatch):
    study_uid = generate_uid()
    series_uid = generate_uid()
    sop_uid = generate_uid()
    src = tmp_path / "source.dcm"
    size = _write_test_dicom(src, study_uid=study_uid, series_uid=series_uid, sop_uid=sop_uid)

    monkeypatch.setattr(storage_service, "read_stream", lambda key, backend_name=None: Path(key).open("rb"))
    inst = disc_burn_service.DiscInstance(sop_uid, series_uid, str(src), None, size)
    zip_path = client_media_service.build_client_media(tmp_path / "pkg", study_uid, [inst], "zip")

    usb_root = tmp_path / "mock_usb"
    usb_root.mkdir()
    phases: list[str] = []
    out_dir = disc_burn_service.write_zip_to_target(
        zip_path, usb_root, phases.append, folder_name="PAXS_TEST_STUDY",
    )

    assert phases == ["preparing", "writing", "verifying"]
    assert (Path(out_dir) / "DICOMDIR").is_file()


def test_extract_zip_safely_rejects_path_traversal(tmp_path: Path):
    bad_zip = tmp_path / "evil.zip"
    with ZipFile(bad_zip, "w", compression=ZIP_STORED) as zf:
        zf.writestr("../escape.txt", b"bad")

    with pytest.raises(disc_burn_service.DiscBurnError, match="unsafe file path"):
        disc_burn_service.extract_zip_safely(bad_zip, tmp_path / "out")


def test_list_removable_devices_returns_structured_inventory():
    inv = disc_burn_service.list_removable_devices()
    assert "recorders" in inv and isinstance(inv["recorders"], list)
    assert "usb_drives" in inv and isinstance(inv["usb_drives"], list)
