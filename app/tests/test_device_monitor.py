"""Device monitor: GUID parsing, endpoint model, and the COM object layout.

COM enumeration itself is exercised by a live probe on real hardware; here we
verify the pure pieces every platform can run, plus the vtable contract of the
hand-rolled ``IMMNotificationClient``.

Why the layout tests exist
--------------------------

The notification client hands Windows a bare interface pointer that Windows
dereferences twice: ``vtbl = *(void**)this`` and then
``call ((void**)vtbl)[index]``. The first version handed out the vtable array
itself, so Windows read the first trampoline's *machine code* as a vtable and
executed the bytes sitting at that address. The trigger was mundane - closing
FxSound removes an audio endpoint, which delivers the first
``OnDeviceRemoved`` - and the result was an execute access violation at 0x0
that killed the process outright: no traceback, no ``panic.log``, no shutdown
path. These tests pin the contract so a regression fails a test instead of
taking the Hub down with it.
"""

import ctypes

import pytest

from ps3hub.audio import device_monitor as dm
from ps3hub.audio.device_monitor import EndpointInfo, _com_guid

#: IUnknown(3) + OnDeviceStateChanged, OnDeviceAdded, OnDeviceRemoved,
#: OnDefaultDeviceChanged, OnPropertyValueChanged.
IMMNotificationClientIID = dm._IID_NOTIFICATION_CLIENT


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


# -- the COM object handed to Windows ---------------------------------------

needs_windows = pytest.mark.skipif(
    not dm.IS_WINDOWS, reason="raw COM vtables are Windows-only"
)


def _client() -> "dm._NotificationClient":
    return dm._NotificationClient(lambda kind: None)


def _slot_address(client, index: int) -> int:
    """Read a vtable entry the way Windows does, starting from the interface."""
    interface = ctypes.cast(client._object, ctypes.c_void_p).value
    vtable = ctypes.cast(
        ctypes.c_void_p(interface), ctypes.POINTER(ctypes.c_void_p)
    ).contents.value
    return ctypes.cast(
        ctypes.c_void_p(vtable + index * ctypes.sizeof(ctypes.c_void_p)),
        ctypes.POINTER(ctypes.c_void_p),
    ).contents.value


@needs_windows
def test_interface_pointer_points_at_a_pointer_to_the_vtable():
    client = _client()
    interface = ctypes.cast(client._object, ctypes.c_void_p).value
    vtable = ctypes.cast(client._vtable, ctypes.c_void_p).value

    # Windows reads *(void**)this and expects to find our vtable there.
    assert _slot_address(client, 0) != 0
    read_back = ctypes.cast(
        ctypes.c_void_p(interface), ctypes.POINTER(ctypes.c_void_p)
    ).contents.value

    assert read_back == vtable, "Windows would read something other than our vtable"
    assert interface != vtable, "the interface pointer must not be the table itself"


@needs_windows
def test_every_vtable_slot_holds_a_callable_trampoline():
    client = _client()
    for index in range(dm._NOTIFICATION_VTABLE_SLOTS):
        assert _slot_address(client, index) != 0, f"slot {index} is NULL"


@needs_windows
def test_windows_can_query_interface_through_the_vtable():
    """The real calling convention, not a direct Python call."""
    client = _client()
    interface = ctypes.cast(client._object, ctypes.c_void_p).value
    query = ctypes.WINFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
    )(_slot_address(client, 0))

    out = ctypes.c_void_p()
    iid = ctypes.create_string_buffer(IMMNotificationClientIID, 16)
    assert query(interface, ctypes.cast(iid, ctypes.c_void_p), ctypes.byref(out)) == 0
    assert out.value == interface
    assert client._ref == 2


@needs_windows
def test_query_interface_refuses_foreign_iids_without_raising():
    """A refusal must be a return value: raising inside a callback is swallowed
    by ctypes, which then reports S_OK and hands out a bogus interface."""
    client = _client()
    interface = ctypes.cast(client._object, ctypes.c_void_p).value
    out = ctypes.c_void_p(0xDEADBEEF)
    bogus = ctypes.create_string_buffer(b"\x11" * 16, 16)

    hr = client._query(
        interface, ctypes.cast(bogus, ctypes.c_void_p).value, ctypes.addressof(out)
    )

    assert hr == dm._NotificationClient.E_NOINTERFACE
    assert out.value == 0xDEADBEEF, "a refused QueryInterface must not write out"
    assert client._ref == 1


@needs_windows
def test_default_device_removed_notification_survives_the_vtable_call():
    """Slot 5 is the call that used to execute a data address and crash.

    Closing FxSound destroys an audio endpoint, which delivers exactly this
    notification to whatever is registered.
    """
    kinds: list[str] = []
    client = dm._NotificationClient(kinds.append)
    interface = ctypes.cast(client._object, ctypes.c_void_p).value
    removed = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_wchar_p)(
        _slot_address(client, 5)
    )

    assert removed(interface, "test-endpoint") == 0
    assert kinds == ["devices"], "the notification must reach the forwarder"


@needs_windows
def test_unregistered_notification_slots_land_on_the_s_ok_stub():
    # Slot 8 is past IMMNotificationClient's own methods; it must still be
    # callable rather than pointing at whatever follows in the heap.
    stub_address = None
    client = _client()
    for index in range(8, dm._NOTIFICATION_VTABLE_SLOTS):
        address = _slot_address(client, index)
        stub_address = stub_address or address
        assert address == stub_address


@needs_windows
def test_s_ok_stub_is_callable_directly():
    client = _client()
    assert client._s_ok() == 0
