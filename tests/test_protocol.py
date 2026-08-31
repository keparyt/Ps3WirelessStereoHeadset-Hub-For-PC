from poc.ps3_headset_protocol import decode_b0


def test_b0_gold_status():
    report = bytes.fromhex("B0 05 64 55 0B 2A 11 00")
    decoded = decode_b0(report)
    assert decoded is not None
    assert decoded["volume_level"] == 5
    assert decoded["chat_balance"] == 100
    assert decoded["battery_percent"] == 0x55
    assert decoded["charging"] is False
    assert decoded["vss"] is True
    assert decoded["mic_muted"] is True
    assert decoded["headset_connected"] is True
    assert decoded["family_flag"] == 0


def test_b0_charging():
    decoded = decode_b0(bytes.fromhex("B0 00 20 80 08 00 11 00"))
    assert decoded is not None
    assert decoded["charging"] is True
    assert decoded["battery_percent"] is None
    assert decoded["headset_connected"] is True


def test_non_b0_is_ignored():
    assert decode_b0(bytes.fromhex("01 02 03 04 05 06 07 08")) is None
    assert decode_b0(b"B0") is None
