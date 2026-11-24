@echo off
setlocal enabledelayedexpansion
title PAXS Archive - Service Health Monitor & Manager

REM ============================================================
REM  PAXS Archive — Health Monitor & Service Manager
REM  Checks, reports status, and allows restarting:
REM    1. PAXS Archive Backend API (Port 8000)
REM    2. PAXS DICOM DIMSE SCP (Port 10400)
REM    3. Redis Server (Port 6379)
REM    4. PAXS Viewer / Study UI (Port 5174 or embedded :8000)
REM ============================================================

:menu
cls
echo ================================================================
echo           PAXS ARCHIVE SERVICE HEALTH & MANAGER TOOL
echo ================================================================
echo.
echo Checking active ports and service status...
echo ----------------------------------------------------------------

REM 1. Check PAXS HTTP / Dashboard (Port 8000)
netstat -ano | findstr /R /C:":8000 .*LISTENING" >nul
if "%ERRORLEVEL%"=="0" (
    echo [PAXS Backend / Dashboard :8000]       : RUNNING [ONLINE]
    set "STATUS_PAXS_API=RUNNING"
) else (
    echo [PAXS Backend / Dashboard :8000]       : STOPPED [OFFLINE]
    set "STATUS_PAXS_API=STOPPED"
)

REM 2. Check PAXS DICOM SCP (Port 10400)
netstat -ano | findstr /R /C:":10400 .*LISTENING" >nul
if "%ERRORLEVEL%"=="0" (
    echo [PAXS DICOM SCP Server :10400]        : LISTENING [ONLINE]
    set "STATUS_PAXS_DICOM=RUNNING"
) else (
    echo [PAXS DICOM SCP Server :10400]        : STOPPED [OFFLINE]
    set "STATUS_PAXS_DICOM=STOPPED"
)

REM 3. Check PostgreSQL (Port 5432)
netstat -ano | findstr /R /C:":5432 .*LISTENING" >nul
if "%ERRORLEVEL%"=="0" (
    echo [PostgreSQL Database :5432]           : LISTENING [ONLINE]
    set "STATUS_PG=RUNNING"
) else (
    echo [PostgreSQL Database :5432]           : NOT DETECTED (Local :5432)
    set "STATUS_PG=STOPPED"
)

REM 4. Check Redis (Port 6379)
netstat -ano | findstr /R /C:":6379 .*LISTENING" >nul
if "%ERRORLEVEL%"=="0" (
    echo [Redis In-Memory Cache :6379]         : RUNNING [ONLINE]
    set "STATUS_REDIS=RUNNING"
) else (
    echo [Redis In-Memory Cache :6379]         : NOT DETECTED (Optional/Offline)
    set "STATUS_REDIS=STOPPED"
)

REM 4. Check PAXS Viewer / Dev UI (Port 5174)
netstat -ano | findstr /R /C:":5174 .*LISTENING" >nul
if "%ERRORLEVEL%"=="0" (
    echo [PAXS Study List Dev UI :5174]        : RUNNING [ONLINE]
    set "STATUS_VIEWER=RUNNING"
) else (
    echo [PAXS Study List Dev UI :5174]        : STANDBY (Available at :8000)
    set "STATUS_VIEWER=STANDBY"
)

echo ----------------------------------------------------------------
echo.
echo Health Endpoints:
echo   - Liveness probe:  http://127.0.0.1:8000/api/v1/health/live
echo   - Web Dashboard:   http://localhost:8000
echo   - PaxsViewer:      http://localhost:8000/viewer/
echo.
echo ----------------------------------------------------------------
echo Choose an option:
echo [1] Quick Health Probe (HTTP curl test)
echo [2] Restart All PAXS Archive Services
echo [3] Restart PAXS Archive Backend / DICOM SCP Only
echo [4] Restart Redis Server
echo [5] Restart PAXS Viewer UI (Dev Server)
echo [6] Stop All PAXS Services
echo [7] Refresh Status
echo [8] Exit
echo ----------------------------------------------------------------
set /p "CHOICE=Select [1-8]: "

if "%CHOICE%"=="1" goto probe_health
if "%CHOICE%"=="2" goto restart_all
if "%CHOICE%"=="3" goto restart_backend
if "%CHOICE%"=="4" goto restart_redis
if "%CHOICE%"=="5" goto restart_viewer
if "%CHOICE%"=="6" goto stop_all
if "%CHOICE%"=="7" goto menu
if "%CHOICE%"=="8" exit /b 0

goto menu

:probe_health
echo.
echo Probing http://127.0.0.1:8000/api/v1/health/live ...
REM 0.0.0.0 is a listen address, not one a client can connect to, and /health
REM on :80 never existed - the old probe could only ever fail.
curl -s -f --max-time 10 http://127.0.0.1:8000/api/v1/health/live
if errorlevel 1 (
    echo [WARN] Server returned an error or is unreachable.
) else (
    echo.
    echo [OK] Backend is alive.
)
echo.
echo Press any key to continue...
pause >nul
goto menu

:restart_all
echo.
echo Stopping running PAXS processes...
call :stop_backend
taskkill /F /T /FI "WINDOWTITLE eq PAXS Viewer UI*" >nul 2>nul
taskkill /F /IM "redis-server.exe" >nul 2>nul
timeout /t 1 /nobreak >nul

echo Starting all services...
cd /d "%~dp0"
if exist "run_all.bat" (
    start "" "run_all.bat"
) else if exist "PAXS-Server.exe" (
    start "" "PAXS-Server.exe"
)
timeout /t 3 /nobreak >nul
goto menu

:restart_backend
echo.
echo Stopping PAXS Backend ^& DICOM receiver...
call :stop_backend

echo Starting PAXS Backend...
cd /d "%~dp0"
REM cmd /k keeps the window open on crash so the traceback stays readable.
REM Explicit venv target; no silent fallback to a global "python".
if exist "venv\Scripts\python.exe" (
    start "PAXS Archive Server" cmd /k venv\Scripts\python.exe run_paxs.py
) else if exist "PAXS-Server.exe" (
    start "PAXS Archive Server" PAXS-Server.exe
) else (
    echo [WARN] Neither venv\Scripts\python.exe nor PAXS-Server.exe found - cannot start backend.
)
timeout /t 2 /nobreak >nul
goto menu

:restart_redis
echo.
echo Restarting Redis...
taskkill /F /IM "redis-server.exe" >nul 2>nul
timeout /t 1 /nobreak >nul
where redis-server >nul 2>nul
if "%ERRORLEVEL%"=="0" (
    start "Redis Server" /min redis-server
    echo [OK] Redis restarted.
) else if exist ".tools\redis\redis-server.exe" (
    start "Redis Server" /min .tools\redis\redis-server.exe
    echo [OK] Local Redis restarted.
) else (
    echo [WARN] redis-server.exe not found on system.
)
timeout /t 2 /nobreak >nul
goto menu

:restart_viewer
echo.
echo Restarting PAXS Viewer Dev server...
taskkill /F /T /FI "WINDOWTITLE eq PAXS Viewer UI*" >nul 2>nul
timeout /t 1 /nobreak >nul
if exist "study-list-ui\package.json" (
    start "PAXS Viewer UI" cmd /k "cd /d study-list-ui && npm run dev"
    echo [OK] Viewer dev server restarted.
) else (
    echo [WARN] study-list-ui folder not found.
)
timeout /t 2 /nobreak >nul
goto menu

:stop_all
echo.
echo Stopping all PAXS related processes...
call :stop_backend
taskkill /F /T /FI "WINDOWTITLE eq PAXS Viewer UI*" >nul 2>nul
taskkill /F /IM "redis-server.exe" >nul 2>nul
echo [OK] All services stopped.
timeout /t 2 /nobreak >nul
goto menu

REM ------------------------------------------------------------------
REM :stop_backend - stop the PAXS server window AND its child processes,
REM then free :8000 / :10400 from any python.exe still bound to them.
REM A non-python program on those ports is reported, never killed.
REM ------------------------------------------------------------------
:stop_backend
REM Try graceful termination first so FastAPI lifespan can flush DB/DIMSE/workers.
taskkill /T /FI "WINDOWTITLE eq PAXS Archive Server*" >nul 2>nul
taskkill /T /IM "PAXS-Server.exe" >nul 2>nul
taskkill /T /IM "PAXS-Archive.exe" >nul 2>nul
for /L %%W in (1,1,5) do (
    netstat -ano | findstr /R /C:":8000 .*LISTENING" >nul
    if errorlevel 1 goto :backend_stopped
    timeout /t 1 /nobreak >nul
)
taskkill /F /T /FI "WINDOWTITLE eq PAXS Archive Server*" >nul 2>nul
taskkill /F /T /IM "PAXS-Server.exe" >nul 2>nul
taskkill /F /T /IM "PAXS-Archive.exe" >nul 2>nul
:backend_stopped
for %%Q in (8000 10400) do call :free_port %%Q
timeout /t 1 /nobreak >nul
exit /b 0

:free_port
set "FP_PID="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":%~1 .*LISTENING"') do (
    if not defined FP_PID set "FP_PID=%%P"
)
if not defined FP_PID exit /b 0
set "FP_IMAGE="
for /f "tokens=1 delims=," %%I in ('tasklist /FI "PID eq !FP_PID!" /FO CSV /NH') do set "FP_IMAGE=%%~I"
REM netstat can name a PID that has already exited: uvicorn's reloader binds
REM the socket, and when it dies its worker child keeps the port open under
REM the dead PID. Resolve that to the surviving python.exe child.
if defined FP_IMAGE if "!FP_IMAGE:~0,5!"=="INFO:" set "FP_IMAGE="
if not defined FP_IMAGE (
    for /f %%C in ('powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter 'ParentProcessId=!FP_PID!' | Where-Object Name -eq 'python.exe' | Select-Object -First 1 -ExpandProperty ProcessId"') do (
        set "FP_PID=%%C"
        set "FP_IMAGE=python.exe"
    )
)
set "IS_PAXS_PROC=0"
if /I "!FP_IMAGE!"=="python.exe" set "IS_PAXS_PROC=1"
if /I "!FP_IMAGE!"=="PAXS-Server.exe" set "IS_PAXS_PROC=1"
if /I "!FP_IMAGE!"=="PAXS-Archive.exe" set "IS_PAXS_PROC=1"
if "!IS_PAXS_PROC!"=="1" (
    echo [*] Stopping leftover PAXS process !FP_IMAGE! ^(PID !FP_PID!^) on port %~1
    taskkill /F /T /PID !FP_PID! >nul 2>nul
) else (
    echo [WARN] Port %~1 is held by !FP_IMAGE! ^(PID !FP_PID!^) - not a PAXS process, left running.
)
exit /b 0
