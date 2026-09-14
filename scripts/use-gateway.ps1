# PowerShell script to load a gateway provider from settings.json and run agy
param (
    [Parameter(Position=0)]
    [string]$Provider = "agentrouter",

    [Parameter(Position=1)]
    [string]$Model = ""
)

$settingsPath = Join-Path $PSScriptRoot "..\settings.json"

if (-not (Test-Path $settingsPath)) {
    Write-Error "settings.json not found at $settingsPath"
    exit 1
}

$config = Get-Content -Raw $settingsPath | ConvertFrom-Json
$availableProviders = $config.gatewayProviders.PSObject.Properties.Name

if (-not $config.gatewayProviders.$Provider) {
    Write-Host "Provider '$Provider' not found in settings.json!" -ForegroundColor Red
    Write-Host "Available providers: $($availableProviders -join ', ')" -ForegroundColor Yellow
    exit 1
}

$selected = $config.gatewayProviders.$Provider

Write-Host "=========================================" -ForegroundColor Cyan
Write-Host " Activating Gateway: $Provider" -ForegroundColor Green
Write-Host " Base URL: $($selected.baseUrl)" -ForegroundColor DarkGray
Write-Host "=========================================" -ForegroundColor Cyan

# Set environment variables for agy CLI (process and user persistent)
$baseUrl = $selected.baseUrl.TrimEnd('/') + "/v1beta"
$env:GOOGLE_GEMINI_BASE_URL = $baseUrl
$env:GEMINI_API_KEY = $selected.apiKey

[System.Environment]::SetEnvironmentVariable('GOOGLE_GEMINI_BASE_URL', $baseUrl, [System.EnvironmentVariableTarget]::User)
[System.Environment]::SetEnvironmentVariable('GEMINI_API_KEY', $selected.apiKey, [System.EnvironmentVariableTarget]::User)

# Update global ~/.gemini/antigravity-cli/settings.json to ensure modelProvider is gemini (without BOM)
$globalSettingsDir = [System.IO.Path]::Combine($env:USERPROFILE, ".gemini", "antigravity-cli")
$globalSettingsFile = Join-Path $globalSettingsDir "settings.json"

if (Test-Path $globalSettingsFile) {
    $globalConfig = Get-Content -Raw $globalSettingsFile | ConvertFrom-Json
    $globalConfig | Add-Member -NotePropertyName "modelProvider" -NotePropertyValue "gemini" -Force
    $jsonStr = $globalConfig | ConvertTo-Json -Depth 5
    $utf8NoBOM = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($globalSettingsFile, $jsonStr.Trim(), $utf8NoBOM)
}

# Launch agy
if ($Model) {
    Write-Host "Launching agy with model: $Model" -ForegroundColor Yellow
    agy --model $Model
} else {
    Write-Host "Launching agy..." -ForegroundColor Yellow
    agy
}
