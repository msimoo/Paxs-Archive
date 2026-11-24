"""Sync the real OHIF / PAXs Viewer v3 release from Viewers-master into app/static/viewer.

Removes the legacy integrated HTML viewer (app/static/viewer.html and old
app/static/viewer/index.html), preserves PAXS study-list helper scripts/logos
needed by /studies-ui and /settings, writes viewer-version.txt, and regenerates
paxs-build-manifest.json with exact SHA-256 digests.

Usage::

    venv\\Scripts\\python.exe scripts/sync_paxs_viewer.py
    venv\\Scripts\\python.exe scripts/sync_paxs_viewer.py --source "D:\\Projects\\NEW_Projects\\Viewers-master"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_VIEWERS_REPO = Path(r"D:\Projects\NEW_Projects\Viewers-master")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
TARGET_DIR = PROJECT_ROOT / "app" / "static" / "viewer"
LEGACY_VIEWER_HTML = PROJECT_ROOT / "app" / "static" / "viewer.html"

# Study-list and settings assets stored inside app/static/viewer/ that must be preserved.
PRESERVE_FILES = (
    "paxs-study-list-enhance.js",
    "paxs-study-list-theme.css",
    "paxs-thumb-engine.js",
    "assets/logomain.png",
    "assets/logo.png",
    "assets/PaxsViewer-logo.png",
)


def resolve_release_dir(source: Path) -> tuple[Path, str]:
    """Resolve the newest staged release under ``packaging/release/paxs-viewer-*`` or ``platform/app/dist``."""
    src = source.resolve()
    release_root = src / "packaging" / "release"
    if release_root.is_dir():
        releases = sorted(
            (p for p in release_root.glob("paxs-viewer-*") if p.is_dir() and (p / "index.html").is_file()),
            key=lambda p: p.name,
            reverse=True,
        )
        if releases:
            return releases[0], releases[0].name
    dist_dir = src / "platform" / "app" / "dist"
    if dist_dir.is_dir() and (dist_dir / "index.html").is_file():
        return dist_dir, "dist"
    if (src / "index.html").is_file() and (src / "app-config.js").is_file():
        return src, src.name
    raise FileNotFoundError(f"No built OHIF/PAXs Viewer release found under {src}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_build_manifest(viewer_dir: Path, version_label: str) -> int:
    """Write ``paxs-build-manifest.json`` covering every file under ``viewer_dir``."""
    files: dict[str, str] = {}
    for asset in sorted(viewer_dir.rglob("*")):
        if not asset.is_file():
            continue
        rel = asset.relative_to(viewer_dir).as_posix()
        if rel == "paxs-build-manifest.json":
            continue
        files[rel] = _sha256(asset)
    manifest = {
        "product": "PAXs Viewer (OHIF v3)",
        "version": version_label,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }
    (viewer_dir / "paxs-build-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return len(files)


def sync_viewer(source: Path = DEFAULT_VIEWERS_REPO, target: Path = TARGET_DIR) -> dict[str, object]:
    """Replace ``target`` with the latest OHIF/PAXs Viewer release from ``source``."""
    release_dir, version_label = resolve_release_dir(source)

    preserved: dict[str, bytes] = {}
    for rel in PRESERVE_FILES:
        existing = target / Path(rel)
        if existing.is_file():
            preserved[rel] = existing.read_bytes()

    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)

    copied = 0
    for src_file in sorted(release_dir.rglob("*")):
        if not src_file.is_file():
            continue
        rel = src_file.relative_to(release_dir)
        if rel.suffix.lower() == ".map":
            continue
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dest)
        copied += 1

    for rel, raw in preserved.items():
        dest = target / Path(rel)
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(raw)

    (target / "viewer-version.txt").write_text(version_label + "\n", encoding="utf-8")
    if LEGACY_VIEWER_HTML.is_file():
        LEGACY_VIEWER_HTML.unlink()

    manifest_count = write_build_manifest(target, version_label)
    return {
        "source": str(release_dir),
        "version": version_label,
        "copied_files": copied,
        "manifest_files": manifest_count,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync OHIF/PAXs Viewer from Viewers-master.")
    parser.add_argument("--source", type=Path, default=DEFAULT_VIEWERS_REPO)
    parser.add_argument("--target", type=Path, default=TARGET_DIR)
    args = parser.parse_args(argv)
    info = sync_viewer(args.source, args.target)
    print(json.dumps(info, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
