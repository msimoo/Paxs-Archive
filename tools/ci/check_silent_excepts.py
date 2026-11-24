"""Fail the build on a swallowed exception (AST scan, ratchet).

`AGENTS.md` §3.3: *"A swallowed exception must be logged. 108 handlers currently
discard the error silently — and a silent failure in a DICOM pipeline looks
identical to success. Every ``except`` either raises, returns or logs."*

This gate enforces that rule with a **shrinking baseline**. FE-1 closed the four
critical pixel/protocol modules (108 → 72) and the Q-2 triage closed the
remaining 72 (2026-10-21), so the baseline is now **empty** — the file is kept
only so the ratchet keeps working, and any new silent handler fails immediately:

* a handler whose body raises, returns, yields or calls a logger is accepted;
* a handler carrying an explicit justification on its ``except`` line — the
  repository's own convention, ``except Exception:  # noqa: BLE001 - <why>``,
  or a prose comment with a marker such as ``expected``/``intentional`` — is
  accepted, because the deliberate decision is then recorded;
* every other handler is a **finding**. A finding not listed in the baseline
  fails the build (that is the zero-regression guarantee), and a baseline entry
  that no longer matches anything also fails, so the list can only shrink.

A **bare** ``except:`` is always a finding, baseline or not: it also catches
``KeyboardInterrupt`` and ``SystemExit``.

Usage::

    python tools/ci/check_silent_excepts.py app
    python tools/ci/check_silent_excepts.py app --update-baseline
    python tools/ci/check_silent_excepts.py app --strict     # tolerate nothing
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BASELINE = Path(__file__).resolve().parent / "silent-except-baseline.txt"

#: Methods that count as "the error was recorded".
LOG_METHODS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
)

#: Words that mark a comment as a deliberate decision to stay silent.
JUSTIFICATION_MARKERS = (
    "noqa", "intentional", "deliberately", "deliberate", "expected",
    "best-effort", "best effort", "must not mask", "never fail", "optional",
)

#: Nodes that mean the handler does not swallow the failure.
_TERMINAL = (ast.Raise, ast.Return, ast.Yield, ast.YieldFrom)

#: Definition nodes that can enclose an exception handler.
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


@dataclass(frozen=True)
class Finding:
    """One exception handler that discards its error without a trace."""

    path: str
    line: int
    kind: str
    qualname: str
    occurrence: int = 1

    @property
    def key(self) -> str:
        """Stable identity for the baseline (survives line-number drift).

        ``occurrence`` disambiguates two handlers with the same scope and
        exception type, so a newly added duplicate cannot hide behind an
        already-baselined one.
        """
        return f"{self.path}::{self.qualname}::{self.kind}::{self.occurrence}"

    def render(self) -> str:
        return f"{self.path}:{self.line}: `except {self.kind}:` swallows the error"


def _calls_a_logger(handler: ast.ExceptHandler) -> bool:
    """True when the handler calls something that looks like a logger."""
    for child in ast.walk(handler):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name in LOG_METHODS:
            return True
    return False


def _is_terminal(statements: list[ast.stmt]) -> bool:
    """True when any statement raises, returns or yields."""
    for statement in statements:
        if any(isinstance(child, _TERMINAL) for child in ast.walk(statement)):
            return True
    return False


def _justified(handler: ast.ExceptHandler, lines: list[str]) -> bool:
    """True when the ``except`` line carries an explicit justification."""
    if not 1 <= handler.lineno <= len(lines):
        return False
    text = lines[handler.lineno - 1]
    comment = text.split("#", 1)[1].lower() if "#" in text else ""
    return any(marker in comment for marker in JUSTIFICATION_MARKERS)


def _qualnames(tree: ast.AST) -> dict[int, str]:
    """Map every node id to its dotted enclosing scope name."""
    names: dict[int, str] = {id(tree): ""}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _SCOPES):
                parent = names.get(id(node), "")
                names[id(child)] = f"{parent}.{child.name}" if parent else child.name
            else:
                names[id(child)] = names.get(id(node), "")
    return names


def _kind_of(handler: ast.ExceptHandler) -> str:
    return "bare" if handler.type is None else ast.unparse(handler.type)


def _is_silent(handler: ast.ExceptHandler, lines: list[str]) -> bool:
    """True when this handler swallows its error without a trace."""
    if handler.type is None:
        return True  # never acceptable, justified or not
    if _is_terminal(handler.body) or _calls_a_logger(handler):
        return False
    return not _justified(handler, lines)


def scan_file(path: Path) -> list[Finding]:
    """Every silently-swallowing handler in one file."""
    try:
        source = path.read_text(encoding="utf-8-sig")
        tree = ast.parse(source, filename=str(path))
    except OSError as exc:
        return [Finding(str(path), 1, f"unreadable: {exc}", "<module>")]
    except SyntaxError as exc:
        return [Finding(str(path), exc.lineno or 1, f"unparsable: {exc.msg}", "<module>")]
    lines = source.splitlines()
    names = _qualnames(tree)
    seen: dict[tuple[str, str], int] = {}
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler) or not _is_silent(node, lines):
            continue
        scope = names.get(id(node), "") or "<module>"
        kind = _kind_of(node)
        seen[(scope, kind)] = seen.get((scope, kind), 0) + 1
        findings.append(
            Finding(str(path).replace("\\", "/"), node.lineno, kind, scope,
                    seen[(scope, kind)])
        )
    return findings


def scan_paths(roots: list[str]) -> list[Finding]:
    """Every silently-swallowing handler under the given roots."""
    findings: list[Finding] = []
    for root in roots:
        target = Path(root)
        files = sorted(target.rglob("*.py")) if target.is_dir() else [target]
        for path in files:
            findings.extend(scan_file(path))
    return findings


def read_baseline(path: Path) -> set[str]:
    """Baseline keys, one per line; blank lines and ``#`` comments ignored."""
    if not path.exists():
        return set()
    entries: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            entries.add(line)
    return entries


def write_baseline(path: Path, findings: list[Finding]) -> None:
    """Record the current findings as the tolerated baseline."""
    header = (
        "# Known swallowed exception handlers — a shrinking allowlist.\n"
        "# A finding NOT listed here fails the build (zero regression).\n"
        "# An entry that no longer matches anything ALSO fails, so fix a handler\n"
        "# and delete its line. Regenerate with --update-baseline.\n"
        "# Format: <path>::<enclosing scope>::<exception type>\n"
    )
    body = "\n".join(sorted(finding.key for finding in findings))
    path.write_text(f"{header}{body}\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="+", help="files or directories to scan")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--update-baseline", action="store_true",
                        help="rewrite the baseline from the current tree")
    parser.add_argument("--strict", action="store_true",
                        help="tolerate nothing, ignoring the baseline")
    args = parser.parse_args()

    findings = scan_paths(args.roots)
    if args.update_baseline:
        write_baseline(args.baseline, findings)
        print(f"baseline updated: {len(findings)} known handler(s) -> {args.baseline}")
        return 0

    tolerated = set() if args.strict else read_baseline(args.baseline)
    keys = {finding.key for finding in findings}
    new = [finding for finding in findings if finding.key not in tolerated]
    stale = sorted(tolerated - keys)

    for finding in new:
        print(f"::error file={finding.path},line={finding.line}::"
              f"`except {finding.kind}:` swallows the error (no raise / return / log)")
        print(f"  {finding.render()}")
    for entry in stale:
        print(f"::error ::stale baseline entry (no such handler any more): {entry}")

    if new or stale:
        print(f"\nsilent-except gate failed: {len(new)} new, {len(stale)} stale.")
        print("Fix the handler (raise / return / log), justify it with "
              "`# noqa: BLE001 - <why>` on the except line, or — for a stale "
              "entry — run --update-baseline.")
        return 1

    print(f"silent-except gate passed: 0 new swallowed handlers "
          f"({len(findings)} known handler(s), all tolerated by the baseline).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
