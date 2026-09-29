"""Device monitor: GUID parsing and endpoint model, offline and fast.

COM enumeration itself is exercised by a live probe on real hardware; here we
verify the pure pieces every platform can run.
"""

from ps3hub.audio.device_monitor import EndpointInfo, _com_guid


def test_guid_binary_layout_is_little_endian_mixed():
    # PKEY_DEVICE_FRIENDLYNAME fmtid, a well-known GUID.
    raw = _com_guid("{a45c254e-df1c-4efd-8020-67d146a850e0}")
    assert len(raw) == 16
    # Data1 is little-endian: 4e 25 5c a4
    assert raw[0:4] == bytes([0x4E, 0x25, 0x5C, 0xA4])
    # Data2 little-endian: 1c df
    assert raw[4:6] == bytes([0x1C, 0xDF])
    # Data3 little-endian: fd 4e
    assert raw[6:8] == bytes([0xFD, 0x4E])
    # Data4 is byte-for-byte.
    assert raw[8:] == bytes([0x80, 0x20, 0x67, 0xD1, 0x46, 0xA8, 0x50, 0xE0])


def test_endpoint_info_dict_round_trip():
    info = EndpointInfo(device_id="\\\\?\\HDEV", name="Speakers", is_default=True)
    assert info.as_dict() == {
        "device_id": "\\\\?\\HDEV", "name": "Speakers", "is_default": True,
    }
