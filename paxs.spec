# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — PAXS Archive Server (Phase 5: Windows distribution).

Build (from project root, after `pip install pyinstaller`):

    pyinstaller --noconfirm paxs.spec

Output lands in dist/PAXS-Server/. The app reads .env, storage/, paxs.db
relative to the exe directory (run_paxs.py resolves its base dir).
"""

from pathlib import Path

project_root = Path(SPECPATH).resolve()
static_dir = project_root / "app" / "static"

a = Analysis(
    ["run_paxs.py"],
    pathex=[str(project_root)],
    binaries=[],
    datas=[
        (str(static_dir), "app/static"),        # web dashboard + viewer
        (str(project_root / "migrations"), "migrations"),  # alembic versions
        (str(project_root / "alembic.ini"), "."),
        (str(project_root / ".env.example"), "."),
    ],
    hiddenimports=[
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
        "pydicom.uid",
        "pydicom.pixel_data_handlers.numpy_handler",
        "pynetdicom._handlers",
        "sqlalchemy.dialects.sqlite.aiosqlite",
        "sqlalchemy.dialects.mysql.pymysql",
        "sqlalchemy.dialects.mysql.aiomysql",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "pytest", "tests"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PAXS-Server",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,          # hospital staff see server logs in the console
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="PAXS-Server",
)
