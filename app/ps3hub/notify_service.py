"""Notification service: event bus -> rules -> Windows toast.

Sits between the event bus and the toast sender. The rules engine
(:mod:`ps3hub.notify_rules`) decides *whether* an event is worth interrupting
the user for; the notifier - :class:`ps3hub.notify.DesktopNotifier` or the
toast center in :mod:`ps3hub.ui.toast` - handles *how* the toast is shown.
This module owns the policy object, feeds it the authoritative logical
state, and turns an allowed :class:`~ps3hub.notify_rules.Toast` into a
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

from typing import Any, Callable

from .applog import get_logger
from .events import AppEvent, Event
from .notify import DesktopNotifier
from .notify_rules import NotificationRules

log = get_logger("notify.service")


class NotificationService:
    """Subscribes to the event bus and shows toasts the rules allow."""

    def __init__(self, notifier: Any, event_bus: Any = None,
                 rules: NotificationRules | None = None,
                 on_click: Callable[[], None] | None = None) -> None:
        """Take any notifier with a ``show(title, body, level, on_click=...)``.

        That covers both the legacy :class:`~ps3hub.notify.DesktopNotifier`
        and the new :class:`~ps3hub.ui.toast.ToastCenter`, so tests and
        alternate front-ends can inject either.
        """
        self._notifier = notifier
        self._bus = event_bus
        self.rules = rules or NotificationRules()
        self._on_click = on_click
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
        try:
            delivered = self._notifier.show(
                toast.title, toast.body, toast.level, on_click=self._on_click)
        except TypeError:
            # A notifier without the on_click keyword (the legacy sender).
            delivered = self._notifier.show(toast.title, toast.body, toast.level)
        if delivered:
            self.shown += 1
