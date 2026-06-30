@echo off
cd /d "%~dp0"
python velog_gui.py
if errorlevel 1 pause
