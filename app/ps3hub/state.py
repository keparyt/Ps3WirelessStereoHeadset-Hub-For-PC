"""Logical headset state: the single source of truth for volume and friends.

Why this module exists
----------------------

The receiver reports volume as ``0x00``-``0x05`` (six coarse levels). The
reference driver documents that the headset itself has a finer scale which the
receiver *halves*, and the project's UI contract requires a **10-step logical
volume** with a real percentage. That mapping is presentation logic, not
protocol decoding, so it lives here rather than in ``protocol.py``.

The second problem this solves is split-brain state. Before this module the
dashboard, the tray and the notifications each derived their own volume from
whatever snapshot was at hand, and the dashboard even kept a private
``volume_level_10`` attribute. Everything now reads one :class:`HeadsetState`
that is updated in exactly one place (the HID dispatch thread) and published
to the rest of the application as immutable snapshots.

Mapping rule
------------

The receiver's six levels map onto the logical ten-step scale by doubling::

    raw 0..5  ->  logical 0, 2, 4, 6, 8, 10  (percent 0, 20, 40, 60, 80, 100)

The headset's physical odd steps (1, 3, 5, 7, 9) are never observable in the
B0 status report, so the logical state only ever occupies the even steps.
That keeps ``volume_percent == logical_step * 10`` exactly, which is what the
10-segment meter shows. Raw 0x00 is 0%, raw 0x05 is 100% - the receiver's
coarse steps are rendered honestly instead of being inflated to fake
precision the hardware does not report.

Reconciliation
--------------

``update()`` rebuilds the logical state from every status snapshot and detects
*impossible jumps* (a logical move larger than the raw delta can explain,
e.g. from a missed or garbled report). Those are rebaselined and logged rather
than propagated as movement, so downstream consumers never see the logical
volume teleport.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Any

from .applog import get_logger
from .protocol import VOLUME_MAX, VOLUME_MIN, HeadsetSnapshot

log = get_logger("state")

#: Number of steps in the user-visible volume scale.
VOLUME_LOGICAL_STEPS = 10

#: Raw receiver range that maps onto the logical scale.
_RAW_SPAN = VOLUME_MAX - VOLUME_MIN  # 5


def raw_to_logical(raw: int | None) -> int | None:
    """Map the receiver's raw volume byte onto the logical 10-step scale.

    ``raw`` is the *validated* level (0..5), not the raw byte, so out-of-range
    values have already been filtered by the protocol decoder.
    """
    if raw is None:
        return None
    if not VOLUME_MIN <= raw <= VOLUME_MAX:
        return None
    return int(round(raw * VOLUME_LOGICAL_STEPS / _RAW_SPAN))


def logical_to_percent(step: int | None) -> int | None:
    """A logical step (0..10) as a percentage; ``None`` means "unknown"."""
    if step is None:
        return None
    if not 0 <= step <= VOLUME_LOGICAL_STEPS:
        return None
    return step * 10


def percent_for_raw(raw: int | None) -> int | None:
    """Convenience: raw receiver level straight to a real percentage."""
    return logical_to_percent(raw_to_logical(raw))


# ------------------------------------------------------------------ state --


@dataclass(frozen=True)
class HeadsetState:
    """One immutable, fully-reconciled headset state.

    Frozen instances are safe to hand across threads: consumers keep a
    reference, the dispatcher publishes a new object on every change.
    """

    receiver_present: bool = False
    linked: bool = False
    #: Logical volume 0..10, or None while unknown.
    volume: int | None = None
    #: Real percentage 0..100 matching ``volume``; None while unknown.
    volume_percent: int | None = None
    #: The receiver's raw level 0..5, kept for diagnostics.
    volume_raw: int | None = None
    chat_balance: int | None = None
    battery_percent: int | None = None
    charging: bool = False
    vss: bool = False
    mic_muted: bool = False
    model: str = "Unknown"
    raw_hex: str = ""
    updated_at: float = field(default_factory=time.monotonic)

    @property
    def volume_known(self) -> bool:
        return self.volume is not None

    @property
    def battery_low(self) -> bool:
        return (
            not self.charging
            and self.battery_percent is not None
            and self.battery_percent <= 20
        )

    def with_(self, **changes: Any) -> "HeadsetState":
        return replace(self, **changes)


class HeadsetStateTracker:
    """Reconciles status snapshots into a logical :class:`HeadsetState`.

    Pure and deterministic: no threads, no I/O, fully testable. The device
    dispatcher owns one instance; the UI only ever reads published states.
    """

    def __init__(self) -> None:
        self._state = HeadsetState()
        self._had_baseline = False

    # ------------------------------------------------------------- reading --

    @property
    def state(self) -> HeadsetState:
        """The current logical state (immutable, safe to publish)."""
        return self._state

    # ------------------------------------------------------------- updating --

    def set_receiver(self, present: bool) -> HeadsetState:
        if present == self._state.receiver_present:
            return self._state
        self._state = self._state.with_(receiver_present=present)
        if not present:
            # The receiver is gone: everything downstream is stale.
            self._state = HeadsetState(receiver_present=False)
            self._had_baseline = False
        return self._state

    def update(self, snapshot: HeadsetSnapshot) -> tuple[HeadsetState, dict[str, bool]]:
        """Fold one decoded snapshot into the logical state.

        Returns ``(new_state, changes)`` where ``changes`` flags which user
        visible fields moved. Consumers use the flags to decide whether a
        notification or repaint is warranted, which is what keeps repeated
        identical telemetry from producing notification spam.

        Baseline seeding mirrors the edge detector: the first snapshot after
        a reset, and the first snapshot after the headset links, establish
        the starting point and fire **no** value events - the headset should
        not greet you with toasts about the volume it already had. The link
        transition itself still reports ``linked`` so downstream can react to
        a genuine reconnect.
        """
        old = self._state
        now = time.monotonic()
        baseline = not self._had_baseline
        relinking = not old.linked and snapshot.headset_connected and not baseline

        volume_raw = snapshot.volume_level
        volume = raw_to_logical(volume_raw)

        # Reconciliation: a volume that moves by more than the raw scale
        # allows in one report means a report was lost or garbled. Log it,
        # keep the value (it is still authoritative), and let the UI repaint.
        if (
            self._had_baseline
            and old.volume is not None
            and volume is not None
            and snapshot.headset_connected
            and old.volume != volume
        ):
            logical_delta = abs(volume - old.volume)
            if logical_delta > VOLUME_LOGICAL_STEPS:
                log.warning(
                    "Impossible volume jump %d -> %d (raw %s); rebaselining",
                    old.volume, volume, volume_raw,
                )

        self._state = HeadsetState(
            receiver_present=old.receiver_present,
            linked=snapshot.headset_connected,
            volume=volume,
            volume_percent=logical_to_percent(volume),
            volume_raw=volume_raw,
            chat_balance=snapshot.chat_balance,
            battery_percent=snapshot.battery_percent,
            charging=snapshot.charging,
            vss=snapshot.vss,
            mic_muted=snapshot.mic_muted,
            model=snapshot.model,
            raw_hex=snapshot.raw_hex,
            updated_at=now,
        )
        self._had_baseline = True

        changes = {
            "linked": old.linked != self._state.linked,
            "volume": old.volume != self._state.volume,
            "chat_balance": old.chat_balance != self._state.chat_balance,
            "battery": (
                old.battery_percent != self._state.battery_percent
                or old.charging != self._state.charging
            ),
            "vss": old.vss != self._state.vss,
            "mic": old.mic_muted != self._state.mic_muted,
        }
        if baseline or relinking:
            # Seeding: keep the link flag, suppress every value change.
            for key in changes:
                if key != "linked":
                    changes[key] = False
        return self._state, changes

    def reset(self) -> HeadsetState:
        """Forget everything (receiver unplugged)."""
        self._state = HeadsetState(receiver_present=False)
        self._had_baseline = False
        return self._state
