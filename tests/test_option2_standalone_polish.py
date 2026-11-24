"""Tests for Option 2: standalone .exe fast-path, pre-flight, and installer packaging."""

from __future__ import annotations

import io
from pathlib import Path

from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage, generate_uid

import paxs_desktop
from app.services import disc_burn_service, storage_service


def _sample_dcm(study_uid: str, series_uid: str, sop_uid: str) -> bytes:
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    meta.MediaStorageSOPInstanceUID = sop_uid
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset("img.dcm", {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID = "P-FAST"
    ds.PatientName = "FAST^PATH"
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.SOPInstanceUID = sop_uid
    ds.SOPClassUID = SecondaryCaptureImageStorage
    ds.Modality = "OT"
    buf = io.BytesIO()
    ds.save_as(buf, enforce_file_format=True)
    return buf.getvalue()


def test_export_study_to_folder_direct_fast_path(tmp_path: Path, monkeypatch) -> None:
    study_uid, series_uid, sop_uid = generate_uid(), generate_uid(), generate_uid()
    raw = _sample_dcm(study_uid, series_uid, sop_uid)
    src = tmp_path / "stored.dcm"
    src.write_bytes(raw)

    monkeypatch.setattr(storage_service, "read_stream", lambda key, backend_name=None: src.open("rb"))

    dest_parent = tmp_path / "usb_dest"
    dest_parent.mkdir()
    phases: list[str] = []
    out_path = disc_burn_service.export_study_to_folder(
        dest_parent,
        study_uid,
        [disc_burn_service.DiscInstance(sop_uid, series_uid, "k/1.dcm", None, len(raw))],
        phases.append,
        folder_name="FastStudy",
    )
    out = Path(out_path)
    assert out.is_dir()
    assert (out / "DICOMDIR").is_file()
    assert phases == ["preparing", "writing", "verifying"]
    assert not list(dest_parent.glob(".paxs-stage-*"))


def test_desktop_loopback_fast_path_detection() -> None:
    api = paxs_desktop.WindowApi(base_url="http://127.0.0.1:8000")
    assert api._is_local_loopback_origin("http://127.0.0.1:8000") is True
    assert api._is_local_loopback_origin("http://localhost:8000") is True
    assert api._is_local_loopback_origin("https://pacs.hospital.org") is False


def test_installer_and_preflight_scripts_exist_and_preserve_data() -> None:
    root = Path(__file__).resolve().parents[1]
    iss = (root / "installer" / "paxs_archive_setup.iss").read_text(encoding="utf-8")
    assert "PAXS-Archive.exe" in iss
    assert "PAXS-Server.exe" in iss
    assert "PAXS-Worker.exe" in iss
    assert "preflight_check.bat" in iss
    assert "storage" in iss

    preflight = (root / "scripts" / "preflight_check.bat").read_text(encoding="utf-8")
    assert "PAXS-Worker.exe --check-codecs" in preflight
    assert ":5432" in preflight


def test_launch_workstation_opens_in_same_page_and_desktop_keeps_internal_links() -> None:
    root = Path(__file__).resolve().parents[1]
    index_html = (root / "app" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'href="/studies-ui" target="_blank"' not in index_html
    assert 'href="/studies-ui"' in index_html
    assert "window.location.assign(targetUrl.href)" in paxs_desktop._KEYS_JS
