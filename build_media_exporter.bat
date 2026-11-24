@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo   BUILDING STANDALONE PAXS-MediaExporter.exe STUDIO
echo ============================================================

set "PYTHON_EXE=python"
if exist "venv\Scripts\python.exe" set "PYTHON_EXE=venv\Scripts\python.exe"
if exist ".venv\Scripts\python.exe" set "PYTHON_EXE=.venv\Scripts\python.exe"

if not exist "build\.tmp" mkdir "build\.tmp"
set "TEMP=%~dp0build\.tmp"
set "TMP=%~dp0build\.tmp"

echo [1/3] Compiling PAXS-MediaExporter.exe via PyInstaller...
"%PYTHON_EXE%" -m PyInstaller --noconfirm paxs_exporter.spec || goto :err

set "DIST_DIR=dist\PAXS-MediaExporter"
echo [2/3] Bundling MicroDicom 2026.5 Autorun viewer...
if exist ".tools\microdicom-autorun\extracted\x64\mDicom.exe" (
    if exist "%DIST_DIR%\microdicom-autorun" rmdir /s /q "%DIST_DIR%\microdicom-autorun"
    xcopy /e /i /q /y ".tools\microdicom-autorun\extracted" "%DIST_DIR%\microdicom-autorun" >nul
    echo [OK] MicroDicom 2026.5 Autorun bundled into %DIST_DIR%\microdicom-autorun
) else (
    echo [WARN] .tools\microdicom-autorun\extracted not found.
)

(
    echo @echo off
    echo cd /d "%%~dp0"
    echo "PAXS-MediaExporter.exe" --register-protocol
    echo echo Protocol paxs-exporter:// registered for current Windows user.
    echo pause
) > "%DIST_DIR%\register_protocol.bat"

echo [3/3] Done! Output: %DIST_DIR%\PAXS-MediaExporter.exe
exit /b 0

:err
echo [!] Build failed.
exit /b 1
