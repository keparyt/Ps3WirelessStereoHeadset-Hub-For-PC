"""Edge detection.

These are the tests that matter most: every false positive the user would
experience as "my music skipped for no reason" is caught here.
"""

from ps3hub.inputs import EdgeDetector, InputId
from ps3hub.protocol import HeadsetSnapshot


def snap(volume=5, balance=50, battery=80, charging=False, vss=False,
         mic=False, linked=True) -> HeadsetSnapshot:
    return HeadsetSnapshot(
        volume_level=volume,
        volume_percent=None if volume is None else volume * 10,
        chat_balance=balance,
        battery_percent=battery,
        charging=charging,
        vss=vss,
        mic_muted=mic,
        headset_connected=linked,
    )


def ids(events):
    return [event.input_id for event in events]


def detector(**kwargs) -> EdgeDetector:
    # Settle and debounce disabled unless a test is specifically about them.
    kwargs.setdefault("settle_seconds", 0.0)
    kwargs.setdefault("debounce_seconds", 0.0)
    return EdgeDetector(**kwargs)


# -------------------------------------------------------------- seeding --

def test_first_snapshot_only_seeds_a_baseline():
    det = detector()
    events = det.feed(snap(volume=5, linked=True), now=0.0)
    # The link event is legitimate; no volume or button events may appear.
    assert ids(events) == [InputId.HEADSET_LINKED]
    assert det.seeded is True


def test_first_snapshot_while_unlinked_emits_nothing():
    det = detector()
    assert det.feed(snap(linked=False), now=0.0) == []


def test_switching_the_headset_on_does_not_fire_every_field():
    """The regression that motivated baseline seeding."""
    det = detector()
    det.feed(snap(volume=0, balance=0, vss=False, mic=False, linked=False), now=0.0)
    events = det.feed(
        snap(volume=7, balance=64, vss=True, mic=True, linked=True), now=1.0
    )
    assert ids(events) == [InputId.HEADSET_LINKED]


# --------------------------------------------------------------- volume --

def test_volume_up_one_step():
    det = detector()
    det.feed(snap(volume=4), now=0.0)
    events = det.feed(snap(volume=5), now=1.0)
    assert ids(events) == [InputId.VOLUME_UP]
    assert events[0].repeat == 1
    assert events[0].value == 5


def test_volume_down_reports_the_step_count():
    det = detector()
    det.feed(snap(volume=6), now=0.0)
    events = det.feed(snap(volume=4), now=1.0)
    assert ids(events) == [InputId.VOLUME_DOWN]
    assert events[0].repeat == 2


def test_identical_report_produces_nothing():
    det = detector()
    det.feed(snap(volume=5), now=0.0)
    assert det.feed(snap(volume=5), now=1.0) == []


def test_large_volume_jump_replays_the_authoritative_state_delta():
    det = detector()
    det.feed(snap(volume=0), now=0.0)
    events = det.feed(snap(volume=5), now=0.01)
    assert ids(events) == [
        InputId.VOLUME_UP,
        InputId.VOLUME_UP,
        InputId.VOLUME_UP,
        InputId.VOLUME_UP,
        InputId.VOLUME_UP,
    ]
    assert all(event.repeat == 1 for event in events)
    assert [event.value for event in events] == [1, 2, 3, 4, 5]


def test_rapid_volume_reports_are_not_debounced():
    det = EdgeDetector(settle_seconds=0.0, debounce_seconds=0.25)
    det.feed(snap(volume=0), now=0.0)
    events = det.feed(snap(volume=1), now=0.010)
    assert ids(events) == [InputId.VOLUME_UP]
    assert events[0].repeat == 1
    events = det.feed(snap(volume=2), now=0.020)
    assert ids(events) == [InputId.VOLUME_UP]
    assert events[0].repeat == 1
    events = det.feed(snap(volume=3), now=0.030)
    assert ids(events) == [InputId.VOLUME_UP]
    assert events[0].repeat == 1
    events = det.feed(snap(volume=4), now=0.040)
    assert ids(events) == [InputId.VOLUME_UP]
    assert events[0].repeat == 1


def test_volume_at_the_top_of_the_range_stops_producing_events():
    """At step 10 the wheel still turns but the value cannot rise."""
    det = detector()
    det.feed(snap(volume=10), now=0.0)
    assert det.feed(snap(volume=10), now=1.0) == []


# -------------------------------------------------------------- chatmix --

def test_chatmix_step_of_eight_counts_as_one_press():
    det = detector()
    det.feed(snap(balance=48), now=0.0)
    events = det.feed(snap(balance=56), now=1.0)
    assert ids(events) == [InputId.CHATMIX_UP]
    assert events[0].repeat == 1


def test_chatmix_down_two_steps():
    det = detector()
    det.feed(snap(balance=64), now=0.0)
    events = det.feed(snap(balance=48), now=1.0)
    assert ids(events) == [InputId.CHATMIX_DOWN]
    assert events[0].repeat == 2


# -------------------------------------------------------------- toggles --

def test_vss_button_emits_both_the_press_and_the_direction():
    det = detector()
    det.feed(snap(vss=False), now=0.0)
    events = det.feed(snap(vss=True), now=1.0)
    assert ids(events) == [InputId.VSS_BUTTON, InputId.VSS_ON]

    events = det.feed(snap(vss=False), now=2.0)
    assert ids(events) == [InputId.VSS_BUTTON, InputId.VSS_OFF]


def test_microphone_button_emits_both():
    det = detector()
    det.feed(snap(mic=False), now=0.0)
    events = det.feed(snap(mic=True), now=1.0)
    assert ids(events) == [InputId.MIC_BUTTON, InputId.MIC_MUTED]


# ------------------------------------------------------------------ link --

def test_unlink_then_relink_does_not_replay_stale_values():
    det = detector()
    det.feed(snap(volume=5), now=0.0)
    off = det.feed(snap(volume=5, linked=False), now=1.0)
    assert ids(off) == [InputId.HEADSET_UNLINKED]

    # Headset comes back at a very different volume. That is not a press.
    on = det.feed(snap(volume=9, linked=True), now=2.0)
    assert ids(on) == [InputId.HEADSET_LINKED]


def test_fields_are_not_interpreted_while_unlinked():
    det = detector()
    det.feed(snap(volume=5), now=0.0)
    det.feed(snap(volume=5, linked=False), now=1.0)
    assert det.feed(snap(volume=6, vss=True, linked=False), now=2.0) == []


def test_reset_clears_the_baseline():
    det = detector()
    det.feed(snap(volume=5), now=0.0)
    det.reset("unplugged")
    assert det.seeded is False
    events = det.feed(snap(volume=9), now=1.0)
    assert ids(events) == [InputId.HEADSET_LINKED]


# ---------------------------------------------------------------- guards --

def test_settle_window_suppresses_echoed_state():
    det = EdgeDetector(settle_seconds=0.5, debounce_seconds=0.0)
    det.feed(snap(volume=5), now=0.0)
    # The receiver repeats itself 100 ms later with a different volume.
    assert det.feed(snap(volume=6), now=0.1) == []
    # After the window, real presses come through.
    assert ids(det.feed(snap(volume=7), now=1.0)) == [InputId.VOLUME_UP]


def test_debounce_collapses_rapid_duplicates():
    det = EdgeDetector(settle_seconds=0.0, debounce_seconds=0.1)
    det.feed(snap(volume=4), now=0.0)
    assert ids(det.feed(snap(volume=5), now=1.00)) == [InputId.VOLUME_UP]
    assert det.feed(snap(volume=6), now=1.02) == []
    assert ids(det.feed(snap(volume=7), now=1.50)) == [InputId.VOLUME_UP]


def test_link_events_survive_the_settle_window():
    det = EdgeDetector(settle_seconds=5.0, debounce_seconds=0.0)
    det.feed(snap(linked=False), now=0.0)
    assert ids(det.feed(snap(linked=True), now=0.1)) == [InputId.HEADSET_LINKED]


# --------------------------------------------------------------- battery --

def test_battery_low_fires_once_on_the_crossing():
    det = detector()
    det.feed(snap(battery=30), now=0.0)
    assert ids(det.feed(snap(battery=18), now=1.0)) == [InputId.BATTERY_LOW]
    assert det.feed(snap(battery=15), now=2.0) == []


def test_charging_transitions():
    det = detector()
    det.feed(snap(charging=False), now=0.0)
    assert ids(det.feed(snap(charging=True), now=1.0)) == [InputId.CHARGING_STARTED]
    assert ids(det.feed(snap(charging=False), now=2.0)) == [InputId.CHARGING_STOPPED]
