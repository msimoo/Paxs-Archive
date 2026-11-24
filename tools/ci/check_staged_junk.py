"""Fail the build on the artefacts D-9 proved a ``git add -A`` will stage.

``plans/legacy/newplan/02-BUGS-AND-ISSUES.md`` D-9 has been "partial" since 2026-10-13, and
the reason is always the same: the tree contains things a first commit must not
carry, and *nothing notices until someone reads the index by hand*. The
2026-10-21 review found the worst instance yet — **60 four-byte files named like
``0967e1qt``, each containing only ``blat``**, written at the project root by a
local tool and staged by a ``git add -A``. They were removed from the index; this
gate is what stops them coming back, because they are generated again on every
run of that tool.

The same review had to check, by hand, that ``.env``, ``paxs.db`` and
``paxs.lic`` were untracked. That check is now automated too: this is exactly
the class of mistake where "it looked clean last week" is not evidence.

Three rules, each mechanically decidable (AGENTS.md N-9 — no secrets in the
tree; §11 — the docs describe a commit, so check the tree):

1. **Root-level scratch artefacts.** A tracked path at the repository root whose
   name is exactly eight ``[A-Za-z0-9_]`` characters. Every legitimate root entry
   is a known name; this shape never is.
2. **Environment files.** ``.env`` and ``.env.<anything>`` are secrets. The
   committed templates (``.env.example`` / ``.sample`` / ``.template``) are the
   only exception, and they are matched exactly rather than by prefix so a
   ``.env.example.local`` still fails.
3. **Runtime state and private keys.** ``*.db`` (and its ``-wal`` / ``-shm``
   sidecars), ``*.lic``, ``*.pem``, ``*.key``, ``*.p12``, ``*.pfx``, ``*.jks``.
   A SQLite archive or a licence file in the history is a PHI disclosure and a
   credential disclosure respectively.

The gate reads ``git ls-files --cached`` — the index, not the working tree —
because the index is what a commit would contain. ``--paths`` exists so the
hygiene suite can drive :func:`find_violations` without a repository.

Usage::

    python tools/ci/check_staged_junk.py
    python tools/ci/check_staged_junk.py --paths 0967e1qt .env app/main/app.py
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

#: D-9: the shape of the generated scratch files (`0967e1qt`, `1fum79dq`, ...).
SCRATCH_ROOT_NAME = re.compile(r"^[A-Za-z0-9_]{8}$")

#: Templates an operator is *supposed* to commit.
ENV_TEMPLATES = frozenset({".env.example", ".env.sample", ".env.template"})

#: Extensions that are runtime state or private key material, never source.
FORBIDDEN_SUFFIXES = (
    ".db", ".db-wal", ".db-shm", ".lic",
    ".pem", ".key", ".p12", ".pfx", ".jks",
)


@dataclass(frozen=True)
class Violation:
    """One staged path that must not be committed."""

    path: str
    reason: str

    def render(self) -> str:
        return f"{self.path}: {self.reason}"


def _root_name_problem(path: str, name: str) -> str | None:
    """Reason a root-level scratch artefact is staged, or ``None``."""
    if "/" in path or not SCRATCH_ROOT_NAME.match(name):
        return None
    return ("an 8-character root artefact (D-9: the generated `blat` scratch "
            "files); delete it and keep it out of the index")


def _env_problem(name: str) -> str | None:
    """Reason an environment file is staged, or ``None``."""
    if name in ENV_TEMPLATES:
        return None
    if name != ".env" and not name.startswith(".env."):
        return None
    return "an environment file - it carries secrets (N-9); commit a template instead"


def _suffix_problem(name: str) -> str | None:
    """Reason a database/licence/key file is staged, or ``None``."""
    lowered = name.lower()
    for suffix in FORBIDDEN_SUFFIXES:
        if lowered.endswith(suffix):
            return (f"runtime state or private key material (`*{suffix}`) - "
                    "never belongs in the tree (N-9)")
    return None


def assess(path: str) -> Violation | None:
    """The violation a single staged path represents, if any.

    ``path`` is repository-relative with forward slashes, exactly as
    ``git ls-files`` prints it.
    """
    name = path.rsplit("/", 1)[-1]
    reason = _root_name_problem(path, name) or _env_problem(name) or _suffix_problem(name)
    return Violation(path, reason) if reason else None


def find_violations(paths: Iterable[str]) -> list[Violation]:
    """Every violation in ``paths``, in the order given."""
    return [finding for finding in map(assess, paths) if finding is not None]


def staged_paths(repo_root: Path) -> list[str]:
    """Repository-relative paths in the git index (``git ls-files --cached``).

    ``-z`` so a path containing a newline cannot be smuggled past the scanner.
    The executable is resolved with ``shutil.which`` rather than spelled ``git``:
    a Git Bash on Windows resolves ``git`` for a human but a scheduled job may not,
    and "the gate could not find git" must be an error, not a clean index.
    """
    git = shutil.which("git")
    if git is None:
        raise SystemExit(
            "check_staged_junk: `git` is not on PATH, so the index cannot be read"
        )
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell, resolved git path
        [git, "ls-files", "--cached", "-z"],
        cwd=repo_root, capture_output=True, check=False,
    )
    if result.returncode != 0:
        raise SystemExit(
            "check_staged_junk: `git ls-files --cached` failed (exit "
            f"{result.returncode}): {result.stderr.decode('utf-8', 'replace').strip()}"
        )
    return [entry for entry in result.stdout.decode("utf-8").split("\0") if entry]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."),
                        help="repository root (default: the current directory)")
    parser.add_argument("--paths", nargs="+", metavar="PATH",
                        help="assess these paths instead of reading the git index")
    args = parser.parse_args(argv)

    paths = args.paths if args.paths else staged_paths(args.root)
    violations = find_violations(paths)
    for violation in violations:
        print(f"::error file={violation.path}::{violation.reason}")
        print(f"  {violation.render()}")
    if violations:
        print(f"\nstaged-junk gate failed: {len(violations)} path(s) must not be "
              "committed.")
        print("Fix: `git restore --staged <path>` (and delete the file if it is "
              "generated), then re-run. For a tracked secret, rotate it first: "
              "removing it from the index does not remove it from history.")
        return 1
    print(f"staged-junk gate passed: none of the {len(paths)} staged path(s) is a "
          "scratch artefact, environment file, database, licence or private key.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
