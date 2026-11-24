"""Changed-line map for diff-aware ("ratchet") CI gates.

The historical defects in this repository are *legacy debt*: 1004 ruff
diagnostics, ~450 functions with incomplete hints, 108 swallowed handlers. A
gate that fails on all of them is unusable, and a gate that ignores them lets
the next one in. The ratchet is the answer: run the analyser over the whole
tree, but only **fail on diagnostics that land on a line this change touched**.

Everything here is deliberately dependency-free (git + stdlib only) so it runs
on a bare CI runner before any `pip install`.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

#: Absolute path to git, so the process is not resolved through PATH (S607).
GIT = shutil.which("git") or "git"

# The empty tree object — diffing against it makes every line "added", which is
# the correct fail-closed behaviour for a repository with no usable history.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


class DiffError(RuntimeError):
    """The diff base could not be resolved (never silently degrade)."""


def repo_root() -> Path:
    """Absolute path of the working tree root."""
    out = _git("rev-parse", "--show-toplevel")
    return Path(out.strip())


def _git(*args: str) -> str:
    """Run git, returning stdout; raise DiffError on failure."""
    result = subprocess.run(  # noqa: S603 - a fixed argv, never shell-parsed
        [GIT, *args], capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if result.returncode != 0:
        raise DiffError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def resolve_base(explicit: str | None = None) -> str:
    """The revision to diff against, from an argument or the CI environment.

    Resolution order: ``--base``, then ``GITHUB_BASE_REF`` (pull requests),
    then ``GITHUB_EVENT_BEFORE`` (pushes), then ``HEAD~1``. Raises
    :class:`DiffError` when nothing resolves, so a misconfigured pipeline fails
    loudly instead of skipping the gate.
    """
    candidates = [explicit] if explicit else []
    base_ref = os.environ.get("GITHUB_BASE_REF", "").strip()
    if base_ref:
        candidates.append(f"origin/{base_ref}")
        candidates.append(base_ref)
    before = os.environ.get("GITHUB_EVENT_BEFORE", "").strip()
    if before and set(before) != {"0"}:
        candidates.append(before)
    candidates.append("HEAD~1")

    for candidate in candidates:
        if not candidate:
            continue
        try:
            _git("rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}")
        except DiffError:
            continue
        return candidate
    raise DiffError(
        "cannot resolve a diff base (tried: "
        + ", ".join(repr(c) for c in candidates if c)
        + "). Fetch full history (`fetch-depth: 0`) or pass --base."
    )


def changed_lines(base: str | None = None) -> dict[str, set[int]]:
    """``{repo-relative posix path: {added/changed line numbers}}``.

    ``base`` may be ``"EMPTY"`` to treat the whole tree as changed.
    """
    if base == "EMPTY":
        return _all_lines()
    revision = resolve_base(base)
    patch = _git("diff", "--unified=0", "--no-color", "--no-ext-diff", revision, "HEAD")

    changed: dict[str, set[int]] = {}
    current: str | None = None
    for line in patch.splitlines():
        if line.startswith("+++ "):
            current = _target_path(line[4:])
            if current is not None:
                changed.setdefault(current, set())
            continue
        if current is None or not line.startswith("@@"):
            continue
        match = _HUNK.match(line)
        if match is None:
            continue
        start = int(match.group(1))
        count = 1 if match.group(2) is None else int(match.group(2))
        changed[current].update(range(start, start + count))
    return {path: lines for path, lines in changed.items() if lines}


def _target_path(raw: str) -> str | None:
    """Normalise a ``+++ b/path`` header to a repo-relative posix path."""
    text = raw.strip()
    if text in ("/dev/null", ""):
        return None
    if text.startswith("b/"):
        text = text[2:]
    return text.replace("\\", "/")


def _all_lines() -> dict[str, set[int]]:
    """Every line of every tracked Python file (the no-history fallback)."""
    files = _git("ls-files", "*.py").splitlines()
    every: dict[str, set[int]] = {}
    for name in files:
        try:
            text = Path(name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        every[name] = set(range(1, text.count("\n") + 2))
    return every


def main() -> int:
    """CLI: print the changed-line map (debugging aid for the gates)."""
    base = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        mapping = changed_lines(base)
    except DiffError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    total = sum(len(lines) for lines in mapping.values())
    print(f"{len(mapping)} changed file(s), {total} changed line(s)")
    for path in sorted(mapping):
        print(f"  {path}: {len(mapping[path])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
