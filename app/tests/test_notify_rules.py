"""Notification rules: policy decisions without any Win32 involvement."""

from ps3hub.events import AppEvent, Event
from ps3hub.notify_rules import NotificationRules, Toast, volume_meter
from ps3hub.state import HeadsetState

import time


def _state(**kwargs) -> HeadsetState:
    defaults = dict(linked=True, volume=6, volume_percent=60,
                    battery_percent=80, charging=False)
    defaults.update(kwargs)
    return HeadsetState(**defaults)


def _event(type_: str, state=None, **payload) -> Event:
    return Event(type_, state, payload)


# --------------------------------------------------------------- meter ---

def test_volume_meter_renders_ten_segments():
    bar = volume_meter(50)
    assert bar.count("█") == 5
    assert bar.count("░") == 5
    assert "50%" in bar


def test_volume_meter_handles_unknown_and_bounds():
    assert volume_meter(None) == "Volume: --"
    assert volume_meter(-5).count("█") == 0
    assert volume_meter(150).count("█") == 10


def test_volume_meter_mentions_mic_mute():
    assert "mic muted" in volume_meter(40, muted=True)


# --------------------------------------------------------------- policy ---

def test_connection_events_always_produce_toasts():
    rules = NotificationRules()
    toast = rules.evaluate(_event(AppEvent.HEADSET_LINKED, _state()))
    assert isinstance(toast, Toast)
    assert toast.level == "ok"
    off = rules.evaluate(_event(AppEvent.HEADSET_UNLINKED, _state(linked=False)))
    assert off.level == "warn"


def test_connection_toasts_can_be_disabled():
    rules = NotificationRules(allow_connection=False)
    assert rules.evaluate(_event(AppEvent.HEADSET_LINKED, _state())) is None


def test_volume_toast_renders_the_meter():
    rules = NotificationRules(per_type_interval={"volume_changed": 0.0})
    toast = rules.evaluate(_event(AppEvent.VOLUME_CHANGED, _state(volume_percent=30)))
    assert toast is not None
    assert "30%" in toast.body
    assert toast.body.count("█") == 3


def test_rapid_volume_changes_are_throttled():
    rules = NotificationRules()
    now = time.monotonic()
    first = rules.evaluate(_event(AppEvent.VOLUME_CHANGED, _state(volume_percent=20)), now)
    second = rules.evaluate(_event(AppEvent.VOLUME_CHANGED, _state(volume_percent=30)), now + 0.2)
    assert first is not None
    assert second is None, "the quiet period must suppress rapid repeats"
    later = rules.evaluate(_event(AppEvent.VOLUME_CHANGED, _state(volume_percent=40)), now + 3.0)
    assert later is not None


def test_volume_toast_requires_a_percentage():
    rules = NotificationRules(per_type_interval={"volume_changed": 0.0})
    assert rules.evaluate(_event(AppEvent.VOLUME_CHANGED, _state(volume_percent=None))) is None


def test_disabled_rules_engine_produces_nothing():
    rules = NotificationRules(enabled=False)
    assert rules.evaluate(_event(AppEvent.HEADSET_LINKED, _state())) is None


def test_events_without_state_are_ignored_for_value_types():
    rules = NotificationRules()
    assert rules.evaluate(_event(AppEvent.VOLUME_CHANGED, None)) is None
    assert rules.evaluate(_event(AppEvent.MIC_CHANGED, None)) is None


def test_mic_mute_toast_warns_unmute_is_info():
    rules = NotificationRules()
    muted = rules.evaluate(_event(AppEvent.MIC_CHANGED, _state(mic_muted=True)))
    assert muted.level == "warn"
    rules2 = NotificationRules()
    live = rules2.evaluate(_event(AppEvent.MIC_CHANGED, _state(mic_muted=False)))
    assert live.level == "info"


# ------------------------------------------------------------- battery ---

def test_battery_low_warns_once_then_reminds_after_the_interval():
    rules = NotificationRules(battery_reminder_seconds=100.0)
    now = 1000.0
    first = rules.evaluate(_event(AppEvent.BATTERY_LOW, _state(battery_percent=10)), now)
    assert first is not None and "10%" in first.body

    # More low events inside the reminder window stay quiet.
    again = rules.evaluate(_event(AppEvent.BATTERY_LOW, _state(battery_percent=9)), now + 50)
    assert again is None

    reminder = rules.evaluate(_event(AppEvent.BATTERY_LOW, _state(battery_percent=8)), now + 150)
    assert reminder is not None and "still low" in reminder.title.lower()


def test_charging_resets_the_battery_warning():
    rules = NotificationRules(battery_reminder_seconds=100.0)
    now = 1000.0
    assert rules.evaluate(_event(AppEvent.BATTERY_LOW, _state(battery_percent=10)), now) is not None
    # Charging clears the warn-once latch.
    rules.evaluate(_event(AppEvent.CHARGING_STARTED, _state(charging=True, battery_percent=None)), now + 1)
    assert rules.evaluate(_event(AppEvent.BATTERY_LOW, _state(battery_percent=10)), now + 2) is not None


def test_healthy_battery_never_warns():
    rules = NotificationRules()
    assert rules.evaluate(_event(AppEvent.BATTERY_CHANGED, _state(battery_percent=90))) is None


# --------------------------------------------------------------- audio ---

def test_audio_state_toasts():
    rules = NotificationRules()
    on = rules.evaluate(_event(AppEvent.AUDIO_STATE_CHANGED, None, processing=True,
                               device_name="Speakers", profile_name="PS3 Headset"))
    assert on is not None and on.level == "ok"
    err = rules.evaluate(_event(AppEvent.AUDIO_ERROR, None, message="boom"))
    assert err is not None and err.level == "error"


def test_reset_clears_the_battery_latch():
    rules = NotificationRules()
    assert rules.evaluate(_event(AppEvent.BATTERY_LOW, _state(battery_percent=10))) is not None
    rules.reset()
    assert rules.evaluate(_event(AppEvent.BATTERY_LOW, _state(battery_percent=10))) is not None
