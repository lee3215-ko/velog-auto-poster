function Refresh-ShellPath {
    $machine = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [System.Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}

function Get-GhExe {
    Refresh-ShellPath
    $cmd = Get-Command gh -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }
    $candidates = @(
        "$env:ProgramFiles\GitHub CLI\gh.exe",
        "${env:ProgramFiles(x86)}\GitHub CLI\gh.exe",
        "$env:LOCALAPPDATA\Programs\GitHub CLI\gh.exe"
    )
    foreach ($path in $candidates) {
        if (Test-Path $path) {
            return $path
        }
    }
    return $null
}

function Invoke-Gh {
    param(
        [Parameter(ValueFromRemainingArguments = $true)]
        [string[]]$GhArgs
    )
    $gh = Get-GhExe
    if (-not $gh) {
        throw "GitHub CLI(gh) not found. Run: winget install GitHub.cli"
    }
    & $gh @GhArgs
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

function Test-GhRelease([string]$Tag) {
    $gh = Get-GhExe
    if (-not $gh) {
        return $false
    }
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"
    & $gh release view $Tag 2>$null | Out-Null
    $ok = $LASTEXITCODE -eq 0
    $ErrorActionPreference = $prev
    return $ok
}

function Ensure-GhInstalled {
    if (Get-GhExe) {
        return
    }
    Write-Host "Installing GitHub CLI..."
    winget install --id GitHub.cli -e --accept-source-agreements --accept-package-agreements | Out-Null
    Refresh-ShellPath
    if (-not (Get-GhExe)) {
        throw "gh not found after install. Restart PowerShell and retry."
    }
}
