@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo   PAXS Archive - Windows Setup Installer Builder (Inno Setup)
echo ============================================================

if not exist "..\dist\PAXS-Server\PAXS-Archive.exe" (
    echo [ERROR] ..\dist\PAXS-Server\PAXS-Archive.exe not found.
    echo         Run build_standalone.bat first.
    exit /b 1
)

set "ISCC="
where iscc >nul 2>nul && set "ISCC=iscc"
if not defined ISCC if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"

if not defined ISCC (
    echo [ERROR] Inno Setup 6 (ISCC.exe) was not found.
    echo         Install from https://jrsoftware.org/isdl.php and re-run.
    exit /b 1
)

if not exist "output" mkdir "output"
"%ISCC%" "paxs_archive_setup.iss" || exit /b 1

echo.
echo [OK] Installer built inside installer\output\
exit /b 0
