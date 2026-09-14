@echo off
REM ============================================================
REM  PAXS Archive Server — install as a Windows Service (NSSM)
REM
REM  Prereqs:
REM   1. Build the exe first:  scripts\build_exe.bat
REM   2. Download NSSM (https://nssm.cc) and put nssm.exe on PATH
REM
REM  Run as Administrator:  scripts\install_service.bat
REM ============================================================
setlocal

set "APP_DIR=%~dp0..\dist\PAXS-Server"
set "APP_EXE=%APP_DIR%\PAXS-Server.exe"
set "SVC_NAME=PAXSServer"

if not exist "%APP_EXE%" (
  echo ERROR: %APP_EXE% not found. Run scripts\build_exe.bat first.
  exit /b 1
)

echo [1/4] Creating service '%SVC_NAME%'...
nssm install %SVC_NAME% "%APP_EXE%" || goto :err

echo [2/4] Setting working directory to %APP_DIR% (so .env / storage / paxs.db resolve)...
nssm set %SVC_NAME% AppDirectory "%APP_DIR%" || goto :err

echo [3/4] Auto-start on boot...
nssm set %SVC_NAME% Start SERVICE_AUTO_START || goto :err

echo [4/4] Starting service...
net start %SVC_NAME% || goto :err

echo.
echo Service %SVC_NAME% installed and running.
echo   Dashboard: http://0.0.0.0
echo   DICOM SCP: port 10400
echo   Uninstall:  nssm remove %SVC_NAME% confirm
echo.
exit /b 0

:err
echo.
echo Setup FAILED — check the error above.
exit /b 1
