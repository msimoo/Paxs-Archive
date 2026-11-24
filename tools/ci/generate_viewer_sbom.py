"""Create a deterministic CycloneDX file inventory from the verified viewer manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.viewer_integrity import MANIFEST_NAME, viewer_manifest_errors

_NAMESPACE = uuid.UUID("71e4408b-cd8c-5b25-a62b-8d20ed73d4af")


def make_viewer_sbom(viewer_directory: Path) -> dict[str, object]:
    """Return a reproducible CycloneDX 1.6 BOM for the shipped viewer files."""
    errors = viewer_manifest_errors(viewer_directory)
    if errors:
        raise ValueError("Viewer inventory invalid: " + "; ".join(errors[:5]))
    manifest = json.loads((viewer_directory / MANIFEST_NAME).read_text(encoding="utf-8"))
    files: dict[str, str] = manifest["files"]
    manifest_digest = hashlib.sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    root_ref = f"urn:uuid:{uuid.uuid5(_NAMESPACE, manifest_digest)}"
    components = [
        {
            "type": "file",
            "name": name,
            "bom-ref": f"urn:sha256:{digest}:{name}",
            "hashes": [{"alg": "SHA-256", "content": digest}],
        }
        for name, digest in sorted(files.items())
    ]
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": root_ref,
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "name": "PAXS shipped viewer asset inventory",
                "version": manifest.get("source", {}).get("version", "unknown"),
                "bom-ref": root_ref,
                "properties": [
                    {
                        "name": "paxs:dependency-graph",
                        "value": "unavailable-no-viewer-lockfile",
                    }
                ],
            },
            "tools": {"components": [{"type": "application", "name": "PAXS viewer SBOM generator"}]},
        },
        "components": components,
        "dependencies": [{"ref": root_ref, "dependsOn": [item["bom-ref"] for item in components]}],
    }


def main() -> int:
    """Write the verified file-level BOM to the requested path."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--viewer-dir", type=Path, default=Path("app/static/viewer"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    viewer_directory = args.viewer_dir if args.viewer_dir.is_absolute() else ROOT / args.viewer_dir
    bom = make_viewer_sbom(viewer_directory)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(bom, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(f"Viewer CycloneDX file inventory written: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
