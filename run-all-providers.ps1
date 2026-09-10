# PowerShell script to launch the Antigravity Unified Gateway and start agy CLI with ALL models from all providers
param (
    [Parameter(Position=0)]
    [string]$Model = ""
)

$scriptDir = $PSScriptRoot
$serverScript = Join-Path $scriptDir "scripts\unified_gateway_server.py"

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "  Antigravity Unified Multi-Provider Gateway              " -ForegroundColor Green
Write-Host "==========================================================" -ForegroundColor Cyan

# Check if proxy server is already running on port 8899
$isRunning = $false
try {
    $testResp = Invoke-RestMethod -Uri "http://127.0.0.1:8899/health" -TimeoutSec 1 -ErrorAction SilentlyContinue
    if ($testResp.status -eq "healthy") {
        $isRunning = $true
    }
} catch {
    $isRunning = $false
}

if (-not $isRunning) {
    Write-Host "Starting Unified Gateway Proxy on http://127.0.0.1:8899..." -ForegroundColor Yellow
    Start-Process -FilePath "python" -ArgumentList """" -WindowStyle Hidden
    Start-Sleep -Seconds 2
} else {
    Write-Host "Unified Gateway Proxy is active on http://127.0.0.1:8899." -ForegroundColor Green
}

# Fetch active models count
try {
    $modelsResp = Invoke-RestMethod -Uri "http://127.0.0.1:8899/v1beta/models" -TimeoutSec 5
    $count = $modelsResp.models.Count
    Write-Host "Loaded $count models across all providers in settings.json!" -ForegroundColor Cyan
} catch {
    Write-Host "Gateway starting..." -ForegroundColor Gray
}

# Configure agy environment variables
$env:GOOGLE_GEMINI_BASE_URL = "http://127.0.0.1:8899/v1beta"
$env:GEMINI_API_KEY = "sk-unified-gateway"

# Ensure global settings has modelProvider: gemini (UTF-8 without BOM)
$globalSettingsDir = [System.IO.Path]::Combine($env:USERPROFILE, ".gemini", "antigravity-cli")
$globalSettingsFile = Join-Path $globalSettingsDir "settings.json"

if (Test-Path $globalSettingsFile) {
    $globalConfig = Get-Content -Raw $globalSettingsFile | ConvertFrom-Json
    $globalConfig | Add-Member -NotePropertyName "modelProvider" -NotePropertyValue "gemini" -Force
    $jsonStr = $globalConfig | ConvertTo-Json -Depth 5
    $utf8NoBOM = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($globalSettingsFile, $jsonStr.Trim(), $utf8NoBOM)
}

Write-Host "Environment configured: GOOGLE_GEMINI_BASE_URL=http://127.0.0.1:8899/v1beta" -ForegroundColor DarkGray
Write-Host "----------------------------------------------------------" -ForegroundColor Cyan

# Launch agy
if ($Model) {
    Write-Host "Starting agy with model: $Model" -ForegroundColor Green
    agy --model $Model
} else {
    Write-Host "Starting agy CLI (Type /model inside to see all models)..." -ForegroundColor Green
    agy
}
