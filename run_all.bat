@echo off
setlocal enabledelayedexpansion
title PAXS Archive - Master Runner

REM ============================================================
REM  PAXS Archive — Master Project Runner
REM  Launches Services: Redis (if installed/local), PAXS Server (Backend + DICOM SCP + Embedded Dashboard/Viewer), and Frontend Dev Server
REM ============================================================

cd /d "%~dp0"

echo ============================================================
echo      PAXS ARCHIVE - STARTING ALL PROJECT SERVICES
echo ============================================================
echo.

REM 1. Check Python virtualenv
if exist "venv\Scripts\python.exe" (
    set "PYTHON_EXE=venv\Scripts\python.exe"
) else (
    set "PYTHON_EXE=python"
)

REM 2. Start Redis if available
echo [1/3] Checking Redis service...
tasklist /FI "IMAGENAME eq redis-server.exe" 2>NUL | find /I /N "redis-server.exe">NUL
if "%ERRORLEVEL%"=="0" (
    echo [OK] Redis is already running.
) else (
    where redis-server >nul 2>nul
    if "%ERRORLEVEL%"=="0" (
        echo [*] Starting Redis in background...
        start "Redis Server" /min redis-server
    ) else (
        if exist ".tools\redis\redis-server.exe" (
            echo [*] Starting local bundled Redis...
            start "Redis Server" /min .tools\redis\redis-server.exe
        ) else (
            echo [!] Redis executable not found in PATH or .tools\redis. (Skipping Redis - standalone mode)
        )
    )
)

REM 3. Start PAXS Archive Server (FastAPI + DICOM Receiver :10400)
echo.
echo [2/3] Starting PAXS Archive Server (Backend + DICOM SCP :10400)...
start "PAXS Archive Server" %PYTHON_EXE% run_paxs.py

REM 4. Start PAXS Viewer / Study List UI Dev Server (optional frontend dev server)
echo.
echo [3/3] Checking PAXS Viewer UI Dev Server...
if exist "study-list-ui\package.json" (
    echo [*] Launching PAXS Study List / Viewer UI (Vite dev server)...
    start "PAXS Viewer UI" cmd /k "cd study-list-ui && npm run dev"
) else (
    echo [!] study-list-ui folder not found. Built-in viewer will be served via PAXS Server (:8000).
)

echo.
echo ============================================================
echo   ALL SERVICES TRIGGERED!
echo   - Backend API + DICOMweb:   http://0.0.0.0
echo   - Web Dashboard:            http://0.0.0.0
echo   - Built-in PaxsViewer:      http://0.0.0.0/viewer/
echo   - Study List UI (Dev):      http://localhost:5174/studies-ui/
echo   - DICOM DIMSE SCP:          Port 10400 (AE: PAXS_SCP)
echo ============================================================
echo.
echo Launching health monitor in 3 seconds...
timeout /t 3 /nobreak >nul
start "" "%~dp0health_monitor.bat"

exit /b 0
