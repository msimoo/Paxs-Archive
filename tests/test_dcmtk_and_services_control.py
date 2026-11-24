"""Tests for DCMTK diagnostics, editable settings, live Services Control API, and Services tab UI."""

from __future__ import annotations

from pathlib import Path

from app.api.v1 import dashboard, services_control
from app.core.config import settings
from app.dicom import dcmtk
from app.services import settings_service


def test_dcmtk_and_web_settings_are_registered_in_editable_keys() -> None:
    for key in (
        "DCMTK_BIN_DIR",
        "DCMTK_MOVE_PORT",
        "DCMTK_MOVE_AE_TITLE",
        "DCMTK_MOVE_PROFILE",
        "DICOM_ALLOWED_CALLING_AES",
        "HL7_TLS_ENABLED",
        "HL7_ALLOWED_SOURCE_IPS",
        "HL7_ALLOWED_SENDING_FACILITIES",
        "DICOMWEB_CLIENT_ALLOW_PRIVATE",
        "WEBHOOK_URLS",
    ):
        assert key in settings_service.EDITABLE_KEYS, f"Missing editable key: {key}"


def test_dcmtk_detailed_status_reports_all_six_tools(monkeypatch) -> None:
    monkeypatch.setattr(settings, "DCMTK_BIN_DIR", "")
    info = dcmtk.detailed_status()
    assert info["available"] is False
    assert info["configured"] is False
    names = [t["name"] for t in info["tools"]]
    assert names == ["echoscu", "findscu", "dcmsend", "movescu", "dcmrecv", "dcmdump"]


def test_dcmtk_find_default_profile_supports_windows_share_layout(tmp_path: Path) -> None:
    bin_dir = tmp_path / "dcmtk-3.6.9-win64" / "bin"
    share_dir = tmp_path / "dcmtk-3.6.9-win64" / "share" / "dcmtk-3.6.9"
    bin_dir.mkdir(parents=True)
    share_dir.mkdir(parents=True)
    cfg = share_dir / "storescp.cfg"
    cfg.write_text("[TransferSyntaxes]\n", encoding="utf-8")

    found = dcmtk.find_default_profile(bin_dir)
    assert found == cfg


def test_live_web_services_reflect_runtime_setting_changes(monkeypatch) -> None:
    monkeypatch.setattr(settings, "FHIR_ENABLED", False)
    monkeypatch.setattr(settings, "WEBHOOK_URLS", "https://ris.example/hook")
    rows = {row["name"]: row for row in dashboard._live_web_services()}
    assert rows["FHIR R4"]["enabled"] is False
    assert rows["Webhooks"]["enabled"] is True
    assert "1 endpoint(s)" in rows["Webhooks"]["detail"]

    monkeypatch.setattr(settings, "FHIR_ENABLED", True)
    rows2 = {row["name"]: row for row in dashboard._live_web_services()}
    assert rows2["FHIR R4"]["enabled"] is True


def test_services_control_tcp_probe_and_ui_wiring() -> None:
    bad = services_control._probe_tcp("127.0.0.1", 1, timeout=0.2)
    assert bad["ok"] is False

    root = Path(__file__).resolve().parents[1]
    html = (root / "app" / "static" / "index.html").read_text(encoding="utf-8")
    js = (root / "app" / "static" / "services-console.js").read_text(encoding="utf-8")
    assert 'id="svc-dcmtk-tools"' in html
    assert 'id="svc-dicom-restart"' in html
    assert 'id="svc-hl7-restart"' in html
    assert 'id="svc-web-save"' in html
    assert "/static/services-console.js" in html
    assert "/api/v1/services/overview" in js
    assert "/api/v1/services/dcmtk/echo" in js
    assert "/api/v1/services/dcmtk/find" in js


def test_dcmtk_env_injects_portable_dicom_dictionary(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("DCMDICTPATH", raising=False)
    bin_dir = tmp_path / "dcmtk" / "bin"
    share_dir = tmp_path / "dcmtk" / "share" / "dcmtk-3.7.1"
    bin_dir.mkdir(parents=True)
    share_dir.mkdir(parents=True)
    dic = share_dir / "dicom.dic"
    dic.write_text("# dic\n", encoding="utf-8")
    env = dcmtk.dcmtk_env(bin_dir / "movescu.exe")
    assert env["DCMDICTPATH"] == str(dic)


def test_live_offis_dcmtk_echo_find_and_store_when_installed(tmp_path: Path, monkeypatch) -> None:
    import pytest
    from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage, generate_uid
    from pynetdicom import AE, evt
    from pynetdicom.sop_class import StudyRootQueryRetrieveInformationModelFind, Verification

    bin_dir = (Path(__file__).resolve().parents[1] / ".tools" / "dcmtk" / "bin").resolve()
    if not (bin_dir / "echoscu.exe").is_file():
        pytest.skip("Portable OFFIS DCMTK not installed under .tools/dcmtk/bin")

    monkeypatch.setattr(settings, "DCMTK_BIN_DIR", str(bin_dir))
    monkeypatch.setattr(settings, "DCMTK_MOVE_PORT", 10401)
    monkeypatch.setattr(settings, "DICOM_TLS_ENABLED", False)

    stored: list[str] = []
    ae = AE(ae_title="TEST_SCP")
    ae.add_supported_context(Verification)
    ae.add_supported_context(StudyRootQueryRetrieveInformationModelFind)
    ae.add_supported_context(SecondaryCaptureImageStorage, ExplicitVRLittleEndian)

    def on_store(event) -> int:
        stored.append(str(event.dataset.SOPInstanceUID))
        return 0x0000

    def on_find(_event):
        row = Dataset()
        row.QueryRetrieveLevel = "STUDY"
        row.PatientID = "P1"
        row.StudyInstanceUID = "1.2.840.1"
        yield (0xFF00, row)

    srv = ae.start_server(
        ("127.0.0.1", 0),
        block=False,
        evt_handlers=[(evt.EVT_C_STORE, on_store), (evt.EVT_C_FIND, on_find)],
    )
    port = srv.server_address[1]
    try:
        echo_res = dcmtk.send_echo("127.0.0.1", port, "TEST_SCP")
        assert echo_res["ok"] is True and echo_res["status"] == 0

        q = Dataset()
        q.QueryRetrieveLevel = "STUDY"
        q.PatientID = ""
        q.StudyInstanceUID = ""
        find_res = dcmtk.send_find("127.0.0.1", port, "TEST_SCP", q)
        assert find_res["ok"] is True and len(find_res["matches"]) == 1

        sop = generate_uid()
        meta = FileMetaDataset()
        meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
        meta.MediaStorageSOPInstanceUID = sop
        meta.TransferSyntaxUID = ExplicitVRLittleEndian
        ds = FileDataset("a.dcm", {}, file_meta=meta, preamble=b"\0" * 128)
        ds.PatientID = "P1"
        ds.StudyInstanceUID = generate_uid()
        ds.SeriesInstanceUID = generate_uid()
        ds.SOPInstanceUID = sop
        ds.SOPClassUID = SecondaryCaptureImageStorage
        dcm_file = tmp_path / "a.dcm"
        ds.save_as(dcm_file, enforce_file_format=True)

        store_res = dcmtk.send_store("127.0.0.1", port, "TEST_SCP", [(dcm_file, sop)])
        assert store_res["results"][0]["ok"] is True
        assert sop in stored
    finally:
        srv.shutdown()
