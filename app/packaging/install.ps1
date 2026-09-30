# -*- mode: powershell -*-
# Install-PS3HeadsetHub.ps1 - install the Hub for the current Windows user.
#
# What it does:
#   1. Copies the onedir app (PS3HeadsetHub\) into %LOCALAPPDATA%\PS3HeadsetHub
#   2. Creates Start Menu shortcuts: "PS3 Headset Hub" and "Uninstall PS3 Headset Hub"
#   3. (default) registers an HKCU Run entry so the Hub starts minimized in
#      the system tray at Windows sign-in. Pass -NoAutostart to skip.
#   4. Writes uninstall.ps1 next to the installed app
#
# Nothing touches machine-wide state: no admin rights, no other users.
#
# Usage (from the extracted zip folder):
#   powershell -ExecutionPolicy Bypass -File install.ps1              # full install
#   powershell ... -File install.ps1 -NoAutostart                     # no start-with-Windows
#   powershell ... -File install.ps1 -DesktopShortcut                 # also a desktop icon
#   powershell ... -File install.ps1 -Uninstall                       # remove everything

[CmdletBinding()]
param(
    [switch]$NoAutostart,
    [switch]$DesktopShortcut,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$AppName    = 'PS3 Headset Hub'
$AppSlug    = 'PS3HeadsetHub'
$ExeName    = 'PS3HeadsetHub.exe'
$SourceDir  = Join-Path $PSScriptRoot $AppSlug          # PS3HeadsetHub\ next to this script
$TargetDir  = Join-Path $env:LOCALAPPDATA $AppSlug
$TargetExe  = Join-Path $TargetDir $ExeName
$StartMenu  = [Environment]::GetFolderPath('Programs')  # user's Start Menu
$MenuDir    = Join-Path $StartMenu $AppName
$RunKey     = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$RunValue   = "PS3$AppSlug"

function Info($m)  { Write-Host "  $m" -ForegroundColor Gray }
function Ok($m)    { Write-Host "[OK] $m" -ForegroundColor Green }
function Step($m)  { Write-Host "==> $m" -ForegroundColor Yellow }

function Stop-RunningApp {
    foreach ($name in @($ExeName, "$AppSlug`_smoke")) {
        Get-Process -Name ([IO.Path]::GetFileNameWithoutExtension($name)) -ErrorAction SilentlyContinue |
            ForEach-Object {
                Info "Stopping a running instance (pid $($_.Id))"
                try { $_.Kill(); $_.WaitForExit(5000) | Out-Null } catch {}
            }
    }
}

function Remove-Shortcuts {
    if (Test-Path $MenuDir) {
        Remove-Item $MenuDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($DesktopShortcut) {
        $desktop = [Environment]::GetFolderPath('Desktop')
        Remove-Item (Join-Path $desktop "$AppName.lnk") -Force -ErrorAction SilentlyContinue
    }
}

function New-Shortcut($path, $target, $arguments, $description, $workingDir) {
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($path)
    $shortcut.TargetPath = $target
    if ($arguments) { $shortcut.Arguments = $arguments }
    if ($description) { $shortcut.Description = $description }
    if ($workingDir) { $shortcut.WorkingDirectory = $workingDir }
    $shortcut.IconLocation = $target + ',0'
    $shortcut.Save()
}

if ($Uninstall) {
    Write-Host "Uninstalling $AppName (current user only)..." -ForegroundColor Yellow
    Step 'Stopping the app if it is running'
    Stop-RunningApp
    Step 'Removing the start-with-Windows entry'
    try {
        $key = Get-Item $RunKey -ErrorAction SilentlyContinue
        if ($key -and $key.GetValue($RunValue, $null) -ne $null) {
            Remove-ItemProperty -Path $RunKey -Name $RunValue
            Ok "Run entry removed"
        } else { Info 'No Run entry present' }
    } catch { Info "Run entry: $_" }
    Step 'Removing Start Menu shortcuts'
    Remove-Shortcuts
    Ok 'Shortcuts removed'
    Step 'Removing the program folder'
    if (Test-Path $TargetDir) {
        # Give the file system a beat to release the just-killed exe.
        Start-Sleep -Milliseconds 600
        Remove-Item $TargetDir -Recurse -Force
        Ok "Removed $TargetDir"
    } else { Info 'Nothing installed at the target location' }
    Write-Host ''
    Ok "$AppName uninstalled. Your settings in %APPDATA%\$AppSlug were kept;"
    Write-Host '     delete that folder too if you want a fully clean slate.'
    exit 0
}

# ----------------------------------------------------------------- install --
Write-Host "Installing $AppName for $env:USERNAME..." -ForegroundColor Yellow

if (-not (Test-Path (Join-Path $SourceDir $ExeName))) {
    Write-Host "ERROR: $SourceDir\$ExeName not found." -ForegroundColor Red
    Write-Host 'Run this script from the extracted zip folder (next to the PS3HeadsetHub folder).'
    exit 1
}

Step 'Stopping any running instance'
Stop-RunningApp

Step "Copying the app to $TargetDir"
if (Test-Path $TargetDir) { Remove-Item $TargetDir -Recurse -Force }
Start-Sleep -Milliseconds 600
Copy-Item $SourceDir $TargetDir -Recurse -Force
if (-not (Test-Path $TargetExe)) { Write-Host 'ERROR: copy failed.' -ForegroundColor Red; exit 1 }
Ok 'Program files in place'

Step 'Creating Start Menu shortcuts'
New-Item -ItemType Directory -Path $MenuDir -Force | Out-Null
New-Shortcut (Join-Path $MenuDir "$AppName.lnk") $TargetExe '' $AppName $TargetDir
$uninstaller = Join-Path $TargetDir 'uninstall.ps1'
New-Shortcut (Join-Path $MenuDir "Uninstall $AppName.lnk") 'powershell.exe' `
    "-NoProfile -ExecutionPolicy Bypass -File `"$uninstaller`"" "Remove $AppName" $TargetDir
Ok 'Shortcuts created'

if ($DesktopShortcut) {
    $desktop = [Environment]::GetFolderPath('Desktop')
    New-Shortcut (Join-Path $desktop "$AppName.lnk") $TargetExe '' $AppName $TargetDir
    Ok 'Desktop shortcut created'
}

if ($NoAutostart) {
    Info 'Skipping the start-with-Windows entry (-NoAutostart)'
} else {
    Step 'Registering start with Windows (system tray)'
    $runCommand = "`"$TargetExe`" --tray"
    try {
        New-ItemProperty -Path $RunKey -Name $RunValue -Value $runCommand `
            -PropertyType String -Force | Out-Null
        Ok "Starts in the tray at sign-in: $runCommand"
    } catch { Write-Host "ERROR: could not write the Run entry: $_" -ForegroundColor Red; exit 1 }
}

# Copy the uninstaller from the zip if present, else synthesise one that
# re-runs this script in uninstall mode from the installed copy.
$installerCopy = Join-Path $TargetDir 'uninstall.ps1'
Copy-Item $PSScriptRoot\install.ps1 $installerCopy -Force
# A copy inside the target uninstalls via the -Uninstall switch itself.
Ok 'Uninstaller written (Start Menu > Uninstall PS3 Headset Hub)'

Write-Host ''
Ok "$AppName installed."
Write-Host '     Start it now from the Start Menu, or launch:'
Write-Host "     $TargetExe"
if (-not $NoAutostart) {
    Write-Host '     It will start itself in the tray the next time you sign in.'
}
