"""Tests for PAXS-MediaExporter Studio, MicroDicom 2026.5 Autorun, and Launcher API."""

from __future__ import annotations

import http.server
import json
import socketserver
import threading
import time
import zipfile
from pathlib import Path

import pycdlib
import pytest
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.fileset import FileSet
from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage, generate_uid

import paxs_media_exporter as exporter
from app.services import microdicom_autorun


def _make_dicom(study_uid: str, series_uid: str, idx: int) -> FileDataset:
    sop_uid = generate_uid()
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    meta.MediaStorageSOPInstanceUID = sop_uid
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset("img.dcm", {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID = "PAT-EXPORTER-1"
    ds.PatientName = "DOE^JANE"
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.SOPInstanceUID = sop_uid
    ds.SOPClassUID = SecondaryCaptureImageStorage
    ds.StudyDate = "20261124"
    ds.StudyTime = "103000"
    ds.StudyID = "ST1"
    ds.SeriesNumber = 1
    ds.InstanceNumber = idx
    ds.Modality = "CT"
    ds.Rows = 32
    ds.Columns = 32
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.PixelData = bytes([idx % 256]) * (32 * 32)
    ds.set_original_encoding(False, True)
    return ds


def _build_sample_dicomdir_zip(tmp_path: Path) -> Path:
    media = tmp_path / "src_media"
    media.mkdir()
    study_uid, series_uid = generate_uid(), generate_uid()
    fs = FileSet()
    for idx in (1, 2):
        fs.add(_make_dicom(study_uid, series_uid, idx))
    fs.write(media)
    zip_path = tmp_path / "sample_dicomdir.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
        for p in sorted(media.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(media).as_posix())
    return zip_path


def test_microdicom_2026_5_autorun_detected_and_configures_specific_dicom_dir(tmp_path: Path):
    status = microdicom_autorun.microdicom_status()
    assert status["available"] is True
    assert status["version"] == "2026.5"
    assert status["has_x64"] is True
    assert status["has_win32"] is True

    media = tmp_path / "disc_root"
    media.mkdir()
    study_uid, series_uid = generate_uid(), generate_uid()
    fs = FileSet()
    fs.add(_make_dicom(study_uid, series_uid, 1))
    fs.add(_make_dicom(study_uid, series_uid, 2))
    fs.write(media)

    folder = microdicom_autorun.relocate_dicomdir_into_subdir(media, "STUDIES")
    assert folder == "STUDIES"
    assert (media / "STUDIES").is_dir()

    report = microdicom_autorun.install_microdicom_autorun(
        media,
        dicom_dir_name="STUDIES",
        arch="x64",
        open_mode="dicomdir",
        volume_label="PAXS_PATIENT_CD",
        study_info={"study_date": "20261124", "modality": "CT", "n_instances": 2},
    )
    assert report["ok"] is True
    assert report["dicom_dir"] == "STUDIES"
    assert report["dicom_instances"] == 2
    assert (media / "autorun.inf").is_file()
    assert "OPEN=x64\\mDicom.exe DICOMDIR" in (media / "autorun.inf").read_text(encoding="utf-8")
    assert (media / "START_VIEWER.bat").is_file()
    assert (media / "run.bat").is_file()
    assert (media / "README_PATIENT_DOCTOR.txt").is_file()
    assert (media / "x64" / "mDicom.exe").is_file()
    assert not (media / "Win32" / "mDicom.exe").exists()  # compact x64 mode


def test_parse_protocol_uri():
    parsed = exporter.parse_protocol_uri(
        "paxs-exporter://open?server=http%3A%2F%2F127.0.0.1%3A8000&session=abc123&token=tok9&mode=dicom-zip"
    )
    assert parsed == {
        "server": "http://127.0.0.1:8000",
        "session": "abc123",
        "token": "tok9",
        "mode": "dicom-zip",
    }


def test_exporter_api_end_to_end_portable_microdicom_iso_and_zip_to_chosen_dir(tmp_path: Path):
    sample_zip = _build_sample_dicomdir_zip(tmp_path)
    zip_bytes = sample_zip.read_bytes()
    heartbeats: list[dict] = []
    expected_token = "secret-session-token"

    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002
            return None

        def do_GET(self):
            if self.headers.get("X-Exporter-Token") != expected_token:
                self.send_response(401)
                self.end_headers()
                return
            if self.path.endswith("/study-info"):
                body = json.dumps({
                    "session_id": "sess1",
                    "mode": "media",
                    "study": {
                        "patient_name": "DOE^JANE",
                        "patient_id": "PAT-EXPORTER-1",
                        "study_date": "20261124",
                        "modality": "CT",
                        "modalities": ["CT"],
                        "n_series": 1,
                        "n_instances": 2,
                        "total_bytes": len(zip_bytes),
                    },
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if "/download/" in self.path:
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(len(zip_bytes)))
                self.end_headers()
                self.wfile.write(zip_bytes)
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            if self.headers.get("X-Exporter-Token") != expected_token:
                self.send_response(401)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            heartbeats.append(payload)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

    httpd = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
    port = httpd.server_address[1]
    srv_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    srv_thread.start()
    try:
        api = exporter.ExporterApi(
            server_url=f"http://127.0.0.1:{port}",
            session_id="sess1",
            token=expected_token,
            mode="media",
        )
        boot = api.get_bootstrap()
        assert boot["study"]["patient_id"] == "PAT-EXPORTER-1"
        assert boot["microdicom"]["available"] is True

        # 1. Portable folder with MicroDicom 2026.5 + DICOM\ directory
        out_folder_parent = tmp_path / "patient_usb_sim"
        out_folder_parent.mkdir()
        res = api.start_export({
            "workflow": "media",
            "kind": "folder",
            "target": str(out_folder_parent),
            "folder_name": "JANE_DOE_CT",
            "volume_label": "PAXS_CT",
            "include_microdicom": True,
            "dicom_dir": "DICOM",
            "microdicom_arch": "x64",
            "microdicom_open": "dicomdir",
        })
        assert res["ok"] is True
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            prog = api.get_progress()
            if prog["phase"] in ("verified", "failed"):
                break
            time.sleep(0.1)
        assert prog["phase"] == "verified", prog
        assert prog["microdicom_verified"] is True
        exported = Path(prog["output_path"])
        assert (exported / "DICOMDIR").is_file()
        assert (exported / "DICOM").is_dir()
        assert (exported / "x64" / "mDicom.exe").is_file()
        assert (exported / "START_VIEWER.bat").is_file()
        assert any(h["phase"] == "verified" for h in heartbeats)

        # 2. Export DICOM ZIP to specific user-chosen directory
        chosen_dir = tmp_path / "doctor_exports"
        res2 = api.start_export({
            "workflow": "dicom-zip",
            "format": "dicom-zip",
            "dest_dir": str(chosen_dir),
            "output_name": "JANE_DOE_STUDY",
        })
        assert res2["ok"] is True
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            prog2 = api.get_progress()
            if prog2["phase"] in ("verified", "failed"):
                break
            time.sleep(0.1)
        assert prog2["phase"] == "verified", prog2
        saved_zip = Path(prog2["output_path"])
        assert saved_zip == chosen_dir / "JANE_DOE_STUDY.zip"
        assert saved_zip.is_file()

        # 3. Build Autorun ISO image with MicroDicom + DICOMDIR + DICOM\
        iso_dir = tmp_path / "iso_out"
        res3 = api.start_export({
            "workflow": "media",
            "kind": "iso",
            "target": str(iso_dir),
            "folder_name": "JANE_AUTORUN",
            "volume_label": "PAXS_ISO",
            "include_microdicom": True,
            "dicom_dir": "DICOM",
            "microdicom_arch": "x64",
            "microdicom_open": "dicomdir",
        })
        assert res3["ok"] is True
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            prog3 = api.get_progress()
            if prog3["phase"] in ("verified", "failed"):
                break
            time.sleep(0.1)
        assert prog3["phase"] == "verified", prog3
        iso_file = Path(prog3["output_path"])
        assert iso_file.is_file()
        iso = pycdlib.PyCdlib()
        iso.open(str(iso_file))
        try:
            root_entries = [c.file_identifier().decode("utf-16-be", errors="ignore") for c in iso.list_children(joliet_path="/")]
            assert "DICOMDIR" in root_entries
            assert "autorun.inf" in root_entries
            assert "DICOM" in root_entries
        finally:
            iso.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
