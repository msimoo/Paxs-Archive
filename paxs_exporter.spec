# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for standalone PAXS-MediaExporter.exe Studio.

Produces a lightweight, self-contained Windows application folder:
    dist/PAXS-MediaExporter/PAXS-MediaExporter.exe
    dist/PAXS-MediaExporter/microdicom-autorun/   (copied by build_media_exporter.bat)
"""

import os
from pathlib import Path

_ROOT = Path(globals().get("SPECPATH") or os.getcwd()).resolve()
_ICON = str(_ROOT / "logo" / "favicon.ico") if (_ROOT / "logo" / "favicon.ico").is_file() else None

_UI_DIR = _ROOT / "app" / "static" / "media-exporter"
_DATAS = [
    (str(p), "app/static/media-exporter")
    for p in sorted(_UI_DIR.rglob("*"))
    if p.is_file()
]

_HIDDEN = [
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    "clr",
    "clr_loader",
    "pythonnet",
    "pycdlib",
    "pydicom",
    "pydicom.fileset",
]

_EXCLUDES = [
    "tkinter",
    "matplotlib",
    "pytest",
    "IPython",
    "PyInstaller",
    "tests",
    "tools",
    "uvicorn",
    "fastapi",
    "starlette",
    "asyncpg",
    "psycopg",
    "psycopg_binary",
    "sqlalchemy",
    "alembic",
    "redis",
    "boto3",
    "botocore",
    "ldap3",
    "pynetdicom",
    "scipy",
    "PIL",
]

a = Analysis(  # noqa: F821
    [str(_ROOT / "paxs_media_exporter.py")],
    pathex=[str(_ROOT)],
    binaries=[],
    datas=_DATAS,
    hiddenimports=_HIDDEN,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=_EXCLUDES,
    noarchive=False,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PAXS-MediaExporter",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=_ICON,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="PAXS-MediaExporter",
)
