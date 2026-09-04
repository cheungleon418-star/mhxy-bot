[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$tracked = & git -C $repoRoot ls-files
if ($LASTEXITCODE -ne 0) { throw "Unable to read the Git file list." }

$forbidden = @(
    '(?i)(^|/)(templates|captures|diagnostics|logs)/',
    '(?i)(^|/)(\.venv|venv|env|__pycache__)/',
    '(?i)\.(png|jpe?g|bmp|webp|gif|mp4|avi|mkv|mov|wmv)$',
    '(?i)\.(exe|msi|zip|7z|rar|tar|tgz|tar\.gz)$',
    '(?i)\.(sqlite|sqlite3)$',
    '(?i)(^|/)config\.json$',
    '(?i)\.local\.json$',
    '(?i)(^|/)[^/]*((private|local)[-_.]?config|config[-_.]?(private|local))[^/]*\.json$',
    '(?i)(^|/)full-window-[^/]*\.json$',
    '(?i)(^|/)\.env($|\.)'
)

$violations = @()
foreach ($file in $tracked) {
    foreach ($pattern in $forbidden) {
        if ($file -match $pattern) {
            $violations += $file
            break
        }
    }
}

if ($violations.Count -gt 0) {
    Write-Error ("Forbidden runtime/private files are tracked:`n" + (($violations | Sort-Object -Unique) -join "`n"))
    exit 1
}
Write-Host "Repository hygiene check passed."
