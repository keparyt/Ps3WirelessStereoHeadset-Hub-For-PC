"""Protocol decoding, including the cases the proof of concept already covered."""

from ps3hub.protocol import (
    HeadsetSnapshot, decode_b0, normalize_report, parse_status, volume_to_percent,
)


def test_b0_gold_status():
    report = bytes.fromhex("B0 05 64 55 4B 2A 11 00")
    decoded = decode_b0(report)
    assert decoded is not None
    assert decoded["volume_level"] == 5
    assert decoded["chat_balance"] == 100
    assert decoded["battery_percent"] == 0x55
    assert decoded["charging"] is False
    assert decoded["vss"] is True
    assert decoded["mic_muted"] is True
    assert decoded["headset_connected"] is True
    assert decoded["family_flag"] == 1
    assert "Gold Wireless Stereo Headset" in decoded["model"]


def test_b0_charging():
    decoded = decode_b0(bytes.fromhex("B0 00 20 80 08 00 11 00"))
    assert decoded is not None
    assert decoded["charging"] is True
    assert decoded["battery_percent"] is None
    assert decoded["headset_connected"] is True


def test_non_b0_is_ignored():
    assert decode_b0(bytes.fromhex("01 02 03 04 05 06 07 08")) is None
    assert decode_b0(b"B0") is None


def test_gold_v1_volume_levels_map_to_percent():
    assert volume_to_percent(0) == 0
    assert volume_to_percent(1) == 20
    assert volume_to_percent(2) == 40
    assert volume_to_percent(3) == 60
    assert volume_to_percent(4) == 80
    assert volume_to_percent(5) == 100
    assert volume_to_percent(6) is None
    assert volume_to_percent(None) is None


def test_volume_above_gold_v1_max_is_rejected_not_guessed():
    decoded = decode_b0(bytes.fromhex("B0 06 20 55 48 00 11 00"))
    assert decoded is not None
    assert decoded["volume_level"] is None
    assert decoded["volume_percent"] is None
    assert decoded["volume_raw"] == 0x06  # the raw value is still available


def test_normalize_strips_leading_report_id_padding():
    padded = bytes.fromhex("00 B0 05 64 55 4B 2A 11 00")
    assert normalize_report(padded) == bytes.fromhex("B0 05 64 55 4B 2A 11 00")


def test_normalize_trims_trailing_padding():
    padded = bytes.fromhex("B0 05 64 55 4B 2A 11 00 00 00")
    assert normalize_report(padded) == bytes.fromhex("B0 05 64 55 4B 2A 11 00")


def test_normalize_leaves_the_exact_case_untouched():
    exact = bytes.fromhex("B0 05 64 55 4B 2A 11 00")
    assert normalize_report(exact) is exact


def test_parse_status_returns_a_snapshot():
    snapshot = parse_status(bytes.fromhex("B0 03 30 4B 49 00 11 00"))
    assert isinstance(snapshot, HeadsetSnapshot)
    assert snapshot.volume_level == 3
    assert snapshot.volume_percent == 60
    assert snapshot.headset_connected is True
    assert snapshot.vss is True
    assert snapshot.mic_muted is False
    assert snapshot.raw_hex.startswith("B0 03")


def test_battery_low_only_when_discharging():
    low = parse_status(bytes.fromhex("B0 03 30 0A 48 00 11 00"))
    assert low is not None and low.battery_low is True
    charging = parse_status(bytes.fromhex("B0 03 30 80 48 00 11 00"))
    assert charging is not None and charging.battery_low is False
