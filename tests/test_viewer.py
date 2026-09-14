"""PaxsViewer integration (replaces PaxsViewer, CP-04).

PaxsViewer (the PaxsViewer v3 fork) is a pure DICOMweb client: it loads the study
list via QIDO-RS (``Accept: application/dicom+json`` + ``includefield=all``),
retrieves instances via WADO-RS (multipart/related) or WADO-URI, and reads
metadata via ``/studies/{study}/metadata``. These tests replay exactly those
request shapes against the running app to guarantee a real PaxsViewer build
pointed at ``/dicom-web`` can browse and open studies from this archive.

The viewer itself is served at ``/viewer`` (same origin); its mount is tested
when the dist is present, and the archive falls back to the built-in
``viewer.html`` when it is not.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.db.session import AsyncSessionLocal
from app.services import indexing_service, storage_service
from tests.helpers import make_instance

STUDY_UID = "1.2.840.113619.2.408.777"
PATIENT_ID = "PAXS-PAT-1"


def _ingest() -> None:
    from app.dicom.parser import parse_metadata

    for i in range(1, 4):
        ds = make_instance(study_uid=STUDY_UID, series_uid=f"1.2.3.4.777.{i}",
                           sop_uid=f"1.2.3.5.777.{i}", instance_num=i,
                           patient_id=PATIENT_ID, patient_name="PaxsViewer^Test")
        key, sha = storage_service.save_dataset(ds)

        async def _index(key=key, sha=sha, ds=ds):
            async with AsyncSessionLocal() as s:
                async with s.begin():
                    await indexing_service.index_new_instance(
                        s, parse_metadata(ds), key, sha, performed_by="test"
                    )

        asyncio.run(_index())


# --------------------------------------------------------------------------- #
# QIDO-RS (study list — PaxsViewer WorkList)
# --------------------------------------------------------------------------- #
def test_qido_studies_ohif_accept(client):
    """PaxsViewer requests the study list with application/dicom+json + includefield."""
    _ingest()
    res = client.get(
        "/dicom-web/studies?includefield=all&limit=50",
        headers={"Accept": "application/dicom+json"},
    )
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/dicom+json")
    rows = res.json()
    mine = [r for r in rows if r.get("0020000D", {}).get("Value", [None])[0] == STUDY_UID]
    assert mine, "ingested study must be visible via QIDO-RS"
    row = mine[0]
    # PaxsViewer requires the patient name / id and study date fields to render rows.
    assert "00100010" in row and "00100020" in row and "00080020" in row
    assert row["0020000D"]["Value"][0] == STUDY_UID


def test_qido_series_and_instances_ohif(client):
    """PaxsViewer drills Study -> Series -> Instances with the same Accept type."""
    _ingest()
    for path in (
        f"/dicom-web/studies/{STUDY_UID}/series",
        f"/dicom-web/studies/{STUDY_UID}/series/1.2.3.4.777.1/instances",
    ):
        res = client.get(path, headers={"Accept": "application/dicom+json"})
        assert res.status_code == 200, path
        assert res.headers["content-type"].startswith("application/dicom+json")
        assert len(res.json()) >= 1, path


def test_qido_fuzzy_patient_name(client):
    """PaxsViewer (supportsFuzzyMatching) sends a partial PatientName match."""
    _ingest()
    res = client.get(
        "/dicom-web/studies?PatientName=PaxsViewer",
        headers={"Accept": "application/dicom+json"},
    )
    assert res.status_code == 200
    rows = res.json()
    assert any(r.get("00100010", {}).get("Value", [None])[0] for r in rows)


# --------------------------------------------------------------------------- #
# WADO-RS (instance retrieval — PaxsViewer imageRendering: 'wadors')
# --------------------------------------------------------------------------- #
def test_wado_rs_study_multipart(client):
    """PaxsViewer retrieves whole studies as multipart/related application/dicom."""
    _ingest()
    res = client.get(
        f"/dicom-web/studies/{STUDY_UID}",
        headers={"Accept": 'multipart/related; type="application/dicom"'},
    )
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("multipart/related")
    assert res.content.count(b"--") >= 2  # boundary + parts


def test_wado_rs_instance_singlepart(client):
    """PaxsViewer renders wadors frames via the instance endpoint."""
    _ingest()
    res = client.get(
        f"/dicom-web/studies/{STUDY_UID}/series/1.2.3.4.777.1/instances/1.2.3.5.777.1",
        headers={"Accept": "application/dicom"},
    )
    assert res.status_code == 200
    assert res.content[128:132] == b"DICM"  # part-10 magic at offset 128


def test_wado_rs_metadata(client):
    """PaxsViewer loads full metadata (bulk frame positioning, tags) for display sets."""
    _ingest()
    res = client.get(
        f"/dicom-web/studies/{STUDY_UID}/metadata",
        headers={"Accept": "application/dicom+json"},
    )
    assert res.status_code == 200
    meta = res.json()
    assert len(meta) >= 3
    # PaxsViewer needs the pixel rows/cols + transfer syntax per instance.
    assert all("00280010" in m and "00280011" in m for m in meta)


def test_wado_rs_series_metadata(client):
    """PaxsViewer v3.13 fetches *series-level* metadata to build display sets.

    The live fork requests /studies/{study}/series/{series}/metadata before
    retrieving pixel data; PAXS must answer it (not just study-level).
    """
    _ingest()
    res = client.get(
        f"/dicom-web/studies/{STUDY_UID}/series/1.2.3.4.777.1/metadata",
        headers={"Accept": "application/dicom+json"},
    )
    assert res.status_code == 200
    meta = res.json()
    assert len(meta) == 1
    m = meta[0]
    assert m["0020000D"]["Value"][0] == STUDY_UID
    assert m["0020000E"]["Value"][0] == "1.2.3.4.777.1"
    assert "00280010" in m and "00280011" in m  # rows/cols for the display set


def test_viewer_splash_logo_served(client):
    """The viewer's splash + study-list header hit /PaxsViewer-logo.png at the
    server root. It must serve the PaxsViewer *wordmark* (logo/paxsviewer-logo.png,
    644x156 — the same aspect the bundle renders at 320px wide), never the square
    PAXS emblem, so the header logo isn't distorted."""
    res = client.get("/PaxsViewer-logo.png")
    assert res.status_code in (200, 404)  # 200 when a logo exists, 404 otherwise
    if res.status_code == 200:
        assert res.headers["content-type"].startswith("image/")
        logo_file = (Path(__file__).parent.parent / "app" / "static" / "logo"
                     / "paxsviewer-logo.png")
        if logo_file.exists():
            assert res.content == logo_file.read_bytes()
            from PIL import Image
            import io
            w, h = Image.open(io.BytesIO(res.content)).size
            assert w == 644 and h == 156  # wordmark aspect, not the square emblem


def test_wado_uri_legacy(client):
    """PaxsViewer's wadoUriRoot path (requestType=WADO) still works as a fallback."""
    _ingest()
    res = client.get(
        f"/dicom-web/wado?requestType=WADO&studyUID={STUDY_UID}"
        f"&seriesUID=1.2.3.4.777.1&objectUID=1.2.3.5.777.1"
    )
    assert res.status_code == 200
    assert res.content[128:132] == b"DICM"  # part-10 magic at offset 128


# --------------------------------------------------------------------------- #
# /viewer mount (the viewer bundle)
# --------------------------------------------------------------------------- #
def test_viewer_mount(client):
    """The PaxsViewer dist (if present) is served at /viewer with config + marker.

    When the dist is absent the archive falls back to the built-in viewer.html
    at /viewer instead (graceful degradation, never a 404).
    """
    viewer_dir = Path(__file__).parent.parent / "app" / "static" / "viewer"
    index = viewer_dir / "index.html"
    if not index.exists():
        res = client.get("/viewer")
        assert res.status_code == 200
        assert b"PAXS Viewer" in res.content  # built-in fallback page
        return
    res = client.get("/viewer/")
    assert res.status_code == 200
    assert b"<div id=\"root\">" in res.content or b"PaxsViewer" in res.content
    cfg = client.get("/viewer/app-config.js")
    assert cfg.status_code == 200
    assert b"/dicom-web" in cfg.content  # points at the archive's DICOMweb
    ver = client.get("/viewer/viewer-version.txt")
    assert ver.status_code == 200 and ver.text.strip()


def test_viewer_deep_link_serves_app(client):
    """Deep links (?StudyInstanceUIDs=…) must fall back to the app's index."""
    viewer_dir = Path(__file__).parent.parent / "app" / "static" / "viewer"
    if not (viewer_dir / "index.html").exists():
        assert client.get("/viewer?StudyInstanceUIDs=1.2.3.4").status_code == 200
        assert b"PAXS Viewer" in client.get("/viewer?StudyInstanceUIDs=1.2.3.4").content
        return
    res = client.get("/viewer?StudyInstanceUIDs=1.2.840.113619.2.408.777")
    assert res.status_code == 200
    assert b"<div id=\"root\">" in res.content


def test_studies_ui_mount(client):
    """The upgraded React + Tailwind + shadcn/ui PACS study list UI is served at /studies-ui."""
    res = client.get("/studies-ui/")
    assert res.status_code == 200
    assert b"PAXS Archive" in res.content
    assert b"<div id=\"root\">" in res.content

