"""Tests for the standalone installer (packaging/installer_gui.py).

Only the pure logic is exercised - option plumbing, URL consistency with
install.ps1, the payload guard - never a real install: the GUI and the
file-copying actions need a desktop session and must stay side-effect free
in CI. The FxSound detection/download logic itself is tested in
``test_fxsound_install.py``; here we make sure the installer delegates to it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import ps3hub.audio.fxsound_install as fxinstall
import pytest

APP_DIR = Path(__file__).resolve().parents[1]


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "installer_gui_under_test", APP_DIR / "packaging" / "installer_gui.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def installer():
    return _load_module()


# --------------------------------------------------------------- constants --

def test_the_fxsound_url_is_the_official_latest_release(installer):
    assert installer.FXSOUND_URL == ("https://github.com/fxsound2/"
                                     "fxsound-app/releases/download/"
                                     "latest/fxsound_setup.exe")


def test_the_fxsound_setup_runs_unattended(installer):
    # VERYSILENT: no wizard, no clicks; NORESTART: no surprise reboot.
    assert "/VERYSILENT" in installer.FXSOUND_SILENT_ARGS
    assert "/SUPPRESSMSGBOXES" in installer.FXSOUND_SILENT_ARGS
    assert "/NORESTART" in installer.FXSOUND_SILENT_ARGS


def test_the_powershell_installer_offers_the_same_fxsound_url(installer):
    """install.ps1 and the GUI installer must advertise the same download."""
    script = (APP_DIR / "packaging" / "install.ps1").read_text(encoding="utf-8")
    assert installer.FXSOUND_URL in script


def test_the_run_value_stays_in_sync_with_install_ps1(installer):
    """Either installer's uninstaller must clean up after both."""
    import re

    script = (APP_DIR / "packaging" / "install.ps1").read_text(encoding="utf-8")
    match = re.search(r"\$AppSlug\s*=\s*'([^']+)'", script)
    assert match, "AppSlug not found in install.ps1"
    # install.ps1 composes $RunValue as "PS3$AppSlug".
    assert installer.RUN_VALUE == "PS3" + match.group(1)


# -------------------------------------------------------------- delegation --

def test_the_installer_shares_the_hubs_fxsound_logic(installer):
    """One implementation: the installer re-exports the shared module."""
    assert installer.FXSOUND_URL is fxinstall.FXSOUND_URL
    assert installer.FXSOUND_SILENT_ARGS is fxinstall.FXSOUND_SILENT_ARGS
    assert installer.find_fxsound is fxinstall.find_fxsound
    assert (installer.install_fxsound_silently
            is fxinstall.install_fxsound_silently)


# ----------------------------------------------------------------- options --

def test_default_options_go_to_the_current_user_directory(monkeypatch, tmp_path,
                                                          installer):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    options = installer.InstallOptions()
    assert options.target_dir == tmp_path / installer.APP_SLUG
    assert options.autostart
    assert not options.desktop_shortcut
    assert options.install_fxsound


def test_target_dir_can_be_overridden(installer, tmp_path):
    options = installer.InstallOptions(target_dir=tmp_path / "elsewhere",
                                       autostart=False,
                                       desktop_shortcut=True,
                                       install_fxsound=False)
    assert options.target_dir == tmp_path / "elsewhere"
    assert not options.autostart
    assert options.desktop_shortcut
    assert not options.install_fxsound


def test_install_fails_cleanly_without_a_payload(installer, tmp_path, capsys):
    """A broken build must report the problem, not half-install."""
    options = installer.InstallOptions(target_dir=tmp_path / "hub")
    assert installer.perform_install(options) is False
    assert "payload is missing" in capsys.readouterr().out


def test_resource_path_returns_none_for_missing_files(installer):
    assert installer.resource_path("definitely-not-bundled.txt") is None


def test_the_cli_parser_knows_the_headless_switches(installer):
    args = installer.build_parser().parse_args(
        ["--silent", "--no-fxsound", "--no-autostart", "--target", "X:\\hub"])
    assert args.silent
    assert args.no_fxsound
    assert args.no_autostart
    assert str(args.target).lower().startswith("x:")
    assert not args.uninstall
