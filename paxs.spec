# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for the standalone PAXS-Server distribution (D-14).

``scripts/build_exe.bat`` (and any Linux equivalent) runs
``pyinstaller --noconfirm paxs.spec``, so this file — not a command line — is
the single source of truth for what ends up inside ``dist/PAXS-Server``.

Two rules shape it.

**1. A frozen server must boot like the source tree.** ``app/main/app.py``
resolves its UI root as ``Path(__file__).parent.parent / "static"`` and
``db_admin`` resolves ``PROJECT_ROOT / "alembic.ini"``, whose
``script_location = migrations`` is *relative to the ini*. Both resolve
correctly under ``sys._MEIPASS`` only if the data is bundled at the same
repo-relative path, which is what :func:`collect_datas` emits. A build that
ships the Python code but not these files starts, serves a 404 for every UI and
fails its first migration — the failure mode that makes a "successful" frozen
build useless.

**2. PHI and secrets never enter the bundle.** The spec runs with the project
root as its working tree, so any relative glob is a candidate for packaging —
including the operator's ``.env`` (SECRET_KEY, DB and S3 credentials), a
development ``paxs.db`` that contains patient rows, DICOM TLS private keys, the
vendor license keys and the object store itself. Every collected file passes
through :func:`is_bundle_excluded`; ``.env.example`` is deliberately kept, so a
fresh install still has the documented template and no secrets.

This module is executed by PyInstaller with ``SPECPATH`` and the builder classes
(``Analysis``, ``PYZ``, ``EXE``, ``COLLECT``) injected into its namespace, and it
is exercised the same way by ``tests/test_packaging_spec.py`` — which is why the
collection rules are plain functions and not inline list literals.
"""

import fnmatch
import os
from pathlib import Path, PurePosixPath

# ``SPECPATH`` is injected by PyInstaller; the fallback keeps the file usable
# from a plain ``exec`` (the test harness) and from an interactive session.
_ROOT = Path(globals().get("SPECPATH") or os.getcwd()).resolve()

# One folder, three programs sharing the same libraries and data:
#   PAXS-Archive.exe  desktop window (resizable, F11 full screen) + server
#   PAXS-Server.exe   headless console server (Windows service, monitor)
#   PAXS-Worker.exe   dedicated multi-core compression/triage/routing worker
APP_NAME = "PAXS-Server"          # onedir folder name + headless exe
ENTRY_POINT = "run_paxs.py"
DESKTOP_NAME = "PAXS-Archive"
DESKTOP_ENTRY_POINT = "paxs_desktop.py"
WORKER_NAME = "PAXS-Worker"
WORKER_ENTRY_POINT = "paxs_worker.py"
ICON = "logo/favicon.ico"

# Directories/files shipped as data, relative to the project root.
#   app/static    every embedded UI (viewer, study list, admin, settings)
#   app/templates any server-rendered templates a deployment has added
#   migrations    the Alembic revision chain (script_location = migrations)
#   alembic.ini   the config db_admin passes to Alembic on first boot
#   .env.example  the documented template — NOT the operator's .env
BUNDLE_PATHS: tuple[str, ...] = (
    "app/static",
    "app/templates",
    "migrations",
    "alembic.ini",
    ".env.example",
)

# Template names are safe (no values); every other ``.env*`` file may carry
# real credentials and is refused.
_ENV_TEMPLATES = frozenset({".env.example", ".env.sample", ".env.template"})

# Matched against the file NAME (glob), against any single path PART (a literal
# name such as `storage` or `__pycache__`, which may sit at any depth), or
# against a run of parts (anything containing "/"). Kept as data so the test
# can assert the shipped rule rather than a copy of it.
_GLOB_CHARS = frozenset("*?[")
FORBIDDEN_BUNDLE_PATTERNS: tuple[str, ...] = (
    # Local databases (patient data, or the alembic bookkeeping of one).
    "*.db",
    "*.db-wal",
    "*.db-shm",
    "*.sqlite",
    "*.sqlite3",
    # Private key material and certificates that embed it.
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    # Trees that must never be packaged: vendor signing keys, the object store
    # (patient pixels), the agent's scratch/memory, VCS metadata, bytecode.
    "scripts/vendor_keys",
    "storage",
    ".workbuddy-ai",
    ".git",
    "__pycache__",
)


def is_bundle_excluded(rel_path: str) -> bool:
    """True when a repo-relative path must not be packaged.

    ``rel_path`` is the path *as it would appear inside the bundle*, so the
    check is independent of where the project happens to be checked out.
    """
    parts = PurePosixPath(str(rel_path).replace("\\", "/")).parts
    if not parts:
        return False
    lowered = [part.lower() for part in parts]
    name = lowered[-1]

    # `.env` and `.env.production` are secrets; `.env.example` is documentation.
    if name.startswith(".env") and name not in _ENV_TEMPLATES:
        return True

    for pattern in FORBIDDEN_BUNDLE_PATTERNS:
        if "/" in pattern:
            needle = pattern.strip("/").lower().split("/")
            width = len(needle)
            for start in range(len(lowered) - width + 1):
                if lowered[start:start + width] == needle:
                    return True
            continue
        if fnmatch.fnmatch(name, pattern):
            return True
        # A literal pattern also names a *directory* that may appear at any
        # depth (`storage/…`, `.workbuddy-ai/…`, `migrations/__pycache__/…`);
        # a glob pattern is matched against the file name only.
        if not (_GLOB_CHARS & set(pattern)) and pattern in lowered:
            return True
    return False


def collect_datas(root: Path) -> list[tuple[str, str]]:
    """``(source, dest_dir)`` pairs for every bundleable data file under *root*.

    Directories are walked file by file (rather than handed to PyInstaller as a
    directory) so the exclusion rule sees every individual path — a single
    ``.env`` or ``*.db`` inside a bundled tree is exactly the leak this exists
    to prevent.
    """
    datas: list[tuple[str, str]] = []
    for rel in BUNDLE_PATHS:
        source = root / rel
        if not source.exists():
            continue
        if not source.is_dir():
            if not is_bundle_excluded(rel):
                datas.append((str(source), PurePosixPath(rel).parent.as_posix()))
            continue
        for path in sorted(source.rglob("*")):
            if not path.is_file():
                continue
            rel_file = path.relative_to(root).as_posix()
            if is_bundle_excluded(rel_file):
                continue
            datas.append((str(path), PurePosixPath(rel_file).parent.as_posix()))
    return datas


# Modules that are selected at runtime (driver chosen by DATABASE_URL, uvicorn
# loop/protocol selected by configuration) and therefore invisible to the
# dependency graph. Without these a frozen build imports cleanly and fails on
# its first connection.
HIDDEN_IMPORTS: list[str] = [
    "asyncpg",
    "ldap3",
    "pyasn1",
    "psycopg",
    "psycopg_binary",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
]

# The desktop window: pywebview on Edge WebView2 through pythonnet (WinForms).
DESKTOP_HIDDEN_IMPORTS: list[str] = [
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    "clr",
    "clr_loader",
    "pythonnet",
]

# Build-time and development-only packages: packaging them inflates the bundle
# and, in the case of the test tooling, ships an interpreter hook that can run
# arbitrary test code inside the production process.
EXCLUDED_MODULES: list[str] = [
    "tkinter",
    "matplotlib",
    "pytest",
    "IPython",
    "PyInstaller",
    "tests",
    "tools",
]

_icon = str(_ROOT / ICON) if (_ROOT / ICON).is_file() else None


def _analysis(entry: str, hidden: list[str]):
    return Analysis(  # noqa: F821 - injected by PyInstaller, undefined outside it
        [str(_ROOT / entry)],
        pathex=[str(_ROOT)],
        binaries=[],
        datas=collect_datas(_ROOT),
        hiddenimports=hidden,
        hookspath=[],
        hooksconfig={},
        runtime_hooks=[],
        excludes=EXCLUDED_MODULES,
        noarchive=False,
    )


a = _analysis(ENTRY_POINT, HIDDEN_IMPORTS)
d = _analysis(DESKTOP_ENTRY_POINT, HIDDEN_IMPORTS + DESKTOP_HIDDEN_IMPORTS)
w = _analysis(WORKER_ENTRY_POINT, HIDDEN_IMPORTS)

exe = EXE(  # noqa: F821
    PYZ(a.pure),  # noqa: F821
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    icon=_icon,
)

desktop_exe = EXE(  # noqa: F821
    PYZ(d.pure),  # noqa: F821
    d.scripts,
    [],
    exclude_binaries=True,
    name=DESKTOP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # a window app: no console; logs go to logs\paxs-desktop.log
    disable_windowed_traceback=False,
    icon=_icon,
)

worker_exe = EXE(  # noqa: F821
    PYZ(w.pure),  # noqa: F821
    w.scripts,
    [],
    exclude_binaries=True,
    name=WORKER_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    icon=_icon,
)

# ``scripts/build_exe.bat`` copies ``.env.example`` into ``dist\PAXS-Server`` and
# creates ``storage\`` there, so the onedir name is a contract between the two
# files. All programs land in it; shared files are deduplicated by COLLECT.
coll = COLLECT(  # noqa: F821
    exe,
    desktop_exe,
    worker_exe,
    a.binaries,
    a.datas,
    d.binaries,
    d.datas,
    w.binaries,
    w.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
)
