# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller specification for the standalone installer.

Build with the tool (recommended - it verifies the payload and reports sizes):

    python packaging/build_installer.py     # -> <repo>/build/PS3HeadsetHubInstaller.exe

Run it after ``packaging/build_exe.py``: this spec packs the existing onedir
application folder (``build/PS3HeadsetHub/``) into a single windowed exe, so
users get one file with a proper installer window instead of a zip plus a
PowerShell script. At runtime the payload is unpacked next to the installer
(``sys._MEIPASS/PS3HeadsetHub``) and copied into %LOCALAPPDATA% by
``installer_gui.py``.
"""

from pathlib import Path

# The spec file runs with SPECPATH set to its own directory.
SPECPATH_PATH = Path(SPECPATH)        # noqa: F821 - injected by PyInstaller
ROOT = SPECPATH_PATH.parent           # .../app
BUILD = ROOT.parent / "build"         # repository build/
PAYLOAD = BUILD / "PS3HeadsetHub"     # the onedir app built by build_exe.py
ICON = SPECPATH_PATH / "ps3hub.ico"
# build_installer.py writes a dedicated version resource for the installer;
# fall back to the app's own so a bare `pyinstaller installer.spec` works too.
VERSION_FILE = SPECPATH_PATH / "version_info.txt"
STAGING = SPECPATH_PATH / "_installer_staging"
if (STAGING / "installer_version_info.txt").exists():
    VERSION_FILE = STAGING / "installer_version_info.txt"

if not (PAYLOAD / "PS3HeadsetHub.exe").exists():
    raise SystemExit(
        "installer.spec: the application payload is missing.\n"
        f"Expected: {PAYLOAD}\n"
        "Run 'python packaging/build_exe.py' first, then build_installer.py.")

# Single source of truth for the version shown in the installer window:
# read APP_VERSION straight out of the package.
app_version = "0.0.0"
for line in (ROOT / "ps3hub" / "__init__.py").read_text(encoding="utf-8").splitlines():
    if line.startswith("APP_VERSION"):
        app_version = line.split("=")[1].strip().strip('"')
        break
STAGING.mkdir(parents=True, exist_ok=True)
(STAGING / "installer_version.txt").write_text(app_version + "\n",
                                               encoding="utf-8")

datas = [
    # The whole application folder, kept under its original name so
    # installer_gui.payload_dir() can find it in the frozen exe.
    (str(PAYLOAD), "PS3HeadsetHub"),
    # The PowerShell uninstaller written next to the installed app (the Start
    # Menu "Uninstall" entry runs it with -Uninstall).
    (str(SPECPATH_PATH / "install.ps1"), "."),
    (str(STAGING / "installer_version.txt"), "."),
]
if ICON.exists():
    datas.append((str(ICON), "."))

block_cipher = None

analysis = Analysis(                   # noqa: F821
    [str(SPECPATH_PATH / "installer_gui.py")],
    pathex=[str(SPECPATH_PATH), str(ROOT)],   # packaging/ + app/ (for ps3hub)
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "numpy", "PIL", "scipy", "pandas", "matplotlib", "pytest",
        "unittest", "pydoc", "doctest", "sounddevice", "hid",
        "ttkbootstrap",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(analysis.pure, analysis.zipped_data, cipher=block_cipher)  # noqa: F821

exe = EXE(                             # noqa: F821
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.zipfiles,
    analysis.datas,
    [],                                # no COLLECT: one-file layout
    name="PS3HeadsetHubInstaller",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                     # a proper window, no console flash
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON) if ICON.exists() else None,
    version=str(VERSION_FILE) if VERSION_FILE.exists() else None,
)
