"""Diff-aware (ratchet) gate for ruff and mypy diagnostics.

Usage::

    python tools/ci/ratchet.py --base <rev> -- ruff check . --output-format=json
    python tools/ci/ratchet.py --base <rev> -- mypy app --ignore-missing-imports

Both analysers are run over the **whole** tree — the pre-existing debt is
printed as a summary so it stays visible — but the exit status only reflects
diagnostics that land on a line this change touched. That is the ratchet: the
existing 1004 ruff diagnostics and ~450 incomplete hint signatures cannot block
a change, and the next one cannot slip in.

A crash of the underlying tool (as opposed to "it found problems") is always a
failure: ruff exits 2 on a bad invocation, mypy exits 2 on an internal error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _diff import DiffError, changed_lines

#: Both analysers report absolute filenames; the diff map is repo-relative.
REPO_ROOT = Path.cwd()

#: Tools whose output this gate knows how to read.
PARSERS = ("ruff", "mypy")


@dataclass(frozen=True)
class Diagnostic:
    """One analyser finding, located at a file and 1-based line."""

    path: str
    line: int
    code: str
    message: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: [{self.code}] {self.message}"


def normalise_path(raw: str) -> str:
    """Repo-relative posix path for an analyser-reported filename.

    Ruff and mypy both emit absolute paths; the diff map is repo-relative, so a
    mismatch here would silently make every finding look "not in this change".
    """
    text = str(raw).replace("\\", "/")
    root = str(REPO_ROOT).replace("\\", "/").rstrip("/")
    if text.startswith(f"{root}/"):
        text = text[len(root) + 1:]
    elif os.path.isabs(text):
        text = os.path.relpath(text, REPO_ROOT).replace("\\", "/")
    return text[2:] if text.startswith("./") else text


def parse_ruff(stdout: str) -> list[Diagnostic]:
    """Parse ``ruff check --output-format=json``."""
    try:
        payload = json.loads(stdout or "[]")
    except json.JSONDecodeError as exc:
        raise DiffError(f"ruff did not emit JSON (is --output-format=json set?): {exc}") from exc
    found: list[Diagnostic] = []
    for item in payload:
        location = item.get("location") or {}
        found.append(
            Diagnostic(
                path=normalise_path(item.get("filename", "")),
                line=int(location.get("row", 0) or 0),
                code=str(item.get("code") or "RUFF"),
                message=str(item.get("message", "")),
            )
        )
    return found


#: ``path:line:col: error: message`` — mypy omits the column unless
#: ``--show-column-numbers`` is passed, and a Windows path may contain a colon,
#: so the path is matched non-greedily.
_MYPY_LINE = re.compile(
    r"^(?P<path>.+?):(?P<line>\d+)(?::(?P<col>\d+))?:\s*"
    r"(?P<severity>error|note|warning):\s*(?P<message>.*)$"
)


def parse_mypy(stdout: str) -> list[Diagnostic]:
    """Parse mypy's ``path:line[:col]: error: message [code]`` lines."""
    found: list[Diagnostic] = []
    for raw in (stdout or "").splitlines():
        match = _MYPY_LINE.match(raw.rstrip())
        if match is None or match.group("severity") != "error":
            continue
        message = match.group("message").strip()
        code = message.rsplit("[", 1)[-1].rstrip("]") if message.endswith("]") else "mypy"
        found.append(
            Diagnostic(
                path=normalise_path(match.group("path")),
                line=int(match.group("line")),
                code=code,
                message=message,
            )
        )
    return found


def run(command: list[str]) -> tuple[int, str]:
    """Run ``command``; return (returncode, combined stdout)."""
    result = subprocess.run(  # noqa: S603 - argv comes from this workflow, not a user
        command, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if result.returncode == 2:
        print(result.stderr.strip() or result.stdout.strip(), file=sys.stderr)
        raise DiffError(f"`{' '.join(command)}` failed to run (exit 2)")
    return result.returncode, result.stdout


def annotate(diagnostic: Diagnostic, note: str) -> None:
    """Emit a GitHub Actions annotation (a no-op outside CI)."""
    print(f"::error file={diagnostic.path},line={diagnostic.line}::{note} {diagnostic.render()}")


def gate(diagnostics: list[Diagnostic], touched: dict[str, set[int]], label: str) -> int:
    """Fail only on diagnostics that land on a changed line."""
    new = [
        d for d in diagnostics
        if d.path in touched and d.line in touched[d.path]
    ]
    print(f"{label}: {len(diagnostics)} finding(s) in the tree, "
          f"{len(new)} on lines changed by this commit.")
    for diagnostic in new:
        annotate(diagnostic, f"new {label} finding")
        print(f"  {diagnostic.render()}")
    if new:
        print(f"\n{label} ratchet failed: {len(new)} new finding(s) above.")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=None, help="diff base revision (default: CI env)")
    parser.add_argument("--tool", choices=PARSERS, default=None, help="defaults to argv[0]")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- <analyser command>")
    args = parser.parse_args()

    command = [part for part in args.command if part != "--"]
    if not command:
        print("error: no analyser command given (use `-- <cmd>`)", file=sys.stderr)
        return 2
    tool = args.tool or Path(command[0]).stem
    if tool not in PARSERS:
        print(f"error: unknown tool {tool!r}; expected one of {PARSERS}", file=sys.stderr)
        return 2

    try:
        touched = changed_lines(args.base)
        exit_code, stdout = run(command)
    except DiffError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    diagnostics = parse_ruff(stdout) if tool == "ruff" else parse_mypy(stdout)
    if exit_code not in (0, 1):
        print(f"error: {tool} exited {exit_code}", file=sys.stderr)
        return 2
    return gate(diagnostics, touched, tool)


if __name__ == "__main__":
    raise SystemExit(main())
