"""Download and silently install FxSound, the audio engine the Hub drives.

One implementation shared by three surfaces:

* the standalone installer (``packaging/installer_gui.py``),
* the PowerShell installer (``packaging/install.ps1``, kept in sync by test),
* the Hub itself - the Audio page's "Install FxSound" button.

The flow is always the same: check whether ``fxsound.exe`` is already
reachable, download the official setup (the ``latest`` release tag is a
stable URL), then run it unattended with Inno Setup's silent switches so it
installs per-machine into Program Files. The only interaction is the UAC
elevation prompt the setup's own manifest triggers - the Hub never asks for
admin rights itself.

This module is deliberately dependency-free (stdlib only, no logging
framework) so the standalone installer can bundle it without pulling in the
application.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

IS_WINDOWS = os.name == "nt"

#: Official FxSound setup. "latest" is a moving GitHub release tag, so this
#: URL is stable and always serves the current build.
FXSOUND_URL = ("https://github.com/fxsound2/fxsound-app/releases/download/"
               "latest/fxsound_setup.exe")

#: Where the downloaded setup is parked before it runs.
FXSOUND_SETUP_NAME = "fxsound_setup.exe"

#: Inno Setup switches: no wizard pages, no message boxes, no reboot. The
#: setup's requireAdministrator manifest still raises one UAC prompt, which
#: is the only interaction; everything else lands in Program Files.
FXSOUND_SILENT_ARGS = ("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART")

#: The setup can legitimately take a while on slow links; past this, call it
#: stuck instead of hanging forever.
SETUP_TIMEOUT_SECONDS = 900


def fxsound_install_dirs() -> list[Path]:
    """Directories where a per-machine or per-user FxSound may live.

    Mirrors what the backend considers a valid installation
    (ps3hub.audio.fxsound_backend) so "already installed" answers agree
    everywhere.
    """
    dirs: list[Path] = []
    program_files = os.environ.get("ProgramFiles")
    if program_files:
        dirs.append(Path(program_files) / "FxSound LLC" / "FxSound")
        dirs.append(Path(program_files) / "FxSound")
    program_files_x86 = os.environ.get("ProgramFiles(x86)")
    if program_files_x86:
        dirs.append(Path(program_files_x86) / "FxSound LLC" / "FxSound")
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        dirs.append(Path(local_appdata) / "Programs" / "FxSound")
        dirs.append(Path(local_appdata) / "Microsoft" / "WindowsApps")
    return dirs


def find_fxsound() -> Path | None:
    """First existing fxsound.exe, or None when FxSound is not installed."""
    for directory in fxsound_install_dirs():
        candidate = directory / "fxsound.exe"
        if candidate.exists():
            return candidate
    on_path = shutil.which("fxsound")
    return Path(on_path) if on_path else None


def fxsound_is_installed() -> bool:
    return find_fxsound() is not None


def setup_download_path() -> Path:
    return Path(os.environ.get("TEMP", ".")) / FXSOUND_SETUP_NAME


def download_file(url: str, destination: Path, progress=None) -> Path:
    """Download *url* to *destination*, reporting (done, total) to *progress*.

    ``total`` may be 0 when the server does not send a Content-Length
    (GitHub release assets do). Raises on network errors - callers translate
    that into a friendly message.
    """
    request = urllib.request.Request(
        url, headers={"User-Agent": "PS3HeadsetHub-fxsound-installer"})
    destination.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(request, timeout=60) as response:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        with open(destination, "wb") as sink:
            while True:
                chunk = response.read(1 << 16)
                if not chunk:
                    break
                sink.write(chunk)
                done += len(chunk)
                if progress is not None:
                    progress(done, total)
    if progress is not None:
        progress(done, done)
    return destination


def install_fxsound_silently(report=print, progress=None) -> bool:
    """Make sure FxSound is installed; download + run it silently if not.

    ``report(text)`` narrates each step (print, a Tk label, the installer
    log...) and ``progress(done, total)`` tracks the download. Returns
    whether FxSound is usable afterwards.
    """
    report("Checking for FxSound (the audio engine the Hub drives)...")
    existing = find_fxsound()
    if existing is not None:
        report(f"FxSound is already installed ({existing}).")
        return True
    if not IS_WINDOWS:
        report("FxSound auto-install is only available on Windows.")
        return False

    report(f"Downloading the FxSound installer from {FXSOUND_URL} ...")
    setup = setup_download_path()
    try:
        download_file(FXSOUND_URL, setup, progress=progress)
    except OSError as error:
        report(f"Could not download FxSound: {error}")
        report("Check the internet connection, or install it manually from "
               "https://www.fxsound.com/download")
        return False
    report(f"Downloaded {setup.stat().st_size / (1 << 20):.1f} MiB.")

    report("Installing FxSound silently "
           "(confirm the UAC prompt if it appears)...")
    try:
        completed = subprocess.run(
            [str(setup), *FXSOUND_SILENT_ARGS],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=SETUP_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as error:
        report(f"Could not start the FxSound setup: {error}")
        report(f"Run it manually from: {setup}")
        return False
    finally:
        try:
            setup.unlink()
        except OSError:
            pass

    if completed.returncode != 0:
        report(f"The FxSound setup exited with code {completed.returncode}.")
        report("It was probably cancelled. You can retry here, or install "
               "it manually from https://www.fxsound.com/download")
        return False
    if find_fxsound() is None:
        report("FxSound setup finished. If the Hub cannot find it yet, "
               "sign out and back in (or reboot) once.")
        return True
    # Give the new install a beat to settle its files before we probe it.
    time.sleep(0.5)
    report("FxSound installed.")
    return True
