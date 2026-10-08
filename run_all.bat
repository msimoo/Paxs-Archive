@echo off
setlocal enableextensions enabledelayedexpansion
title PAXS Archive - Master Runner

REM ============================================================
REM  PAXS Archive - Master Project Runner
REM  Launches: Redis (optional), PAXS Server (Backend + DICOM SCP
REM  + embedded Dashboard/Viewer), and the Frontend Dev Server.
REM
REM  Fail-loud design: the venv is validated and an import + DB
REM  pre-flight runs IN THIS WINDOW before any detached window is
REM  spawned, so a startup error is visible here instead of
REM  flashing past in a child console that closes on crash.
REM ============================================================

cd /d "%~dp0"

REM Explicit venv interpreter (absolute, space-safe). No silent
REM fallback to a global "python" that may be the wrong version or
REM lack the project's dependencies.
set "PYTHON=%~dp0venv\Scripts\python.exe"

echo ============================================================
echo      PAXS ARCHIVE - STARTING ALL PROJECT SERVICES
echo ============================================================
echo.

REM --- 0. Validate the virtualenv -----------------------------
if not exist "%PYTHON%" (
    echo [FATAL] venv interpreter not found:
    echo         "%PYTHON%"
    echo.
    echo Create it first:
    echo     python -m venv venv
    echo     "%PYTHON%" -m pip install -r requirements.txt
    goto :die
)

REM --- 1. Pre-flight checks (run here so errors are readable) --
echo [pre-flight] Verifying application imports...
"%PYTHON%" -c "import app.main" || goto :preflight_import

echo [pre-flight] Verifying database is reachable and migrated...
"%PYTHON%" -c "import asyncio; from app.db.session import init_db; asyncio.run(init_db())" || goto :preflight_db
echo [pre-flight] OK.
echo.

REM --- 2. Redis (optional; standalone mode works without it) ---
echo [1/3] Checking Redis service...
tasklist /FI "IMAGENAME eq redis-server.exe" 2>nul | find /I "redis-server.exe" >nul && (
    echo [OK] Redis is already running.
) || (
    where redis-server >nul 2>&1 && (
        echo [*] Starting Redis from PATH...
        start "Redis Server" /min redis-server
    ) || (
        if exist ".tools\redis\redis-server.exe" (
            echo [*] Starting local bundled Redis...
            start "Redis Server" /min ".tools\redis\redis-server.exe"
        ) else (
            echo [!] Redis not found in PATH or .tools\redis - skipping ^(standalone mode^).
        )
    )
)

REM --- 3. PAXS Archive Server (FastAPI + DICOM SCP :10400) -----
REM cmd /k keeps the window open if the server exits, so a crash
REM leaves the traceback on screen. cwd was set above, so the
REM relative venv path resolves without space-quoting hazards.
echo.
echo [2/3] Starting PAXS Archive Server (Backend + DICOM SCP :10400)...
start "PAXS Archive Server" cmd /k venv\Scripts\python.exe run_paxs.py

REM --- 4. Viewer / Study List UI dev server (optional) --------
echo.
echo [3/3] Checking PAXS Viewer UI Dev Server...
if exist "study-list-ui\package.json" (
    echo [*] Launching PAXS Study List / Viewer UI ^(Vite dev server^)...
    start "PAXS Viewer UI" cmd /k "cd /d study-list-ui && npm run dev"
) else (
    echo [!] study-list-ui not found. Built-in viewer is served by PAXS Server on :8000.
)

echo.
echo ============================================================
echo   ALL SERVICES TRIGGERED
echo   - Backend API + DICOMweb : http://localhost:8000
echo   - Web Dashboard          : http://localhost:8000
echo   - Built-in PaxsViewer    : http://localhost:8000/viewer/
echo   - Study List UI (Dev)    : http://localhost:5174/studies-ui/
echo   - DICOM DIMSE SCP        : port 10400 (AE: PAXS_SCP)
echo ============================================================
echo.
echo Launching health monitor in 3 seconds...
timeout /t 3 /nobreak >nul
start "" "%~dp0health_monitor.bat"
exit /b 0

:preflight_import
echo.
echo [FATAL] "import app.main" failed - a Python dependency or a code error is
echo         blocking startup. Read the traceback printed above.
echo         Reinstall deps with: "%PYTHON%" -m pip install -r requirements.txt
goto :die

:preflight_db
echo.
echo [FATAL] Database initialization failed - read the traceback above.
echo         Common causes and fixes:
echo           * MySQL not running, or wrong DATABASE_URL in .env
echo           * schema not migrated:
echo               "%PYTHON%" -m alembic upgrade head
echo           * corrupted alembic_version (stamped but no tables):
echo               "%PYTHON%" -m alembic stamp base
echo               "%PYTHON%" -m alembic upgrade head
echo           * or use zero-setup SQLite in .env:
echo               DATABASE_URL=sqlite+aiosqlite:///./paxs.db
goto :die

:die
echo.
echo Startup aborted. Press any key to close...
pause >nul
exit /b 1
