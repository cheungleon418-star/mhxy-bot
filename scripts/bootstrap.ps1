[CmdletBinding()]
param(
    [switch]$WithOcr,
    [switch]$Dev
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$venvDir = Join-Path $repoRoot ".venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"

if ($env:OS -ne "Windows_NT" -or -not [Environment]::Is64BitOperatingSystem) {
    throw "Only 64-bit Windows is supported."
}

$pythonProbe = "import platform,struct,sys; raise SystemExit(0 if platform.python_implementation() == 'CPython' and sys.version_info[:2] == (3,11) and struct.calcsize('P') == 8 else 1)"
$venvProbe = "import platform,struct,sys; raise SystemExit(0 if platform.python_implementation() == 'CPython' and sys.version_info[:2] == (3,11) and struct.calcsize('P') == 8 and sys.prefix != getattr(sys, 'base_prefix', sys.prefix) else 1)"
if (-not (Test-Path -LiteralPath $venvPython)) {
    $pythonCommand = $null
    $pythonPrefix = @()
    $pyLauncher = Get-Command "py.exe" -ErrorAction SilentlyContinue
    if ($null -ne $pyLauncher) {
        & $pyLauncher.Source -3.11 -c $pythonProbe
        if ($LASTEXITCODE -eq 0) {
            $pythonCommand = $pyLauncher.Source
            $pythonPrefix = @("-3.11")
        }
    }

    if ($null -eq $pythonCommand) {
        $python = Get-Command "python.exe" -ErrorAction SilentlyContinue
        if ($null -eq $python) {
            throw "CPython 3.11 x64 was not found. Install it first and add it to PATH."
        }
        & $python.Source -c $pythonProbe
        if ($LASTEXITCODE -ne 0) {
            throw "python.exe is not CPython 3.11 x64."
        }
        $pythonCommand = $python.Source
    }

    Write-Host "Creating Python 3.11 virtual environment: $venvDir"
    & $pythonCommand @pythonPrefix -m venv $venvDir
    if ($LASTEXITCODE -ne 0) { throw "Unable to create the virtual environment." }
}

& $venvPython -c $venvProbe
if ($LASTEXITCODE -ne 0) {
    throw "The existing .venv is not a CPython 3.11 x64 virtual environment. Move or remove '$venvDir', then run bootstrap again."
}

$requirementsFile = if ($Dev) { "requirements-dev.txt" } else { "requirements.txt" }
$dependencyKind = if ($Dev) { "development" } else { "base" }
Write-Host "Installing pinned $dependencyKind dependencies..."
& $venvPython -m pip install --disable-pip-version-check -r (Join-Path $repoRoot $requirementsFile)
if ($LASTEXITCODE -ne 0) { throw "$dependencyKind dependency installation failed." }

if ($WithOcr) {
    Write-Host "Installing optional OCR dependencies..."
    & $venvPython -m pip install --disable-pip-version-check -r (Join-Path $repoRoot "requirements-ocr.txt")
    if ($LASTEXITCODE -ne 0) { throw "OCR dependency installation failed. OCR is not needed for treasure-map v1." }
}

& $venvPython -m pip check
if ($LASTEXITCODE -ne 0) { throw "Dependency consistency check failed." }

Push-Location $repoRoot
try {
    # In initialization mode missing calibration is a warning, while config,
    # directory and dependency failures still produce a non-zero exit code.
    & $venvPython -m config.doctor --init
    if ($LASTEXITCODE -ne 0) {
        throw "Runtime data initialization or environment validation failed."
    }
} finally {
    Pop-Location
}

Write-Host "Bootstrap complete. Run scripts\doctor.ps1, then scripts\run.ps1."
