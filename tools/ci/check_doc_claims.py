"""Fail the build when the docs claim something the tree does not (FDC-6).

``AGENTS.md`` §11 spells the problem out: *"The docs describe the code" — they
describe a commit.* Three drifts have already cost this repository real time:

* the README pointed at ``app/main.py``, a file that has never existed (the
  package is ``app/main/{app,middleware,startup}.py``);
* ``.env.example`` lists ``PAXS_ROOT_PATH`` and ``PAXS_FORWARDED_ALLOW_IPS`` as
  if they were ``Settings`` fields — they are read from the environment by
  ``run_paxs.py``, which nothing in the file said, so the next reader "fixes"
  one of them and the sub-path deployment silently stops working;
* a documented default (``DEFAULT_COMPRESSION=RLE``) disagreed with the code,
  so every operator who copied the example file got a different archive than
  the documentation promised.

This gate makes each of those a build failure. It is deliberately narrow — it
checks claims that are **mechanically decidable**, because a linter with
opinions gets switched off within a week:

1. **PATH claims.** A backticked repo-relative path, or a relative Markdown
   link, in the docs must exist. Globs are expanded (at least one match).
2. **SETTING claims.** Every active ``KEY=value`` in ``.env.example`` must be a
   ``Settings`` field or a variable read by an explicit ``os.getenv`` /
   ``os.environ`` call; and a documented example value must satisfy the field's
   declared type — a ``Literal[...]`` value must be one of its members, a
   ``bool`` must parse as a boolean, a number must be numeric.
3. **DEFAULT claims.** ``NAME=value`` on a line that talks about a default, and
   "``NAME``, whose default is value", must match the literal default declared
   in ``config.py``.

Only the first two are exhaustive; the third checks the claim shapes the docs
actually use. Nothing here imports ``app.core.config`` — a CI gate must not need
a ``SECRET_KEY`` to read a file.

Usage::

    python tools/ci/check_doc_claims.py
    python tools/ci/check_doc_claims.py --root . --strict
    python tools/ci/check_doc_claims.py --docs README.md deploy/MONITORING.md
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

DOC_FILES: tuple[str, ...] = ("README.md", "deploy/MONITORING.md")
ENV_EXAMPLE = ".env.example"
CONFIG_FILE = "app/core/config.py"
#: Where an ``os.getenv`` call may legitimately consume an env var.
ENV_CONSUMER_ROOTS: tuple[str, ...] = ("app", "run_paxs.py", "scripts")

_KEY_RE = re.compile(r"^(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=(.*)$")
_SETTING_RE = r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+"
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_LINK_RE = re.compile(r"\]\(([^)\s]+)\)")
_SETTING_ASSIGN_RE = re.compile(rf"^({_SETTING_RE})=(\S+)$")
#: "`NAME`, whose default is loopback only" / "`NAME` defaults to 8000".
_DEFAULT_PHRASE_RE = re.compile(
    rf"`({_SETTING_RE})`[^.\n]{{0,80}}?\bdefaults?\s+(?:is|to)\s+([^.;\n]{{1,40}})",
    re.IGNORECASE,
)
_PATH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.*/-]*$")

_GETENV_PATTERNS = (
    re.compile(r"os\.getenv\(\s*[\"']([A-Z0-9_]+)[\"']"),
    re.compile(r"os\.environ\.get\(\s*[\"']([A-Z0-9_]+)[\"']"),
    re.compile(r"os\.environ\[\s*[\"']([A-Z0-9_]+)[\"']"),
)
_TRUE_WORDS = frozenset({"true", "yes", "on", "enabled", "1"})
_FALSE_WORDS = frozenset({"false", "no", "off", "disabled", "0"})
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


@dataclass(frozen=True)
class Setting:
    """One ``Settings`` field, as the AST sees it."""

    name: str
    annotation: str
    default: object | None
    literals: frozenset[str] | None

    def render_default(self) -> str:
        return "<computed>" if self.default is None else repr(self.default)


@dataclass(frozen=True)
class Finding:
    """One documented claim the code contradicts."""

    path: str
    line: int
    claim: str
    detail: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.claim} - {self.detail}"


def _clean(text: str) -> str:
    """Normalise a value for comparison: no markup, quotes or trailing punctuation."""
    stripped = text.strip().strip("`*\"'()[] ").rstrip(".,;:")
    return stripped.strip().lower()


def _literal_set(annotation: ast.expr) -> frozenset[str] | None:
    """Members of a ``Literal[...]`` annotation, or ``None`` for any other type."""
    if not (isinstance(annotation, ast.Subscript) and isinstance(annotation.value, ast.Name)):
        return None
    if annotation.value.id != "Literal":
        return None
    members = annotation.slice
    if isinstance(members, ast.Tuple):
        values = [ast.literal_eval(element) for element in members.elts]
    else:
        values = [ast.literal_eval(members)]
    return frozenset(str(value) for value in values)


def load_settings(config_path: Path) -> dict[str, Setting]:
    """Every field of the ``Settings`` class, without importing the module."""
    tree = ast.parse(config_path.read_text(encoding="utf-8-sig"), filename=str(config_path))
    declared: dict[str, Setting] = {}
    for node in tree.body:
        if not (isinstance(node, ast.ClassDef) and node.name == "Settings"):
            continue
        for statement in node.body:
            if not isinstance(statement, ast.AnnAssign):
                continue
            name = getattr(statement.target, "id", None)
            if name is None:
                continue
            default: object | None = None
            if isinstance(statement.value, ast.Constant):
                default = statement.value.value
            declared[name] = Setting(
                name=name,
                annotation=ast.unparse(statement.annotation),
                default=default,
                literals=_literal_set(statement.annotation),
            )
    return declared


def env_consumers(root: Path, roots: Sequence[str] = ENV_CONSUMER_ROOTS) -> set[str]:
    """Env var names read by an explicit ``os.getenv``/``os.environ`` call.

    These are legitimate settings that deliberately bypass ``Settings`` (a
    value uvicorn needs *before* the settings object exists), so the checker
    accepts them instead of demanding a field that must not be there.
    """
    names: set[str] = set()
    for entry in roots:
        target = root / entry
        files = sorted(target.rglob("*.py")) if target.is_dir() else [target]
        for path in files:
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for pattern in _GETENV_PATTERNS:
                names.update(pattern.findall(text))
    return names


def env_example_entries(path: Path) -> list[tuple[int, str, str]]:
    """``(line, key, value)`` for every active assignment in the example file."""
    text = path.read_bytes().decode("utf-8-sig", errors="replace")
    entries: list[tuple[int, str, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        match = _KEY_RE.match(line.strip())
        if match:
            entries.append((number, match.group(1), match.group(2).strip()))
    return entries


def _value_fits(annotation: str, literals: frozenset[str] | None, value: str) -> bool:
    """Does a documented example value satisfy the field's declared type?

    Empty is always allowed: ``KEY=`` is how the example file says "unset, so
    the code default applies", which is exactly what the shipped file means by
    an empty ``SECRET_KEY``.
    """
    if value == "":
        return True
    cleaned = _clean(value)
    if literals is not None:
        return cleaned in {member.lower() for member in literals}
    if annotation == "bool":
        return cleaned in _TRUE_WORDS | _FALSE_WORDS
    if annotation in {"int", "float"}:
        try:
            float(cleaned.replace("_", ""))
        except ValueError:
            return False
        return annotation == "float" or "." not in cleaned
    return True


def claim_matches(claim: str, setting: Setting) -> bool | None:
    """Does *claim* agree with the code?

    ``None`` means "not decidable here" (the default is computed, so there is no
    literal to compare against) — reported as neither pass nor failure.
    """
    if setting.default is None:
        return None
    default = setting.default
    text = _clean(claim)

    if isinstance(default, bool):
        if text in _TRUE_WORDS:
            return default is True
        if text in _FALSE_WORDS:
            return default is False
        return False
    if isinstance(default, (int, float)):
        number = _NUMBER_RE.search(text)
        return number is not None and float(number.group()) == float(default)

    default_text = _clean(str(default))
    if text == default_text:
        return True
    if text.startswith("loopback"):
        return default_text.split(",")[0].strip() in _LOOPBACK
    members = {part.strip() for part in default_text.split(",")}
    return text in members


def _strip_parentheticals(text: str) -> str:
    """Drop ``(...)`` asides before looking for the word "default".

    A bare ``(default)`` annotates the noun beside it — "Local filesystem
    (default)" says nothing about the ``STORAGE_BACKEND=s3`` example later in
    the same table row — so counting it would turn every documented example
    into a false default claim. A parenthesised *claim* is unaffected: the
    setting name and value it names are still on the line, and the phrase form
    ("`NAME`, whose default is ...") is matched separately.
    """
    return re.sub(r"\([^)]*\)", " ", text)


def check_default_claims(path: Path, settings: dict[str, Setting]) -> list[Finding]:
    """Every default claim in one document, checked against ``config.py``."""
    findings: list[Finding] = []
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    for index, line in enumerate(lines, start=1):
        mentions_default = "default" in _strip_parentheticals(line).lower()
        for code in _BACKTICK_RE.findall(line):
            match = _SETTING_ASSIGN_RE.match(code.strip())
            if not match or not mentions_default:
                continue
            name, claimed = match.groups()
            setting = settings.get(name)
            if setting is None:
                findings.append(Finding(
                    str(path), index, f"`{code}`",
                    f"{name} is not a Settings field, so it cannot have a default",
                ))
                continue
            verdict = claim_matches(claimed, setting)
            if verdict is False:
                findings.append(Finding(
                    str(path), index, f"`{code}`",
                    f"config.py declares {name} = {setting.render_default()}",
                ))
        for name, claimed in _DEFAULT_PHRASE_RE.findall(line):
            setting = settings.get(name)
            if setting is None:
                continue
            verdict = claim_matches(claimed, setting)
            if verdict is False:
                findings.append(Finding(
                    str(path), index, f"`{name}` default is {claimed.strip()}",
                    f"config.py declares {name} = {setting.render_default()}",
                ))
    return findings


def _path_claim_tokens(text: str) -> list[tuple[int, str]]:
    """``(line, token)`` for every repo-relative path the document claims."""
    claims: list[tuple[int, str]] = []
    for index, line in enumerate(text.splitlines(), start=1):
        candidates = _BACKTICK_RE.findall(line) + _LINK_RE.findall(line)
        for raw in candidates:
            token = raw.strip()
            if "://" in token or token.startswith(("/", "#", "..", "mailto:")):
                continue
            if not _PATH_RE.match(token) or "/" not in token:
                continue
            claims.append((index, token.rstrip("/")))
    return claims


def check_path_claims(root: Path, path: Path) -> list[Finding]:
    """Every path a document names must exist under *root*."""
    findings: list[Finding] = []
    text = path.read_text(encoding="utf-8", errors="ignore")
    for index, token in _path_claim_tokens(text):
        target = root / token
        if "*" in token or "?" in token:
            if any(True for _ in root.glob(token)):
                continue
        elif target.exists():
            continue
        findings.append(Finding(
            str(path), index, f"`{token}`", "documented path does not exist",
        ))
    return findings


def check_env_example(
    path: Path, settings: dict[str, Setting], consumers: set[str]
) -> list[Finding]:
    """Every documented setting exists, and its example value fits its type."""
    findings: list[Finding] = []
    for number, key, value in env_example_entries(path):
        setting = settings.get(key)
        if setting is None:
            if key not in consumers:
                findings.append(Finding(
                    str(path), number, f"{key}=",
                    "not a Settings field and never read via os.getenv",
                ))
            continue
        if not _value_fits(setting.annotation, setting.literals, value):
            findings.append(Finding(
                str(path), number, f"{key}={value}",
                f"value does not fit {key}: {setting.annotation}",
            ))
    return findings


def undocumented_settings(
    path: Path, settings: dict[str, Setting], consumers: set[str]
) -> list[str]:
    """Settings fields with no active entry in the example file (``--strict``)."""
    documented = {key for _line, key, _value in env_example_entries(path)}
    return sorted(
        name for name in settings if name not in documented and name not in consumers
    )


def collect(root: Path, docs: Sequence[str], strict: bool = False) -> list[Finding]:
    """Every documented claim under *root* that the code contradicts."""
    config_path = root / CONFIG_FILE
    env_path = root / ENV_EXAMPLE
    missing = [str(p) for p in (config_path, env_path) if not p.is_file()]
    missing += [doc for doc in docs if not (root / doc).is_file()]
    if missing:
        raise FileNotFoundError(", ".join(missing))

    settings = load_settings(config_path)
    consumers = env_consumers(root)

    findings = check_env_example(env_path, settings, consumers)
    for doc in docs:
        doc_path = root / doc
        findings.extend(check_path_claims(root, doc_path))
        findings.extend(check_default_claims(doc_path, settings))
    if strict:
        for name in undocumented_settings(env_path, settings, consumers):
            findings.append(Finding(
                ENV_EXAMPLE, 0, name,
                "Settings field has no entry (AGENTS.md §4 requires one)",
            ))
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument(
        "--docs", nargs="*", default=list(DOC_FILES),
        help="documents whose claims are checked (default: the shipped docs)",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="also fail on Settings fields with no .env.example entry",
    )
    args = parser.parse_args(argv)

    try:
        findings = collect(args.root, args.docs, args.strict)
    except (OSError, SyntaxError, ValueError) as exc:
        print(f"error: cannot read the tree: {exc}", file=sys.stderr)
        return 2

    for finding in findings:
        print(f"::error file={finding.path},line={finding.line}::{finding.detail}")
        print(f"  {finding.render()}")

    if findings:
        print(f"\ndoc-claim gate failed: {len(findings)} claim(s) contradict the code.")
        print("Fix the document (or the code, if the document is right) — a claim "
              "that is not checked drifts back within a release.")
        return 1

    print("doc-claim gate passed: every documented path, setting and default "
          "matches the tree.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
