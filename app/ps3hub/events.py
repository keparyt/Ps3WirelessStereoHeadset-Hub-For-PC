"""Application event bus.

Notifications, the tray, the UI and the audio engine must all react to headset
changes, but none of them should *infer* state by scraping other components.
Everything publishes here; everything subscribes here. Publishing is cheap and
non-blocking, delivery is synchronous by default (subscribers are expected to
be cheap), and every subscriber failure is contained so one bad listener
cannot take down the dispatcher thread.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .applog import get_logger
from .state import HeadsetState

log = get_logger("events")

Subscriber = Callable[["AppEvent"], None]


class AppEvent:
    """Namespaced event constants."""

    # HID / device lifecycle
    RECEIVER_ATTACHED = "receiver_attached"
    RECEIVER_DETACHED = "receiver_detached"
    HEADSET_LINKED = "headset_linked"
    HEADSET_UNLINKED = "headset_unlinked"
    # Logical state changes (fired only when the value actually moved)
    VOLUME_CHANGED = "volume_changed"
    CHAT_BALANCE_CHANGED = "chat_balance_changed"
    BATTERY_CHANGED = "battery_changed"
    BATTERY_LOW = "battery_low"
    CHARGING_STARTED = "charging_started"
    CHARGING_STOPPED = "charging_stopped"
    VSS_CHANGED = "vss_changed"
    MIC_CHANGED = "mic_changed"
    # Audio processing
    AUDIO_STATE_CHANGED = "audio_state_changed"
    AUDIO_ERROR = "audio_error"
    # Input / actions
    INPUT_EVENT = "input_event"
    ACTION_EXECUTED = "action_executed"
    ACTION_FAILED = "action_failed"
    # Misc
    ERROR = "error"
    NOTICE = "notice"


@dataclass(frozen=True)
class Event:
    """One published application event.

    ``state`` carries the authoritative logical headset state at publish
    time, so subscribers never need to query the tracker themselves.
    """

    type: str
    state: HeadsetState | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


class EventBus:
    """Synchronous, thread-safe pub/sub with per-subscriber isolation."""

    def __init__(self, history_limit: int = 200) -> None:
        self._lock = threading.RLock()
        self._subscribers: dict[str, list[Subscriber]] = {}
        self._any_subscribers: list[Subscriber] = []
        self._history: list[Event] = []
        self._history_limit = history_limit

    # ------------------------------------------------------------- publish --

    def publish(self, type_: str, state: HeadsetState | None = None, **payload: Any) -> Event:
        event = Event(type_, state, payload)
        with self._lock:
            self._history.append(event)
            if len(self._history) > self._history_limit:
                del self._history[: len(self._history) - self._history_limit]
            # Snapshot the subscriber lists under the lock so delivery cannot
            # race a subscribe/unsubscribe.
            subscribers = (
                list(self._subscribers.get(type_, [])) + list(self._any_subscribers)
            )
        for subscriber in subscribers:
            try:
                subscriber(event)
            except Exception:
                log.exception(
                    "Event subscriber failed for %s", type_
                )
        return event

    # ----------------------------------------------------------- subscribe --

    def subscribe(self, type_: str, callback: Subscriber) -> None:
        with self._lock:
            self._subscribers.setdefault(type_, []).append(callback)

    def subscribe_all(self, callback: Subscriber) -> None:
        with self._lock:
            self._any_subscribers.append(callback)

    def unsubscribe(self, type_: str, callback: Subscriber) -> bool:
        with self._lock:
            listeners = self._subscribers.get(type_, [])
            if callback in listeners:
                listeners.remove(callback)
                return True
            return False

    # ------------------------------------------------------------- history --

    def recent(self, limit: int = 50) -> list[Event]:
        with self._lock:
            return list(self._history[-limit:])

    def clear_history(self) -> None:
        with self._lock:
            self._history.clear()


#: Shared bus instance. The application is single-process; a module-level bus
#: keeps wiring simple while remaining a plain object that tests can replace.
bus = EventBus()
