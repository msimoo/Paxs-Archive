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
echo [1/5] Building PAXS Viewer / Study List frontend assets...
if exist "study-list-ui\package.json" (
    pushd study-list-ui
    if not exist "node_modules" (
        echo [*] Installing UI node dependencies...
        call npm install
    )
    echo [*] Compiling Vite build...
    call npm run build
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
%PIP_EXE% install --quiet pyinstaller || (
    echo [!] Failed to install or verify PyInstaller.
    goto :err
)

REM Step 3: Run PyInstaller
echo.
echo [3/5] Compiling PAXS-Server executable bundle via PyInstaller...
%PYINSTALLER_EXE% --noconfirm paxs.spec || goto :err

REM Step 4: Package runtime support files
echo.
echo [4/5] Preparing standalone distribution folder (dist\PAXS-Server)...
set "DIST_DIR=dist\PAXS-Server"

if not exist "%DIST_DIR%\storage" mkdir "%DIST_DIR%\storage"
if not exist "%DIST_DIR%\backups" mkdir "%DIST_DIR%\backups"
if not exist "%DIST_DIR%\certs" mkdir "%DIST_DIR%\certs"

if exist ".env.example" copy /y ".env.example" "%DIST_DIR%\.env.example" >nul
if exist ".env" (
    copy /y ".env" "%DIST_DIR%\.env" >nul
    echo [*] Copied existing .env configuration.
) else (
    if exist ".env.example" copy /y ".env.example" "%DIST_DIR%\.env" >nul
    echo [*] Created default .env from .env.example.
)

REM Copy health monitor & management scripts into standalone distribution
copy /y "health_monitor.bat" "%DIST_DIR%\" >nul 2>nul
copy /y "scripts\install_service.bat" "%DIST_DIR%\" >nul 2>nul

REM Create starter launcher in distribution folder
(
    echo @echo off
    echo title PAXS Standalone Server
    echo cd /d "%%~dp0"
    echo echo Starting PAXS Archive Standalone Engine...
    echo start "" "PAXS-Server.exe"
    echo timeout /t 2 /nobreak ^>nul
    echo start "" "health_monitor.bat"
    echo exit
) > "%DIST_DIR%\start_paxs.bat"

echo.
echo [5/5] Packaging Finished Successfully!
echo ============================================================
echo   DISTRIBUTION DIRECTORY:  dist\PAXS-Server
echo   STANDALONE EXECUTABLE:   dist\PAXS-Server\PAXS-Server.exe
echo   STANDALONE LAUNCHER:     dist\PAXS-Server\start_paxs.bat
echo ============================================================
echo.
echo You can move the entire 'dist\PAXS-Server' directory to any
echo Windows machine without needing Python, Node.js or Git installed!
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
