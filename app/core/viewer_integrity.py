"""Verify the exact set and SHA-256 digests of shipped viewer assets."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re

MANIFEST_NAME = "paxs-build-manifest.json"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def viewer_manifest_errors(directory: Path) -> list[str]:
    """Return safe, deterministic inventory errors without reading file content into logs."""
    root = directory.resolve()
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        return ["manifest missing"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ["manifest unreadable"]
    entries = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(entries, dict):
        return ["manifest files map invalid"]

    errors: list[str] = []
    listed: set[str] = set()
    for name, expected in entries.items():
        if (
            not isinstance(name, str)
            or "\\" in name
            or PurePosixPath(name).is_absolute()
            or ".." in PurePosixPath(name).parts
            or PurePosixPath(name).as_posix() != name
            or not isinstance(expected, str)
            or not _SHA256.fullmatch(expected)
        ):
            errors.append("manifest entry invalid")
            continue
        listed.add(name)
        asset = root.joinpath(*PurePosixPath(name).parts)
        try:
            resolved = asset.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError):
            errors.append(f"missing or unsafe asset: {name}")
            continue
        if not resolved.is_file():
            errors.append(f"missing asset: {name}")
            continue
        if _sha256_file(resolved) != expected:
            errors.append(f"hash mismatch: {name}")

    actual: set[str] = set()
    for asset in root.rglob("*"):
        if asset.name == MANIFEST_NAME and asset.parent == root:
            continue
        if asset.is_file():
            actual.add(asset.relative_to(root).as_posix())
    errors.extend(f"unlisted asset: {name}" for name in sorted(actual - listed))
    return sorted(set(errors))


def enforce_viewer_manifest(
    directory: Path, *, production: bool, strict_development: bool
) -> list[str]:
    """Raise on integrity drift in production or strict development mode."""
    errors = viewer_manifest_errors(directory)
    if errors and (production or strict_development):
        raise RuntimeError(f"viewer asset integrity check failed ({len(errors)} issue(s))")
    return errors


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
