$ErrorActionPreference = "Stop"

$ProjectRoot = $PSScriptRoot
$PidFile = Join-Path $ProjectRoot "data\feishu_ws.pid"

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

if (-not (Test-Path -LiteralPath $PidFile)) {
    Write-Host "Feishu long-connection is not running: PID file not found."
    exit 0
}

$PidValue = (Get-Content -LiteralPath $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
if (-not $PidValue) {
    Remove-Item -LiteralPath $PidFile -Force
    Write-Host "Feishu long-connection is not running: PID file was empty."
    exit 0
}

$Process = Get-Process -Id $PidValue -ErrorAction SilentlyContinue
if (-not $Process) {
    Remove-Item -LiteralPath $PidFile -Force
    Write-Host "Feishu long-connection is not running: process $PidValue was not found."
    exit 0
}

Stop-ProcessTree -ProcessId ([int]$PidValue)
Remove-Item -LiteralPath $PidFile -Force

Write-Host "Feishu long-connection stopped. PID: $PidValue"
