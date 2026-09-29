"""Notification service: bus -> rules -> notifier wiring."""

from ps3hub.events import AppEvent, EventBus
from ps3hub.notify_service import NotificationService
from ps3hub.state import HeadsetState


class FakeNotifier:
    def __init__(self, available=True):
        self.available = available
        self.shown = []

    def show(self, title, body, level="info"):
        if not self.available:
            return False
        self.shown.append((title, body, level))
        return True


def _state(volume_percent=60, battery_percent=10, charging=False):
    return HeadsetState(linked=True, volume=6, volume_percent=volume_percent,
                        battery_percent=battery_percent, charging=charging)


def _service(bus, notifier):
    service = NotificationService(notifier=notifier)
    service.attach(bus)
    # Zero quiet periods so tests are deterministic.
    for key in list(service.rules.per_type_interval):
        service.rules.per_type_interval[key] = 0.0
    service.rules.per_type_interval.setdefault("volume_changed", 0.0)
    service.rules.per_type_interval.setdefault("headset_linked", 0.0)
    return service


def test_volume_event_ends_up_as_a_toast():
    bus = EventBus()
    notifier = FakeNotifier()
    service = _service(bus, notifier)
    bus.publish(AppEvent.VOLUME_CHANGED, _state())
    assert notifier.shown, "an allowed volume event must reach the notifier"
    title, body, level = notifier.shown[0]
    assert title == "Volume"
    assert "60%" in body
    assert level == "info"


def test_suppressed_events_never_reach_the_notifier():
    bus = EventBus()
    notifier = FakeNotifier()
    service = _service(bus, notifier)
    # Battery change with a healthy level is not toast-worthy.
    bus.publish(AppEvent.BATTERY_CHANGED, _state(battery_percent=90))
    assert notifier.shown == []
    assert service.suppressed >= 1


def test_category_switches_are_honoured():
    bus = EventBus()
    notifier = FakeNotifier()
    service = _service(bus, notifier)
    service.apply_settings(connection=False, volume=True, audio=True)
    bus.publish(AppEvent.HEADSET_LINKED, _state())
    assert notifier.shown == []
    service.apply_settings(connection=True, volume=True, audio=True)
    bus.publish(AppEvent.HEADSET_LINKED, _state())
    assert len(notifier.shown) == 1


def test_unavailable_notifier_does_not_raise():
    bus = EventBus()
    service = _service(bus, FakeNotifier(available=False))
    bus.publish(AppEvent.HEADSET_LINKED, _state())
    assert service.allowed == 1
    assert service.shown == 0
