@echo off
setlocal enabledelayedexpansion
title PAXS Archive - Complete Standalone Builder

REM ============================================================
REM  PAXS Archive — Standalone Windows Application Builder
REM  Builds:
REM   1. PAXS Viewer / Study List Web Assets
REM   2. Standalone Python Backend & DICOM SCP Binary (PAXS-Server.exe)
REM   3. Complete Standalone Distribution Package with Health & Service scripts
REM ============================================================

cd /d "%~dp0"

echo ============================================================
echo   BUILDING STANDALONE WINDOWS PAXS ARCHIVE SYSTEM
echo ============================================================
echo.

REM Verify Python environment
if exist "venv\Scripts\python.exe" (
    set "PYTHON_EXE=venv\Scripts\python.exe"
    set "PIP_EXE=venv\Scripts\pip.exe"
    set "PYINSTALLER_EXE=venv\Scripts\pyinstaller.exe"
) else (
    set "PYTHON_EXE=python"
    set "PIP_EXE=pip"
    set "PYINSTALLER_EXE=pyinstaller"
)

REM Step 1: Build Frontend UI if available
echo [1/5] Study List frontend assets...
REM Opt-in: the deployed app\static\studies-ui\index.html carries hand-added
REM script tags (study-list enhancer, thumbnail engine) that the Vite source does
REM not have, so rebuilding by default would silently strip them.
REM Run "build_standalone.bat rebuild-ui" only after porting those tags.
if /I not "%~1"=="rebuild-ui" (
    echo [i] Using the existing app\static\studies-ui build ^(pass rebuild-ui to rebuild^).
) else if exist "study-list-ui\package.json" (
    pushd study-list-ui
    if not exist "node_modules" (
        echo [*] Installing UI node dependencies...
        call npm install || goto :err
    )
    echo [*] Compiling Vite build...
    call npm run build || goto :err
    popd
    
    if exist "study-list-ui\dist" (
        echo [*] Copying frontend build artifacts into app\static\studies-ui...
        if not exist "app\static\studies-ui" mkdir "app\static\studies-ui"
        xcopy /E /I /Y "study-list-ui\dist\*" "app\static\studies-ui\" >nul
        echo [OK] Frontend bundled into backend static directory.
    )
) else (
    echo [i] study-list-ui not present; relying on existing app/static assets.
)

REM Step 2: Ensure PyInstaller and requirements
echo.
echo [2/5] Checking Python build tools...
%PIP_EXE% install --quiet pyinstaller pywebview==6.2.1 || (
    echo [!] Failed to install or verify PyInstaller.
    goto :err
)

REM Step 3: Run PyInstaller
echo.
echo [3/5] Compiling PAXS-Archive.exe + PAXS-Server.exe via PyInstaller...
if not exist "build" mkdir "build"
if not exist "build\.tmp" mkdir "build\.tmp"
set "TEMP=%~dp0build\.tmp"
set "TMP=%~dp0build\.tmp"
set "KEEP_ENV=0"
if /I "%~1"=="--keep-env" set "KEEP_ENV=1"
if "%KEEP_ENV%"=="1" if exist "dist\PAXS-Server\.env" copy /y "dist\PAXS-Server\.env" "build\.standalone.env.backup" >nul
%PYTHON_EXE% -m PyInstaller --noconfirm paxs.spec || goto :err

REM Step 4: Package runtime support files
echo.
echo [4/5] Preparing standalone distribution folder (dist\PAXS-Server)...
set "DIST_DIR=dist\PAXS-Server"

if not exist "%DIST_DIR%\storage" mkdir "%DIST_DIR%\storage"
if not exist "%DIST_DIR%\backups" mkdir "%DIST_DIR%\backups"
if not exist "%DIST_DIR%\certs" mkdir "%DIST_DIR%\certs"
if not exist "%DIST_DIR%\logs" mkdir "%DIST_DIR%\logs"

if exist ".env.example" copy /y ".env.example" "%DIST_DIR%\.env.example" >nul
REM Never leak the developer's local .env into a distributable bundle.
REM Pass --keep-env for local workstation rebuilds that should preserve dist\.env.
if "%KEEP_ENV%"=="1" (
    if exist "build\.standalone.env.backup" copy /y "build\.standalone.env.backup" "%DIST_DIR%\.env" >nul
) else (
    if exist "%DIST_DIR%\.env" del /q "%DIST_DIR%\.env" >nul 2>nul
)
if exist "build\.standalone.env.backup" del /q "build\.standalone.env.backup" >nul 2>nul
if exist "%DIST_DIR%\.env" (
    echo [WARN] %DIST_DIR%\.env is present ^(--keep-env^). Remove it before shipping to another machine.
) else (
    echo [*] Clean distribution: copy .env.example to .env on the target machine and set DATABASE_URL + SECRET_KEY.
)
if exist "%DIST_DIR%\paxs.lic" (
    echo [WARN] %DIST_DIR%\paxs.lic is present. Remove machine-specific license files before shipping.
)

REM Copy health monitor, preflight & management scripts into standalone distribution
copy /y "health_monitor.bat" "%DIST_DIR%\" >nul 2>nul
copy /y "scripts\install_service.bat" "%DIST_DIR%\" >nul 2>nul
copy /y "scripts\preflight_check.bat" "%DIST_DIR%\" >nul 2>nul
if exist ".tools\dcmtk\bin\echoscu.exe" (
    echo [*] Bundling native OFFIS DCMTK into %DIST_DIR%\dcmtk...
    if exist "%DIST_DIR%\dcmtk" rmdir /s /q "%DIST_DIR%\dcmtk"
    xcopy /e /i /q /y ".tools\dcmtk" "%DIST_DIR%\dcmtk" >nul
)
if exist "dist\PAXS-MediaExporter\PAXS-MediaExporter.exe" (
    echo [*] Bundling PAXS-MediaExporter Studio + MicroDicom 2026.5 into %DIST_DIR%\PAXS-MediaExporter...
    if exist "%DIST_DIR%\PAXS-MediaExporter" rmdir /s /q "%DIST_DIR%\PAXS-MediaExporter"
    xcopy /e /i /q /y "dist\PAXS-MediaExporter" "%DIST_DIR%\PAXS-MediaExporter" >nul
)

REM Launchers: the desktop window (windowed / full screen) and the headless
REM server with its health monitor.
(
    echo @echo off
    echo cd /d "%%~dp0"
    echo start "" "PAXS-Archive.exe" --fullscreen
) > "%DIST_DIR%\PAXS-Archive-fullscreen.bat"
(
    echo @echo off
    echo title PAXS Standalone Server
    echo cd /d "%%~dp0"
    echo echo Starting PAXS Archive headless server...
    echo start "PAXS Archive Server" "PAXS-Server.exe"
    echo timeout /t 2 /nobreak ^>nul
    echo start "" "health_monitor.bat"
    echo exit
) > "%DIST_DIR%\start_paxs_server.bat"

echo.
echo [5/5] Packaging Finished Successfully!
echo ============================================================
echo   DISTRIBUTION DIRECTORY:  dist\PAXS-Server
echo   DESKTOP APP:             dist\PAXS-Server\PAXS-Archive.exe
echo                            ^(resizable window, F11 = full screen^)
echo   FULL-SCREEN LAUNCHER:    dist\PAXS-Server\PAXS-Archive-fullscreen.bat
echo   HEADLESS SERVER:         dist\PAXS-Server\PAXS-Server.exe
echo   STANDALONE WORKER:       dist\PAXS-Server\PAXS-Worker.exe
echo   SERVER + MONITOR:        dist\PAXS-Server\start_paxs_server.bat
echo ============================================================
echo.
echo Copy the whole 'dist\PAXS-Server' folder to the target machine - no
echo Python, Node.js or Git needed. It needs PostgreSQL reachable at the
echo DATABASE_URL in its .env, and Microsoft Edge WebView2 for the window
echo ^(built into Windows 10/11^).
echo.
pause
exit /b 0

:err
echo.
echo ============================================================
echo [ERROR] Build process failed. Please inspect the output above.
echo ============================================================
pause
exit /b 1
