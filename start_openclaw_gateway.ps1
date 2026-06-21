param(
    [string]$Profile = "autoreview",
    [string]$GatewayToken = "autoreview-local-token",
    [string]$ProxyUrl = "http://127.0.0.1:7897",
    [int]$ReadyTimeoutSeconds = 45
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

function Resolve-OpenClawCommand {
    $candidate = Join-Path $env:APPDATA "npm\openclaw.ps1"
    if (Test-Path -LiteralPath $candidate) {
        return $candidate
    }

    $command = Get-Command openclaw.ps1 -ErrorAction SilentlyContinue
    if ($command -and $command.Source) {
        return $command.Source
    }

    $command = Get-Command openclaw -ErrorAction SilentlyContinue
    if ($command -and $command.Source) {
        return $command.Source
    }

    throw "openclaw command not found"
}

function Resolve-CodexExe {
    $root = Join-Path $env:APPDATA "npm\node_modules\@openai\codex"
    if (-not (Test-Path -LiteralPath $root)) {
        throw "codex package directory not found: $root"
    }

    $codex = Get-ChildItem $root -Recurse -Filter codex.exe -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $codex) {
        throw "codex.exe not found under $root"
    }

    return $codex.FullName
}

function Set-OpenClawEnvironment {
    $env:PYTHONUNBUFFERED = "1"
    $env:HTTPS_PROXY = $ProxyUrl
    $env:HTTP_PROXY = $ProxyUrl
    $env:ALL_PROXY = $ProxyUrl
    $env:NO_PROXY = "localhost,127.0.0.1"
    $env:https_proxy = $env:HTTPS_PROXY
    $env:http_proxy = $env:HTTP_PROXY
    $env:all_proxy = $env:ALL_PROXY
    $env:no_proxy = $env:NO_PROXY
    $env:OPENCLAW_GATEWAY_TOKEN = $GatewayToken
    $env:OPENCLAW_CODEX_APP_SERVER_BIN = Resolve-CodexExe
}

function Stop-ProcessTree([int]$ProcessId) {
    $Children = Get-CimInstance Win32_Process -Filter "ParentProcessId = $ProcessId" -ErrorAction SilentlyContinue
    foreach ($Child in $Children) {
        Stop-ProcessTree -ProcessId ([int]$Child.ProcessId)
    }

    $Target = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($Target) {
        Stop-Process -Id $ProcessId -Force
    }
}

function Show-RecentLogs {
    if (Test-Path -LiteralPath $StdoutLog) {
        Write-Host "Recent stdout:"
        Get-Content -LiteralPath $StdoutLog -Tail 40
    }
    if (Test-Path -LiteralPath $StderrLog) {
        Write-Host "Recent stderr:"
        Get-Content -LiteralPath $StderrLog -Tail 40
    }
}

function Test-GatewayReady([string]$OpenClawCommand) {
    $statusOutput = & $OpenClawCommand --profile $Profile status 2>&1
    $statusCode = $LASTEXITCODE
    if ($statusCode -ne 0) {
        return $false
    }
    $statusText = ($statusOutput | Out-String)
    return ($statusText -match "ready|listening|running|Gateway")
}

$OpenClaw = Resolve-OpenClawCommand
Set-OpenClawEnvironment

if (Test-Path -LiteralPath $PidFile) {
    $ExistingPid = (Get-Content -LiteralPath $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($ExistingPid) {
        $ExistingProcess = Get-Process -Id $ExistingPid -ErrorAction SilentlyContinue
        if ($ExistingProcess) {
            if (Test-GatewayReady -OpenClawCommand $OpenClaw) {
                Write-Host "OpenClaw Gateway is already running and reachable. PID: $ExistingPid"
                Write-Host "Logs: $StdoutLog"
                exit 0
            }

            Write-Host "OpenClaw Gateway PID exists but health check failed. Restarting PID: $ExistingPid"
            Stop-ProcessTree -ProcessId ([int]$ExistingPid)
        }
    }
}

$ExistingGatewayProcesses = @(Get-GatewayProcesses)
if ($ExistingGatewayProcesses.Count -gt 0) {
    if (Test-GatewayReady -OpenClawCommand $OpenClaw) {
        Write-Host "OpenClaw Gateway is already running and reachable. PID(s): $($ExistingGatewayProcesses.ProcessId -join ', ')"
        Write-Host "Logs: $StdoutLog"
        exit 0
    }

    Write-Host "Found stale/unreachable OpenClaw Gateway process(es). Restarting PID(s): $($ExistingGatewayProcesses.ProcessId -join ', ')"
    foreach ($Existing in $ExistingGatewayProcesses) {
        Stop-ProcessTree -ProcessId ([int]$Existing.ProcessId)
    }
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
    '$env:OPENCLAW_CODEX_APP_SERVER_BIN = ' + (Quote-PowerShellArgument $env:OPENCLAW_CODEX_APP_SERVER_BIN),
    "Set-Location -LiteralPath $(Quote-PowerShellArgument $ProjectRoot)",
    "& $(Quote-PowerShellArgument $OpenClaw) --profile $(Quote-PowerShellArgument $Profile) gateway run --force 1>> $(Quote-PowerShellArgument $StdoutLog) 2>> $(Quote-PowerShellArgument $StderrLog)"
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
    Show-RecentLogs
    exit 1
}

Set-Content -LiteralPath $PidFile -Value $Process.Id -Encoding ASCII

Write-Host "OpenClaw Gateway process started. PID: $($Process.Id)"
Write-Host "Waiting for Gateway readiness..."

$deadline = (Get-Date).AddSeconds($ReadyTimeoutSeconds)
$ready = $false
while ((Get-Date) -lt $deadline) {
    $liveProcess = Get-Process -Id $Process.Id -ErrorAction SilentlyContinue
    if (-not $liveProcess) {
        break
    }
    if (Test-GatewayReady -OpenClawCommand $OpenClaw) {
        $ready = $true
        break
    }
    Start-Sleep -Seconds 2
}

if (-not $ready) {
    $liveProcess = Get-Process -Id $Process.Id -ErrorAction SilentlyContinue
    if ($liveProcess) {
        Stop-ProcessTree -ProcessId $Process.Id
    }
    if (Test-Path -LiteralPath $PidFile) {
        Remove-Item -LiteralPath $PidFile -Force
    }
    Write-Host "OpenClaw Gateway did not become ready within $ReadyTimeoutSeconds second(s)."
    Show-RecentLogs
    exit 1
}

Write-Host "OpenClaw Gateway started in background. PID: $($Process.Id)"
Write-Host "Stdout: $StdoutLog"
Write-Host "Stderr: $StderrLog"
Write-Host "Logs are appended, not overwritten."
Write-Host "Stop with: .\\stop_openclaw_gateway.ps1"
