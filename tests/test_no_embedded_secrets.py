"""Secret scanner — no private signing material in source control (C01).

``plan_9_2026.md`` C01: the vendor RSA private key was committed inside
``deploy/vendor-portal-php/config.php``. Removing it once is not enough — this
test fails if private-key material is ever committed again, and it verifies that
the operator-local key files that legitimately live on disk are covered by
``.gitignore``.

The scanner is intentionally self-checking: ``test_detector_flags_planted_marker``
proves the detection works, so the suite cannot pass because the scanner
silently matched nothing.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# PEM headers that must never appear in a tracked file.
#
# Assembled from fragments on purpose: this scanner is itself part of the
# scanned source tree, so writing the markers literally would make the scan
# flag its own definition list.
_PEM_OPEN = "-----" + "BEGIN "
_PEM_CLOSE = "-----"
_KEY_LABELS = (
    "PRIVATE KEY",
    "RSA PRIVATE KEY",
    "EC PRIVATE KEY",
    "OPENSSH PRIVATE KEY",
    "DSA PRIVATE KEY",
    "ENCRYPTED PRIVATE KEY",
)
_PRIVATE_KEY_MARKERS = tuple(f"{_PEM_OPEN}{label}{_PEM_CLOSE}" for label in _KEY_LABELS)

# Directories that are generated, vendored, or hold local runtime data.
_SKIP_DIRS = {
    ".git", "venv", ".venv", "node_modules", "dist", "build", "build-win",
    "__pycache__", ".pytest_cache", ".tools", ".freebuff", ".workbuddy-ai",
    "backups", "scratch", "certs", "outputs", "storage", "test_storage",
    "debug_outputs", ".idea", ".vscode",
}

# Binary / generated artefacts (the release-artefact scan is a separate step).
_SKIP_SUFFIXES = {
    ".pyc", ".pyo", ".db", ".sqlite", ".sqlite3", ".zip", ".pkg", ".exe",
    ".pem", ".key", ".crt", ".lic", ".png", ".jpg", ".jpeg", ".ico", ".gif",
    ".woff", ".woff2", ".ttf", ".eot", ".map", ".wasm", ".dcm", ".gz", ".whl",
    ".so", ".dll", ".pyd", ".bin", ".pdf", ".mp4",
}

# Windows reserved device names cannot be read as ordinary files.
_SKIP_NAMES = {"nul", "con", "prn", "aux", "com1", "lpt1"}

# Roots worth scanning for committed source.
_SCAN_DIRS = ("app", "deploy", "scripts", "tests", "migrations", "feature-plans")


def _iter_scannable_files():
    """Yield text files under the project that would be committed."""
    for name in _SCAN_DIRS:
        base = PROJECT_ROOT / name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            if path.suffix.lower() in _SKIP_SUFFIXES:
                continue
            if path.name.lower() in _SKIP_NAMES:
                continue
            yield path
    for path in sorted(PROJECT_ROOT.glob("*")):
        if (
            path.is_file()
            and path.suffix.lower() not in _SKIP_SUFFIXES
            and path.name.lower() not in _SKIP_NAMES
        ):
            yield path


def find_private_key_markers(text: str) -> list[str]:
    """PEM private-key headers present in ``text`` (case-sensitive)."""
    return [marker for marker in _PRIVATE_KEY_MARKERS if marker in text]


# --------------------------------------------------------------------------- #
# the scanner must be able to detect something
# --------------------------------------------------------------------------- #
def test_detector_flags_planted_marker():
    planted = f"{_PEM_OPEN}PRIVATE KEY{_PEM_CLOSE}"
    sample = (
        "<?php\n$key = <<<EOD\n"
        f"{planted}\nMIIabc\n-----END PRIVATE KEY-----\nEOD;\n"
    )
    assert find_private_key_markers(sample) == [planted]


def test_detector_ignores_the_public_key():
    assert find_private_key_markers(f"{_PEM_OPEN}PUBLIC KEY{_PEM_CLOSE}\nMIIabc\n") == []


# --------------------------------------------------------------------------- #
# the repository
# --------------------------------------------------------------------------- #
def test_no_private_key_material_in_source():
    offenders = []
    for path in _iter_scannable_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:  # pragma: no cover - unreadable file, not a secret
            continue
        markers = find_private_key_markers(text)
        if markers:
            offenders.append(f"{path.relative_to(PROJECT_ROOT)} ({', '.join(markers)})")
    assert not offenders, (
        "Private key material found in source control:\n  " + "\n  ".join(offenders)
    )


def test_vendor_portal_config_defines_no_private_key():
    """The specific regression from C01 must not come back."""
    config = (PROJECT_ROOT / "deploy" / "vendor-portal-php" / "config.php").read_text(
        encoding="utf-8", errors="ignore"
    )
    assert find_private_key_markers(config) == []
    # The constant must only be *referenced* by the resolver, never defined here.
    assert "define('PAXS_VENDOR_PRIVATE_KEY'" not in config
    assert 'define("PAXS_VENDOR_PRIVATE_KEY"' not in config


def test_crypto_signing_fails_closed_without_a_configured_key():
    """crypto.php must not fall back to a bundled constant."""
    crypto = (PROJECT_ROOT / "deploy" / "vendor-portal-php" / "crypto.php").read_text(
        encoding="utf-8", errors="ignore"
    )
    assert "paxs_vendor_private_key()" in crypto
    assert "$privateKeyPem ?: PAXS_VENDOR_PRIVATE_KEY" not in crypto


def test_operator_local_secret_files_are_gitignored():
    """.gitignore must cover every key/secret file that lives on disk."""
    gitignore_lines = [
        line.strip()
        for line in (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]

    def is_ignored(rel_path: str) -> bool:
        parts = rel_path.split("/")
        for pattern in gitignore_lines:
            if fnmatch.fnmatch(rel_path, pattern) or fnmatch.fnmatch(parts[-1], pattern):
                return True
            if pattern.endswith("/"):
                # directory rule: applies to anything beneath it
                if rel_path.startswith(pattern) or f"/{pattern}" in f"/{rel_path}":
                    return True
        return False

    on_disk_key_files = [
        path
        for base in ("scripts", "deploy", "certs")
        if (PROJECT_ROOT / base).is_dir()
        for path in (PROJECT_ROOT / base).rglob("*")
        if path.is_file()
        and path.suffix.lower() in {".pem", ".key"}
        and not any(part in _SKIP_DIRS for part in path.parts)
    ]

    unprotected = [
        str(path.relative_to(PROJECT_ROOT))
        for path in on_disk_key_files
        if not is_ignored(path.relative_to(PROJECT_ROOT).as_posix())
    ]
    assert not unprotected, (
        "Private key files on disk are NOT covered by .gitignore:\n  "
        + "\n  ".join(unprotected)
    )

    # The operator-local portal secrets added by C01 must be ignored too.
    assert is_ignored("deploy/vendor-portal-php/config.local.php")
    assert is_ignored("deploy/vendor-portal-php/vendor_private.pem")


@pytest.mark.parametrize(
    "relative_path",
    [
        "deploy/vendor-portal-php/config.php",
        "deploy/vendor-portal-php/crypto.php",
        "app/core/licensing/crypto.py",
    ],
)
def test_key_handling_files_exist_and_are_scanned(relative_path: str):
    path = PROJECT_ROOT / relative_path
    assert path.is_file(), f"{relative_path} is missing"
    assert find_private_key_markers(path.read_text(encoding="utf-8", errors="ignore")) == []
