param(
    [string]$Profile = "autoreview",
    [string]$GatewayToken = "autoreview-local-token",
    [string]$ProxyUrl = "http://127.0.0.1:7897"
)

$ErrorActionPreference = "Stop"

$ProjectRoot = $PSScriptRoot
$PidFile = Join-Path $ProjectRoot "data\openclaw_gateway.pid"
$LogDir = Join-Path $ProjectRoot "logs"
$StdoutLog = Join-Path $LogDir "openclaw-gateway.out.log"
$StderrLog = Join-Path $LogDir "openclaw-gateway.err.log"
$PowerShellExe = (Get-Process -Id $PID).Path

function Quote-PowerShellArgument([string]$Value) {
    return "'" + $Value.Replace("'", "''") + "'"
}

function Get-GatewayProcesses {
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -and
            $_.CommandLine -match "openclaw" -and
            $_.CommandLine -match "\bgateway\s+run\b" -and
            $_.CommandLine -match [regex]::Escape("--profile $Profile")
        }
}

if (Test-Path -LiteralPath $PidFile) {
    $ExistingPid = (Get-Content -LiteralPath $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($ExistingPid) {
        $ExistingProcess = Get-Process -Id $ExistingPid -ErrorAction SilentlyContinue
        if ($ExistingProcess) {
            Write-Host "OpenClaw Gateway is already running. PID: $ExistingPid"
            Write-Host "Logs: $StdoutLog"
            exit 0
        }
    }
}

$ExistingGatewayProcesses = @(Get-GatewayProcesses)
if ($ExistingGatewayProcesses.Count -gt 0) {
    Write-Host "OpenClaw Gateway is already running. PID(s): $($ExistingGatewayProcesses.ProcessId -join ', ')"
    Write-Host "Logs: $StdoutLog"
    exit 0
}

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $PidFile) | Out-Null
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$Command = @(
    '$ErrorActionPreference = "Stop"',
    '$env:PYTHONUNBUFFERED = "1"',
    '$env:HTTPS_PROXY = ' + (Quote-PowerShellArgument $ProxyUrl),
    '$env:HTTP_PROXY = ' + (Quote-PowerShellArgument $ProxyUrl),
    '$env:ALL_PROXY = ' + (Quote-PowerShellArgument $ProxyUrl),
    '$env:NO_PROXY = ' + (Quote-PowerShellArgument "localhost,127.0.0.1"),
    '$env:https_proxy = $env:HTTPS_PROXY',
    '$env:http_proxy = $env:HTTP_PROXY',
    '$env:all_proxy = $env:ALL_PROXY',
    '$env:no_proxy = $env:NO_PROXY',
    '$env:OPENCLAW_GATEWAY_TOKEN = ' + (Quote-PowerShellArgument $GatewayToken),
    '$CodexExe = (Get-ChildItem "$env:APPDATA\npm\node_modules\@openai\codex" -Recurse -Filter codex.exe | Select-Object -First 1).FullName',
    'if (-not $CodexExe) { throw "codex.exe not found under $env:APPDATA\\npm\\node_modules\\@openai\\codex" }',
    '$env:OPENCLAW_CODEX_APP_SERVER_BIN = $CodexExe',
    "Set-Location -LiteralPath $(Quote-PowerShellArgument $ProjectRoot)",
    "& openclaw --profile $(Quote-PowerShellArgument $Profile) gateway run --force 1>> $(Quote-PowerShellArgument $StdoutLog) 2>> $(Quote-PowerShellArgument $StderrLog)"
) -join [Environment]::NewLine
$EncodedCommand = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($Command))

try {
    Add-Content -LiteralPath $StdoutLog -Value "[$(Get-Date -Format o)] Starting OpenClaw Gateway..." -Encoding UTF8
}
catch {
    Write-Warning "Could not append startup marker to stdout log: $($_.Exception.Message)"
}

$Process = Start-Process `
    -FilePath $PowerShellExe `
    -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", $EncodedCommand) `
    -WorkingDirectory $ProjectRoot `
    -WindowStyle Hidden `
    -PassThru

Start-Sleep -Seconds 3
if ($Process.HasExited) {
    if (Test-Path -LiteralPath $PidFile) {
        Remove-Item -LiteralPath $PidFile -Force
    }
    Write-Host "OpenClaw Gateway failed to stay running."
    if (Test-Path -LiteralPath $StderrLog) {
        Write-Host "Recent stderr:"
        Get-Content -LiteralPath $StderrLog -Tail 20
    }
    exit 1
}

Set-Content -LiteralPath $PidFile -Value $Process.Id -Encoding ASCII

Write-Host "OpenClaw Gateway started in background. PID: $($Process.Id)"
Write-Host "Stdout: $StdoutLog"
Write-Host "Stderr: $StderrLog"
Write-Host "Logs are appended, not overwritten."
Write-Host "Stop with: .\\stop_openclaw_gateway.ps1"
