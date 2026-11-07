"""Event bus: change-only publication from the device service and delivery."""

from ps3hub import device as device_module
from ps3hub.device import HeadsetService
from ps3hub.events import AppEvent, EventBus, Event
from ps3hub.protocol import parse_status
from ps3hub.state import HeadsetStateTracker


class _FakeHid:
    def __init__(self, devices_by_pid):
        self.devices_by_pid = devices_by_pid
        self.calls = []

    def enumerate(self, vendor_id, product_id):
        self.calls.append((vendor_id, product_id))
        return self.devices_by_pid.get(product_id, [])


def test_enumeration_does_not_mix_receiver_generations(monkeypatch):
    fake = _FakeHid({
        0x0035: [{"product_id": 0x0035, "path": b"newer"}],
        0x0030: [{"product_id": 0x0030, "path": b"original"}],
    })
    monkeypatch.setattr(device_module, "hid", fake)

    assert HeadsetService()._enumerate() == [
        {"product_id": 0x0035, "path": b"newer"}
    ]
    assert fake.calls == [(0x12BA, 0x0035)]


def test_enumeration_falls_back_to_original_receiver(monkeypatch):
    fake = _FakeHid({
        0x0030: [{"product_id": 0x0030, "path": b"original"}],
    })
    monkeypatch.setattr(device_module, "hid", fake)

    assert HeadsetService()._enumerate() == [
        {"product_id": 0x0030, "path": b"original"}
    ]
    assert fake.calls == [(0x12BA, 0x0035), (0x12BA, 0x0030)]


def test_bus_delivers_to_typed_and_catch_all_subscribers():
    bus = EventBus()
    seen_typed = []
    seen_all = []
    bus.subscribe(AppEvent.VOLUME_CHANGED, seen_typed.append)
    bus.subscribe_all(seen_all.append)
    bus.publish(AppEvent.VOLUME_CHANGED)
    assert len(seen_typed) == 1
    assert len(seen_all) == 1


def test_bus_isolates_failing_subscribers():
    bus = EventBus()

    def boom(_event):
        raise RuntimeError("subscribers must not take the bus down")

    seen = []
    bus.subscribe(AppEvent.NOTICE, boom)
    bus.subscribe(AppEvent.NOTICE, seen.append)
    bus.publish(AppEvent.NOTICE)  # must not raise
    assert len(seen) == 1


def test_bus_keeps_a_bounded_history():
    bus = EventBus(history_limit=5)
    for index in range(10):
        bus.publish(AppEvent.NOTICE, None, i=index)
    recent = bus.recent(limit=50)
    assert len(recent) == 5
    assert recent[-1].payload["i"] == 9


def test_event_carries_state_and_payload():
    bus = EventBus()
    event = bus.publish(AppEvent.VOLUME_CHANGED, None, extra=1)
    assert isinstance(event, Event)
    assert event.type == AppEvent.VOLUME_CHANGED
    assert event.payload == {"extra": 1}
    assert event.state is None


# ----------------------------------------------- device service wiring ---

class _FakeDetector:
    """Stands in for the EdgeDetector so no timing is involved."""

    def __init__(self, events=None):
        self._events = events or []
        self.last_snapshot = None

    def feed(self, snapshot, now=None):
        self.last_snapshot = snapshot
        return list(self._events)

    def reset(self, reason=""):
        pass


def _service_with(monkeypatch, detector):
    import ps3hub.device as device_module

    service = HeadsetService.__new__(HeadsetService)
    # Mimic __init__ without starting threads or opening hardware.
    from ps3hub.device import ServiceState
    from ps3hub.state import HeadsetStateTracker

    service._state = ServiceState()
    import threading

    service._lock = threading.RLock()
    service._reports = None
    service._ui_events = None
    service._readers = {}
    service._collections = {}
    service._non_input_collections = set()
    service._fingerprints = {}
    service._input_handler = None
    service.read_all_collections = False
    service._tracker = HeadsetStateTracker()
    from ps3hub.events import bus as app_bus

    service._bus = EventBus()
    service._detector = detector
    return service


def _emit(service, report):
    # _process logs and touches the UI queue; provide minimal stubs.
    import queue

    service._ui_events = queue.Queue(maxsize=10)
    service._reports = queue.Queue(maxsize=10)
    service._process("fake-path", report)


def test_volume_change_publishes_exactly_one_volume_event():
    detector = _FakeDetector()
    service = _service_with(None, detector)
    seen = []
    service._bus.subscribe(AppEvent.VOLUME_CHANGED, seen.append)

    _emit(service, bytes([0xB0, 0x02, 0x32, 0x50, 0x09, 0x00, 0x11, 0x00]))  # seed
    _emit(service, bytes([0xB0, 0x02, 0x32, 0x50, 0x09, 0x00, 0x11, 0x00]))
    _emit(service, bytes([0xB0, 0x03, 0x32, 0x50, 0x09, 0x00, 0x11, 0x00]))

    assert len(seen) == 1, "seed and identical repeats must not publish"
    assert seen[0].state.volume == 6


def test_linked_transition_publishes_link_events():
    detector = _FakeDetector()
    service = _service_with(None, detector)
    linked = []
    unlinked = []
    service._bus.subscribe(AppEvent.HEADSET_LINKED, linked.append)
    service._bus.subscribe(AppEvent.HEADSET_UNLINKED, unlinked.append)

    _emit(service, bytes([0xB0, 0x02, 0x32, 0x50, 0x09, 0x00, 0x11, 0x00]))
    _emit(service, bytes([0xB0, 0x02, 0x32, 0x50, 0x01, 0x00, 0x11, 0x00]))

    assert len(linked) == 1
    assert len(unlinked) == 1


def test_no_events_before_any_snapshot_changes():
    detector = _FakeDetector()
    service = _service_with(None, detector)
    seen = []
    for type_ in (AppEvent.VOLUME_CHANGED, AppEvent.MIC_CHANGED,
                  AppEvent.VSS_CHANGED, AppEvent.CHAT_BALANCE_CHANGED):
        service._bus.subscribe(type_, seen.append)

    # Ten identical reports: the anti-spam guarantee is silence.
    for _ in range(10):
        _emit(service, bytes([0xB0, 0x02, 0x32, 0x50, 0x0B, 0x00, 0x11, 0x00]))

    assert seen == []
