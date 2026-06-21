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
$actualSessionKey = "agent:${Agent}:autoreview-" + ([guid]::NewGuid().ToString("N"))
$arguments = @(
    "--profile",
    "autoreview",
    "agent",
    "--agent",
    $Agent,
    "--session-key",
    $actualSessionKey,
    "--message",
    $prompt,
    "--json",
    "--timeout",
    [string]$Timeout
)

$raw = & $openclaw @arguments 2>&1
$exitCode = $LASTEXITCODE

if ($exitCode -ne 0) {
    $raw | ForEach-Object { [Console]::Error.WriteLine($_) }
    exit $exitCode
}

$text = ($raw | Out-String).Trim()
if (-not $text) {
    exit 0
}

try {
    $parsed = $text | ConvertFrom-Json -Depth 100
}
catch {
    [Console]::Out.Write($text)
    exit 0
}

$finalText = $null
if ($parsed.result) {
    if ($parsed.result.finalAssistantVisibleText) {
        $finalText = [string]$parsed.result.finalAssistantVisibleText
    }
    elseif ($parsed.result.finalAssistantRawText) {
        $finalText = [string]$parsed.result.finalAssistantRawText
    }
    elseif ($parsed.result.payloads -and $parsed.result.payloads.Count -gt 0 -and $parsed.result.payloads[0].text) {
        $finalText = [string]$parsed.result.payloads[0].text
    }
}

if ($finalText) {
    [Console]::Out.Write($finalText)
}
else {
    [Console]::Out.Write($text)
}

exit 0
