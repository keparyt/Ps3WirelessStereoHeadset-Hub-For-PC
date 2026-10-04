#!/usr/bin/env python3
"""Standalone GUI installer for the PS3 Wireless Stereo Headset Hub.

Built by :mod:`packaging.build_installer` into a single
``PS3HeadsetHubInstaller.exe`` (PyInstaller onefile) that carries the whole
application folder as payload. Double-clicking it opens a proper installer
window - no PowerShell, no console - with three choices:

* where to install (default ``%LOCALAPPDATA%\\PS3HeadsetHub``, current user,
  no admin rights),
* whether the Hub should start in the tray at sign-in,
* whether to also download and silently install **FxSound**, the audio engine
  the Hub drives, when it is missing. The FxSound setup runs unattended
  (``/VERYSILENT``) and installs per-machine into Program Files; the only
  interaction left is the elevation prompt its own manifest triggers.

The same operations are available headless for scripted installs::

    PS3HeadsetHubInstaller.exe --silent              # defaults, incl. FxSound
    PS3HeadsetHubInstaller.exe --silent --no-fxsound
    PS3HeadsetHubInstaller.exe --uninstall --silent

This module is intentionally dependency-free (stdlib + tkinter) so the
installer stays small and never pulls the app's audio stack. The FxSound
download-and-silent-install logic is shared with the Hub itself and lives
in ``ps3hub.audio.fxsound_install`` (stdlib only), so all three surfaces -
this installer, install.ps1 and the in-app button - behave identically.
"""

from __future__ import annotations

import argparse
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from ps3hub.audio.fxsound_install import (
    FXSOUND_SILENT_ARGS,
    FXSOUND_URL,
    find_fxsound,
    install_fxsound_silently,
)

IS_WINDOWS = os.name == "nt"

APP_NAME = "PS3 Headset Hub"
APP_SLUG = "PS3HeadsetHub"
EXE_NAME = "PS3HeadsetHub.exe"

#: Registry value name for the start-with-Windows entry. Must stay in sync
#: with install.ps1 ($RunValue) so either installer's uninstaller cleans up
#: after both.
RUN_VALUE = "PS3PS3HeadsetHub"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def log(message: str) -> None:
    print(message, flush=True)


def resource_path(name: str) -> Path | None:
    """Locate a file bundled next to this script (or inside the frozen exe)."""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        base = Path(__file__).resolve().parent
    candidate = base / name
    return candidate if candidate.exists() else None


def payload_dir() -> Path | None:
    """The bundled application folder (PS3HeadsetHub/) carried by this exe."""
    candidate = resource_path(APP_SLUG)
    if candidate is not None and (candidate / EXE_NAME).exists():
        return candidate
    return None


def installer_version() -> str:
    """Version of the bundled app, written by build_installer.py."""
    file = resource_path("installer_version.txt")
    if file is not None:
        return file.read_text(encoding="utf-8").strip() or "?"
    return "?"


def default_target_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
    return Path(base) / APP_SLUG


def start_menu_dir() -> Path:
    """The user's Programs folder (no admin rights needed)."""
    script = "[Environment]::GetFolderPath('Programs')"
    try:
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=30,
            creationflags=_no_window(),
        )
        folder = out.stdout.strip()
        if folder:
            return Path(folder)
    except (OSError, subprocess.SubprocessError):
        pass
    fallback = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
    return Path(fallback) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def desktop_dir() -> Path:
    try:
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             "[Environment]::GetFolderPath('Desktop')"],
            capture_output=True, text=True, timeout=30,
            creationflags=_no_window(),
        )
        folder = out.stdout.strip()
        if folder:
            return Path(folder)
    except (OSError, subprocess.SubprocessError):
        pass
    return Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"


def _no_window() -> int:
    """Flag that keeps child console windows from flashing."""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0


# ------------------------------------------------------------- Windows bits --

def stop_running_app(report=log) -> None:
    """Kill any running instance so its files can be replaced or removed."""
    if not IS_WINDOWS:
        return
    for image in (EXE_NAME, f"{APP_SLUG}_smoke.exe"):
        completed = subprocess.run(
            ["taskkill", "/IM", image, "/F"],
            capture_output=True, creationflags=_no_window(),
        )
        if completed.returncode == 0:
            report(f"Stopped a running instance ({image}).")
    time.sleep(0.6)  # let the file system release the just-killed exe


def _ps_quote(text: str) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def create_shortcut(path: Path, target: Path, arguments: str = "",
                    description: str = "", working_dir: str = "") -> None:
    """Create a .lnk via the WScript.Shell COM object (what install.ps1 does)."""
    script = (
        f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut("
        f"{_ps_quote(path)})"
        f"; $s.TargetPath = {_ps_quote(target)}"
        + (f"; $s.Arguments = {_ps_quote(arguments)}" if arguments else "")
        + (f"; $s.Description = {_ps_quote(description)}" if description else "")
        + (f"; $s.WorkingDirectory = {_ps_quote(working_dir)}" if working_dir else "")
        + f"; $s.IconLocation = {_ps_quote(str(target) + ',0')}"
        + "; $s.Save()"
    )
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-Command", script],
        check=True, timeout=30, creationflags=_no_window(),
    )


def add_run_entry(target_exe: Path) -> None:
    """Register the start-with-Windows (system tray) entry for the current user."""
    import winreg  # deferred: keeps the module importable off-Windows

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                        winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ,
                          f'"{target_exe}" --tray')


def remove_run_entry() -> None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, RUN_VALUE)
    except FileNotFoundError:
        pass


def write_uninstaller(target_dir: Path, report=log) -> None:
    """Drop the PowerShell uninstaller next to the installed app.

    The Start Menu entry runs it with -Uninstall; the same file backs the
    double-click uninstaller.cmd flow of the zip distribution.
    """
    source = resource_path("install.ps1")
    if source is None:
        report("WARNING: bundled uninstaller missing; "
               "the Start Menu uninstall entry was not written.")
        return
    shutil.copy2(source, target_dir / "uninstall.ps1")


# ----------------------------------------------------------------- actions --

class InstallOptions:
    """Everything the install needs; shared by the GUI and the CLI."""

    def __init__(self, target_dir: Path = None, autostart: bool = True,
                 desktop_shortcut: bool = False, install_fxsound: bool = True):
        self.target_dir = Path(target_dir) if target_dir else default_target_dir()
        self.autostart = autostart
        self.desktop_shortcut = desktop_shortcut
        self.install_fxsound = install_fxsound


def perform_install(options: InstallOptions, report=log, progress=None) -> bool:
    """Copy the payload in place, wire shortcuts, optionally add FxSound."""
    payload = payload_dir()
    if payload is None:
        report("ERROR: the application payload is missing from this "
               "installer build.")
        return False

    target = options.target_dir
    target_exe = target / EXE_NAME

    report(f"Installing {APP_NAME} {installer_version()} "
           f"for the current user...")
    report(f"Location: {target}")

    report("Stopping any running instance...")
    stop_running_app(report)

    report("Copying program files...")
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
        time.sleep(0.4)
    try:
        shutil.copytree(payload, target)
    except OSError as error:
        report(f"ERROR: could not copy the program files: {error}")
        return False
    if not target_exe.exists():
        report("ERROR: copy failed; the main executable is missing.")
        return False
    report("Program files in place.")

    report("Creating Start Menu shortcuts...")
    menu_dir = start_menu_dir() / APP_NAME
    menu_dir.mkdir(parents=True, exist_ok=True)
    try:
        create_shortcut(menu_dir / f"{APP_NAME}.lnk", target_exe,
                        description=APP_NAME, working_dir=str(target))
        write_uninstaller(target, report)
        create_shortcut(
            menu_dir / f"Uninstall {APP_NAME}.lnk", Path("powershell.exe"),
            arguments=(f"-NoProfile -ExecutionPolicy Bypass -File "
                       f"\"{target / 'uninstall.ps1'}\" -Uninstall"),
            description=f"Remove {APP_NAME}", working_dir=str(target),
        )
    except (OSError, subprocess.SubprocessError) as error:
        report(f"WARNING: shortcut creation failed: {error}")
    report("Shortcuts created.")

    if options.desktop_shortcut:
        try:
            create_shortcut(desktop_dir() / f"{APP_NAME}.lnk", target_exe,
                            description=APP_NAME, working_dir=str(target))
            report("Desktop shortcut created.")
        except (OSError, subprocess.SubprocessError) as error:
            report(f"WARNING: desktop shortcut failed: {error}")

    if options.autostart:
        try:
            add_run_entry(target_exe)
            report("Starts in the tray at sign-in.")
        except OSError as error:
            report(f"WARNING: could not write the autostart entry: {error}")
    else:
        report("Autostart skipped.")

    if options.install_fxsound:
        install_fxsound_silently(progress=progress, report=report)

    report("")
    report(f"{APP_NAME} installed.")
    report(f"Launch it with: {target_exe}")
    return True


def perform_uninstall(target_dir: Path = None, report=log) -> bool:
    """Remove the program folder, shortcuts, autostart. Settings are kept."""
    target = Path(target_dir) if target_dir else default_target_dir()
    report(f"Uninstalling {APP_NAME} (current user only)...")
    stop_running_app(report)

    report("Removing the start-with-Windows entry...")
    try:
        remove_run_entry()
        report("Run entry removed.")
    except OSError as error:
        report(f"Run entry: {error}")

    report("Removing Start Menu shortcuts...")
    shutil.rmtree(start_menu_dir() / APP_NAME, ignore_errors=True)
    desktop_link = desktop_dir() / f"{APP_NAME}.lnk"
    try:
        desktop_link.unlink()
    except OSError:
        pass
    report("Shortcuts removed.")

    report("Removing the program folder...")
    if target.exists():
        # When this installer itself runs from inside the target folder its
        # image file is locked; move it out of the way before deleting.
        frozen_self = Path(sys.executable) if getattr(sys, "frozen", False) else None
        preserve = None
        if frozen_self is not None:
            try:
                frozen_self.relative_to(target)
                preserve = frozen_self
            except ValueError:
                preserve = None
        if preserve is not None:
            moved = Path(os.environ.get("TEMP", ".")) / preserve.name
            try:
                shutil.move(str(preserve), str(moved))
                report("Set the installer itself aside (it was running "
                       "from the folder being removed).")
            except OSError:
                pass
        shutil.rmtree(target, ignore_errors=True)
        if target.exists():
            report(f"WARNING: could not fully remove {target}; "
                   "close the app and delete the folder manually.")
        else:
            report(f"Removed {target}.")
    else:
        report("Nothing installed at the target location.")

    report("")
    report(f"{APP_NAME} uninstalled. Your settings in %APPDATA%\\{APP_SLUG} "
           "were kept.")
    return True


# --------------------------------------------------------------------- GUI --

class InstallerApp:
    """The installer window. Owns the Tk root and a worker thread."""

    def __init__(self, root, *, uninstall_mode: bool = False,
                 options: InstallOptions | None = None):
        self.root = root
        self.options = options or InstallOptions()
        self.uninstall_mode = uninstall_mode
        self.messages: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.busy = False
        self.installed_exe: Path | None = None

        self._apply_dpi_awareness()
        self.root.title(f"{APP_NAME} installer")
        self.root.minsize(560, 560)
        self._build_widgets()
        self.root.after(100, self._poll_messages)
        if uninstall_mode:
            self._enter_uninstall_mode()

    @staticmethod
    def _apply_dpi_awareness() -> None:
        # Crisp text on high-DPI screens; harmless if unsupported.
        if IS_WINDOWS:
            try:
                import ctypes

                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except (OSError, AttributeError):
                pass

    def _build_widgets(self) -> None:
        self.root.option_add("*Font", ("Segoe UI", 10))
        padding = {"padx": 16, "pady": 4}

        header = ttk.Frame(self.root)
        header.pack(fill="x", padx=16, pady=(16, 8))
        title = ttk.Label(header, text=APP_NAME,
                          font=("Segoe UI", 16, "bold"))
        title.pack(anchor="w")
        ttk.Label(
            header,
            text=(f"Version {installer_version()}  -  installs for the "
                  "current user (no admin rights required)"),
        ).pack(anchor="w")

        self.options_frame = ttk.Labelframe(
            self.root, text="Options", padding=10)
        self.options_frame.pack(fill="x", **padding)

        location_row = ttk.Frame(self.options_frame)
        location_row.pack(fill="x", pady=(0, 8))
        ttk.Label(location_row, text="Install to:").pack(side="left")
        self.target_var = tk.StringVar(value=str(self.options.target_dir))
        self.target_entry = ttk.Entry(location_row, textvariable=self.target_var)
        self.target_entry.pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(location_row, text="Browse...",
                   command=self._browse).pack(side="left")

        self.autostart_var = tk.BooleanVar(value=self.options.autostart)
        ttk.Checkbutton(
            self.options_frame, variable=self.autostart_var,
            text="Start with Windows (in the system tray)",
        ).pack(anchor="w", pady=2)

        self.desktop_var = tk.BooleanVar(value=self.options.desktop_shortcut)
        ttk.Checkbutton(
            self.options_frame, variable=self.desktop_var,
            text="Also create a desktop shortcut",
        ).pack(anchor="w", pady=2)

        self.fxsound_var = tk.BooleanVar(value=self.options.install_fxsound)
        self.fxsound_checkbox = ttk.Checkbutton(
            self.options_frame, variable=self.fxsound_var,
            text="Install FxSound, the audio engine this Hub drives "
                 "(recommended)",
        )
        self.fxsound_checkbox.pack(anchor="w", pady=2)
        if find_fxsound() is not None:
            self.fxsound_var.set(False)
            self.fxsound_checkbox.state(["disabled"])
            self.fxsound_checkbox.configure(
                text="FxSound is already installed")
        elif IS_WINDOWS:
            self.fxsound_checkbox.configure(
                text="Install FxSound, the audio engine this Hub drives "
                     "(recommended; silent - only its UAC prompt needs a "
                     "click)")

        buttons = ttk.Frame(self.root)
        buttons.pack(fill="x", **padding)
        self.install_button = ttk.Button(buttons, text="Install",
                                         command=self._start_install)
        self.install_button.pack(side="left")
        self.uninstall_button = ttk.Button(
            buttons, text="Uninstall...", command=self._start_uninstall)
        self.uninstall_button.pack(side="left", padx=8)
        self.launch_button = ttk.Button(
            buttons, text="Launch the Hub", command=self._launch,
            state="disabled")
        self.launch_button.pack(side="left", padx=8)
        self.close_button = ttk.Button(buttons, text="Close",
                                       command=self.root.destroy)
        self.close_button.pack(side="right")

        self.progress = ttk.Progressbar(self.root, maximum=100)
        self.progress.pack(fill="x", **padding)

        self.log_text = tk.Text(self.root, height=14, wrap="word",
                                state="disabled", relief="flat",
                                background="#111111", foreground="#d8d8d8")
        self.log_text.pack(fill="both", expand=True, **padding)

        self.status_var = tk.StringVar(
            value="Ready. Click Install to set everything up.")
        ttk.Label(self.root, textvariable=self.status_var,
                  anchor="w").pack(fill="x", padx=16, pady=(0, 12))

    # ---- user actions ------------------------------------------------------

    def _browse(self) -> None:
        chosen = filedialog.askdirectory(initialdir=str(self.target_var.get()))
        if chosen:
            self.target_var.set(str(Path(chosen) / APP_SLUG))

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        for widget in (self.install_button, self.uninstall_button,
                       self.close_button, self.target_entry):
            widget.state(["disabled" if busy else "!disabled"])
        if busy:
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.configure(value=0)

    def _start_install(self) -> None:
        if self.busy:
            return
        target = Path(self.target_var.get().strip() or default_target_dir())
        self.options = InstallOptions(
            target_dir=target,
            autostart=self.autostart_var.get(),
            desktop_shortcut=self.desktop_var.get(),
            install_fxsound=(self.fxsound_var.get()
                             and "disabled" not in self.fxsound_checkbox.state()),
        )
        self._set_busy(True)
        self.status_var.set("Installing...")
        self._clear_log()
        self.worker = threading.Thread(
            target=self._run_worker,
            args=(lambda message: self.messages.put(("log", message)),
                  lambda done, total: self.messages.put(
                      ("progress", done, total)),
                  lambda success: self.messages.put(("install-done", success)),
                  self.options),
            daemon=True,
        )
        self.worker.start()

    def _start_uninstall(self) -> None:
        if self.busy:
            return
        if not messagebox.askyesno(
                f"Uninstall {APP_NAME}",
                f"Remove {APP_NAME}?\n\n"
                f"Your settings in %APPDATA%\\{APP_SLUG} are kept."):
            return
        self._set_busy(True)
        self.status_var.set("Uninstalling...")
        self._clear_log()
        self.worker = threading.Thread(
            target=self._run_worker,
            args=(lambda message: self.messages.put(("log", message)),
                  None,
                  lambda success: self.messages.put(
                      ("uninstall-done", success)),
                  None),
            daemon=True,
        )
        self.worker.start()

    @staticmethod
    def _run_worker(report, progress, finished, options) -> None:
        if options is None:
            finished(perform_uninstall(report=report))
        else:
            finished(perform_install(options, report=report,
                                     progress=progress))

    def _launch(self) -> None:
        if self.installed_exe is None:
            return
        if IS_WINDOWS:
            os.startfile(self.installed_exe)  # noqa: S606 - intended
        else:
            subprocess.Popen([str(self.installed_exe)])
        self.status_var.set("Launched.")

    def _enter_uninstall_mode(self) -> None:
        self.options_frame.pack_forget()
        self.install_button.pack_forget()
        self.status_var.set("Ready to uninstall.")
        self.uninstall_button.configure(text="Uninstall")
        self._start_uninstall()

    # ---- worker messages ---------------------------------------------------

    def _poll_messages(self) -> None:
        try:
            while True:
                message = self.messages.get_nowait()
                kind = message[0]
                if kind == "log":
                    self._append_log(message[1])
                elif kind == "progress":
                    done, total = message[1], message[2]
                    if total:
                        self.progress.configure(value=100 * done / total)
                elif kind == "install-done":
                    self._install_finished(message[1])
                elif kind == "uninstall-done":
                    self._uninstall_finished(message[1])
        except queue.Empty:
            pass
        self.root.after(100, self._poll_messages)

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _append_log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _install_finished(self, success: bool) -> None:
        self._set_busy(False)
        if success:
            self.installed_exe = self.options.target_dir / EXE_NAME
            self.status_var.set("Installed. You can launch the Hub now.")
            self.launch_button.state(["!disabled"])
        else:
            self.status_var.set("Install did not complete - read the log.")

    def _uninstall_finished(self, success: bool) -> None:
        self._set_busy(False)
        if success:
            self.status_var.set("Uninstalled. You can close this window.")
            self.launch_button.state(["disabled"])
        else:
            self.status_var.set("Uninstall did not complete - read the log.")


# --------------------------------------------------------------------- CLI --

def _ensure_stdio() -> None:
    """Give the windowed exe a stdout/stderr sink so prints do not crash.

    A console=False PyInstaller exe starts with sys.stdout and sys.stderr set
    to None; argparse's --help (and the --silent log) write to them, which
    would raise AttributeError and exit non-zero for no visible reason.
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))  # noqa: SIM115


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=f"Install or remove {APP_NAME} for the current user.")
    parser.add_argument("--silent", action="store_true",
                        help="install without the window, using the options "
                             "below (FxSound is included unless --no-fxsound)")
    parser.add_argument("--uninstall", action="store_true",
                        help="remove the app instead of installing it")
    parser.add_argument("--target", type=Path, default=None,
                        help="install location (default: %%LOCALAPPDATA%%\\"
                             f"{APP_SLUG})")
    parser.add_argument("--no-autostart", action="store_true",
                        help="do not register start-with-Windows")
    parser.add_argument("--desktop-shortcut", action="store_true",
                        help="also create a desktop shortcut")
    parser.add_argument("--with-fxsound", action="store_true",
                        help="install FxSound if missing (default in "
                             "--silent)")
    parser.add_argument("--no-fxsound", action="store_true",
                        help="never touch FxSound")
    return parser


def main(argv: list[str] | None = None) -> int:
    _ensure_stdio()
    args = build_parser().parse_args(argv)

    if args.uninstall:
        if args.silent:
            return 0 if perform_uninstall(report=log) else 1
        root = tk.Tk()
        InstallerApp(root, uninstall_mode=True)
        root.mainloop()
        return 0

    install_fxsound = not args.no_fxsound
    options = InstallOptions(
        target_dir=args.target,
        autostart=not args.no_autostart,
        desktop_shortcut=args.desktop_shortcut,
        install_fxsound=install_fxsound,
    )
    if args.silent:
        return 0 if perform_install(options, report=log) else 1

    root = tk.Tk()
    try:
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except tk.TclError:
        pass
    InstallerApp(root, options=options)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
