[CmdletBinding()]
param(
    [switch]$Live,
    [switch]$Capture,
    [string]$DataDir,
    [string]$Config,
    [string]$Profile
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "The .venv environment is missing. Run scripts\bootstrap.ps1 first."
}

$arguments = @("-m", "config.doctor")
if ($Live) { $arguments += "--live" }
if ($Capture) { $arguments += "--capture" }
if ($DataDir) { $arguments += @("--data-dir", $DataDir) }
if ($Config) { $arguments += @("--config", $Config) }
if ($Profile) { $arguments += @("--profile", $Profile) }

Push-Location $repoRoot
try {
    & $venvPython @arguments
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
