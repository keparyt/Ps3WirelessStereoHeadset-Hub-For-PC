"""Windows receive-only HID reader for Sony 12BA:0035.

This is the native diagnostic transport retained from the proof of concept.
The application prefers the hidapi interrupt-IN path because it matches the
reference driver's normal HID raw-event transport. The overlapped ``ReadFile`` design is the reason
the application can be closed, and the receiver unplugged, without a wedged
thread: a synchronous read blocks forever inside the HID class driver and
cannot be interrupted.

Behaviour that must not regress:

* ``GENERIC_READ`` only. No write access is ever requested.
* No output reports, feature reports, control transfers or polling commands.
* ``FILE_FLAG_OVERLAPPED`` plus ``CancelIoEx`` so shutdown is immediate.
* Reads are sized to the collection's real ``InputReportByteLength``.

Changes from the proof of concept:

* Diagnostics go through the logging module, so they survive in a windowed
  build that has no console attached.
* ``ERROR_DEVICE_NOT_CONNECTED`` and friends are classified as an expected
  unplug rather than a crash, which lets the device layer reconnect quietly.
* The read buffer is allocated once instead of on every loop iteration.
"""

from __future__ import annotations

import ctypes
import os
import threading
import time
from ctypes import wintypes
from typing import Callable

try:
    import hid as _hidapi
except Exception:
    _hidapi = None  # type: ignore[assignment]

from .applog import get_logger

log = get_logger("hid")

if os.name == "nt":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _hid = ctypes.WinDLL("hid", use_last_error=True)
else:  # allows the module to be imported for tests on other platforms
    _kernel32 = _hid = None

GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x00000080
FILE_FLAG_OVERLAPPED = 0x40000000
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x102
ERROR_IO_PENDING = 997
ERROR_OPERATION_ABORTED = 995
ERROR_DEVICE_NOT_CONNECTED = 1167
ERROR_INVALID_HANDLE = 6
ERROR_OPERATION_IN_PROGRESS = 112
ERROR_ACCESS_DENIED = 5
ERROR_FILE_NOT_FOUND = 2
ERROR_NO_SUCH_DEVICE = 433
ERROR_DEVICE_REMOVED = 1617
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
ULONG_PTR = ctypes.c_size_t

HIDP_STATUS_SUCCESS = 0x00110000

REFERENCE_REPORT_ID = 0xB0
REFERENCE_REPORT_SIZE = 8

#: Errors that mean "the device went away", not "something is broken".
DISCONNECT_ERRORS = {
    ERROR_OPERATION_ABORTED,
    ERROR_DEVICE_NOT_CONNECTED,
    ERROR_INVALID_HANDLE,
    ERROR_FILE_NOT_FOUND,
    ERROR_NO_SUCH_DEVICE,
    ERROR_DEVICE_REMOVED,
}


class DeviceGoneError(OSError):
    """Raised/reported when the receiver disappears during a read."""


class NoInputReportError(OSError):
    """HID collection exists but declares no input-report payload."""


class OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ULONG_PTR),
        ("InternalHigh", ULONG_PTR),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


class HIDP_CAPS(ctypes.Structure):
    _fields_ = [
        ("Usage", wintypes.USHORT),
        ("UsagePage", wintypes.USHORT),
        ("InputReportByteLength", wintypes.USHORT),
        ("OutputReportByteLength", wintypes.USHORT),
        ("FeatureReportByteLength", wintypes.USHORT),
        ("Reserved", wintypes.USHORT * 17),
        ("NumberLinkCollectionNodes", wintypes.USHORT),
        ("NumberInputButtonCaps", wintypes.USHORT),
        ("NumberInputValueCaps", wintypes.USHORT),
        ("NumberInputDataIndices", wintypes.USHORT),
        ("NumberOutputButtonCaps", wintypes.USHORT),
        ("NumberOutputValueCaps", wintypes.USHORT),
        ("NumberOutputDataIndices", wintypes.USHORT),
        ("NumberFeatureButtonCaps", wintypes.USHORT),
        ("NumberFeatureValueCaps", wintypes.USHORT),
        ("NumberFeatureDataIndices", wintypes.USHORT),
    ]


if _kernel32 is not None:
    _CreateFileW = _kernel32.CreateFileW
    _CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    _CreateFileW.restype = wintypes.HANDLE

    _ReadFile = _kernel32.ReadFile
    _ReadFile.argtypes = [
        wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(OVERLAPPED),
    ]
    _ReadFile.restype = wintypes.BOOL

    _GetOverlappedResult = _kernel32.GetOverlappedResult
    _GetOverlappedResult.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(OVERLAPPED),
        ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
    ]
    _GetOverlappedResult.restype = wintypes.BOOL

    _CreateEventW = _kernel32.CreateEventW
    _CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    _CreateEventW.restype = wintypes.HANDLE

    _ResetEvent = _kernel32.ResetEvent
    _ResetEvent.argtypes = [wintypes.HANDLE]
    _ResetEvent.restype = wintypes.BOOL

    _WaitForSingleObject = _kernel32.WaitForSingleObject
    _WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _WaitForSingleObject.restype = wintypes.DWORD

    _CancelIoEx = _kernel32.CancelIoEx
    _CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED)]
    _CancelIoEx.restype = wintypes.BOOL

    _CloseHandle = _kernel32.CloseHandle
    _CloseHandle.argtypes = [wintypes.HANDLE]
    _CloseHandle.restype = wintypes.BOOL

if _hid is not None:
    _HidD_GetPreparsedData = _hid.HidD_GetPreparsedData
    _HidD_GetPreparsedData.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
    _HidD_GetPreparsedData.restype = wintypes.BOOLEAN

    _HidD_FreePreparsedData = _hid.HidD_FreePreparsedData
    _HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
    _HidD_FreePreparsedData.restype = wintypes.BOOLEAN

    _HidP_GetCaps = _hid.HidP_GetCaps
    _HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDP_CAPS)]
    _HidP_GetCaps.restype = ctypes.c_int

    _HidD_SetNumInputBuffers = _hid.HidD_SetNumInputBuffers
    _HidD_SetNumInputBuffers.argtypes = [wintypes.HANDLE, wintypes.ULONG]
    _HidD_SetNumInputBuffers.restype = wintypes.BOOLEAN



def native_windows_available() -> bool:
    return os.name == "nt" and _kernel32 is not None and _hid is not None


def hidapi_available() -> bool:
    """Return whether the Python hidapi backend is importable."""
    return _hidapi is not None


def windows_path(path: object) -> str:
    if isinstance(path, bytes):
        return path.decode("utf-8", errors="replace")
    return str(path)


def _winerr() -> int:
    return ctypes.get_last_error()


def _hv(handle: object) -> int:
    try:
        return int(handle)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


class HidApiReader:
    """Receive-only HID interrupt-IN reader using Python hidapi.

    This is the preferred Windows transport for the PS3 receiver because the
    reference Linux driver consumes the HID raw-event stream directly. hidapi
    opens the same HID collection path and exposes the incoming interrupt-IN
    reports without issuing output, feature, or control requests.
    """

    POLL_INTERVAL_SECONDS = 0.005
    DEFAULT_READ_SIZE = 512

    def __init__(
        self,
        path: object,
        on_report: Callable[[bytes], None],
        on_error: Callable[[Exception], None] | None = None,
        label: str = "",
        read_size: int = DEFAULT_READ_SIZE,
    ) -> None:
        if _hidapi is None:
            raise OSError("Python hidapi is not installed")
        self.path = path
        self.label = label or str(path)[-40:]
        self.on_report = on_report
        self.on_error = on_error
        self.read_size = max(8, int(read_size))
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.device = None
        self.report_count = 0
        self._lock = threading.Lock()
        self._stopped = False

    def open(self) -> None:
        if _hidapi is None:
            raise OSError("Python hidapi is not installed")

        device = _hidapi.device()
        try:
            device.open_path(self.path)
            device.set_nonblocking(True)
        except Exception:
            try:
                device.close()
            except Exception:
                pass
            raise

        with self._lock:
            self.device = device
            self._stopped = False

        log.info("Opened %s via hidapi (nonblocking interrupt-IN)", self.label)

    def start(self) -> None:
        self.stop_event.clear()
        self.open()
        self.thread = threading.Thread(
            target=self._worker,
            name=f"hidapi-reader-{self.label}",
            daemon=True,
        )
        self.thread.start()

    def _worker(self) -> None:
        with self._lock:
            device = self.device

        if device is None:
            log.error("hidapi reader started without a device: %s", self.label)
            return

        try:
            while not self.stop_event.is_set():
                try:
                    report = device.read(self.read_size)
                except Exception as exc:
                    if self.stop_event.is_set():
                        return
                    log.error("%s: hidapi read failed: %s", self.label, exc)
                    if self.on_error is not None:
                        try:
                            self.on_error(exc)
                        except Exception:
                            log.exception("hidapi error callback raised")
                    return

                if report:
                    self.report_count += 1
                    payload = bytes(report)
                    try:
                        self.on_report(payload)
                    except Exception as exc:
                        log.exception(
                            "hidapi report callback raised on %s: %s",
                            self.label,
                            exc,
                        )

                time.sleep(self.POLL_INTERVAL_SECONDS)
        except Exception as exc:
            if self.stop_event.is_set():
                return
            log.exception("Unhandled hidapi reader failure on %s", self.label)
            if self.on_error is not None:
                try:
                    self.on_error(exc)
                except Exception:
                    log.exception("hidapi error callback raised")

    def stop(self) -> None:
        if self.stop_event.is_set() and self._stopped:
            return

        self.stop_event.set()
        with self._lock:
            device = self.device
            self.device = None
            self._stopped = True

        if device is not None:
            try:
                device.close()
            except Exception:
                pass

        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=1.5)

        log.debug("hidapi reader stopped: %s", self.label)

    @property
    def alive(self) -> bool:
        return self.thread is not None and self.thread.is_alive()


class NativeWindowsHIDReader:
    """Reads input reports from a single HID collection on a worker thread."""

    WAIT_SLICE_MS = 100
    INPUT_BUFFER_COUNT = 64

    def __init__(
        self,
        path: object,
        on_report: Callable[[bytes], None],
        on_error: Callable[[Exception], None] | None = None,
        label: str = "",
    ) -> None:
        if not native_windows_available():
            raise OSError("Native Windows HID backend is only available on Windows")
        self.path = windows_path(path)
        self.label = label or self.path[-40:]
        self.on_report = on_report
        self.on_error = on_error
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.handle = None
        self.event = None
        self.input_report_length = 0
        self.output_report_length = 0
        self.feature_report_length = 0
        self.usage = 0
        self.usage_page = 0
        self.report_count = 0
        self._lock = threading.Lock()
        self._stopped = False

    # ------------------------------------------------------------- opening --

    def _get_caps(self, handle) -> HIDP_CAPS:
        preparsed = ctypes.c_void_p()
        if not _HidD_GetPreparsedData(handle, ctypes.byref(preparsed)):
            err = _winerr()
            raise OSError(err, f"HidD_GetPreparsedData failed (WinError {err})")
        try:
            caps = HIDP_CAPS()
            status = _HidP_GetCaps(preparsed, ctypes.byref(caps))
            if status != HIDP_STATUS_SUCCESS:
                raise OSError(
                    status, f"HidP_GetCaps failed (NTSTATUS 0x{status & 0xFFFFFFFF:08X})"
                )
            return caps
        finally:
            _HidD_FreePreparsedData(preparsed)

    def open(self) -> None:
        log.debug("Opening collection %s", self.label)
        handle = _CreateFileW(
            self.path,
            GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OVERLAPPED,
            None,
        )
        if handle in (None, INVALID_HANDLE_VALUE):
            err = _winerr()
            hint = ""
            if err == ERROR_ACCESS_DENIED:
                hint = " (access denied - another application may hold this collection)"
            raise OSError(err, f"CreateFileW failed (WinError {err}){hint}")

        try:
            if not _HidD_SetNumInputBuffers(handle, self.INPUT_BUFFER_COUNT):
                log.debug(
                    "HidD_SetNumInputBuffers failed (WinError %d); continuing", _winerr()
                )
            caps = self._get_caps(handle)
            if caps.InputReportByteLength <= 0:
                raise NoInputReportError(
                    "HID collection declares InputReportByteLength=0; "
                    "it has no readable input-report stream"
                )
            self.input_report_length = int(caps.InputReportByteLength)
            self.output_report_length = int(caps.OutputReportByteLength)
            self.feature_report_length = int(caps.FeatureReportByteLength)
            self.usage = int(caps.Usage)
            self.usage_page = int(caps.UsagePage)
            log.info(
                "Opened %s usage_page=0x%04X usage=0x%04X input=%d",
                self.label, self.usage_page, self.usage, self.input_report_length,
            )
        except Exception:
            _CloseHandle(handle)
            raise

        event = _CreateEventW(None, True, False, None)
        if not event:
            err = _winerr()
            _CloseHandle(handle)
            raise OSError(err, f"CreateEventW failed (WinError {err})")

        with self._lock:
            self.handle = handle
            self.event = event
            self._stopped = False

    def start(self) -> None:
        self.stop_event.clear()
        self.open()
        self.thread = threading.Thread(
            target=self._worker, name=f"hid-reader-{self.usage_page:04X}", daemon=True
        )
        self.thread.start()

    # -------------------------------------------------------------- reading --

    def _worker(self) -> None:
        with self._lock:
            handle, event = self.handle, self.event
        if handle is None or event is None:
            log.error("Reader started with an invalid handle")
            return

        buffer = ctypes.create_string_buffer(self.input_report_length)
        try:
            while not self.stop_event.is_set():
                _ResetEvent(event)
                overlapped = OVERLAPPED()
                overlapped.hEvent = event
                received = wintypes.DWORD(0)

                ok = _ReadFile(
                    handle, buffer, self.input_report_length,
                    ctypes.byref(received), ctypes.byref(overlapped),
                )
                if ok:
                    count = int(received.value)
                    if count:
                        self._deliver(bytes(buffer.raw[:count]))
                    continue

                err = _winerr()
                if err != ERROR_IO_PENDING:
                    if err in DISCONNECT_ERRORS and self.stop_event.is_set():
                        return
                    self._fail(err, "ReadFile")
                    return

                # Wait in slices so stop() is honoured promptly.
                while not self.stop_event.is_set():
                    result = _WaitForSingleObject(event, self.WAIT_SLICE_MS)
                    if result == WAIT_OBJECT_0:
                        break
                    if result != WAIT_TIMEOUT:
                        self._fail(_winerr(), "WaitForSingleObject")
                        return

                if self.stop_event.is_set():
                    _CancelIoEx(handle, ctypes.byref(overlapped))
                    return

                received = wintypes.DWORD(0)
                if not _GetOverlappedResult(
                    handle, ctypes.byref(overlapped), ctypes.byref(received), False
                ):
                    err = _winerr()
                    if err == ERROR_OPERATION_IN_PROGRESS:
                        continue
                    self._fail(err, "GetOverlappedResult")
                    return

                count = int(received.value)
                if count:
                    self._deliver(bytes(buffer.raw[:count]))
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("Unhandled reader failure on %s", self.label)
            self._report_error(exc)

    def _deliver(self, report: bytes) -> None:
        self.report_count += 1
        try:
            self.on_report(report)
        except Exception as exc:  # a UI bug must not kill the reader
            log.exception("Report callback raised: %s", exc)

    def _fail(self, err: int, where: str) -> None:
        if err in DISCONNECT_ERRORS:
            log.info("%s: device gone during %s (WinError %d)", self.label, where, err)
            self._report_error(DeviceGoneError(err, f"{where}: device disconnected"))
        else:
            log.error("%s: %s failed (WinError %d)", self.label, where, err)
            self._report_error(OSError(err, f"{where} failed (WinError {err})"))

    def _report_error(self, exc: Exception) -> None:
        if self.on_error and not self.stop_event.is_set():
            try:
                self.on_error(exc)
            except Exception:
                log.exception("Error callback raised")

    # ------------------------------------------------------------ shutdown --

    def stop(self) -> None:
        if self.stop_event.is_set() and self._stopped:
            return
        self.stop_event.set()
        with self._lock:
            handle, event = self.handle, self.event
            self.handle = self.event = None
            self._stopped = True

        if handle not in (None, INVALID_HANDLE_VALUE):
            try:
                _CancelIoEx(handle, None)
            except Exception:
                pass
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=1.5)
        for closable in (event, handle):
            if closable not in (None, INVALID_HANDLE_VALUE):
                try:
                    _CloseHandle(closable)
                except Exception:
                    pass
        log.debug("Reader stopped: %s", self.label)

    @property
    def alive(self) -> bool:
        return self.thread is not None and self.thread.is_alive()
