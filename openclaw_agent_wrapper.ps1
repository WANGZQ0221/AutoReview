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
$arguments = @(
    "agent",
    "--agent",
    $Agent,
    "--session-key",
    $SessionKey,
    "--message",
    $prompt,
    "--json",
    "--timeout",
    [string]$Timeout
)

& $openclaw @arguments
exit $LASTEXITCODE
