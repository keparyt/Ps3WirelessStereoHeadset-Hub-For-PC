@echo off
setlocal
cd /d "%~dp0"
cd app

python --version >nul 2>&1
if errorlevel 1 (
    echo Python was not found on PATH.
    echo Install Python 3.10 or newer from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" during setup.
    pause
    exit /b 1
)

python -c "import hid" >nul 2>&1
if errorlevel 1 (
    echo Installing the one required package...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Could not install hidapi. Check your internet connection.
        pause
        exit /b 1
    )
)

python main.py --tray
if errorlevel 1 (
    echo.
    echo The hub exited with an error. The log is in:
    echo    %%APPDATA%%\PS3HeadsetHub\logs
    pause
)
