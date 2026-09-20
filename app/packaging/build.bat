@echo off
REM Build the distributable Windows executable.
setlocal
cd /d "%~dp0\.."

python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 (
    echo Could not install the build dependencies.
    pause
    exit /b 1
)

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

pyinstaller packaging\ps3hub.spec
if errorlevel 1 (
    echo Build failed.
    pause
    exit /b 1
)

echo.
echo Built dist\PS3HeadsetHub.exe
pause
