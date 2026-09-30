@echo off
REM Build the distributable Windows application into <repo>\build.
REM One windowed onedir app: build\PS3HeadsetHub\PS3HeadsetHub.exe
setlocal
cd /d "%~dp0\.."

python -m pip install -r requirements.txt pyinstaller numpy
if errorlevel 1 (
    echo Could not install the build dependencies.
    pause
    exit /b 1
)

python packaging\build_exe.py --clean %*
if errorlevel 1 (
    echo.
    echo Build FAILED - read the messages above.
    pause
    exit /b 1
)

echo.
echo Done. The app is in build\PS3HeadsetHub\PS3HeadsetHub.exe
echo A distributable zip and build-report.txt are next to it in build\.
pause
