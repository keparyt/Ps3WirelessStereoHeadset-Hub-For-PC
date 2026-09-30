# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller specification for the PS3 Wireless Stereo Headset Hub.

Build with the tool (recommended - it writes the version resource, verifies
dependencies and produces a build report):

    python packaging/build_exe.py            # -> <repo>/build/PS3HeadsetHub/

or directly:

    python -m PyInstaller --distpath ../build --workpath ../build/_work \
        packaging/ps3hub.spec

The result is a **onedir** application: ``PS3HeadsetHub/PS3HeadsetHub.exe``
next to an ``_internal`` folder of libraries. A directory build starts
faster than a one-file exe (no archive to unpack at every launch) and is far
less likely to be quarantined by antivirus software, which is why the
one-file layout was dropped. The app logs to the per-subsystem files on the
Diagnostics page, so suppressing the console is safe here.
"""

import sys
from pathlib import Path

# The spec file runs with SPECPATH set to its own directory.
ROOT = Path(SPECPATH).parent          # noqa: F821 - injected by PyInstaller
ICON = Path(SPECPATH) / "ps3hub.ico"
EXAMPLES = ROOT / "EQExamples"
VERSION_FILE = Path(SPECPATH) / "version_info.txt"

datas = []
if ICON.exists():
    # Embedded in the exe by EXE(icon=...) for Explorer, and bundled here as
    # data so the running window can load it too.
    datas.append((str(ICON), "."))
if EXAMPLES.is_dir():
    # The Audio page's example-preset row reads these at runtime; without
    # them the dropdown is empty in the packaged app.
    for example in sorted(EXAMPLES.glob("*")):
        if example.suffix.lower() in (".fac", ".txt"):
            datas.append((str(example), "EQExamples"))

block_cipher = None

analysis = Analysis(                   # noqa: F821
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    # hidapi loads its backend lazily, so PyInstaller cannot see it by
    # following imports alone. ttkbootstrap's toast/icon machinery imports
    # submodules by string at runtime for the same reason.
    hiddenimports=[
        "hid",
        "ttkbootstrap.style.icons",
        "ttkbootstrap.style.engine",
        "ttkbootstrap.widgets.toast",
        "ttkbootstrap.constants",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Stdlib modules the application never touches. NOTE: numpy and PIL must
    # NOT be excluded - numpy is the loopback DSP's math backend and PIL
    # renders ttkbootstrap's toast icon glyphs; excluding either ships an exe
    # whose audio path or toasts are silently broken.
    excludes=[
        "pandas", "matplotlib", "scipy", "pytest",
        "unittest", "pydoc", "doctest",
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
    [],
    exclude_binaries=True,             # onedir: binaries go to COLLECT
    name="PS3HeadsetHub",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON) if ICON.exists() else None,
    # Windows Explorer version info, generated from ps3hub.APP_VERSION by
    # packaging/build_exe.py. Missing file => no resource, build still works.
    version=str(VERSION_FILE) if VERSION_FILE.exists() else None,
)

coll = COLLECT(                        # noqa: F821
    exe,
    analysis.binaries,
    analysis.zipfiles,
    analysis.datas,
    strip=False,
    upx=False,
    name="PS3HeadsetHub",
)
