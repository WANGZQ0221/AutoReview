param(
    [string]$Config = "config\oppo_submission.json",
    [ValidateSet("DEBUG", "INFO", "WARN", "ERROR")]
    [string]$LogLevel = "INFO",
    [string]$GatewayToken = ""
)

$ErrorActionPreference = "Stop"

$ProjectRoot = $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Main = Join-Path $ProjectRoot "main.py"
$PidFile = Join-Path $ProjectRoot "data\feishu_ws.pid"
$LogDir = Join-Path $ProjectRoot "logs"
$StdoutLog = Join-Path $LogDir "feishu-ws.out.log"
$StderrLog = Join-Path $LogDir "feishu-ws.err.log"

function Quote-PowerShellArgument([string]$Value) {
    return "'" + $Value.Replace("'", "''") + "'"
}

function Get-FeishuWsProcesses {
    $MainPattern = [regex]::Escape($Main)
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -and
            $_.CommandLine -match $MainPattern -and
            $_.CommandLine -match "\bserve-feishu-ws\b"
        }
}

function Resolve-GatewayToken {
    if (-not [string]::IsNullOrWhiteSpace($GatewayToken)) {
        return $GatewayToken
    }
    if (-not [string]::IsNullOrWhiteSpace($env:OPENCLAW_GATEWAY_TOKEN)) {
        return $env:OPENCLAW_GATEWAY_TOKEN
    }

    $candidatePaths = @()
    if (-not [string]::IsNullOrWhiteSpace($env:OPENCLAW_CONFIG_PATH)) {
        $candidatePaths += $env:OPENCLAW_CONFIG_PATH
    }
    $candidatePaths += (Join-Path $HOME ".openclaw-autoreview\openclaw.json")
    $candidatePaths += (Join-Path $HOME ".openclaw\openclaw.json")

    foreach ($CandidatePath in $candidatePaths) {
        if (-not (Test-Path -LiteralPath $CandidatePath)) {
            continue
        }
        try {
            $ConfigJson = Get-Content -Raw -LiteralPath $CandidatePath | ConvertFrom-Json
            $Token = $ConfigJson.gateway.auth.token
            if (-not [string]::IsNullOrWhiteSpace($Token)) {
                return [string]$Token
            }
        }
        catch {
            Write-Warning "Could not read OpenClaw gateway token from ${CandidatePath}: $($_.Exception.Message)"
        }
    }

    return "autoreview-local-token"
}

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python not found: $Python"
}

if (Test-Path -LiteralPath $PidFile) {
    $ExistingPid = (Get-Content -LiteralPath $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($ExistingPid) {
        $ExistingProcess = Get-Process -Id $ExistingPid -ErrorAction SilentlyContinue
        if ($ExistingProcess) {
            Write-Host "Feishu long-connection is already running. PID: $ExistingPid"
            Write-Host "Logs: $StdoutLog"
            exit 0
        }
    }
}

$ExistingWsProcesses = @(Get-FeishuWsProcesses)
if ($ExistingWsProcesses.Count -gt 0) {
    Write-Host "Feishu long-connection is already running. PID(s): $($ExistingWsProcesses.ProcessId -join ', ')"
    Write-Host "Logs: $StdoutLog"
    exit 0
}

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $PidFile) | Out-Null
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$ConfigPath = Join-Path $ProjectRoot $Config
$PowerShellExe = (Get-Process -Id $PID).Path
$ResolvedGatewayToken = Resolve-GatewayToken
$Command = @(
    '$ErrorActionPreference = "Stop"',
    '$env:PYTHONUNBUFFERED = "1"',
    '$env:OPENCLAW_GATEWAY_TOKEN = ' + (Quote-PowerShellArgument $ResolvedGatewayToken),
    "Set-Location -LiteralPath $(Quote-PowerShellArgument $ProjectRoot)",
    "& $(Quote-PowerShellArgument $Python) $(Quote-PowerShellArgument $Main) -c $(Quote-PowerShellArgument $ConfigPath) serve-feishu-ws --log-level $(Quote-PowerShellArgument $LogLevel) 1>> $(Quote-PowerShellArgument $StdoutLog) 2>> $(Quote-PowerShellArgument $StderrLog)"
) -join [Environment]::NewLine
$EncodedCommand = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($Command))

try {
    Add-Content -LiteralPath $StdoutLog -Value "[$(Get-Date -Format o)] Starting Feishu long-connection..." -Encoding UTF8
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

Start-Sleep -Seconds 2
if ($Process.HasExited) {
    if (Test-Path -LiteralPath $PidFile) {
        Remove-Item -LiteralPath $PidFile -Force
    }
    Write-Host "Feishu long-connection failed to stay running."
    if (Test-Path -LiteralPath $StderrLog) {
        Write-Host "Recent stderr:"
        Get-Content -LiteralPath $StderrLog -Tail 20
    }
    exit 1
}

Set-Content -LiteralPath $PidFile -Value $Process.Id -Encoding ASCII

Write-Host "Feishu long-connection started in background. PID: $($Process.Id)"
Write-Host "Stdout: $StdoutLog"
Write-Host "Stderr: $StderrLog"
Write-Host "Logs are appended, not overwritten."
Write-Host "Stop with: .\stop_feishu_ws.ps1"
