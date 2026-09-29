"""Notification rules: deciding *whether* something is worth a toast.

The notify module answers "how do we show a Windows toast". This module
answers "should we show one at all". Keeping the policy here (pure, offline,
fully unit-tested) away from the Win32 delivery keeps the anti-spam rules
honest: every duplicate-suppression decision is made on plain data.

Rules implemented:

* **No state, no toast.** Everything needs the logical headset state.
* **Connection transitions** are always interesting.
* **Value changes** (volume, chat balance, VSS, mic) are interesting once;
  identical repeats are suppressed by the event bus already, and a minimum
  interval guards against rapid oscillation.
* **Battery warnings** fire once per crossing, plus a periodic reminder after
  ``battery_reminder_seconds`` while the headset stays low and off charge.
* **Charging** transitions are interesting.
* **Volume notifications render a 10-segment meter** in the toast body so the
  toast matches the dashboard's representation of the same logical state.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .events import AppEvent, Event
from .state import HeadsetState, VOLUME_LOGICAL_STEPS

#: Minimum seconds between two toasts of the same event type. Rapid wheel
#: spinning still updates the UI live; it just does not produce one toast per
#: logical step.
DEFAULT_PER_TYPE_INTERVAL = 2.0

#: How long a battery warning stays quiet before reminding again.
DEFAULT_BATTERY_REMINDER_SECONDS = 900.0


def volume_meter(percent: int | None, muted: bool = False) -> str:
    """Render the 10-segment meter used in volume toasts.

    Uses block glyphs so the toast visually matches the dashboard ladder:
    exactly ten segments, filled proportional to the real logical state.
    """
    if percent is None:
        return "Volume: --"
    percent = max(0, min(100, int(percent)))
    filled = round(percent * VOLUME_LOGICAL_STEPS / 100)
    bar = "█" * filled + "░" * (VOLUME_LOGICAL_STEPS - filled)
    if muted:
        return f"{bar}  {percent}% (mic muted)"
    return f"{bar}  {percent}%"


@dataclass
class Toast:
    """One notification the rules engine decided to allow."""

    title: str
    body: str
    level: str = "info"  # info | warn | error | ok


@dataclass
class NotificationRules:
    """Stateful policy over application events. Feed it events; get toasts."""

    enabled: bool = True
    #: Per-event-type quiet periods, overridable for tests.
    per_type_interval: dict[str, float] = field(default_factory=dict)
    battery_reminder_seconds: float = DEFAULT_BATTERY_REMINDER_SECONDS
    #: Callers can turn individual categories off (Settings page).
    allow_connection: bool = True
    allow_volume: bool = True
    allow_chat_balance: bool = True
    allow_vss: bool = True
    allow_mic: bool = True
    allow_battery: bool = True
    allow_charging: bool = True
    allow_audio: bool = True

    # Runtime bookkeeping -------------------------------------------------
    _last_shown: dict[str, float] = field(default_factory=dict)
    _battery_warned: bool = False
    _last_battery_reminder: float = 0.0
    _last_was_linked: bool | None = None

    # ------------------------------------------------------------- policy --

    def _interval_for(self, type_: str) -> float:
        return self.per_type_interval.get(type_, DEFAULT_PER_TYPE_INTERVAL)

    def _throttled(self, type_: str, now: float) -> bool:
        last = self._last_shown.get(type_)
        if last is not None and (now - last) < self._interval_for(type_):
            return True
        self._last_shown[type_] = now
        return False

    def reset(self) -> None:
        """Clear all bookkeeping (used when the receiver is unplugged)."""
        self._last_shown.clear()
        self._battery_warned = False
        self._last_battery_reminder = 0.0
        self._last_was_linked = None

    # ------------------------------------------------------------ feeding --

    def evaluate(self, event: Event, now: float | None = None) -> Toast | None:
        """Decide whether one application event deserves a toast.

        Pure policy: never performs I/O, so tests can drive it offline.
        """
        if not self.enabled:
            return None
        now = time.monotonic() if now is None else now
        type_ = event.type
        state = event.state

        # -- connection ---------------------------------------------------
        if type_ in (AppEvent.HEADSET_LINKED, AppEvent.HEADSET_UNLINKED):
            if not self.allow_connection:
                return None
            if self._throttled(type_, now):
                return None
            if type_ == AppEvent.HEADSET_LINKED:
                return Toast("Headset connected", "🎧 PS3 Wireless Headset is ready", "ok")
            return Toast("Headset disconnected", "🎧 PS3 Wireless Headset link lost", "warn")

        # -- audio processing ----------------------------------------------
        if type_ in (AppEvent.AUDIO_STATE_CHANGED, AppEvent.AUDIO_ERROR):
            if not self.allow_audio:
                return None
            return self._evaluate_audio(type_, event, now)

        # Everything below is about headset values; without state, stop.
        if state is None:
            return None

        # -- volume ----------------------------------------------------------
        if type_ == AppEvent.VOLUME_CHANGED:
            if not self.allow_volume:
                return None
            if self._throttled(type_, now):
                return None
            if state.volume_percent is None:
                return None
            body = volume_meter(state.volume_percent, state.mic_muted)
            return Toast("Volume", body, "info")

        # -- chat balance ------------------------------------------------------
        if type_ == AppEvent.CHAT_BALANCE_CHANGED:
            if not self.allow_chat_balance:
                return None
            if self._throttled(type_, now):
                return None
            if state.chat_balance is None:
                return None
            return Toast("Chat mix", f"{state.chat_balance}% toward chat", "info")

        # -- VSS / mic ------------------------------------------------------------
        if type_ == AppEvent.VSS_CHANGED:
            if not self.allow_vss:
                return None
            if self._throttled(type_, now):
                return None
            body = "Virtual Surround ON" if state.vss else "Virtual Surround OFF"
            return Toast("VSS", body, "info")

        if type_ == AppEvent.MIC_CHANGED:
            if not self.allow_mic:
                return None
            if self._throttled(type_, now):
                return None
            body = "Microphone muted" if state.mic_muted else "Microphone live"
            return Toast(
                "Microphone", body, "warn" if state.mic_muted else "info"
            )

        # -- battery / charging -------------------------------------------------
        if type_ in (AppEvent.BATTERY_CHANGED, AppEvent.BATTERY_LOW,
                     AppEvent.CHARGING_STARTED, AppEvent.CHARGING_STOPPED):
            if not (self.allow_battery or self.allow_charging):
                return None
            return self._evaluate_battery(type_, state, now)

        return None

    # ------------------------------------------------------------ helpers --

    def _evaluate_audio(self, type_: str, event: Event, now: float) -> Toast | None:
        payload = event.payload
        if type_ == AppEvent.AUDIO_ERROR:
            if self._throttled("audio_error", now):
                return None
            message = str(payload.get("message", "Audio processing problem"))
            return Toast("Audio processing", message, "error")
        processing = payload.get("processing")
        device_name = payload.get("device_name") or ""
        profile_name = payload.get("profile_name") or ""
        if processing is True:
            body = f"{device_name}\nProfile: {profile_name or 'default'}".strip()
            return Toast("Audio effects active", body, "ok")
        if processing is False:
            return Toast("Audio effects off", device_name, "info")
        return None

    def _evaluate_battery(self, type_: str, state: HeadsetState, now: float) -> Toast | None:
        if type_ == AppEvent.CHARGING_STARTED:
            if self._throttled(type_, now):
                return None
            self._battery_warned = False  # charging resets the warning state
            return Toast("Charging started", "🎧 Headset is charging", "ok")
        if type_ == AppEvent.CHARGING_STOPPED:
            if self._throttled(type_, now):
                return None
            return Toast("Charging stopped", "🎧 Headset off the charger", "info")

        # Battery percentage changed / low-crossing handling
        percent = state.battery_percent
        if state.charging or percent is None:
            self._battery_warned = False
            return None

        low = percent <= 20
        if low and not self._battery_warned:
            self._battery_warned = True
            self._last_battery_reminder = now
            return Toast(
                "Headset battery low",
                f"{percent}% remaining. Put the headset on charge.",
                "warn",
            )
        if low and self._battery_warned:
            if (now - self._last_battery_reminder) >= self.battery_reminder_seconds:
                self._last_battery_reminder = now
                return Toast(
                    "Headset battery still low",
                    f"{percent}% remaining. Put the headset on charge.",
                    "warn",
                )
        if not low:
            self._battery_warned = False
        return None
