@echo off
setlocal
cd /d "%~dp0"
python poc\ps3_headset_panel.py
if errorlevel 1 (
    echo.
    echo Failed to start the headset dashboard.
    pause
)
