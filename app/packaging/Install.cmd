@echo off
REM Double-click installer for the PS3 Wireless Stereo Headset Hub.
REM Installs for the current user (no admin rights) and registers
REM start-with-Windows in the system tray. At the end it offers to download
REM and silently install FxSound (the audio engine the Hub drives) - the
REM only click is the setup's own UAC prompt. Use install.ps1 -Uninstall
REM (or the Start Menu entry) to remove.
setlocal
cd /d "%~dp0"

where powershell >nul 2>nul
if errorlevel 1 (
    echo Windows PowerShell is required but was not found.
    pause
    exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set EXITCODE=%errorlevel%

echo.
if %EXITCODE% neq 0 (
    echo Install FAILED - read the messages above.
) else (
    echo You can close this window now.
)
pause
exit /b %EXITCODE%
