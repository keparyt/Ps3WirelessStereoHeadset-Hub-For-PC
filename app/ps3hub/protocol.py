"""Receive-side protocol decoding for the Sony PlayStation Gold Wireless Headset.

This module performs no I/O whatsoever. It turns bytes that somebody else
already read into meaning, which is what makes the protocol testable against
recorded captures without any hardware present.

Observed status report (8 bytes):

    B0 VV CC BB FF XX 11 00
    │  │  │  │  │  │  │  └── observed constant 0x00
    │  │  │  │  │  │  └───── observed constant 0x11
    │  │  │  │  │  └──────── unknown, changes
    │  │  │  │  └─────────── flags: b0 VSS, b1 mic mute, b3 link, b6-7 family
    │  │  │  └────────────── battery 0x00-0x64, 0x80 while charging
    │  │  └───────────────── sound/chat balance 0x00-0x64
    │  └──────────────────── volume level 0x00-0x0A (10 discrete steps)
    └─────────────────────── status report id
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------- identity --

TARGET_VID = 0x12BA
TARGET_PID = 0x0035
TARGET_HEADSET_MODEL = "Sony PlayStation Gold Wireless Stereo Headset (CUHYA-0080)"
TARGET_HEADSET_MARKING = "[NO60]"
TARGET_ADAPTER_MODEL = "CUHYA-0081"

#: The vendor collection that actually carries the 0xB0 status reports.
STATUS_USAGE_PAGE = 0xFF01
STATUS_USAGE = 0x0020

#: Friendly names for every collection the receiver exposes.
COLLECTION_NAMES = {
    (0x000C, 0x0001): "Consumer Control",
    (0xFF00, 0x0001): "Vendor FF00",
    (0xFF03, 0x0020): "Vendor FF03",
    (0xFF01, 0x0020): "Status (FF01:0020)",
}

# ---------------------------------------------------------------- protocol --

STATUS_REPORT_ID = 0xB0
STATUS_LENGTH = 8

VOLUME_MIN = 0x00
VOLUME_MAX = 0x0A  # 10 discrete hardware steps, reported as 0..10
VOLUME_STEPS = VOLUME_MAX - VOLUME_MIN

CHAT_BALANCE_MIN = 0x00
CHAT_BALANCE_MAX = 0x64
#: The reference driver documents chat balance moving in steps of 8.
CHAT_BALANCE_STEP = 0x08

BATTERY_MIN = 0x00
BATTERY_MAX = 0x64
BATTERY_CHARGING = 0x80

VSS_MASK = 0x01
MIC_MUTE_MASK = 0x02
CONNECTED_MASK = 0x08
MODEL_MASK = 0xC0

#: Battery percentage at or below which the headset is considered low.
BATTERY_LOW_THRESHOLD = 20


def hex_bytes(data: bytes | bytearray) -> str:
    return " ".join(f"{b:02X}" for b in data)


def collection_name(usage_page: int, usage: int) -> str:
    known = COLLECTION_NAMES.get((usage_page, usage))
    if known:
        return known
    return f"Vendor {usage_page:04X}:{usage:04X}"


def is_status_collection(usage_page: int, usage: int) -> bool:
    return usage_page == STATUS_USAGE_PAGE and usage == STATUS_USAGE


def volume_to_percent(level: int | None) -> int | None:
    """Map a discrete hardware step onto 0-100%.

    The hardware exposes 10 steps and nothing finer. The percentage is a
    presentation of those steps, not a claim of continuous resolution.
    """
    if level is None:
        return None
    if not VOLUME_MIN <= level <= VOLUME_MAX:
        return None
    return round(level * 100 / VOLUME_STEPS)


def normalize_report(report: bytes) -> bytes:
    """Strip HID report-ID padding so decoding sees the documented 8 bytes.

    Windows returns a buffer sized to ``InputReportByteLength``. Depending on
    how the collection declares report IDs that buffer may carry a leading
    0x00, or trailing zero padding. The PoC only ever handled the exact
    8-byte case; both variants are normalised here so a correctly received
    status packet is never silently dropped.
    """
    if not report:
        return report
    if len(report) == STATUS_LENGTH and report[0] == STATUS_REPORT_ID:
        return report  # the common, already-correct case
    if len(report) > STATUS_LENGTH and report[0] == 0x00 and report[1] == STATUS_REPORT_ID:
        return report[1 : STATUS_LENGTH + 1]
    if len(report) > STATUS_LENGTH and report[0] == STATUS_REPORT_ID:
        return report[:STATUS_LENGTH]
    return report


def decode_b0(report: bytes) -> dict[str, Any] | None:
    """Decode one incoming B0 status report; never performs I/O.

    Returns ``None`` for anything that is not a status report, so callers can
    use it as a cheap filter over every inbound HID report.
    """
    if len(report) != STATUS_LENGTH or report[0] != STATUS_REPORT_ID:
        return None

    volume_raw = report[1]
    chat_balance_raw = report[2]
    battery_raw = report[3]
    flags = report[4]

    volume_level = volume_raw if VOLUME_MIN <= volume_raw <= VOLUME_MAX else None
    volume_percent = volume_to_percent(volume_level)
    chat_balance = (
        chat_balance_raw
        if CHAT_BALANCE_MIN <= chat_balance_raw <= CHAT_BALANCE_MAX
        else None
    )

    if battery_raw == BATTERY_CHARGING:
        battery_percent = None
        charging = True
    elif BATTERY_MIN <= battery_raw <= BATTERY_MAX:
        battery_percent = battery_raw
        charging = False
    else:
        battery_percent = None
        charging = False

    family_flag = (flags & MODEL_MASK) >> 6
    model = (
        TARGET_HEADSET_MODEL
        if family_flag == 0b01
        else f"Sony headset (family flag {family_flag:02b})"
    )

    return {
        "report_id": STATUS_REPORT_ID,
        "length": len(report),
        "model": model,
        "volume_level": volume_level,
        "volume_percent": volume_percent,
        "volume_raw": volume_raw,
        "chat_balance": chat_balance,
        "chat_balance_raw": chat_balance_raw,
        "battery_percent": battery_percent,
        "battery_raw": battery_raw,
        "charging": charging,
        "vss": bool(flags & VSS_MASK),
        "mic_muted": bool(flags & MIC_MUTE_MASK),
        "headset_connected": bool(flags & CONNECTED_MASK),
        "flags": flags,
        "family_flag": family_flag,
        "byte5_unknown": report[5],
        "byte6_observed_constant": report[6],
        "byte7_observed_constant": report[7],
        "raw": hex_bytes(report),
    }


@dataclass(frozen=True)
class HeadsetSnapshot:
    """One fully decoded status report.

    Frozen so a snapshot can be handed across threads without copying.
    """

    volume_level: int | None = None
    volume_percent: int | None = None
    chat_balance: int | None = None
    battery_percent: int | None = None
    charging: bool = False
    vss: bool = False
    mic_muted: bool = False
    headset_connected: bool = False
    flags: int = 0
    family_flag: int = 0
    model: str = "Unknown"
    raw: bytes = b""
    unknown_bytes: tuple[int, int, int] = (0, 0, 0)

    @property
    def raw_hex(self) -> str:
        return hex_bytes(self.raw)

    @property
    def battery_low(self) -> bool:
        return (
            not self.charging
            and self.battery_percent is not None
            and self.battery_percent <= BATTERY_LOW_THRESHOLD
        )

    @classmethod
    def from_decoded(cls, decoded: dict[str, Any], raw: bytes) -> "HeadsetSnapshot":
        return cls(
            volume_level=decoded["volume_level"],
            volume_percent=decoded["volume_percent"],
            chat_balance=decoded["chat_balance"],
            battery_percent=decoded["battery_percent"],
            charging=decoded["charging"],
            vss=decoded["vss"],
            mic_muted=decoded["mic_muted"],
            headset_connected=decoded["headset_connected"],
            flags=decoded["flags"],
            family_flag=decoded["family_flag"],
            model=decoded["model"],
            raw=bytes(raw),
            unknown_bytes=(
                decoded["byte5_unknown"],
                decoded["byte6_observed_constant"],
                decoded["byte7_observed_constant"],
            ),
        )


def parse_status(report: bytes) -> HeadsetSnapshot | None:
    """Normalise then decode an inbound report into a snapshot."""
    normalized = normalize_report(report)
    decoded = decode_b0(normalized)
    if decoded is None:
        return None
    return HeadsetSnapshot.from_decoded(decoded, normalized)


@dataclass
class ReportFingerprint:
    """Bookkeeping for reports the decoder does not understand.

    Requirement: "Provide useful debugging information when an input cannot be
    identified." Rather than discarding unknown traffic, each distinct shape is
    counted so the Diagnostics view can show exactly what arrived.
    """

    usage_page: int
    usage: int
    length: int
    first_byte: int
    count: int = 0
    last_raw: str = ""
    samples: list[str] = field(default_factory=list)

    @property
    def key(self) -> tuple[int, int, int, int]:
        return (self.usage_page, self.usage, self.length, self.first_byte)

    @property
    def label(self) -> str:
        return (
            f"{collection_name(self.usage_page, self.usage)} · "
            f"{self.length} bytes · first byte 0x{self.first_byte:02X}"
        )

    def observe(self, raw: bytes, sample_limit: int = 5) -> None:
        self.count += 1
        self.last_raw = hex_bytes(raw)
        if len(self.samples) < sample_limit and self.last_raw not in self.samples:
            self.samples.append(self.last_raw)
