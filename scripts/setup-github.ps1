$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
Set-Location $Root
. (Join-Path $PSScriptRoot "gh-env.ps1")

$cfg = Get-Content (Join-Path $Root "deploy.json") -Raw | ConvertFrom-Json
$remoteUrl = "https://github.com/$($cfg.github_owner)/$($cfg.github_repo).git"

Write-Host "=== GitHub setup ==="
Ensure-GhInstalled

if (-not (Test-Path (Join-Path $Root ".git"))) {
    git init
    git branch -M main
}

$hasOrigin = @(git remote 2>$null) -contains "origin"
if (-not $hasOrigin) {
    git remote add origin $remoteUrl
    Write-Host "origin: $remoteUrl"
} else {
    Write-Host "origin: $(git remote get-url origin)"
}

Write-Host ""
Write-Host "Logging in to GitHub (browser)..."
Invoke-Gh auth login

Write-Host ""
Write-Host "Deploy with: .\deploy.bat"
