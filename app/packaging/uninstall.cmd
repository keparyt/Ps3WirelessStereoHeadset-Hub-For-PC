@echo off
REM Double-click uninstaller for the PS3 Wireless Stereo Headset Hub.
REM Removes the program folder, shortcuts and the start-with-Windows entry
REM for the current user. Settings in %APPDATA%\PS3HeadsetHub are kept.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" -Uninstall %*
pause
