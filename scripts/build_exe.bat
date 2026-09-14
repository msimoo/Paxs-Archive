@echo off
REM ============================================================
REM  PAXS Archive Server — Windows EXE build (Phase 5)
REM  Run from the project root:  scripts\build_exe.bat
REM ============================================================
setlocal

cd /d "%~dp0.."

echo [1/4] Installing build dependencies...
python -m pip install --quiet pyinstaller || goto :err

echo [2/4] Building EXE with PyInstaller...
pyinstaller --noconfirm paxs.spec || goto :err

echo [3/4] Copying runtime extras into dist...
if exist .env.example copy /y .env.example dist\PAXS-Server\.env.example >nul
if not exist dist\PAXS-Server\storage mkdir dist\PAXS-Server\storage

echo [4/4] Done!
echo.
echo   Output:  dist\PAXS-Server\PAXS-Server.exe
echo   Deploy:  copy the whole dist\PAXS-Server folder to the target machine
echo            (no Python install needed), then run PAXS-Server.exe.
echo.
exit /b 0

:err
echo.
echo Build FAILED — check the error above.
exit /b 1
