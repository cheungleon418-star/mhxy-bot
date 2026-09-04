[CmdletBinding()]
param(
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

$commit = (& git -C $repoRoot rev-parse --short=12 HEAD).Trim()
if ($LASTEXITCODE -ne 0) { $commit = "unknown" }
$env:MHXY_BOT_GIT_SHA = $commit
$env:MHXY_BOT_COMMIT = $commit

$arguments = @((Join-Path $repoRoot "launcher.py"))
if ($DataDir) { $arguments += @("--data-dir", $DataDir) }
if ($Config) { $arguments += @("--config", $Config) }
if ($Profile) { $arguments += @("--profile", $Profile) }

Write-Host "Starting MHXY Bot at commit $commit. The GUI always starts in dry-run mode."
Push-Location $repoRoot
try {
    & $venvPython @arguments
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
