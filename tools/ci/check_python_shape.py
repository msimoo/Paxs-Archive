"""Fail the build on unannotated or over-long functions *added by this change*.

`AGENTS.md` §3.3 requires a full signature (parameters **and** return) on every
function, and §3.1 caps a function at 60 lines (40 is the target). The tree has
452 incomplete hints and 160 functions over 40 lines, so — like the ruff and
mypy gates — this one is a **ratchet**: it only judges functions whose signature
this change touches.

Usage::

    python tools/ci/check_python_shape.py --base origin/main app
    python tools/ci/check_python_shape.py --all app          # whole-tree audit
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _diff import DiffError, changed_lines

#: AGENTS.md §3.1 — a hard ceiling, justified in the docstring beyond this.
MAX_FUNCTION_LINES = 60

#: Receivers that are implicitly typed and need no annotation.
_SELF_LIKE = frozenset({"self", "cls"})

_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


class Finding:
    """One function that breaks the annotation or length rule."""

    def __init__(self, path: str, line: int, name: str, detail: str) -> None:
        self.path = path
        self.line = line
        self.name = name
        self.detail = detail

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.name}(): {self.detail}"


def _missing_annotations(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Names of parameters (and the return) that carry no annotation."""
    missing = [
        arg.arg for arg in (*func.args.posonlyargs, *func.args.args, *func.args.kwonlyargs)
        if arg.annotation is None and arg.arg not in _SELF_LIKE
    ]
    for extra, label in ((func.args.vararg, "*args"), (func.args.kwarg, "**kwargs")):
        if extra is not None and extra.annotation is None:
            missing.append(label)
    if func.returns is None:
        missing.append("-> return")
    return missing


def _signature_lines(func: ast.FunctionDef | ast.AsyncFunctionDef) -> range:
    """Line numbers of the ``def``/``async def`` signature (not the body)."""
    end = func.body[0].lineno - 1 if func.body else func.lineno
    return range(func.lineno, max(func.lineno, end) + 1)


def _length(func: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Number of source lines the function spans (inclusive)."""
    end = getattr(func, "end_lineno", None) or func.lineno
    return end - func.lineno + 1


def check_function(func: ast.FunctionDef | ast.AsyncFunctionDef, path: str) -> list[Finding]:
    """Every shape violation in one function."""
    findings: list[Finding] = []
    missing = _missing_annotations(func)
    if missing:
        findings.append(Finding(path, func.lineno, func.name,
                                f"missing annotation(s): {', '.join(missing)}"))
    length = _length(func)
    if length > MAX_FUNCTION_LINES:
        findings.append(Finding(path, func.lineno, func.name,
                                f"{length} lines exceeds the {MAX_FUNCTION_LINES}-line ceiling"))
    return findings


def check_file(path: Path, touched: set[int] | None) -> list[Finding]:
    """Shape violations in one file; ``touched=None`` checks every function."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    except (OSError, SyntaxError):
        return []
    name = str(path).replace("\\", "/")
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, _FUNCTIONS):
            continue
        if touched is not None and not any(
            line in touched for line in _signature_lines(node)
        ):
            continue
        findings.extend(check_function(node, name))
    return findings


def collect(roots: list[str], base: str | None) -> list[Finding]:
    """Shape violations across the roots, restricted to changed signatures."""
    if base == "ALL":
        return [f for root in roots for f in _scan_all(root)]
    touched = changed_lines(base)
    findings: list[Finding] = []
    for root in roots:
        target = Path(root)
        files = sorted(target.rglob("*.py")) if target.is_dir() else [target]
        for path in files:
            key = str(path).replace("\\", "/")
            if key not in touched:
                continue
            findings.extend(check_file(path, touched[key]))
    return findings


def _scan_all(root: str) -> list[Finding]:
    target = Path(root)
    files = sorted(target.rglob("*.py")) if target.is_dir() else [target]
    return [finding for path in files for finding in check_file(path, None)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="+", help="files or directories to check")
    parser.add_argument("--base", default=None, help="diff base revision (default: CI env)")
    parser.add_argument("--all", action="store_true", help="check every function, not the diff")
    args = parser.parse_args()

    try:
        findings = collect(args.roots, "ALL" if args.all else args.base)
    except DiffError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    for finding in findings:
        print(f"::error file={finding.path},line={finding.line}::{finding.detail}")
        print(f"  {finding.render()}")
    if findings:
        print(f"\nshape gate failed: {len(findings)} problem(s) in changed functions.")
        print(f"Annotate every parameter and the return; keep a function under "
              f"{MAX_FUNCTION_LINES} lines (AGENTS.md §3.1/§3.3).")
        return 1
    print("shape gate passed: every function this change touches is fully "
          f"annotated and under {MAX_FUNCTION_LINES} lines.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
