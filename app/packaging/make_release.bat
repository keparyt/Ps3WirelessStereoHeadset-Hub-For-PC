@echo off
REM ===========================================================================
REM make_release.bat - one command from clean sources to a shippable installer.
REM
REM   packaging\make_release.bat               full pipeline
REM   packaging\make_release.bat --skip-tests  faster iteration
REM   packaging\make_release.bat --no-clean    keep the previous build folder
REM
REM What it does, in order:
REM   1. Verifies Python and installs every build/runtime dependency
REM   2. Runs the unit test suite (skipped with --skip-tests)
REM   3. Builds the onedir application  ->  build\PS3HeadsetHub\PS3HeadsetHub.exe
REM   4. Stages the installer/uninstaller next to the exe and makes the
REM      distributable zip: PS3HeadsetHub-<version>-win64.zip containing
REM      the app folder + Install.cmd + install.ps1 + uninstall.cmd,
REM      then builds the standalone GUI installer exe
REM      (build\PS3HeadsetHubInstaller.exe) which packs the whole app and
REM      can also download + silently install FxSound
REM   5. Optionally builds a real Inno Setup uninstallable installer
REM      (build\PS3HeadsetHub-<version>-setup.exe) when Inno Setup 6 is
REM      installed - this is a bonus, the zip installer always works
REM   6. Writes build\build-report.txt with version, commit and sha256
REM   7. Optionally launches the built app (make_release.bat --run)
REM      or installs it right away (make_release.bat --install)
REM
REM Everything is per-user: no admin rights anywhere in the pipeline.
REM ===========================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0\.."

set SKIP_TESTS=0
set CLEAN=--clean
set DO_RUN=0
set DO_INSTALL=0
for %%a in (%*) do (
    if /i "%%~a"=="--skip-tests" set SKIP_TESTS=1
    if /i "%%~a"=="--no-clean"   set CLEAN=
    if /i "%%~a"=="--run"        set DO_RUN=1
    if /i "%%~a"=="--install"    set DO_INSTALL=1
)

echo.
echo === [1/6] Python and dependencies ==========================================
python --version >nul 2>&1
if errorlevel 1 (
    echo Python was not found on PATH.
    echo Install Python 3.10 or newer from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" during setup.
    goto :failed
)
for /f "delims=" %%v in ('python --version 2^>^&1') do echo %%v

python -m pip install --quiet --disable-pip-version-check ^
    -r requirements.txt numpy pyinstaller
if errorlevel 1 (
    echo Could not install the dependencies. Check your internet connection.
    goto :failed
)
echo Dependencies OK.

echo.
echo === [2/6] Tests ============================================================
if %SKIP_TESTS%==1 (
    echo Skipped (--skip-tests).
) else (
    python -m pytest tests/ -q
    if errorlevel 1 (
        echo The test suite FAILED - fix the failures before releasing.
        goto :failed
    )
)

echo.
echo === [3/6] Build ============================================================
python packaging\build_exe.py %CLEAN%
if errorlevel 1 (
    echo The build FAILED - read the messages above.
    goto :failed
)

echo.
echo === [4/6] Installer staging ================================================
REM build_exe.py already copies Install.cmd / install.ps1 / uninstall.cmd to
REM the build folder root and zips them with the app; verify and say so.
if not exist "build\Install.cmd" (
    echo WARNING: build\Install.cmd is missing - the zip installer is incomplete.
) else (
    echo Installer files staged at build\ root:
    echo   Install.cmd   - double-click installer (current user, no admin)
    echo   install.ps1   - full installer with -NoAutostart/-DesktopShortcut/-Uninstall/-WithFxSound
    echo   uninstall.cmd - double-click uninstaller
)

REM --- standalone GUI installer -----------------------------------------------
if not exist "build\PS3HeadsetHub\PS3HeadsetHub.exe" (
    echo WARNING: the app payload is missing; skipping the installer exe.
) else (
    echo Building the standalone installer exe...
    python packaging\build_installer.py
    if errorlevel 1 (
        echo WARNING: PS3HeadsetHubInstaller.exe failed to build - the zip
        echo          installer above is still complete.
    ) else (
        echo   PS3HeadsetHubInstaller.exe - double-click GUI installer;
        echo   headless install: PS3HeadsetHubInstaller.exe --silent
    )
)
REM (uninstall.ps1 is placed inside the installed folder by install.ps1 at
REM first install; the Start Menu entry runs it with -Uninstall.)

REM --- optional Inno Setup installer ------------------------------------------
set "ISCC="
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe"     set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if defined ISCC (
    if exist "packaging\setup.iss" (
        echo Building the Inno Setup installer...
        "%ISCC%" /O"build" "packaging\setup.iss" >nul 2>&1
        if errorlevel 1 (
            echo Inno Setup failed; the zip installer is still valid.
        ) else (
            echo Inno Setup installer written to build\.
        )
    ) else (
        echo packaging\setup.iss not present; skipping the optional setup.exe.
    )
) else (
    echo Inno Setup 6 not installed; skipping the optional setup.exe.
    echo The zip + Install.cmd installer is complete without it.
)

echo.
echo === [5/6] Artifacts ========================================================
for %%z in ("build\PS3HeadsetHub-*-win64.zip") do echo Zip installer : %%~z
if exist "build\PS3HeadsetHubInstaller.exe" echo GUI installer: build\PS3HeadsetHubInstaller.exe
if exist "build\PS3HeadsetHub-*-setup.exe" for %%s in ("build\PS3HeadsetHub-*-setup.exe") do echo Setup exe    : %%~s
echo Portable app  : build\PS3HeadsetHub\PS3HeadsetHub.exe
echo Report        : build\build-report.txt

echo.
echo === [6/6] Post-build =======================================================
if %DO_INSTALL%==1 (
    echo Installing the fresh build for the current user...
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "build\install.ps1"
    if errorlevel 1 goto :failed
) else if %DO_RUN%==1 (
    echo Launching the freshly built app...
    start "" "build\PS3HeadsetHub\PS3HeadsetHub.exe"
) else (
    echo Try it:   packaging\make_release.bat --run
    echo Install:  packaging\make_release.bat --install   (or build\Install.cmd)
    echo Release:  tag v^(version^) and GitHub Actions attaches the zip.
)

echo.
echo === RELEASE READY ==========================================================
exit /b 0

:failed
echo.
echo === RELEASE FAILED - read the messages above ==============================
pause
exit /b 1
