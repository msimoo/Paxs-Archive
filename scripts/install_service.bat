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

if exist "%~dp0PAXS-Server.exe" (
  set "APP_DIR=%~dp0"
) else (
  set "APP_DIR=%~dp0..\dist\PAXS-Server"
)
if "%APP_DIR:~-1%"=="\" set "APP_DIR=%APP_DIR:~0,-1%"
set "APP_EXE=%APP_DIR%\PAXS-Server.exe"
set "SVC_NAME=PAXSServer"

if not exist "%APP_EXE%" (
  echo ERROR: %APP_EXE% not found. Run build_standalone.bat first.
  exit /b 1
)
if not exist "%APP_DIR%\logs" mkdir "%APP_DIR%\logs"

echo [1/5] Creating service '%SVC_NAME%'...
nssm install %SVC_NAME% "%APP_EXE%" || goto :err

echo [2/5] Setting working directory to %APP_DIR% (so .env / storage / logs resolve)...
nssm set %SVC_NAME% AppDirectory "%APP_DIR%" || goto :err

echo [3/5] Configuring log rotation (10 MB) and crash-restart policy...
nssm set %SVC_NAME% AppStdout "%APP_DIR%\logs\service-stdout.log" || goto :err
nssm set %SVC_NAME% AppStderr "%APP_DIR%\logs\service-stderr.log" || goto :err
nssm set %SVC_NAME% AppRotateFiles 1 || goto :err
nssm set %SVC_NAME% AppRotateOnline 1 || goto :err
nssm set %SVC_NAME% AppRotateBytes 10485760 || goto :err
nssm set %SVC_NAME% AppExit Default Restart || goto :err
nssm set %SVC_NAME% AppRestartDelay 5000 || goto :err
nssm set %SVC_NAME% AppStopMethodConsole 15000 || goto :err

echo [4/5] Auto-start on boot...
nssm set %SVC_NAME% Start SERVICE_AUTO_START || goto :err

echo [5/5] Starting service...
net start %SVC_NAME% || goto :err

echo.
echo Service %SVC_NAME% installed and running.
echo   Dashboard: http://localhost:8000
echo   DICOM SCP: port 10400
echo   Logs:      %APP_DIR%\logs\service-stdout.log
echo   Uninstall: nssm remove %SVC_NAME% confirm
echo.
exit /b 0

:err
echo.
echo Setup FAILED — check the error above.
exit /b 1
