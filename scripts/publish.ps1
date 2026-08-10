param(
    [string]$Notes = "업데이트",
    [ValidateSet("patch", "minor", "major", "none")]
    [string]$Bump = "",
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
Set-Location $Root
. (Join-Path $PSScriptRoot "gh-env.ps1")

function Invoke-Soft {
    # git/pyinstaller 가 stderr 로 경고를 내도 중단하지 않는다.
    param([scriptblock]$Block)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $Block
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prev
    return $code
}

function Read-DeployConfig {
    Get-Content (Join-Path $Root "deploy.json") -Raw | ConvertFrom-Json
}

function Read-AppVersion($cfg) {
    $path = Join-Path $Root $cfg.version.file
    $text = Get-Content $path -Raw
    $var = [regex]::Escape($cfg.version.variable)
    if ($text -match "${var}\s*=\s*`"([^`"]+)`"") {
        return $Matches[1]
    }
    throw "Version not found: $($cfg.version.variable) in $($cfg.version.file)"
}

function Set-AppVersion($cfg, [string]$Version) {
    $path = Join-Path $Root $cfg.version.file
    $text = Get-Content $path -Raw
    $var = [regex]::Escape($cfg.version.variable)
    $text = $text -replace "${var}\s*=\s*`"[^`"]+`"", "$($cfg.version.variable) = `"$Version`""
    Set-Content -Path $path -Value $text -Encoding UTF8
}

function Bump-Version([string]$Version, [string]$Part) {
    $parts = $Version.Split(".")
    if ($parts.Count -lt 3) { throw "Invalid version: $Version" }
    [int]$major = $parts[0]
    [int]$minor = $parts[1]
    [int]$patch = $parts[2]
    switch ($Part) {
        "major" { $major++; $minor = 0; $patch = 0 }
        "minor" { $minor++; $patch = 0 }
        "patch" { $patch++ }
        "none" { }
    }
    return "$major.$minor.$patch"
}

function Write-VersionJson(
    $cfg,
    [string]$Version,
    [string]$ReleaseNotes,
    $AssetId = $null
) {
    $owner = $cfg.github_owner
    $repo = $cfg.github_repo
    $asset = $cfg.release_asset
    $versionedUrl = "https://github.com/$owner/$repo/releases/download/v$Version/$asset"
    $latestUrl = "https://github.com/$owner/$repo/releases/latest/download/$asset"
    $urls = New-Object System.Collections.Generic.List[string]
    $primary = $latestUrl
    if ($null -ne $AssetId -and "$AssetId" -ne "") {
        $apiUrl = "https://api.github.com/repos/$owner/$repo/releases/assets/$AssetId"
        $urls.Add($apiUrl) | Out-Null
        $primary = $apiUrl
    }
    $urls.Add($versionedUrl) | Out-Null
    $urls.Add($latestUrl) | Out-Null
    $payloadObj = [ordered]@{
        version           = $Version
        url               = $primary
        download_url      = $versionedUrl
        notes             = $ReleaseNotes
        download_urls     = @($urls)
    }
    if ($null -ne $AssetId -and "$AssetId" -ne "") {
        $payloadObj["asset_id"] = [int]$AssetId
        $payloadObj["api_download_url"] = "https://api.github.com/repos/$owner/$repo/releases/assets/$AssetId"
    }
    $payload = $payloadObj | ConvertTo-Json -Depth 5
    $path = Join-Path $Root "version.json"
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($path, $payload, $utf8NoBom)
}

function Get-ReleaseAssetId($cfg, [string]$Tag) {
    $gh = Get-GhExe
    if (-not $gh) { return $null }
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"
    $json = & $gh api "repos/$($cfg.github_owner)/$($cfg.github_repo)/releases/tags/$Tag" 2>$null
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prev
    if ($code -ne 0 -or -not $json) { return $null }
    $release = $json | ConvertFrom-Json
    foreach ($asset in @($release.assets)) {
        if ($asset.name -eq $cfg.release_asset) {
            return [int]$asset.id
        }
    }
    return $null
}

function Ensure-GitRemote($cfg) {
    if (-not (Test-Path (Join-Path $Root ".git"))) {
        git init | Out-Null
    }
    $branch = git branch --show-current 2>$null
    if ($branch -and $branch -ne "main") {
        git branch -M main | Out-Null
    } elseif (-not $branch) {
        git checkout -B main 2>$null | Out-Null
    }
    $remoteUrl = "https://github.com/$($cfg.github_owner)/$($cfg.github_repo).git"
    $hasOrigin = @(git remote 2>$null) -contains "origin"
    if (-not $hasOrigin) {
        git remote add origin $remoteUrl
        Write-Host "[git] origin: $remoteUrl"
    }
}

function Ensure-GhAuth {
    Invoke-Gh auth status *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Run .\scripts\setup-github.ps1 or gh auth login"
    }
}

$cfg = Read-DeployConfig
$bumpPart = if ($Bump) { $Bump } else { $cfg.default_bump }
$current = Read-AppVersion $cfg
$newVersion = Bump-Version $current $bumpPart
$tag = "v$newVersion"
$displayName = if ($cfg.app_display_name) { $cfg.app_display_name } else { $cfg.github_repo }

Write-Host "============================================"
Write-Host " $displayName deploy"
Write-Host " version: $current -> $newVersion"
Write-Host "============================================"

Set-AppVersion $cfg $newVersion
Write-VersionJson $cfg $newVersion $Notes

if (-not $SkipBuild) {
    Write-Host "[1/4] Building..."
    $buildScript = Join-Path $Root $cfg.build.script
    if (-not (Test-Path $buildScript)) { throw "Build script missing: $($cfg.build.script)" }
    $buildCode = Invoke-Soft { & $buildScript }
    if ($buildCode -ne 0) { throw "Build failed" }
}

$distDir = Join-Path $Root ($cfg.build.dist_dir -replace "/", "\")
if (-not (Test-Path $distDir)) {
    throw "Build output missing: $($cfg.build.dist_dir)"
}

Write-Host "[2/4] Creating zip..."
$zipPath = Join-Path $Root "dist\$($cfg.release_asset)"
$distParent = Split-Path $zipPath -Parent
if (-not (Test-Path $distParent)) { New-Item -ItemType Directory -Path $distParent | Out-Null }
if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
Compress-Archive -Path $distDir -DestinationPath $zipPath -Force

Ensure-GhInstalled
Ensure-GitRemote $cfg
Ensure-GhAuth

Write-Host "[3/4] Pushing to GitHub..."
Invoke-Soft {
    foreach ($item in $cfg.git_add) {
        git add -- $item 2>$null
    }
    git add deploy.json deploy.bat version.json scripts 2>$null
    git add -u 2>$null
}

if (git status --porcelain) {
    Invoke-Soft { git commit -m "Release $newVersion" }
}

Invoke-Soft { git push -u origin main }
if ($LASTEXITCODE -ne 0) {
    Write-Host "[git] pull --rebase then push..."
    Invoke-Soft { git pull origin main --rebase }
    Invoke-Soft { git push -u origin main }
    if ($LASTEXITCODE -ne 0) { throw "git push failed" }
}

Write-Host "[4/4] GitHub Release..."
if (Test-GhRelease $tag) {
    Invoke-Gh release upload $tag $zipPath --clobber
    Invoke-Gh release edit $tag --notes $Notes --title $newVersion
} else {
    Invoke-Gh release create $tag $zipPath --title $newVersion --notes $Notes --latest
}

Write-Host "[version.json] asset_id 갱신..."
$assetId = Get-ReleaseAssetId $cfg $tag
Write-VersionJson $cfg $newVersion $Notes -AssetId $assetId
Invoke-Soft { git add version.json }
if (git status --porcelain -- version.json) {
    Invoke-Soft { git commit -m "Update version.json asset_id for $newVersion" }
    Invoke-Soft { git push origin main }
}

Write-Host ""
Write-Host "Done!"
Write-Host "  version: $newVersion"
if ($assetId) { Write-Host "  asset_id: $assetId" }
Write-Host "  https://github.com/$($cfg.github_owner)/$($cfg.github_repo)/releases/tag/$tag"
