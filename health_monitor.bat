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
netstat -ano | findstr /R /C:":8000 " >nul
if "%ERRORLEVEL%"=="0" (
    echo [PAXS Backend / Dashboard :8000]       : RUNNING [ONLINE]
    set "STATUS_PAXS_API=RUNNING"
) else (
    echo [PAXS Backend / Dashboard :8000]       : STOPPED [OFFLINE]
    set "STATUS_PAXS_API=STOPPED"
)

REM 2. Check PAXS DICOM SCP (Port 10400)
netstat -ano | findstr /R /C:":10400 " >nul
if "%ERRORLEVEL%"=="0" (
    echo [PAXS DICOM SCP Server :10400]        : LISTENING [ONLINE]
    set "STATUS_PAXS_DICOM=RUNNING"
) else (
    echo [PAXS DICOM SCP Server :10400]        : STOPPED [OFFLINE]
    set "STATUS_PAXS_DICOM=STOPPED"
)

REM 3. Check Redis (Port 6379)
netstat -ano | findstr /R /C:":6379 " >nul
if "%ERRORLEVEL%"=="0" (
    echo [Redis In-Memory Cache :6379]         : RUNNING [ONLINE]
    set "STATUS_REDIS=RUNNING"
) else (
    echo [Redis In-Memory Cache :6379]         : NOT DETECTED (Optional/Offline)
    set "STATUS_REDIS=STOPPED"
)

REM 4. Check PAXS Viewer / Dev UI (Port 5174)
netstat -ano | findstr /R /C:":5174 " >nul
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
echo   - Backend & DICOM: http://0.0.0.0/health
echo   - Web Dashboard:   http://0.0.0.0
echo   - PaxsViewer:      http://0.0.0.0/viewer/
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
echo Probing http://0.0.0.0/health ...
curl -s http://0.0.0.0/health 2>nul
if "%ERRORLEVEL%" neq "0" (
    echo [!] Server returned error or is unreachable.
)
echo.
echo Press any key to continue...
pause >nul
goto menu

:restart_all
echo.
echo Stopping running PAXS processes...
taskkill /F /FI "WINDOWTITLE eq PAXS Archive Server*" >nul 2>nul
taskkill /F /FI "WINDOWTITLE eq PAXS Viewer UI*" >nul 2>nul
taskkill /F /IM "PAXS-Server.exe" >nul 2>nul
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
echo Stopping PAXS Backend & DICOM receiver...
taskkill /F /FI "WINDOWTITLE eq PAXS Archive Server*" >nul 2>nul
taskkill /F /IM "PAXS-Server.exe" >nul 2>nul
timeout /t 1 /nobreak >nul

echo Starting PAXS Backend...
cd /d "%~dp0"
if exist "venv\Scripts\python.exe" (
    start "PAXS Archive Server" venv\Scripts\python.exe run_paxs.py
) else if exist "PAXS-Server.exe" (
    start "PAXS Archive Server" PAXS-Server.exe
) else (
    start "PAXS Archive Server" python run_paxs.py
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
    echo [!] redis-server.exe not found on system.
)
timeout /t 2 /nobreak >nul
goto menu

:restart_viewer
echo.
echo Restarting PAXS Viewer Dev server...
taskkill /F /FI "WINDOWTITLE eq PAXS Viewer UI*" >nul 2>nul
timeout /t 1 /nobreak >nul
if exist "study-list-ui\package.json" (
    start "PAXS Viewer UI" cmd /k "cd study-list-ui && npm run dev"
    echo [OK] Viewer dev server restarted.
) else (
    echo [!] study-list-ui folder not found.
)
timeout /t 2 /nobreak >nul
goto menu

:stop_all
echo.
echo Stopping all PAXS related processes...
taskkill /F /FI "WINDOWTITLE eq PAXS Archive Server*" >nul 2>nul
taskkill /F /FI "WINDOWTITLE eq PAXS Viewer UI*" >nul 2>nul
taskkill /F /IM "PAXS-Server.exe" >nul 2>nul
taskkill /F /IM "redis-server.exe" >nul 2>nul
echo [OK] All services stopped.
timeout /t 2 /nobreak >nul
goto menu
