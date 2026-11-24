"""Tests verifying the real OHIF/PAXs Viewer (from Viewers-master) replaces the old integrated viewer."""

from __future__ import annotations

from pathlib import Path

from app.core.viewer_integrity import enforce_viewer_manifest
from app.main.app import _resolve_viewer_dir


def test_real_ohif_paxs_viewer_installed_and_legacy_removed() -> None:
    root = Path(__file__).resolve().parents[1]
    viewer_dir = root / "app" / "static" / "viewer"

    # Legacy lightweight fallback viewer.html must be removed.
    assert not (root / "app" / "static" / "viewer.html").exists()

    # Real OHIF / PAXs Viewer v3 files must be present.
    index_html = (viewer_dir / "index.html").read_text(encoding="utf-8")
    assert "window.PUBLIC_URL = '/viewer/';" in index_html
    assert "app-config.js" in index_html

    app_config = (viewer_dir / "app-config.js").read_text(encoding="utf-8")
    assert 'wadoRoot:"/dicom-web"' in app_config or "wadoRoot: '/dicom-web'" in app_config

    assert (viewer_dir / "viewer-version.txt").is_file()
    assert any(viewer_dir.rglob("*.wasm")), "WASM DICOM decoders must be present in OHIF/PAXs Viewer"

    # Study-list helper scripts required by /studies-ui must still exist.
    assert (viewer_dir / "paxs-study-list-enhance.js").is_file()
    assert (viewer_dir / "paxs-thumb-engine.js").is_file()

    # Production manifest integrity check must pass with zero errors.
    assert enforce_viewer_manifest(viewer_dir, production=True, strict_development=True) == []


def test_resolve_viewer_dir_picks_latest_viewers_master_release(tmp_path: Path) -> None:
    repo = tmp_path / "Viewers-master"
    r1 = repo / "packaging" / "release" / "paxs-viewer-2026-11-20T10-00-00"
    r2 = repo / "packaging" / "release" / "paxs-viewer-2026-11-23T20-45-08"
    for r in (r1, r2):
        r.mkdir(parents=True)
        (r / "index.html").write_text("<html></html>", encoding="utf-8")

    resolved = _resolve_viewer_dir(str(repo), tmp_path / "fallback")
    assert resolved == r2
