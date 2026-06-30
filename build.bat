@echo off
setlocal
cd /d "%~dp0"

echo [1/3] Installing build dependencies...
python -m pip install -r requirements.txt pyinstaller --quiet
if errorlevel 1 goto fail

echo [2/3] Building VelogPoster...
python -m PyInstaller VelogPoster.spec --noconfirm --clean
if errorlevel 1 goto fail

echo.
echo [3/3] Done.
echo Output folder: dist\VelogPoster\
echo Run: dist\VelogPoster\VelogPoster.exe
goto end

:fail
echo Build failed.
exit /b 1

:end
endlocal
