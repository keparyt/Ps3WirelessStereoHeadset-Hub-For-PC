"""Tests for platform-neutral tray state formatting."""
from types import SimpleNamespace

from ps3hub.tray import format_tray_status


def _state(**kwargs):
    defaults = {
        "backend_available": True,
        "receiver_present": True,
        "snapshot": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_tray_reports_no_receiver():
    assert format_tray_status(_state(receiver_present=False)) == (
        "PS3 Headset Hub • No supported headset receiver"
    )


def test_tray_reports_connected_headset_and_battery():
    snapshot = SimpleNamespace(
        headset_connected=True,
        battery_percent=73,
        charging=False,
    )
    assert format_tray_status(_state(snapshot=snapshot)) == (
        "PS3 Headset Hub • Headset connected • Battery 73%"
    )


def test_tray_reports_battery_when_headset_is_not_linked():
    snapshot = SimpleNamespace(
        headset_connected=False,
        battery_percent=41,
        charging=False,
    )
    assert format_tray_status(_state(snapshot=snapshot)) == (
        "PS3 Headset Hub • Receiver connected • Headset not connected • Battery 41%"
    )


def test_tray_reports_charging_without_percentage():
    snapshot = SimpleNamespace(
        headset_connected=True,
        battery_percent=None,
        charging=True,
    )
    assert format_tray_status(_state(snapshot=snapshot)) == (
        "PS3 Headset Hub • Headset connected • Charging"
    )
