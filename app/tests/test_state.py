"""Logical state: raw-to-logical volume mapping and the state tracker."""

import pytest

from ps3hub.protocol import parse_status
from ps3hub.state import (
    VOLUME_LOGICAL_STEPS, HeadsetStateTracker, logical_to_percent,
    percent_for_raw, raw_to_logical,
)


def _status(volume=0x03, chat=0x32, battery=0x50, flags=0x09) -> bytes:
    return bytes([0xB0, volume, chat, battery, flags, 0x00, 0x11, 0x00])


# ------------------------------------------------------------ mapping ---

def test_raw_levels_map_onto_even_logical_steps():
    assert [raw_to_logical(v) for v in range(6)] == [0, 2, 4, 6, 8, 10]


def test_raw_out_of_range_maps_to_unknown():
    assert raw_to_logical(None) is None
    assert raw_to_logical(-1) is None
    assert raw_to_logical(6) is None


def test_logical_percent_is_step_times_ten():
    assert logical_to_percent(0) == 0
    assert logical_to_percent(5) == 50
    assert logical_to_percent(10) == 100
    assert logical_to_percent(None) is None
    assert logical_to_percent(11) is None


def test_percent_for_raw_combines_both_mappings():
    assert percent_for_raw(0x00) == 0
    assert percent_for_raw(0x05) == 100
    assert percent_for_raw(None) is None


def test_logical_step_count_is_ten():
    assert VOLUME_LOGICAL_STEPS == 10


# ------------------------------------------------------------- tracker ---

def test_tracker_publishes_logical_volume_from_a_snapshot():
    tracker = HeadsetStateTracker()
    tracker.update(parse_status(_status(volume=0x02)))  # seed baseline
    state, changes = tracker.update(parse_status(_status(volume=0x03)))
    assert state.volume == 6
    assert state.volume_percent == 60
    assert state.volume_raw == 0x03
    assert changes["volume"] is True


def test_first_snapshot_is_a_seed_not_a_change():
    """Baseline seeding: no value events for the state the headset had."""
    tracker = HeadsetStateTracker()
    state, changes = tracker.update(parse_status(_status(volume=0x03)))
    assert state.volume == 6
    assert changes["volume"] is False
    assert changes["battery"] is False
    assert changes["mic"] is False


def test_tracker_flags_change_only_when_the_value_moved():
    tracker = HeadsetStateTracker()
    _, seed = tracker.update(parse_status(_status(volume=0x03)))
    _, same = tracker.update(parse_status(_status(volume=0x03)))
    _, moved = tracker.update(parse_status(_status(volume=0x04)))
    assert seed["volume"] is False  # seeding, not movement
    assert same["volume"] is False
    assert moved["volume"] is True


def test_tracker_tracks_link_and_battery_changes():
    tracker = HeadsetStateTracker()
    tracker.update(parse_status(_status(flags=0x09, battery=0x32)))  # seed
    linked, changes = tracker.update(parse_status(_status(flags=0x09, battery=0x32)))
    assert linked.linked is True
    assert changes["battery"] is False

    _, changes = tracker.update(parse_status(_status(flags=0x09, battery=0x0A)))
    assert changes["battery"] is True


def test_tracker_mic_and_vss_flags():
    # Flag byte: bit 0 = VSS, bit 1 = mic mute, bit 3 = linked.
    tracker = HeadsetStateTracker()
    tracker.update(parse_status(_status(flags=0x08)))  # seed: linked, vss off, mic live
    muted, changes = tracker.update(parse_status(_status(flags=0x0A)))  # + mic mute
    assert muted.mic_muted is True
    assert muted.vss is False
    assert changes["mic"] is True
    on, changes = tracker.update(parse_status(_status(flags=0x09)))  # + VSS, mic live
    assert on.vss is True
    assert on.mic_muted is False
    assert changes["vss"] is True
    assert changes["mic"] is True


def test_charging_state_is_tracked():
    tracker = HeadsetStateTracker()
    tracker.update(parse_status(_status(battery=0x50)))  # seed: 80%, not charging
    charging, changes = tracker.update(parse_status(_status(battery=0x80)))
    assert charging.charging is True
    assert charging.battery_percent is None
    assert changes["battery"] is True


def test_receiver_reset_clears_state():
    tracker = HeadsetStateTracker()
    tracker.update(parse_status(_status()))
    tracker.set_receiver(True)
    state = tracker.reset()
    assert state.receiver_present is False
    assert state.volume is None
    assert state.linked is False
    # A fresh update works normally afterwards.
    state, _ = tracker.update(parse_status(_status()))
    assert state.volume == 6


def test_state_is_immutable():
    from dataclasses import FrozenInstanceError
    tracker = HeadsetStateTracker()
    state, _ = tracker.update(parse_status(_status()))
    with pytest.raises(FrozenInstanceError):
        state.volume = 3  # type: ignore[misc]


def test_battery_low_property_reflects_state():
    tracker = HeadsetStateTracker()
    low, _ = tracker.update(parse_status(_status(battery=0x0A)))
    assert low.battery_low is True
    ok, _ = tracker.update(parse_status(_status(battery=0x50)))
    assert ok.battery_low is False
