@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ================================================================
echo         PAXS ARCHIVE - STANDALONE PRE-FLIGHT DIAGNOSTICS
echo ================================================================
echo.

set "FAIL=0"

REM 1. Check .env
if exist ".env" (
    echo [OK]   .env configuration file present.
) else (
    echo [WARN] .env not found. Copy .env.example to .env and configure DATABASE_URL + SECRET_KEY.
    set "FAIL=1"
)

REM 2. Check PostgreSQL port 5432
netstat -ano | findstr /R /C:":5432 .*LISTENING" >nul
if "%ERRORLEVEL%"=="0" (
    echo [OK]   Local PostgreSQL detected listening on :5432.
) else (
    echo [INFO] No local PostgreSQL on :5432 (OK if DATABASE_URL points to a remote PostgreSQL server).
)

REM 3. Check free disk space on storage drive
if not exist "storage" mkdir "storage"
for /f "usebackq delims=" %%G in (`powershell -NoProfile -Command "[math]::Round((Get-PSDrive -Name (Resolve-Path 'storage').Drive.Name).Free / 1GB, 2)"`) do set "FREE_GB=%%G"
echo [INFO] Free space on storage drive: !FREE_GB! GB

REM 4. Check Microsoft Edge WebView2 Runtime
reg query "HKLM\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" >nul 2>nul
if "%ERRORLEVEL%"=="0" (
    echo [OK]   Microsoft Edge WebView2 Runtime detected.
) else (
    echo [WARN] Edge WebView2 Runtime not found in HKLM; PAXS-Archive.exe will fall back to browser mode.
)

REM 5. Check lossless codecs via PAXS-Worker.exe
echo.
if exist "PAXS-Worker.exe" (
    echo Checking DICOM lossless codecs via PAXS-Worker.exe...
    PAXS-Worker.exe --check-codecs
) else if exist "venv\Scripts\python.exe" (
    venv\Scripts\python.exe -m app.services.worker --check-codecs
) else (
    echo [WARN] PAXS-Worker.exe not found; skipping codec check.
)

echo.
if "!FAIL!"=="0" (
    echo [READY] Pre-flight diagnostics finished.
) else (
    echo [ACTION REQUIRED] Complete the [WARN] items above before launching PAXS Archive.
)
echo.
pause
exit /b !FAIL!
