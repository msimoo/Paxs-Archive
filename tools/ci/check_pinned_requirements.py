"""Fail the build on an unpinned or undeclared dependency.

Two historical defect classes live here:

* **unpinned** — ``pyjpegls>=1.9`` did not exist on any index, so a clean
  ``pip install -r requirements.txt`` failed outright; a floating range means
  the archive is built from whatever the index served that morning;
* **missing** — ``redis`` was commented out of ``requirements.txt`` while
  ``app/services/redis_service.py`` did a module-level ``import redis``, which
  broke the import of the entire application.

``--mode pins`` requires every requirement to carry an exact ``==`` pin.
``--mode imports`` requires every third-party module imported by the app to be
declared. ``--mode all`` runs both.

Usage::

    python tools/ci/check_pinned_requirements.py --mode all --requirements requirements.txt --app app
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

#: A requirement line: name, optional extras, specifiers, optional marker.
_REQUIREMENT = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?P<extras>\[[^\]]*\])?"
    r"(?P<specifiers>[^;#]*?)"
    r"(?P<marker>;.*)?$"
)

#: Exact pins only. ``==`` also matches the ``===`` arbitrary-equality form.
_PIN = re.compile(r"(^|,)\s*==")

#: Import name -> distribution name, for the ones that differ.
IMPORT_ALIASES = {
    "pil": "pillow",
    "pydantic_settings": "pydantic-settings",
    "multipart": "python-multipart",
    "yaml": "pyyaml",
    "jose": "python-jose",
    "dotenv": "python-dotenv",
    "cv2": "opencv-python",
    "sklearn": "scikit-learn",
    "bs4": "beautifulsoup4",
    "serial": "pyserial",
    "dateutil": "python-dateutil",
    "jwt": "pyjwt",
}

#: Third-party modules imported by the app but shipped by a declared package.
TRANSITIVE_OK = frozenset(
    {"starlette", "anyio", "greenlet", "certifi", "click", "h11", "idna", "sniffio"}
)


def normalise(name: str) -> str:
    """PEP 503 name normalisation (``Foo_Bar`` -> ``foo-bar``)."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def parse_requirements(path: Path) -> list[tuple[int, str, str, str]]:
    """``(line number, name, specifiers, marker)`` for each requirement line."""
    parsed: list[tuple[int, str, str, str]] = []
    if not path.exists():
        return parsed
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith(("-", "--")):
            continue
        match = _REQUIREMENT.match(line)
        if match is None:
            parsed.append((number, line, "", ""))
            continue
        parsed.append((number, match.group("name"),
                       (match.group("specifiers") or "").strip(),
                       (match.group("marker") or "").strip()))
    return parsed


def check_pins(path: Path) -> list[str]:
    """Findings for unpinned, duplicate or unparsable requirement lines."""
    findings: list[str] = []
    seen: dict[str, int] = {}
    for number, name, specifiers, _marker in parse_requirements(path):
        if not specifiers and not name.startswith("-"):
            findings.append(f"{path}:{number}: {name!r} has no version specifier at all")
            continue
        if specifiers and not _PIN.search(specifiers):
            findings.append(
                f"{path}:{number}: {name} uses {specifiers!r} — pin it exactly (==)"
            )
        key = normalise(name)
        if key in seen:
            findings.append(f"{path}:{number}: {name} duplicates line {seen[key]}")
        seen.setdefault(key, number)
    return findings


def declared_distributions(path: Path) -> set[str]:
    """Normalised distribution names declared in the requirements file."""
    return {normalise(name) for _n, name, _s, _m in parse_requirements(path)}


def _module_level_imports(tree: ast.Module) -> list[str]:
    """Names imported *unconditionally* at module scope.

    Only direct children of the module body count. An import inside ``try:``,
    ``if``, a function or a class is conditional — the caller has a fallback
    (``ldap3`` is imported lazily inside ``ldap_service`` because LDAP is
    optional). A bare module-level import has no such escape: if the package is
    missing, importing the app fails outright. That was the ``redis`` defect.
    """
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.append(node.module.split(".")[0])
    return names


def imported_top_level(roots: list[str]) -> dict[str, set[str]]:
    """``{module name: {files that import it unconditionally}}``."""
    found: dict[str, set[str]] = {}
    for root in roots:
        target = Path(root)
        files = sorted(target.rglob("*.py")) if target.is_dir() else [target]
        for path in files:
            try:
                tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            except (OSError, SyntaxError):
                continue
            for name in _module_level_imports(tree):
                found.setdefault(name, set()).add(str(path).replace("\\", "/"))
    return found


def check_imports(roots: list[str], declared: set[str]) -> list[str]:
    """Findings for third-party imports with no matching requirement."""
    findings: list[str] = []
    for name, files in sorted(imported_top_level(roots).items()):
        if name in sys.stdlib_module_names or name in TRANSITIVE_OK or name == "app":
            continue
        distribution = normalise(IMPORT_ALIASES.get(name.lower(), name))
        if distribution not in declared:
            where = ", ".join(sorted(files)[:3])
            findings.append(f"import {name!r} ({where}) is not declared in requirements.txt")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("pins", "imports", "all"), default="all")
    parser.add_argument("--requirements", type=Path, default=Path("requirements.txt"))
    parser.add_argument("--app", nargs="+", default=["app"])
    args = parser.parse_args()

    findings: list[str] = []
    if args.mode in ("pins", "all"):
        findings.extend(check_pins(args.requirements))
    if args.mode in ("imports", "all"):
        findings.extend(check_imports(args.app, declared_distributions(args.requirements)))

    for finding in findings:
        print(f"::error ::{finding}")
        print(f"  {finding}")
    if findings:
        print(f"\ndependency gate failed: {len(findings)} problem(s).")
        return 1
    print(f"dependency gate passed: {args.requirements} is fully pinned and every "
          f"third-party import is declared.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
