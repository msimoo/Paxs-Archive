@echo off
setlocal
cd /d "%~dp0.."

echo ============================================================
echo   PAXS Archive - Official OFFIS DCMTK 3.7.1 Win64 Installer
echo ============================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop'; " ^
  "New-Item -ItemType Directory -Force -Path '.tools' | Out-Null; " ^
  "$zip='.tools\dcmtk-3.7.1-win64-dynamic.zip'; " ^
  "$url='https://dicom.offis.de/download/dcmtk/release/bin/dcmtk-3.7.1-win64-dynamic.zip'; " ^
  "Write-Host '[1/3] Downloading official OFFIS DCMTK 3.7.1 win64...'; " ^
  "Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $zip; " ^
  "Write-Host '[2/3] Extracting into .tools\dcmtk...'; " ^
  "if (Test-Path '.tools\dcmtk') { Remove-Item -Recurse -Force '.tools\dcmtk' }; " ^
  "Expand-Archive -Path $zip -DestinationPath '.tools\_dcmtk_extract' -Force; " ^
  "$inner = Get-ChildItem '.tools\_dcmtk_extract' -Directory | Select-Object -First 1; " ^
  "Move-Item $inner.FullName '.tools\dcmtk'; " ^
  "Remove-Item -Recurse -Force '.tools\_dcmtk_extract'; Remove-Item -Force $zip; " ^
  "Write-Host '[3/3] Verifying echoscu.exe...'; " ^
  "& '.tools\dcmtk\bin\echoscu.exe' --version | Select-Object -First 2" || exit /b 1

echo.
echo [OK] OFFIS DCMTK installed to .tools\dcmtk\bin
echo      Open http://localhost:8000/#admin ^> Services and click Auto-Detect DCMTK.
pause
exit /b 0
