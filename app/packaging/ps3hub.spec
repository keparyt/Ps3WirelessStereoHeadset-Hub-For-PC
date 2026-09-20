# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller specification for the PS3 Wireless Stereo Headset Hub.

Build from the repository root:

    python -m pip install pyinstaller
    pyinstaller packaging/ps3hub.spec

The result is ``dist/PS3HeadsetHub.exe``: one windowed executable with no
console. Anything the proof of concept printed to stdout now goes to the log
file and the Diagnostics page instead, which is why suppressing the console is
safe here.
"""

import sys
from pathlib import Path

# The spec file runs with SPECPATH set to its own directory.
ROOT = Path(SPECPATH).parent          # noqa: F821 - injected by PyInstaller
ICON = ROOT / "packaging" / "ps3hub.ico"

block_cipher = None

analysis = Analysis(                   # noqa: F821
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    # Embedded in the exe by EXE(icon=...) for Explorer, and bundled here as
    # data so the running window can load it too.
    datas=[(str(ICON), ".")] if ICON.exists() else [],
    # hidapi loads its backend lazily, so PyInstaller cannot see it by
    # following imports alone.
    hiddenimports=["hid"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Large stdlib and third-party packages this application never touches.
    # Excluding them keeps the executable to a sensible size.
    excludes=[
        "numpy", "pandas", "matplotlib", "scipy", "PIL", "pytest",
        "unittest", "pydoc", "doctest", "email", "http", "xml",
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
    [],
    name="PS3HeadsetHub",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    # No console: this is a GUI application. Diagnostics live in the app.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON) if ICON.exists() else None,
)
