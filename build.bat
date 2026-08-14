@echo off
setlocal
cd /d "%~dp0"

set "DIST_SETTINGS=dist\VelogPoster\velog_settings.json"
set "APP_SETTINGS=%LOCALAPPDATA%\VelogPoster\velog_settings.json"
set "PRESERVE=%TEMP%\velog_settings_build_preserve.json"

echo [0/3] Preserving settings...
if not exist "%LOCALAPPDATA%\VelogPoster" mkdir "%LOCALAPPDATA%\VelogPoster" >nul 2>&1
if exist "%DIST_SETTINGS%" (
  copy /Y "%DIST_SETTINGS%" "%PRESERVE%" >nul
  if not exist "%APP_SETTINGS%" (
    copy /Y "%DIST_SETTINGS%" "%APP_SETTINGS%" >nul
    echo   migrated dist settings -^> %%LOCALAPPDATA%%\VelogPoster\
  ) else (
    echo   AppData settings kept; dist copy backed up
  )
) else if exist "%APP_SETTINGS%" (
  copy /Y "%APP_SETTINGS%" "%PRESERVE%" >nul
  echo   AppData settings backed up
) else if exist "velog_settings.json" (
  copy /Y "velog_settings.json" "%PRESERVE%" >nul
  if not exist "%APP_SETTINGS%" copy /Y "velog_settings.json" "%APP_SETTINGS%" >nul
  echo   project settings backed up
) else (
  echo   no existing settings found
)

echo [1/3] Installing build dependencies...
python -m pip install -r requirements.txt pyinstaller --quiet
if errorlevel 1 goto fail

echo [2/3] Building VelogPoster...
python -m PyInstaller VelogPoster.spec --noconfirm --clean
if errorlevel 1 goto fail

echo [3/3] Restoring settings into dist...
if exist "%PRESERVE%" (
  if not exist "dist\VelogPoster" mkdir "dist\VelogPoster" >nul 2>&1
  copy /Y "%PRESERVE%" "%DIST_SETTINGS%" >nul
  if not exist "%APP_SETTINGS%" copy /Y "%PRESERVE%" "%APP_SETTINGS%" >nul
  echo   restored velog_settings.json
) else if exist "%APP_SETTINGS%" (
  if not exist "dist\VelogPoster" mkdir "dist\VelogPoster" >nul 2>&1
  copy /Y "%APP_SETTINGS%" "%DIST_SETTINGS%" >nul
  echo   copied AppData settings into dist
) else (
  echo   nothing to restore
)

echo.
echo Done.
echo Output folder: dist\VelogPoster\
echo Settings: %%LOCALAPPDATA%%\VelogPoster\velog_settings.json
echo Run: dist\VelogPoster\VelogPoster.exe
goto end

:fail
echo Build failed.
exit /b 1

:end
endlocal
