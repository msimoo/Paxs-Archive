"""PHI-free support status and local viewer integrity checks."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from app.core.viewer_integrity import viewer_manifest_errors

CHECK_NAMES = ("server", "database", "viewer_http", "dicom_listener", "redis", "viewer_integrity")
SAFE_STATES = frozenset({"ONLINE", "OFFLINE", "UNAVAILABLE", "AUTH REQUIRED", "LISTENING",
                         "NOT LISTENING", "INTEGRITY OK", "INTEGRITY FAILED", "UNKNOWN"})


def create_support_report(statuses: dict[str, str]) -> dict:
    """Copy only known status codes; reject arbitrary text and all other fields."""
    checks = {name: statuses.get(name, "UNKNOWN") if statuses.get(name) in SAFE_STATES else "UNKNOWN"
              for name in CHECK_NAMES}
    return {"format": "paxs-control-support-v1", "created_utc": datetime.now(timezone.utc).isoformat(),
            "checks": checks}


def viewer_integrity_status(directory: Path) -> str:
    """Check the local packaged viewer, not a remote server's assets."""
    if not (directory / "paxs-build-manifest.json").is_file():
        return "UNAVAILABLE"
    try:
        return "INTEGRITY FAILED" if viewer_manifest_errors(directory) else "INTEGRITY OK"
    except OSError:
        return "UNAVAILABLE"
