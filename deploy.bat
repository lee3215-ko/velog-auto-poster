@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\publish.ps1" %*
if errorlevel 1 (
    echo.
    echo Deploy failed. First time: scripts\setup-github.ps1 then gh auth login
    exit /b 1
)
endlocal
