#!/usr/bin/env python3
"""Receive-side protocol decoder for the Sony PlayStation Gold Wireless Headset.

This module only decodes already-received bytes. It performs no I/O and sends
no HID output, feature, or control reports.

Observed status report:
    B0 VV CC BB FF XX 11 00

VV = volume level, 0..10 (10 steps, exposed as 0..100 percent)
CC = chat balance, 0..100
BB = battery, 0..100; 0x80 while charging
FF = flags: bit 0 VSS, bit 1 microphone mute, bit 3 connected
"""

from __future__ import annotations

from typing import Any

STATUS_REPORT_ID = 0xB0
STATUS_LENGTH = 8

TARGET_VID = 0x12BA
TARGET_PID = 0x0035
TARGET_HEADSET_MODEL = "Sony PlayStation Gold Wireless Stereo Headset (CUHYA-0080)"
TARGET_HEADSET_MARKING = "[NO60]"
TARGET_ADAPTER_MODEL = "CUHYA-0081"

VOLUME_MIN = 0x00
VOLUME_MAX = 0x0A  # 10 volume levels: 0..10
CHAT_BALANCE_MIN = 0x00
CHAT_BALANCE_MAX = 0x64
BATTERY_MIN = 0x00
BATTERY_MAX = 0x64
BATTERY_CHARGING = 0x80

VSS_MASK = 0x01
MIC_MUTE_MASK = 0x02
CONNECTED_MASK = 0x08
MODEL_MASK = 0xC0


def hex_bytes(data: bytes | bytearray) -> str:
    return " ".join(f"{b:02X}" for b in data)


def decode_b0(report: bytes) -> dict[str, Any] | None:
    """Decode one incoming B0 status report; never performs I/O."""
    if len(report) != STATUS_LENGTH or report[0] != STATUS_REPORT_ID:
        return None

    volume_raw = report[1]
    chat_balance_raw = report[2]
    battery_raw = report[3]
    flags = report[4]

    volume_level = volume_raw if VOLUME_MIN <= volume_raw <= VOLUME_MAX else None
    volume_percent = volume_level * 10 if volume_level is not None else None
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
