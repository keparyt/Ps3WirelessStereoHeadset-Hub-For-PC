# -*- mode: powershell -*-
# Install-PS3HeadsetHub.ps1 - install the Hub for the current Windows user.
#
# What it does:
#   1. Copies the onedir app (PS3HeadsetHub\) into %LOCALAPPDATA%\PS3HeadsetHub
#   2. Creates Start Menu shortcuts: "PS3 Headset Hub" and "Uninstall PS3 Headset Hub"
#   3. (default) registers an HKCU Run entry so the Hub starts minimized in
#      the system tray at Windows sign-in. Pass -NoAutostart to skip.
#   4. Writes uninstall.ps1 next to the installed app
#   5. Offers to download and silently install FxSound (the audio engine the
#      Hub drives) when it is missing. The setup runs unattended and installs
#      per-machine into Program Files; only its own UAC elevation (and any
#      driver prompt Windows shows) needs attention.
#      -NoFxSound skips the offer, -WithFxSound installs without asking.
#
# Nothing touches machine-wide state: no admin rights, no other users.
#
# Usage (from the extracted zip folder):
#   powershell -ExecutionPolicy Bypass -File install.ps1              # full install
#   powershell ... -File install.ps1 -NoAutostart                     # no start-with-Windows
#   powershell ... -File install.ps1 -DesktopShortcut                 # also a desktop icon
#   powershell ... -File install.ps1 -Uninstall                       # remove everything
#   powershell ... -File install.ps1 -NoFxSound                        # don't offer FxSound
#   powershell ... -File install.ps1 -WithFxSound                      # install FxSound without asking

[CmdletBinding()]
param(
    [switch]$NoAutostart,
    [switch]$DesktopShortcut,
    [switch]$Uninstall,
    [switch]$NoFxSound,
    [switch]$WithFxSound
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
#: Official FxSound setup. "latest" is a moving GitHub release tag, so this
#: URL is stable and always serves the current build.
$FxSoundUrl   = 'https://github.com/fxsound2/fxsound-app/releases/download/latest/fxsound_setup.exe'
$FxSoundSetup = Join-Path $env:TEMP 'fxsound_setup.exe'

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

function Test-FxSoundInstalled {
    # Mirrors the Hub's own detection (ps3hub.audio.fxsound_backend).
    $dirs = @()
    if ($env:ProgramFiles) {
        $dirs += (Join-Path $env:ProgramFiles 'FxSound LLC\FxSound')
        $dirs += (Join-Path $env:ProgramFiles 'FxSound')
    }
    if (${env:ProgramFiles(x86)}) {
        $dirs += (Join-Path ${env:ProgramFiles(x86)} 'FxSound LLC\FxSound')
    }
    if ($env:LOCALAPPDATA) {
        $dirs += (Join-Path $env:LOCALAPPDATA 'Programs\FxSound')
        $dirs += (Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps')
    }
    foreach ($dir in $dirs) {
        if (Test-Path (Join-Path $dir 'fxsound.exe')) { return $true }
    }
    return [bool](Get-Command fxsound -ErrorAction SilentlyContinue)
}

function Install-FxSoundSilently {
    # Download the official setup and run it unattended (/VERYSILENT). It has a
    # requireAdministrator manifest, so Windows shows one UAC prompt - that is
    # the only interaction; everything else, including the install into
    # Program Files, happens without further clicks.
    Remove-Item $FxSoundSetup -Force -ErrorAction SilentlyContinue
    Step 'Downloading the FxSound installer (this can take a minute)'
    try {
        $ProgressPreference = 'SilentlyContinue'   # IWR's progress bar is slow
        [Net.ServicePointManager]::SecurityProtocol = `
            [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $FxSoundUrl -OutFile $FxSoundSetup -UseBasicParsing
    } catch {
        Write-Host "Could not download FxSound: $_" -ForegroundColor Red
        Write-Host "Get it manually from: $FxSoundUrl" -ForegroundColor Yellow
        return $false
    } finally {
        $ProgressPreference = 'Continue'
    }
    $size = [math]::Round((Get-Item $FxSoundSetup).Length / 1MB, 1)
    Ok "Downloaded $size MiB"

    Step 'Installing FxSound silently (confirm the UAC prompt if it appears)'
    try {
        $process = Start-Process -FilePath $FxSoundSetup `
            -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART' `
            -PassThru -Wait
    } catch {
        Write-Host "Could not start the FxSound setup: $_" -ForegroundColor Red
        Write-Host "Run it manually from: $FxSoundSetup" -ForegroundColor Yellow
        return $false
    }
    if ($process.ExitCode -ne 0) {
        Write-Host "The FxSound setup exited with code $($process.ExitCode)." -ForegroundColor Yellow
        Write-Host "You can install it later from: $FxSoundUrl" -ForegroundColor Yellow
        return $false
    }
    if (Test-FxSoundInstalled) {
        Ok 'FxSound installed'
    } else {
        Write-Host 'FxSound setup finished. If the Hub cannot find it yet, reboot once.' -ForegroundColor Yellow
    }
    return $true
}

function Offer-FxSound {
    if ($NoFxSound) { Info 'FxSound suggestion skipped (-NoFxSound)'; return }
    Step 'Checking for FxSound (the audio engine the Hub drives)'
    if (Test-FxSoundInstalled) { Ok 'FxSound is already installed'; return }
    Write-Host ''
    Write-Host 'The Hub drives FxSound, which is not installed yet. Without it the' -ForegroundColor Yellow
    Write-Host 'equalizer and effects cannot be applied.' -ForegroundColor Yellow
    if (-not $WithFxSound) {
        $answer = Read-Host 'Download and install FxSound now, silently? [Y/n]'
        if ($answer -match '^\s*(n|no)\s*$') {
            Info 'Skipped. You can re-run this installer, or get FxSound from:'
            Write-Host "  $FxSoundUrl"
            return
        }
    }
    Install-FxSoundSilently | Out-Null
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

Offer-FxSound

Write-Host ''
Ok "$AppName installed."
Write-Host '     Start it now from the Start Menu, or launch:'
Write-Host "     $TargetExe"
if (-not $NoAutostart) {
    Write-Host '     It will start itself in the tray the next time you sign in.'
}
if (Test-FxSoundInstalled) {
    Write-Host '     FxSound is installed, so the audio features are ready to use.'
} else {
    Write-Host "     Audio features need FxSound: $FxSoundUrl"
}
