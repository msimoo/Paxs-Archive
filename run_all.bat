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

REM --- 1. Ports: never start a second server on top of a running one ---
REM A second uvicorn cannot bind :8000 / :10400 and dies with
REM "address already in use" in its own window - which looked like
REM "run_all does not work". Detect the owner first.
set "SERVER_RUNNING="
call :port_owner 8000
if defined PORT_PID (
    if /I "!PORT_IMAGE!"=="python.exe" (
        set "SERVER_RUNNING=1"
        echo [OK] PAXS server already running on :8000 ^(PID !PORT_PID!^) - not starting a second copy.
        echo      To restart it use health_monitor.bat option [3].
    ) else (
        echo [FATAL] Port 8000 is used by another program: !PORT_IMAGE! ^(PID !PORT_PID!^).
        echo         Close it, or set PAXS_PORT to a free port.
        goto :die
    )
)
if not defined SERVER_RUNNING (
    call :port_owner 10400
    if defined PORT_PID (
        if /I "!PORT_IMAGE!"=="python.exe" (
            echo [WARN] A leftover PAXS process ^(PID !PORT_PID!^) still holds DICOM port 10400 - stopping it.
            taskkill /F /T /PID !PORT_PID! >nul 2>nul
        ) else (
            echo [FATAL] DICOM port 10400 is used by another program: !PORT_IMAGE! ^(PID !PORT_PID!^).
            goto :die
        )
    )
)
echo.

REM --- 2. Pre-flight checks (run here so errors are readable) --
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
            echo [WARN] Redis not found in PATH or .tools\redis - skipping ^(standalone mode^).
        )
    )
)

REM --- 3. PAXS Archive Server (FastAPI + DICOM SCP :10400) -----
REM cmd /k keeps the window open if the server exits, so a crash
REM leaves the traceback on screen. cwd was set above, so the
REM relative venv path resolves without space-quoting hazards.
echo.
if defined SERVER_RUNNING (
    echo [2/3] PAXS Archive Server already running - skipped.
) else (
    echo [2/3] Starting PAXS Archive Server ^(Backend + DICOM SCP :10400^)...
    start "PAXS Archive Server" cmd /k venv\Scripts\python.exe run_paxs.py
)

REM --- 4. Viewer / Study List UI dev server (optional) --------
echo.
echo [3/3] Checking PAXS Viewer UI Dev Server...
call :port_owner 5174
if defined PORT_PID (
    echo [OK] Study List UI dev server already running on :5174 ^(PID !PORT_PID!^).
) else if exist "study-list-ui\package.json" (
    echo [*] Launching PAXS Study List / Viewer UI ^(Vite dev server^)...
    start "PAXS Viewer UI" cmd /k "cd /d study-list-ui && npm run dev"
) else (
    echo [WARN] study-list-ui not found. Built-in viewer is served by PAXS Server on :8000.
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
echo           * PostgreSQL not running, or wrong DATABASE_URL in .env
echo           * role/database missing - create them in psql:
echo               CREATE ROLE paxs_user LOGIN PASSWORD '...';
echo               CREATE DATABASE paxs_db OWNER paxs_user ENCODING 'UTF8' TEMPLATE template0;
echo           * schema not migrated:
echo               "%PYTHON%" -m alembic upgrade head
echo           * "multiple heads" from alembic: a migration branched - merge it:
echo               "%PYTHON%" -m alembic heads
echo               "%PYTHON%" -m alembic merge -m "merge heads" heads
goto :die

REM --- helper: PORT_PID / PORT_IMAGE = process LISTENING on port %1 ---
:port_owner
set "PORT_PID="
set "PORT_IMAGE="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":%~1 .*LISTENING"') do (
    if not defined PORT_PID set "PORT_PID=%%P"
)
if defined PORT_PID (
    for /f "tokens=1 delims=," %%I in ('tasklist /FI "PID eq !PORT_PID!" /FO CSV /NH') do set "PORT_IMAGE=%%~I"
)
REM netstat can name a PID that has already exited: uvicorn's reloader binds
REM the socket, and when it dies its worker child keeps the port open under
REM the dead PID. Resolve that to the surviving python.exe child.
if defined PORT_IMAGE if "!PORT_IMAGE:~0,5!"=="INFO:" set "PORT_IMAGE="
if defined PORT_PID if not defined PORT_IMAGE (
    for /f %%C in ('powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter 'ParentProcessId=!PORT_PID!' | Where-Object Name -eq 'python.exe' | Select-Object -First 1 -ExpandProperty ProcessId"') do (
        set "PORT_PID=%%C"
        set "PORT_IMAGE=python.exe"
    )
)
exit /b 0

:die
echo.
echo Startup aborted. Press any key to close...
pause >nul
exit /b 1
