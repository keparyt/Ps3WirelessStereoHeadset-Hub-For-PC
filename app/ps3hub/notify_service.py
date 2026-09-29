"""Notification service: event bus -> rules -> Windows toast.

Sits between the event bus and the toast sender. The rules engine
(:mod:`ps3hub.notify_rules`) decides *whether* an event is worth interrupting
the user for; :class:`ps3hub.notify.DesktopNotifier` handles *how* the toast
is shown. This module owns the policy object, feeds it the authoritative
logical state, and turns an allowed :class:`~ps3hub.notify_rules.Toast` into a
queued notification.

Wiring lives in one place so the anti-spam behaviour is auditable:

* the bus already fires value events only on change;
* the rules add a per-type quiet period, a warn-once battery policy with
  periodic reminders, and category switches from Settings;
* the sender replaces a visible balloon instead of stacking toasts.

Any thread may call :meth:`NotificationService.handle_event` because the
notifier queues internally, but the bus delivers synchronously from the HID
dispatch thread, so subscribers stay cheap: a dict lookup and one queue put.
"""

from __future__ import annotations

from typing import Any

from .applog import get_logger
from .events import AppEvent, Event
from .notify import DesktopNotifier
from .notify_rules import NotificationRules

log = get_logger("notify.service")


class NotificationService:
    """Subscribes to the event bus and shows toasts the rules allow."""

    def __init__(self, notifier: DesktopNotifier, event_bus: Any = None,
                 rules: NotificationRules | None = None) -> None:
        self._notifier = notifier
        self._bus = event_bus
        self.rules = rules or NotificationRules()
        self.allowed = 0
        self.suppressed = 0
        self.shown = 0

    # ------------------------------------------------------------- wiring ---

    def attach(self, event_bus: Any) -> None:
        """Subscribe to every event type the rules engine understands."""
        self._bus = event_bus
        for type_ in (
            AppEvent.HEADSET_LINKED, AppEvent.HEADSET_UNLINKED,
            AppEvent.VOLUME_CHANGED, AppEvent.CHAT_BALANCE_CHANGED,
            AppEvent.VSS_CHANGED, AppEvent.MIC_CHANGED,
            AppEvent.BATTERY_CHANGED, AppEvent.BATTERY_LOW,
            AppEvent.CHARGING_STARTED, AppEvent.CHARGING_STOPPED,
            AppEvent.AUDIO_STATE_CHANGED, AppEvent.AUDIO_ERROR,
        ):
            event_bus.subscribe(type_, self.handle_event)

    # ------------------------------------------------------------ policy ---

    def apply_settings(self, *, connection: bool, volume: bool,
                       audio: bool) -> None:
        """Mirror the Settings switches into the policy."""
        self.rules.allow_connection = connection
        self.rules.allow_volume = volume
        self.rules.allow_audio = audio

    # ------------------------------------------------------------- events ---

    def handle_event(self, event: Event) -> None:
        """Bus subscriber: run the policy, show what survives it."""
        toast = self.rules.evaluate(event)
        if toast is None:
            self.suppressed += 1
            return
        self.allowed += 1
        if self._notifier.show(toast.title, toast.body, toast.level):
            self.shown += 1
