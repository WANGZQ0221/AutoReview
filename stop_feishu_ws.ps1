$ErrorActionPreference = "Stop"

$ProjectRoot = $PSScriptRoot
$PidFile = Join-Path $ProjectRoot "data\feishu_ws.pid"
$Main = Join-Path $ProjectRoot "main.py"

function Get-FeishuWsProcesses {
    $MainPattern = [regex]::Escape($Main)
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -and
            $_.CommandLine -match $MainPattern -and
            $_.CommandLine -match "\bserve-feishu-ws\b"
        }
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

$StoppedPids = New-Object 'System.Collections.Generic.HashSet[int]'

if (Test-Path -LiteralPath $PidFile) {
    $PidValue = (Get-Content -LiteralPath $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if (-not $PidValue) {
        Remove-Item -LiteralPath $PidFile -Force
        Write-Host "Feishu long-connection PID file was empty."
    }
    else {
        $Process = Get-Process -Id $PidValue -ErrorAction SilentlyContinue
        if (-not $Process) {
            Remove-Item -LiteralPath $PidFile -Force
            Write-Host "Feishu long-connection PID file was stale: process $PidValue was not found."
        }
        else {
            Stop-ProcessTree -ProcessId ([int]$PidValue)
            [void]$StoppedPids.Add([int]$PidValue)
        }
    }
}
else {
    Write-Host "Feishu long-connection PID file not found."
}

$OrphanProcesses = @(Get-FeishuWsProcesses)
foreach ($Orphan in $OrphanProcesses) {
    $OrphanPid = [int]$Orphan.ProcessId
    if ($StoppedPids.Contains($OrphanPid)) {
        continue
    }
    Stop-ProcessTree -ProcessId $OrphanPid
    [void]$StoppedPids.Add($OrphanPid)
}

if (Test-Path -LiteralPath $PidFile) {
    Remove-Item -LiteralPath $PidFile -Force
}

if ($StoppedPids.Count -eq 0) {
    Write-Host "Feishu long-connection is not running."
}
else {
    $StoppedPidList = @($StoppedPids) -join ', '
    Write-Host "Feishu long-connection stopped. PID(s): $StoppedPidList"
}
