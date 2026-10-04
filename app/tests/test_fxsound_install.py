"""Tests for the shared FxSound install logic (ps3hub.audio.fxsound_install).

Pure logic only - detection paths, URL/switches, the downloader and the
already-installed fast path. No real download or setup run ever happens.
"""

from __future__ import annotations

from pathlib import Path

import ps3hub.audio.fxsound_install as fxinstall


# --------------------------------------------------------------- constants --

def test_the_setup_url_is_the_official_latest_release():
    assert fxinstall.FXSOUND_URL == ("https://github.com/fxsound2/"
                                     "fxsound-app/releases/download/"
                                     "latest/fxsound_setup.exe")


def test_the_setup_runs_unattended():
    # VERYSILENT: no wizard, no clicks; NORESTART: no surprise reboot.
    assert fxinstall.FXSOUND_SILENT_ARGS == ("/VERYSILENT",
                                             "/SUPPRESSMSGBOXES",
                                             "/NORESTART")


# -------------------------------------------------------------- detection ---

def test_detection_covers_the_program_files_locations(monkeypatch, tmp_path):
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "pf"))
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "lad"))
    monkeypatch.setattr(fxinstall.shutil, "which", lambda name: None)

    directories = fxinstall.fxsound_install_dirs()
    assert (tmp_path / "pf" / "FxSound LLC" / "FxSound") in directories
    assert (tmp_path / "pf" / "FxSound") in directories
    assert (tmp_path / "lad" / "Programs" / "FxSound") in directories
    assert (tmp_path / "lad" / "Microsoft" / "WindowsApps") in directories
    # The x86 directory must be absent, not a broken path, when unset.
    assert not any("Program Files (x86)" in str(path) for path in directories)


def test_find_fxsound_sees_a_per_machine_install(monkeypatch, tmp_path):
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(fxinstall.shutil, "which", lambda name: None)

    assert fxinstall.find_fxsound() is None
    installed = tmp_path / "FxSound LLC" / "FxSound"
    installed.mkdir(parents=True)
    (installed / "fxsound.exe").write_bytes(b"")
    assert fxinstall.find_fxsound() == installed / "fxsound.exe"
    assert fxinstall.fxsound_is_installed()


def test_find_fxsound_falls_back_to_path(monkeypatch, tmp_path):
    monkeypatch.delenv("ProgramFiles", raising=False)
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    on_path = tmp_path / "fxsound.exe"
    monkeypatch.setattr(fxinstall.shutil, "which",
                        lambda name: str(on_path) if name == "fxsound" else None)
    assert fxinstall.find_fxsound() == on_path


# -------------------------------------------------------------- downloader --

def test_download_file_copies_bytes_and_reports_progress(tmp_path):
    source = tmp_path / "payload.bin"
    source.write_bytes(b"fxsound-setup-bytes" * 1000)
    destination = tmp_path / "out" / "downloaded.bin"

    seen = []
    fxinstall.download_file(source.as_uri(), destination,
                            progress=lambda done, total: seen.append(done))

    assert destination.read_bytes() == source.read_bytes()
    assert seen[0] > 0
    assert seen[-1] == source.stat().st_size


def test_download_file_raises_on_a_bad_url(tmp_path):
    import pytest

    with pytest.raises(OSError):
        fxinstall.download_file("file:///definitely/not/here.exe",
                                tmp_path / "nope.bin")


# ------------------------------------------------------------ silent install -

def test_install_succeeds_immediately_when_already_installed(monkeypatch,
                                                             tmp_path):
    monkeypatch.setattr(fxinstall, "find_fxsound",
                        lambda: tmp_path / "fxsound.exe")
    lines: list[str] = []
    assert fxinstall.install_fxsound_silently(report=lines.append)
    assert any("already installed" in line for line in lines)


def test_install_reports_a_failed_download(monkeypatch, tmp_path):
    monkeypatch.setattr(fxinstall, "IS_WINDOWS", True)
    monkeypatch.setattr(fxinstall, "find_fxsound", lambda: None)
    monkeypatch.setattr(fxinstall, "download_file",
                        _raise_os_error)
    lines: list[str] = []
    assert not fxinstall.install_fxsound_silently(report=lines.append)
    assert any("Could not download" in line for line in lines)


def _raise_os_error(*_args, **_kwargs):
    raise OSError("no network")
