"""Verify exact hashes and inventory for the shipped viewer directory."""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.viewer_integrity import viewer_manifest_errors


def main() -> int:
    """Exit nonzero on missing, changed, unsafe, or unlisted viewer assets."""
    errors = viewer_manifest_errors(ROOT / "app" / "static" / "viewer")
    if errors:
        for error in errors:
            print(error)
        return 1
    print("Viewer manifest verified: exact inventory and hashes match")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
