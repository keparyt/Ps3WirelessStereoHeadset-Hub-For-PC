#!/usr/bin/env python3
"""Known receive-side protocol for Sony 12BA:0035 PlayStation headsets."""

from __future__ import annotations

from typing import Any

STATUS_REPORT_ID = 0xB0
STATUS_LENGTH = 8

VSS_MASK = 0x01
MIC_MUTE_MASK = 0x02
CONNECTED_MASK = 0x08
MODEL_MASK = 0xC0


def hex_bytes(data: bytes | bytearray) -> str:
    return " ".join(f"{b:02X}" for b in data)


def decode_b0(report: bytes) -> dict[str, Any] | None:
    """Decode an incoming 0xB0 report. Never performs I/O."""
    if len(report) < 5 or report[0] != STATUS_REPORT_ID:
        return None

    # The known report format is:
    # B0 volume chat-balance battery flags unknown constant constant
    volume_raw = report[1] if len(report) > 1 else None
    chat_balance = report[2] if len(report) > 2 else None
    battery_raw = report[3] if len(report) > 3 else None
    flags = report[4]

    charging = battery_raw == 0x80
    battery = None if charging else battery_raw
    if battery is not None and battery > 100:
        battery = None

    family = (flags & MODEL_MASK) >> 6
    if family == 0b01:
        model = "PlayStation Gold Wireless Headset"
    elif family == 0b10:
        model = "Sony headset (family flag 10)"
    elif family == 0b11:
        model = "Sony headset (family flag 11)"
    else:
        model = "Sony headset (family flag 00)"

    return {
        "report_id": STATUS_REPORT_ID,
        "volume_level": volume_raw if volume_raw is not None and volume_raw <= 5 else None,
        "volume_raw": volume_raw,
        "chat_balance": chat_balance if chat_balance is not None and chat_balance <= 100 else None,
        "chat_balance_raw": chat_balance,
        "battery_percent": battery,
        "battery_raw": battery_raw,
        "charging": charging,
        "vss": bool(flags & VSS_MASK),
        "mic_muted": bool(flags & MIC_MUTE_MASK),
        "headset_connected": bool(flags & CONNECTED_MASK),
        "flags": flags,
        "family_flag": family,
        "byte5_unknown": report[5] if len(report) > 5 else None,
        "byte6": report[6] if len(report) > 6 else None,
        "byte7": report[7] if len(report) > 7 else None,
        "raw": hex_bytes(report),
    }
