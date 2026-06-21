param(
    [string]$Agent = "main",
    [string]$SessionKey = "agent:main:autoreview-autoreview",
    [int]$Timeout = 120
)

$ErrorActionPreference = "Stop"

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

$prompt = [Console]::In.ReadToEnd()
if (-not $prompt) {
    throw "empty prompt"
}

$prompt = ($prompt -replace "\s+", " ").Trim()
if ($prompt.Length -gt 12000) {
    $prompt = $prompt.Substring(0, 12000) + " [内容过长，已截断]"
}

$openclaw = Resolve-OpenClawCommand
$powerShellExe = (Get-Process -Id $PID).Path

$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $powerShellExe
$psi.UseShellExecute = $false
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$null = $psi.ArgumentList.Add("-NoProfile")
$null = $psi.ArgumentList.Add("-ExecutionPolicy")
$null = $psi.ArgumentList.Add("Bypass")
$null = $psi.ArgumentList.Add("-File")
$null = $psi.ArgumentList.Add($openclaw)
$null = $psi.ArgumentList.Add("agent")
$null = $psi.ArgumentList.Add("--agent")
$null = $psi.ArgumentList.Add($Agent)
$null = $psi.ArgumentList.Add("--session-key")
$null = $psi.ArgumentList.Add($SessionKey)
$null = $psi.ArgumentList.Add("--message")
$null = $psi.ArgumentList.Add($prompt)
$null = $psi.ArgumentList.Add("--json")
$null = $psi.ArgumentList.Add("--timeout")
$null = $psi.ArgumentList.Add([string]$Timeout)

$process = New-Object System.Diagnostics.Process
$process.StartInfo = $psi
$null = $process.Start()
$stdout = $process.StandardOutput.ReadToEnd()
$stderr = $process.StandardError.ReadToEnd()
$process.WaitForExit()

if ($stdout) {
    [Console]::Out.Write($stdout)
}
if ($stderr) {
    [Console]::Error.Write($stderr)
}

exit $process.ExitCode
