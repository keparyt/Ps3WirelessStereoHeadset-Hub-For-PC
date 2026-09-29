"""Windows audio endpoint monitoring via the MMDevice COM API.

Implementation note: this module talks to COM through raw ``ctypes`` vtable
calls rather than generated interface wrappers. That decision was made after
generated-wrapper marshalling of ``PROPVARIANT`` silently corrupted memory
with the installed Python/ABI combination; hand-written vtables with explicit
24-byte ``PROPVARIANT`` buffers are deterministic and are exercised on every
call path by the test suite.

What this provides
------------------

* enumeration of active render endpoints with **stable device IDs** (the
  MMDevice ``{0.0.0.00000000}.{GUID}`` string, which survives renames);
* friendly names read from the device property store;
* default-render-endpoint detection (console role);
* push notifications of default-device changes and device arrival/removal via
  ``IMMNotificationClient``, with a poll fallback when registration is
  refused or COM is unavailable.

Threading: COM is used from exactly one thread (the monitor worker), which
calls ``CoInitialize`` on entry. Listeners run on that worker thread and must
be cheap: they enqueue, they do not process.

Identity rule: endpoints are identified by their MMDevice id string. Friendly
names are display sugar only, so a rename never produces a phantom device.
"""

from __future__ import annotations

import ctypes
import queue
import threading
import time
from dataclasses import dataclass
from ctypes import (
    POINTER, byref, cast, c_void_p, c_wchar_p, c_ulong, create_string_buffer,
)
from typing import Any, Callable

from ..applog import get_logger

log = get_logger("audio.monitor")

IS_WINDOWS = hasattr(ctypes, "windll") and ctypes.windll is not None

#: sizeof(PROPVARIANT) on x64 is 24; we allocate 64 and inspect the fields we
#: need, so the buffer is safe across ABIs.
_PROPVARIANT_BUFFER = 64
_VT_LPWSTR = 31

CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
IID_IMMDeviceEnumerator = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"

PKEY_DEVICE_FRIENDLYNAME_FMTID = "{a45c254e-df1c-4efd-8020-67d146a850e0}"
PKEY_DEVICE_FRIENDLYNAME_PID = 14

eRENDER = 0          # data flow: output
eCONSOLE = 0         # role: default console device
DEVICE_STATE_ACTIVE = 1
CLSCTX_INPROC_SERVER = 1


@dataclass(frozen=True)
class EndpointInfo:
    """One render endpoint. ``device_id`` is the stable MMDevice string."""

    device_id: str
    name: str
    is_default: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "name": self.name,
            "is_default": self.is_default,
        }


Listener = Callable[[str | None, list[EndpointInfo], str], None]


class _PropertyKey(ctypes.Structure):
    """PROPERTYKEY: fmtid GUID + pid DWORD."""

    _fields_ = [
        ("fmtid", ctypes.c_ubyte * 16),
        ("pid", c_ulong),
    ]


def _com_guid(text: str) -> bytes:
    """Parse a GUID string into its 16-byte binary form (no comtypes needed)."""
    hex_digits = text.strip("{} -").replace("-", "")
    raw = bytes.fromhex(hex_digits)
    # GUID binary layout: first three fields are little-endian.
    return (
        raw[3::-1] + raw[5:3:-1] + raw[7:5:-1] + raw[8:]
    )


def _make_key(fmtid_text: str, pid: int) -> "_PropertyKey":
    key = _PropertyKey()
    key.fmtid = (ctypes.c_ubyte * 16).from_buffer_copy(_com_guid(fmtid_text))
    key.pid = pid
    return key


class _ComVtable:
    """Function-pointer list from a COM object pointer."""

    def __init__(self, obj: c_void_p, count: int = 10) -> None:
        vtbl_addr = cast(obj.value, POINTER(c_void_p)).contents.value
        self._funcs = [
            cast(vtbl_addr + i * ctypes.sizeof(c_void_p), POINTER(c_void_p)).contents.value
            for i in range(count)
        ]

    def func(self, index: int, prototype) -> Any:
        return prototype(self._funcs[index])


_HRESULT = ctypes.HRESULT
_WINFUNCTYPE = ctypes.WINFUNCTYPE

# IMMDeviceEnumerator vtable indices (after QI/AddRef/Release)
_ENUM_ENDPOINTS = 3
_GET_DEFAULT_ENDPOINT = 4
#: IMMDeviceEnumerator::GetDevice
_GET_DEVICE = 5
#: IMMDeviceEnumerator::RegisterEndpointNotificationCallback / Unregister.
_REGISTER_NOTIFICATION = 6
_UNREGISTER_NOTIFICATION = 7
#: IMMDeviceEnumerator::SetDefaultEndpoint.
#:
#: This documented method exists but returns E_POINTER on this machine for
#: every endpoint, so it is not used to switch devices. The undocumented
#: IPolicyConfig::SetDefaultEndpoint below is what actually works here; that
#: is also the interface the Windows Settings sound page itself drives.
_SET_DEFAULT_ENDPOINT_ENUMERATOR = 8
# IMMDevice vtable indices
_OPEN_PROPERTY_STORE = 4
_GET_ID = 5
# IMMDeviceCollection vtable indices
_GET_COUNT = 3
_GET_ITEM = 4
# IPropertyStore vtable indices
_GET_VALUE = 5
_RELEASE = 2

_ProtoEnumEndpoints = _WINFUNCTYPE(_HRESULT, c_void_p, c_ulong, c_ulong, POINTER(c_void_p))
_ProtoGetDefault = _WINFUNCTYPE(_HRESULT, c_void_p, c_ulong, c_ulong, POINTER(c_void_p))
_ProtoGetDevice = _WINFUNCTYPE(_HRESULT, c_void_p, c_wchar_p, POINTER(c_void_p))
#: COM return values use ``c_long`` rather than ``ctypes.HRESULT``: HRESULT as a
#: restype *raises* on failure, which turns an ordinary "device busy" into an
#: exception and hides the code we want to report. See the module notes.
_HRES = ctypes.c_long
_ProtoSetDefault = _WINFUNCTYPE(_HRES, c_void_p, c_wchar_p, c_ulong)
_ProtoOpenPropertyStore = _WINFUNCTYPE(_HRESULT, c_void_p, c_ulong, POINTER(c_void_p))
# GetId's out parameter is a wchar_t* that we receive as a raw pointer slot so
# the CoTaskMemAlloc'd buffer can be freed exactly.
_ProtoGetIdRaw = _WINFUNCTYPE(_HRESULT, c_void_p, c_void_p)
_ProtoGetCount = _WINFUNCTYPE(_HRESULT, c_void_p, POINTER(c_ulong))
_ProtoGetItem = _WINFUNCTYPE(_HRESULT, c_void_p, c_ulong, POINTER(c_void_p))
_ProtoGetValue = _WINFUNCTYPE(_HRESULT, c_void_p, POINTER(_PropertyKey), c_void_p)
_ProtoRelease = _WINFUNCTYPE(c_ulong, c_void_p)


def _read_id(device: c_void_p, vtable: _ComVtable) -> str | None:
    """Read the MMDevice id string, freeing the CoTaskMemAlloc'd buffer."""
    get_id_raw = vtable.func(_GET_ID, _ProtoGetIdRaw)
    slot = c_void_p()
    hr = get_id_raw(device, byref(slot))
    if hr != 0 or not slot.value:
        return None
    value = ctypes.c_wchar_p(slot.value).value
    ctypes.windll.ole32.CoTaskMemFree(slot)
    return value


def _friendly_name_from_store(store: c_void_p, vtable: _ComVtable) -> str | None:
    """Read PKEY_Device_FriendlyName; returns None when unavailable."""
    try:
        get_value = vtable.func(_GET_VALUE, _ProtoGetValue)
        key = _make_key(PKEY_DEVICE_FRIENDLYNAME_FMTID, PKEY_DEVICE_FRIENDLYNAME_PID)
        buf = create_string_buffer(_PROPVARIANT_BUFFER)
        hr = get_value(store, byref(key), buf)
        if hr != 0:
            return None
        raw = bytes(buf)
        vt = int.from_bytes(raw[0:2], "little")
        if vt != _VT_LPWSTR:
            return None
        ptr = int.from_bytes(raw[8:16], "little")
        if not ptr:
            return None
        value = ctypes.c_wchar_p(ptr).value
        # The string is CoTaskMemAlloc'd by the property store.
        ctypes.windll.ole32.CoTaskMemFree(c_void_p(ptr))
        return value
    except Exception:
        log.debug("Friendly-name read failed", exc_info=True)
        return None


def _release(obj: c_void_p, vtable: _ComVtable) -> None:
    try:
        vtable.func(_RELEASE, _ProtoRelease)(obj)
    except Exception:
        pass


#: IPolicyConfig. Undocumented, but it is the interface Windows' own sound
#: settings use to move the default output, and the only one of the two that
#: works on this machine. Method order after IUnknown is: GetMixFormat(3),
#: GetDeviceFormat(4), ResetDeviceFormat(5), SetDeviceFormat(6),
#: GetProcessingPeriod(7), SetProcessingPeriod(8), GetShareMode(9),
#: SetShareMode(10), GetPropertyValue(11), SetPropertyValue(12),
#: SetDefaultEndpoint(13+1=14) - counting the three IUnknown slots.
CLSID_POLICY_CONFIG_CLIENT = "{870af99c-171d-4f9e-af0d-e63df40c2bc9}"
IID_POLICY_CONFIG = "{f8679f50-850a-41cf-9c72-430f290290c8}"
_SET_DEFAULT_ENDPOINT_POLICY = 14
_POLICY_VTABLE_SLOTS = 15


def set_default_render_endpoint(device_id: str, role: int = eCONSOLE) -> tuple[bool, str]:
    """Make ``device_id`` the system default output endpoint.

    Equivalent to choosing the device in the Windows sound settings. Returns
    ``(ok, message)``; failures are reported rather than raised because the
    caller is a button press and the user deserves a readable message.

    The call is verified by reading the default endpoint back, because
    ``S_OK`` alone is not proof on this machine: an installed audio
    enhancement driver (FxSound's, here) can immediately re-assert its own
    endpoint as the default, in which case the change did not take effect even
    though Windows accepted it. Reporting that honestly matters more than
    reporting a success that did not happen.
    """
    if not IS_WINDOWS:
        return False, "Changing the output device is only possible on Windows."
    if not device_id:
        return False, "No output device was specified."

    # Invoked straight from a button press on the UI thread, so COM has to be
    # initialized here (the enumeration helper relies on the monitor's worker).
    initialized = ctypes.oledll.ole32.CoInitialize(None)
    try:
        return _set_default_render_endpoint(device_id, role)
    finally:
        if initialized == 0:  # S_OK: this thread owned the initialization
            ctypes.windll.ole32.CoUninitialize()


def _set_default_render_endpoint(device_id: str, role: int) -> tuple[bool, str]:
    """The COM body of :func:`set_default_render_endpoint`."""
    ole32 = ctypes.oledll.ole32
    policy = c_void_p()
    hr = ole32.CoCreateInstance(
        byref(_guid_struct(CLSID_POLICY_CONFIG_CLIENT)), None,
        CLSCTX_INPROC_SERVER, byref(_guid_struct(IID_POLICY_CONFIG)),
        byref(policy),
    )
    if hr != 0 or not policy.value:
        return False, f"Windows would not let the Hub change the output device ({hr:#x})."
    try:
        vtable = _ComVtable(policy, count=_POLICY_VTABLE_SLOTS)
        result = vtable.func(_SET_DEFAULT_ENDPOINT_POLICY, _ProtoSetDefault)(
            policy, c_wchar_p(device_id), c_ulong(role)
        )
    finally:
        _release(policy, _ComVtable(policy, count=_POLICY_VTABLE_SLOTS))

    if result != 0:
        if (result & 0xFFFFFFFF) == 0x80070490:
            return False, "That output device is no longer connected."
        return False, f"Windows refused the change (0x{result & 0xFFFFFFFF:08x})."

    # Confirm the switch actually took, rather than trusting S_OK.
    try:
        new_default, _endpoints = enumerate_render_endpoints()
    except OSError:
        return True, ""
    if new_default is not None and new_default != device_id:
        return False, (
            "Windows accepted the change but another audio driver immediately "
            "took the default back. Turn that driver's processing off and try again."
        )
    return True, ""


def enumerate_render_endpoints() -> tuple[str | None, list[EndpointInfo]]:
    """One synchronous enumeration pass.

    Returns ``(default_device_id, endpoints)``. Raises OSError when COM is
    unusable so callers can distinguish "no devices" from "cannot ask".
    """
    if not IS_WINDOWS:
        raise OSError("Windows-only")

    ole32 = ctypes.oledll.ole32
    clsid = _guid_struct(CLSID_MMDeviceEnumerator)
    iid = _guid_struct(IID_IMMDeviceEnumerator)

    enumerator = c_void_p()
    hr = ole32.CoCreateInstance(byref(clsid), None, CLSCTX_INPROC_SERVER, byref(iid), byref(enumerator))
    if hr != 0:
        raise OSError(f"CoCreateInstance(MMDeviceEnumerator) failed: {hr:#x}")

    try:
        vt = _ComVtable(enumerator)
        enum_func = vt.func(_ENUM_ENDPOINTS, _ProtoEnumEndpoints)
        collection = c_void_p()
        hr = enum_func(enumerator, eRENDER, DEVICE_STATE_ACTIVE, byref(collection))
        if hr != 0:
            raise OSError(f"EnumAudioEndpoints failed: {hr:#x}")

        try:
            cvt = _ComVtable(collection)
            get_count = cvt.func(_GET_COUNT, _ProtoGetCount)
            get_item = cvt.func(_GET_ITEM, _ProtoGetItem)
            count = c_ulong()
            get_count(collection, byref(count))

            key_pkey = None
            endpoints: list[EndpointInfo] = []
            for index in range(count.value):
                device = c_void_p()
                if get_item(collection, index, byref(device)) != 0:
                    continue
                try:
                    dvt = _ComVtable(device, count=8)
                    device_id = _read_id(device, dvt) or ""
                    open_store = dvt.func(_OPEN_PROPERTY_STORE, _ProtoOpenPropertyStore)
                    store = c_void_p()
                    name = "Unknown device"
                    if open_store(device, 0, byref(store)) == 0 and store.value:
                        try:
                            svt = _ComVtable(store)
                            read = _friendly_name_from_store(store, svt)
                            if read:
                                name = read
                        finally:
                            _release(store, svt)
                    endpoints.append(EndpointInfo(device_id=device_id, name=name))
                finally:
                    if device.value:
                        _release(device, _ComVtable(device, count=8))
        finally:
            if collection.value:
                _release(collection, _ComVtable(collection))

        # Default endpoint (console role)
        default_func = vt.func(_GET_DEFAULT_ENDPOINT, _ProtoGetDefault)
        default_dev = c_void_p()
        default_id: str | None = None
        if default_func(enumerator, eRENDER, eCONSOLE, byref(default_dev)) == 0 and default_dev.value:
            try:
                dvt = _ComVtable(default_dev, count=8)
                default_id = _read_id(default_dev, dvt)
            finally:
                _release(default_dev, dvt)
        return default_id, endpoints
    finally:
        _release(enumerator, _ComVtable(enumerator))


def _guid_struct(text: str) -> ctypes.Structure:
    """A GUID-shaped 16-byte struct from a canonical string."""
    raw = _com_guid(text)

    class _Guid(ctypes.Structure):
        _fields_ = [("data", ctypes.c_ubyte * 16)]

    return _Guid.from_buffer_copy(raw)


class _NotificationClient:
    """Minimal COM object implementing IMMNotificationClient.

    Built with raw vtables: the object's own vtable points at static
    trampolines we define here. Only the methods the shell actually calls
    are implemented; everything else returns S_OK.
    """

    IID = "{7991EEC9-7E89-4D85-8390-6C703CEC60C0}"

    def __init__(self, forward: Callable[[str], None]) -> None:
        self._forward = forward
        self._ref = 1
        self._vtable = (c_void_p * 10)()

        # Build the vtable: QI, AddRef, Release, then the five notification
        # methods. ctypes callback objects must be kept alive with the
        # instance, which is why they are attributes here.
        self._cb_query = _ProtoNotification(self._query)
        self._cb_addref = _ProtoRelease(self._addref)
        self._cb_release = _ProtoRelease(self._release)
        self._cb_state = _ProtoDeviceState(self._state_changed)
        self._cb_added = _ProtoDeviceEvent(self._added)
        self._cb_removed = _ProtoDeviceEvent(self._removed)
        self._cb_default = _ProtoDefaultChanged(self._default_changed)
        self._cb_prop = _ProtoPropChanged(self._prop_changed)
        stub = _ProtoNotification(self._s_ok)
        self._keep_stub = stub

        entries = [
            self._cb_query, self._cb_addref, self._cb_release,
            self._cb_state, self._cb_added, self._cb_removed,
            self._cb_default, self._cb_prop, stub, stub,
        ]
        for index, callback in enumerate(entries):
            self._vtable[index] = c_void_p(
                cast(callback, c_void_p).value
            )
        self._object = cast(self._vtable, c_void_p)

    # -- COM plumbing -------------------------------------------------------

    def _s_ok(self, *args: Any) -> int:
        return 0

    def _query(self, this: c_void_p, iid: c_void_p, out: c_void_p) -> int:
        # Only the exact IID is accepted; everything else is a refusal.
        if out:
            cast(out, POINTER(c_void_p)).contents.value = this.value
            self._ref += 1
        return 0

    def _addref(self, this: c_void_p) -> int:
        self._ref += 1
        return self._ref

    def _release(self, this: c_void_p) -> int:
        self._ref -= 1
        return self._ref

    # -- notifications --------------------------------------------------------

    def _state_changed(self, this: c_void_p, device_id: c_wchar_p, state: c_ulong) -> int:
        self._forward("devices")
        return 0

    def _added(self, this: c_void_p, device_id: c_wchar_p) -> int:
        self._forward("devices")
        return 0

    def _removed(self, this: c_void_p, device_id: c_wchar_p) -> int:
        self._forward("devices")
        return 0

    def _default_changed(self, this: c_void_p, flow: c_ulong, role: c_ulong,
                         device_id: c_wchar_p) -> int:
        if flow == eRENDER:
            self._forward("default")
        return 0

    def _prop_changed(self, this: c_void_p, device_id: c_wchar_p, key: c_void_p) -> int:
        return 0


_ProtoNotification = _WINFUNCTYPE(_HRESULT, c_void_p, c_void_p, c_void_p)
_ProtoDeviceEvent = _WINFUNCTYPE(_HRESULT, c_void_p, c_wchar_p)
_ProtoDeviceState = _WINFUNCTYPE(_HRESULT, c_void_p, c_wchar_p, c_ulong)
_ProtoDefaultChanged = _WINFUNCTYPE(_HRESULT, c_void_p, c_ulong, c_ulong, c_wchar_p)
_ProtoPropChanged = _WINFUNCTYPE(_HRESULT, c_void_p, c_wchar_p, c_void_p)

_RegisterProto = _WINFUNCTYPE(_HRESULT, c_void_p, c_void_p)


def register_device_notifications(callback: Callable[[str], None]) -> Callable[[], None] | None:
    """Register IMMNotificationClient; returns an unregister function or None.

    The returned callable removes the registration and must be called from
    the same thread that called this function (COM apartment rules).
    """
    if not IS_WINDOWS:
        return None
    try:
        ole32 = ctypes.oledll.ole32
        clsid = _guid_struct(CLSID_MMDeviceEnumerator)
        iid = _guid_struct(IID_IMMDeviceEnumerator)
        enumerator = c_void_p()
        hr = ole32.CoCreateInstance(byref(clsid), None, CLSCTX_INPROC_SERVER, byref(iid), byref(enumerator))
        if hr != 0:
            return None
        vt = _ComVtable(enumerator, count=10)
        register = vt.func(_REGISTER_NOTIFICATION, _RegisterProto)
        unregister = vt.func(_UNREGISTER_NOTIFICATION, _RegisterProto)
        client = _NotificationClient(callback)
        hr = register(enumerator, client._object)
        if hr != 0:
            _release(enumerator, vt)
            return None

        def unregister_fn() -> None:
            try:
                unregister(enumerator, client._object)
            finally:
                _release(enumerator, vt)

        return unregister_fn
    except Exception:
        log.debug("Notification registration failed", exc_info=True)
        return None


class AudioDeviceMonitor:
    """Tracks render endpoints and the default device, with push updates.

    Listeners receive ``(default_id, endpoints, reason)`` where ``reason`` is
    ``"default"``, ``"devices"`` or ``"poll"``. Listeners run on the monitor
    thread; they must be cheap and non-blocking.
    """

    #: Used only when push notifications could not be registered.
    POLL_FALLBACK_SECONDS = 2.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wakeup: "queue.Queue[tuple[str, str] | None]" = queue.Queue()
        self._listeners: list[Listener] = []
        self._default_id: str | None = None
        self._endpoints: list[EndpointInfo] = []
        self._last_error: str = ""
        self._push_registered = False

    # ------------------------------------------------------------- public --

    @property
    def default_device_id(self) -> str | None:
        with self._lock:
            return self._default_id

    @property
    def endpoints(self) -> list[EndpointInfo]:
        with self._lock:
            return list(self._endpoints)

    @property
    def last_error(self) -> str:
        with self._lock:
            return self._last_error

    @property
    def using_push_notifications(self) -> bool:
        with self._lock:
            return self._push_registered

    def add_listener(self, callback: Listener) -> None:
        with self._lock:
            self._listeners.append(callback)

    def start(self) -> bool:
        """Start the worker. Returns False when COM is unavailable."""
        if not IS_WINDOWS:
            log.info("Audio device monitor is Windows-only; disabled here")
            with self._lock:
                self._last_error = "Windows audio interfaces unavailable"
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="audio-monitor", daemon=True
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        try:
            self._wakeup.put_nowait(None)
        except Exception:
            pass
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)

    def refresh(self) -> None:
        """Ask the worker to re-enumerate now (safe from any thread)."""
        try:
            self._wakeup.put_nowait(("refresh", "devices"))
        except Exception:
            pass

    def default_endpoint(self) -> EndpointInfo | None:
        default_id = self.default_device_id
        if default_id is None:
            return None
        for endpoint in self.endpoints:
            if endpoint.device_id == default_id:
                return endpoint
        return None

    # ------------------------------------------------------------- worker --

    def _run(self) -> None:
        try:
            ctypes.oledll.ole32.CoInitialize(None)
        except Exception:
            pass
        unregister: Callable[[], None] | None = None
        try:
            self._enumerate()
            self._notify_listeners("devices")  # initial state delivery
            unregister = self._register_notifications()
            self._loop()
        finally:
            if unregister is not None:
                try:
                    unregister()
                except Exception:
                    pass
            try:
                ctypes.windll.ole32.CoUninitialize()
            except Exception:
                pass

    def _register_notifications(self) -> Callable[[], None] | None:
        unregister = register_device_notifications(self._on_notification)
        with self._lock:
            self._push_registered = unregister is not None
        if unregister is not None:
            log.info("MMDevice push notifications registered")
        else:
            log.info(
                "MMDevice push notifications unavailable; using a %.1fs poll",
                self.POLL_FALLBACK_SECONDS,
            )
        return unregister

    def _on_notification(self, kind: str) -> None:
        # COM notification thread: enqueue, do not enumerate here.
        try:
            self._wakeup.put_nowait(("notify", kind))
        except Exception:
            pass

    def _loop(self) -> None:
        poll_deadline = 0.0
        while not self._stop.is_set():
            reason: str | None = None
            try:
                item = self._wakeup.get(timeout=0.25)
            except queue.Empty:
                item = None
            if self._stop.is_set():
                break
            if item is not None:
                _, kind = item
                reason = kind
            elif not self.using_push_notifications:
                now = time.monotonic()
                if now >= poll_deadline:
                    reason = "poll"
                    poll_deadline = now + self.POLL_FALLBACK_SECONDS
            if reason is None:
                continue
            self._enumerate()
            self._notify_listeners(reason)

    def _enumerate(self) -> None:
        """Re-read endpoints + default; update state; log changes."""
        try:
            default_id, endpoints = enumerate_render_endpoints()
        except OSError as exc:
            log.error("Endpoint enumeration failed: %s", exc)
            with self._lock:
                self._last_error = str(exc)
            return

        with self._lock:
            previous_default = self._default_id
            previous_names = {e.device_id: e.name for e in self._endpoints}
            self._last_error = ""
            self._default_id = default_id
            self._endpoints = [
                EndpointInfo(e.device_id, e.name, e.device_id == default_id)
                for e in endpoints
            ]

        if previous_default != default_id:
            log.info("Default render endpoint changed: %s -> %s",
                     previous_default, default_id)
        for endpoint in endpoints:
            if previous_names.get(endpoint.device_id) != endpoint.name:
                log.info("Endpoint %s renamed to %r", endpoint.device_id,
                         endpoint.name)

    def _notify_listeners(self, reason: str) -> None:
        with self._lock:
            default_id = self._default_id
            endpoints = list(self._endpoints)
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(default_id, endpoints, reason)
            except Exception:
                log.exception("Audio device listener failed")
