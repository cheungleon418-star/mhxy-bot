[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

$branch = (& git -C $repoRoot branch --show-current).Trim()
if ($LASTEXITCODE -ne 0) { throw "This is not a valid Git repository." }
if ($branch -ne "main") {
    throw "Safe update only supports the main branch. Current branch: $branch"
}

$changes = & git -C $repoRoot status --porcelain
if ($LASTEXITCODE -ne 0) { throw "Unable to inspect the Git worktree." }
if ($changes) {
    throw "The worktree has local changes. Commit or back them up before updating."
}

Write-Host "Updating origin/main in fast-forward-only mode..."
& git -C $repoRoot pull --ff-only origin main
if ($LASTEXITCODE -ne 0) { throw "Update failed. No merge or force overwrite was attempted." }

Write-Host "Reconciling the pinned base dependencies..."
& (Join-Path $PSScriptRoot "bootstrap.ps1")

$commit = (& git -C $repoRoot rev-parse --short=12 HEAD).Trim()
Write-Host "Update complete: $commit"
Write-Host "Existing LocalAppData configuration, templates, captures and logs were preserved."
