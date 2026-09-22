"""Turning headset state into discrete input events.

The single most important fact about this device: **it does not send button
events.** The receiver emits an 8-byte ``0xB0`` report describing its current
state whenever something about that state changes. There is no "volume up was
pressed" message anywhere in the protocol.

So a press has to be reconstructed as the *difference between two consecutive
state snapshots*:

    volume 4 -> volume 5      becomes   volume_up
    vss off  -> vss on        becomes   vss_button
    balance 48 -> balance 56  becomes   chatmix_up

That reconstruction is what makes mapping possible at all, and it is also
where every false-positive risk lives. Three guards are applied:

1. **Baseline seeding.** The first snapshot after the headset links only
   establishes a reference point; it never emits events. Without this, simply
   switching the headset on fires one event per field.
2. **A settle window.** Right after linking, the receiver often reports its
   state two or three times in quick succession. Events are suppressed for a
   short window so those do not register as presses.
3. **Resync rejection.** A jump larger than a human could produce means
   reports were dropped, not that the button was pressed nine times. The
   detector re-baselines silently instead of firing a burst of actions.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Iterable

from .applog import get_logger
from .protocol import CHAT_BALANCE_STEP, HeadsetSnapshot

log = get_logger("inputs")

#: A change bigger than this many steps means dropped reports, not a press.
RESYNC_THRESHOLD = 4
#: Events are ignored for this long after the headset links.
SETTLE_SECONDS = 0.45
#: Two identical inputs closer together than this are treated as one.
DEBOUNCE_SECONDS = 0.035


class Category:
    VOLUME = "Volume"
    CHATMIX = "Chat mix"
    BUTTON = "Buttons"
    LINK = "Connection"
    POWER = "Battery"


@dataclass(frozen=True)
class InputDescriptor:
    """Everything the UI needs to describe one mappable input."""

    id: str
    label: str
    category: str
    description: str
    #: True when the input repeats while the control is held.
    repeatable: bool = False
    #: True when the headset also performs its own action for this input.
    hardware_acts: bool = False
    mappable: bool = True


class InputId:
    VOLUME_UP = "volume_up"
    VOLUME_DOWN = "volume_down"
    CHATMIX_UP = "chatmix_up"
    CHATMIX_DOWN = "chatmix_down"
    VSS_BUTTON = "vss_button"
    VSS_ON = "vss_on"
    VSS_OFF = "vss_off"
    MIC_BUTTON = "mic_button"
    MIC_MUTED = "mic_muted"
    MIC_UNMUTED = "mic_unmuted"
    HEADSET_LINKED = "headset_linked"
    HEADSET_UNLINKED = "headset_unlinked"
    BATTERY_LOW = "battery_low"
    CHARGING_STARTED = "charging_started"
    CHARGING_STOPPED = "charging_stopped"


INPUT_DESCRIPTORS: tuple[InputDescriptor, ...] = (
    InputDescriptor(
        InputId.VOLUME_UP, "Volume up", Category.VOLUME,
        "The volume wheel moved up one of the headset's 10 steps.",
        repeatable=True, hardware_acts=True,
    ),
    InputDescriptor(
        InputId.VOLUME_DOWN, "Volume down", Category.VOLUME,
        "The volume wheel moved down one of the headset's 10 steps.",
        repeatable=True, hardware_acts=True,
    ),
    InputDescriptor(
        InputId.CHATMIX_UP, "Chat mix up", Category.CHATMIX,
        "The game/chat balance moved toward chat.",
        repeatable=True, hardware_acts=True,
    ),
    InputDescriptor(
        InputId.CHATMIX_DOWN, "Chat mix down", Category.CHATMIX,
        "The game/chat balance moved toward game audio.",
        repeatable=True, hardware_acts=True,
    ),
    InputDescriptor(
        InputId.VSS_BUTTON, "VSS button", Category.BUTTON,
        "The virtual surround button was pressed, in either direction.",
        hardware_acts=True,
    ),
    InputDescriptor(
        InputId.VSS_ON, "VSS switched on", Category.BUTTON,
        "Virtual surround turned on. Fires only in that direction.",
        hardware_acts=True,
    ),
    InputDescriptor(
        InputId.VSS_OFF, "VSS switched off", Category.BUTTON,
        "Virtual surround turned off. Fires only in that direction.",
        hardware_acts=True,
    ),
    InputDescriptor(
        InputId.MIC_BUTTON, "Microphone button", Category.BUTTON,
        "The mute control changed state, in either direction.",
        hardware_acts=True,
    ),
    InputDescriptor(
        InputId.MIC_MUTED, "Microphone muted", Category.BUTTON,
        "The microphone was muted.", hardware_acts=True,
    ),
    InputDescriptor(
        InputId.MIC_UNMUTED, "Microphone unmuted", Category.BUTTON,
        "The microphone was unmuted.", hardware_acts=True,
    ),
    InputDescriptor(
        InputId.HEADSET_LINKED, "Headset connected", Category.LINK,
        "The headset linked to the receiver.",
    ),
    InputDescriptor(
        InputId.HEADSET_UNLINKED, "Headset disconnected", Category.LINK,
        "The headset powered off or went out of range.",
    ),
    InputDescriptor(
        InputId.BATTERY_LOW, "Battery low", Category.POWER,
        "Battery fell to the low-battery threshold.",
    ),
    InputDescriptor(
        InputId.CHARGING_STARTED, "Charging started", Category.POWER,
        "The headset was plugged in to charge.",
    ),
    InputDescriptor(
        InputId.CHARGING_STOPPED, "Charging stopped", Category.POWER,
        "The headset was unplugged from the charger.",
    ),
)

DESCRIPTOR_BY_ID: dict[str, InputDescriptor] = {d.id: d for d in INPUT_DESCRIPTORS}


def describe_input(input_id: str) -> InputDescriptor:
    known = DESCRIPTOR_BY_ID.get(input_id)
    if known is not None:
        return known
    return InputDescriptor(
        input_id, input_id.replace("_", " ").capitalize(), "Other",
        "Input reported by the device but not described by the protocol.",
    )


def inputs_by_category() -> dict[str, list[InputDescriptor]]:
    grouped: dict[str, list[InputDescriptor]] = {}
    for descriptor in INPUT_DESCRIPTORS:
        grouped.setdefault(descriptor.category, []).append(descriptor)
    return grouped


@dataclass(frozen=True)
class InputEvent:
    """One reconstructed interaction with a headset control."""

    input_id: str
    timestamp: float = field(default_factory=time.monotonic)
    #: How many steps the control moved. Always 1 for toggles.
    repeat: int = 1
    #: The value the field landed on, where that is meaningful.
    value: int | None = None
    detail: str = ""

    @property
    def label(self) -> str:
        return describe_input(self.input_id).label

    def __str__(self) -> str:
        suffix = f" x{self.repeat}" if self.repeat > 1 else ""
        return f"{self.label}{suffix}"


class EdgeDetector:
    """Converts a stream of snapshots into a stream of input events.

    Not thread safe by itself; the device layer calls it from the single
    reader-dispatch thread.
    """

    def __init__(
        self,
        settle_seconds: float = SETTLE_SECONDS,
        debounce_seconds: float = DEBOUNCE_SECONDS,
        resync_threshold: int = RESYNC_THRESHOLD,
    ) -> None:
        self.settle_seconds = settle_seconds
        self.debounce_seconds = debounce_seconds
        self.resync_threshold = resync_threshold
        self._previous: HeadsetSnapshot | None = None
        self._seeded = False
        self._settle_until = 0.0
        self._last_fired: dict[str, float] = {}
        self._suppressed = 0

    # ----------------------------------------------------------- lifecycle --

    def reset(self, reason: str = "") -> None:
        """Forget the baseline. Called on unplug so a later reconnect is clean."""
        if self._seeded:
            log.debug("Edge detector reset (%s)", reason or "no reason given")
        self._previous = None
        self._seeded = False
        self._settle_until = 0.0
        self._last_fired.clear()

    @property
    def seeded(self) -> bool:
        return self._seeded

    @property
    def suppressed_count(self) -> int:
        return self._suppressed

    @property
    def last_snapshot(self) -> HeadsetSnapshot | None:
        return self._previous

    # ------------------------------------------------------------- feeding --

    def feed(self, snapshot: HeadsetSnapshot, now: float | None = None) -> list[InputEvent]:
        now = time.monotonic() if now is None else now
        previous = self._previous
        self._previous = snapshot

        if previous is None or not self._seeded:
            self._seeded = True
            self._settle_until = now + self.settle_seconds
            log.debug("Baseline established: %s", snapshot.raw_hex)
            # A headset that is already linked at baseline still deserves the
            # link event, because the UI and any "connected" mapping need it.
            if snapshot.headset_connected:
                return self._admit([InputEvent(InputId.HEADSET_LINKED)], now, force=True)
            return []

        # -- link state. Never suppressed: it drives the whole UI. ----------
        #
        # A link transition re-baselines. Whatever the receiver reported while
        # the headset was off is not comparable with what it reports now, so
        # diffing across the boundary would invent presses. This is a hard
        # rule rather than a timing guard: the settle window below is a second
        # line of defence, not the only one.
        if snapshot.headset_connected != previous.headset_connected:
            if snapshot.headset_connected:
                self._settle_until = now + self.settle_seconds
                return self._admit(
                    [InputEvent(InputId.HEADSET_LINKED)], now, force=True
                )
            self._settle_until = 0.0
            return self._admit(
                [InputEvent(InputId.HEADSET_UNLINKED)], now, force=True
            )

        # While unlinked, the remaining fields are stale. Do not interpret them.
        if not snapshot.headset_connected:
            return []

        events: list[InputEvent] = []
        events.extend(self._volume_events(previous, snapshot))
        events.extend(self._chatmix_events(previous, snapshot))
        events.extend(self._toggle_events(previous, snapshot))
        events.extend(self._battery_events(previous, snapshot))

        return self._admit(events, now)

    # -------------------------------------------------------------- fields --

    def _volume_events(
        self, previous: HeadsetSnapshot, current: HeadsetSnapshot
    ) -> list[InputEvent]:
        before, after = previous.volume_level, current.volume_level
        if before is None or after is None or before == after:
            return []
        delta = after - before
        # Volume is different from the other fields: the receiver's B0 value is
        # the headset's authoritative current volume state. A large delta is
        # therefore still real information, not something to discard as a
        # guessed burst. If several reports were coalesced by Windows, the
        # resulting delta is the number of volume steps we can safely replay.
        input_id = InputId.VOLUME_UP if delta > 0 else InputId.VOLUME_DOWN
        return [
            InputEvent(
                input_id,
                repeat=abs(delta),
                value=after,
                detail=f"step {before} -> {after}",
            )
        ]

    def _chatmix_events(
        self, previous: HeadsetSnapshot, current: HeadsetSnapshot
    ) -> list[InputEvent]:
        before, after = previous.chat_balance, current.chat_balance
        if before is None or after is None or before == after:
            return []
        raw_delta = after - before
        steps = max(1, round(abs(raw_delta) / CHAT_BALANCE_STEP))
        if steps > self.resync_threshold:
            log.debug("Chat mix jumped %d steps; treating as resync", steps)
            return []
        input_id = InputId.CHATMIX_UP if raw_delta > 0 else InputId.CHATMIX_DOWN
        return [
            InputEvent(
                input_id, repeat=steps, value=after, detail=f"{before} -> {after}"
            )
        ]

    def _toggle_events(
        self, previous: HeadsetSnapshot, current: HeadsetSnapshot
    ) -> list[InputEvent]:
        events: list[InputEvent] = []
        if current.vss != previous.vss:
            events.append(
                InputEvent(
                    InputId.VSS_BUTTON,
                    value=int(current.vss),
                    detail="on" if current.vss else "off",
                )
            )
            events.append(
                InputEvent(InputId.VSS_ON if current.vss else InputId.VSS_OFF)
            )
        if current.mic_muted != previous.mic_muted:
            events.append(
                InputEvent(
                    InputId.MIC_BUTTON,
                    value=int(current.mic_muted),
                    detail="muted" if current.mic_muted else "unmuted",
                )
            )
            events.append(
                InputEvent(
                    InputId.MIC_MUTED if current.mic_muted else InputId.MIC_UNMUTED
                )
            )
        return events

    def _battery_events(
        self, previous: HeadsetSnapshot, current: HeadsetSnapshot
    ) -> list[InputEvent]:
        events: list[InputEvent] = []
        if current.charging != previous.charging:
            events.append(
                InputEvent(
                    InputId.CHARGING_STARTED
                    if current.charging
                    else InputId.CHARGING_STOPPED
                )
            )
        if current.battery_low and not previous.battery_low:
            events.append(
                InputEvent(InputId.BATTERY_LOW, value=current.battery_percent)
            )
        return events

    # ------------------------------------------------------------ admission --

    def _admit(
        self, events: Iterable[InputEvent], now: float, force: bool = False
    ) -> list[InputEvent]:
        admitted: list[InputEvent] = []
        for event in events:
            # Volume is already backed by the receiver's authoritative B0
            # state. It must not be hidden by the connection settle window:
            # if the user turns the wheel while the headset is coming online,
            # that real state transition is still a real input.
            volume_event = event.input_id in (InputId.VOLUME_UP, InputId.VOLUME_DOWN)

            if not force and not volume_event and now < self._settle_until:
                self._suppressed += 1
                log.debug("Suppressed %s inside settle window", event.input_id)
                continue

            last = self._last_fired.get(event.input_id)
            # Volume events come from the headset's state transition itself.
            # Never time-debounce them: rapid presses and hold-repeat must be
            # represented by every new B0 state the receiver gives us.
            if (
                not force
                and not volume_event
                and last is not None
                and (now - last) < self.debounce_seconds
            ):
                self._suppressed += 1
                log.debug("Debounced duplicate %s", event.input_id)
                continue
            self._last_fired[event.input_id] = now
            admitted.append(event)
        return admitted
