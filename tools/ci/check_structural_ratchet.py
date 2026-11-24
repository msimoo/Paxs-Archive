"""Fail the build when the tree's structural debt grows — on any change.

``AGENTS.md`` §3.1/§3.3 and ``plans/legacy/newplan/08-CODE-REVIEW-2026-10-21.md`` §3 tell the
same story: between 2026-10-10 and 2026-10-21, functions over 40 lines went
**160 → 208**, incomplete type hints **452 → 565**, modules over 800 lines
**8 → 10**, and ``noqa`` comments **41 → 114** — while every gate stayed green,
because ``check_python_shape.py`` judges only the signatures a diff touches. A
ratchet that only looks at changed lines cannot see a metric move: the change
that adds line 41 to a function is not the change that touches its signature.

So this gate measures the **whole tree** against four numbers and refuses both
directions of drift:

* **worse than the baseline** → exit 1. The debt grew; pay it down or split the
  change.
* **better than the baseline** → exit 1, unless ``--update-baseline`` was run.
  Improvement must be *recorded*, or the next regression can spend it silently:
  the 2026-10-21 breach is exactly what happens when headroom is unobserved.
* **equal** → exit 0.

Same semantics as ``check_silent_excepts.py`` (a shrinking allowlist, where a
stale entry fails too), applied to counters instead of handler locations.

Metrics (measured under ``app/``, parsed with ``ast`` — no import, no app
boot, no database):

====================  =========================================================
``functions_over_40_lines``      Any ``def``/``async def`` spanning > 40 lines.
``functions_incomplete_type_hints``  A parameter or the return lacks a hint
                                     (``self``/``cls`` excepted).
``modules_over_800_lines``       Any ``.py`` with more than 800 source lines.
``unexplained_noqa_comments``    ``# noqa`` directives without a written reason.
                                 Explained exceptions are covered by regression
                                 checks and do not count as structural debt.
====================  =========================================================

Usage::

    python tools/ci/check_structural_ratchet.py
    python tools/ci/check_structural_ratchet.py --update-baseline
    python tools/ci/check_structural_ratchet.py --root app --baseline other.json
    python tools/ci/check_structural_ratchet.py --json        # machine-readable
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

#: ``AGENTS.md`` §3.1 — the target is 40; 60 is the hard ceiling this measures.
FUNCTION_LINE_TARGET = 40
#: ``AGENTS.md`` §3.1 — a module must stay under ~600, 800 is already debt.
MODULE_LINE_LIMIT = 800

NOQA = re.compile(r"#\s*noqa", re.IGNORECASE)
NOQA_REASON = re.compile(r"#\s*noqa\b[^\r\n]*\s-\s+\S", re.IGNORECASE)
_SELF_LIKE = frozenset({"self", "cls"})
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)

DEFAULT_BASELINE = Path(__file__).resolve().parent / "structural-baseline.json"
DEFAULT_ROOT = "app"

#: Written into the baseline on every update. It states the rule where the number
#: lives, because a reader who finds only integers has no way to know that a
#: smaller number is also a failure.
BASELINE_NOTE = (
    "The one-way floor for tools/ci/check_structural_ratchet.py over `app/`. "
    "A metric may only fall, and a fall must be recorded with --update-baseline "
    "so the reduction is reviewed instead of being spent silently. Recorded "
    "2026-10-27 after reducing long functions to 206 and large modules to 6, "
    "and recording the type-hint improvement. `unexplained_noqa_comments` "
    "counts suppressions without a written reason; explained suppressions are "
    "validated by tests/test_structural_ratchet.py."
)

#: `current > baseline` is a regression; `<` is an unrecorded improvement.
METRIC_ORDER = (
    "functions_over_40_lines",
    "functions_incomplete_type_hints",
    "modules_over_800_lines",
    "unexplained_noqa_comments",
)


@dataclass(frozen=True)
class Metrics:
    """The four counters, one row per metric the gate enforces."""

    functions_over_40_lines: int = 0
    functions_incomplete_type_hints: int = 0
    modules_over_800_lines: int = 0
    unexplained_noqa_comments: int = 0

    def as_dict(self) -> dict[str, int]:
        """Plain mapping, in the documented order."""
        return {name: getattr(self, name) for name in METRIC_ORDER}


def _missing_hints(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Names of parameters (and the return) that carry no annotation."""
    missing = [
        arg.arg
        for arg in (*func.args.posonlyargs, *func.args.args, *func.args.kwonlyargs)
        if arg.annotation is None and arg.arg not in _SELF_LIKE
    ]
    for extra, label in ((func.args.vararg, "*args"), (func.args.kwarg, "**kwargs")):
        if extra is not None and extra.annotation is None:
            missing.append(label)
    if func.returns is None:
        missing.append("-> return")
    return missing


def _span(func: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Inclusive line span of one function (decorators excluded)."""
    end = getattr(func, "end_lineno", None) or func.lineno
    return end - func.lineno + 1


def scan_source(path: Path) -> tuple[int, int, int]:
    """Return long functions, incomplete hints, and unexplained suppressions."""
    text = path.read_text(encoding="utf-8-sig")
    tree = ast.parse(text, filename=str(path))
    long_functions = 0
    unannotated = 0
    for node in ast.walk(tree):
        if not isinstance(node, _FUNCTIONS):
            continue
        if _span(node) > FUNCTION_LINE_TARGET:
            long_functions += 1
        if _missing_hints(node):
            unannotated += 1
    unexplained_noqa = sum(
        1 for line in text.splitlines()
        if NOQA.search(line) and not NOQA_REASON.search(line)
    )
    return long_functions, unannotated, unexplained_noqa


def python_files(root: Path) -> Iterator[Path]:
    """Every ``.py`` under ``root``, sorted, excluding caches."""
    if root.is_file():
        yield root
        return
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


def measure(root: Path) -> Metrics:
    """All four metrics for ``root`` (a directory or a single file)."""
    long_functions = unannotated = modules_over = unexplained_noqa = 0
    for path in python_files(root):
        file_long, file_unannotated, file_unexplained_noqa = scan_source(path)
        long_functions += file_long
        unannotated += file_unannotated
        unexplained_noqa += file_unexplained_noqa
        if len(path.read_text(encoding="utf-8-sig").splitlines()) > MODULE_LINE_LIMIT:
            modules_over += 1
    return Metrics(long_functions, unannotated, modules_over, unexplained_noqa)


def read_baseline(path: Path) -> dict[str, int]:
    """The recorded counters, validated as a complete set of integers."""
    data = json.loads(path.read_text(encoding="utf-8"))
    metrics = data.get("metrics") if isinstance(data, dict) else None
    if not isinstance(metrics, dict):
        raise SystemExit(f"{path}: missing a `metrics` object")
    absent = [name for name in METRIC_ORDER if name not in metrics]
    if absent:
        raise SystemExit(f"{path}: missing metric(s): {', '.join(absent)}")
    for name in METRIC_ORDER:
        if not isinstance(metrics[name], int):
            raise SystemExit(f"{path}: `{name}` must be an integer")
    return {name: metrics[name] for name in METRIC_ORDER}


def write_baseline(path: Path, current: Metrics, note: str = BASELINE_NOTE) -> None:
    """Record ``current`` as the new floor."""
    payload = {"note": note, "metrics": current.as_dict()}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def compare(current: Metrics, baseline: dict[str, int]) -> list[tuple[str, int, int]]:
    """Every metric that differs, as ``(name, baseline, current)``."""
    now = current.as_dict()
    return [(name, baseline[name], now[name])
            for name in METRIC_ORDER if now[name] != baseline[name]]


def report(drift: list[tuple[str, int, int]]) -> list[tuple[str, int, int]]:
    """Print each difference with its direction; return the regressions."""
    worse = [row for row in drift if row[2] > row[1]]
    better = [row for row in drift if row[2] < row[1]]
    for name, before, after in worse:
        print(f"::error ::structural regression: {name} {before} -> {after} "
              f"(+{after - before})")
    for name, before, after in better:
        print(f"::error ::unrecorded improvement: {name} {before} -> {after} "
              f"({after - before}); run --update-baseline to lock it in")
    return worse


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(DEFAULT_ROOT),
                        help=f"tree to measure (default: {DEFAULT_ROOT})")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE,
                        help="baseline JSON (default: tools/ci/structural-baseline.json)")
    parser.add_argument("--update-baseline", action="store_true",
                        help="record the current numbers as the new floor")
    parser.add_argument("--json", action="store_true",
                        help="print the measurements as JSON and exit")
    args = parser.parse_args(argv)

    if not args.root.exists():
        print(f"error: {args.root} does not exist", file=sys.stderr)
        return 2
    current = measure(args.root)
    if args.json:
        print(json.dumps(current.as_dict(), indent=2))
        return 0
    if args.update_baseline:
        write_baseline(args.baseline, current, BASELINE_NOTE)
        print(f"baseline updated: {args.baseline} <- {json.dumps(current.as_dict())}")
        return 0

    drift = compare(current, read_baseline(args.baseline))
    if not drift:
        print(f"structural ratchet passed: {json.dumps(current.as_dict())} "
              f"matches the baseline (nothing grew, nothing improved unrecorded).")
        return 0
    report(drift)
    print(f"\nstructural ratchet failed: {len(drift)} metric(s) moved.")
    print("Growth is a regression — split the change or reduce it. An improvement "
          "must be recorded: `python tools/ci/check_structural_ratchet.py "
          "--update-baseline` in the same commit, so the new floor is reviewed.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
