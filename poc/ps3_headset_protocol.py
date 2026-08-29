#!/usr/bin/env python3
"""Known receive-side protocol for the Sony PlayStation Gold Wireless Headset.

Target hardware:
    Headset:  Sony PlayStation Gold Wireless Stereo Headset, CUHYA-0080
    Adapter:  CUHYA-0081 wireless adapter
    Marking:  [NO60] (user-provided hardware marking)
    Receiver: USB VID 0x12BA / PID 0x0035

This module ONLY decodes bytes that have already been received from the HID
input path. It never opens devices and never performs any I/O.

Known incoming status report:
    B0 VV CC BB FF XX 11 00

    VV = receiver volume level, 0x00..0x05 (6 reported levels)
    CC = sound/chat balance, 0x00..0x64
    BB = battery level, 0x00..0x64; 0x80 while charging
    FF = flags
         bit 0 = VSS enabled
         bit 1 = microphone muted
         bit 3 = headset connected to receiver
         bits 6-7 = family/mode flags
    XX = unknown changing byte
    11 = observed constant
    00 = observed constant

The byte-level mapping is based on the public reverse-engineered
counter185/hid-playstation-headset driver for receiver 12BA:0035.
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
VOLUME_MAX = 0x05
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
    if family_flag == 0b01:
        model = TARGET_HEADSET_MODEL
    else:
        model = f"Sony headset (family flag {family_flag:02b})"

    return {
        "report_id": STATUS_REPORT_ID,
        "length": len(report),
        "volume_level": volume_level,
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
