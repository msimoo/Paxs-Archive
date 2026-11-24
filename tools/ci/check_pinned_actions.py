"""Fail CI when an external GitHub Action uses a mutable reference."""

from __future__ import annotations

import re
from pathlib import Path
import sys

_USES = re.compile(r"^\s*(?:-\s*)?uses:\s*([^\s#]+)")
_SHA = re.compile(r"^[0-9a-fA-F]{40}$")


def unpinned_actions(workflow_text: str) -> list[str]:
    """Return external action references that are not pinned to a full SHA."""
    findings: list[str] = []
    for line in workflow_text.splitlines():
        match = _USES.match(line)
        if not match:
            continue
        reference = match.group(1)
        if reference.startswith("./"):
            continue
        action, separator, ref = reference.rpartition("@")
        if not separator or not action or not _SHA.fullmatch(ref):
            findings.append(reference)
    return findings


def main() -> int:
    """Scan all checked-in workflow YAML files without evaluating expressions."""
    root = Path(__file__).resolve().parents[2]
    workflows = sorted((root / ".github" / "workflows").glob("*.yml"))
    workflows += sorted((root / ".github" / "workflows").glob("*.yaml"))
    failed = False
    for workflow in workflows:
        for line_number, line in enumerate(workflow.read_text(encoding="utf-8").splitlines(), 1):
            matches = unpinned_actions(line)
            for reference in matches:
                print(f"{workflow.relative_to(root)}:{line_number}: unpinned action {reference}")
                failed = True
    if not failed:
        print(f"Pinned action check passed ({len(workflows)} workflow files)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
