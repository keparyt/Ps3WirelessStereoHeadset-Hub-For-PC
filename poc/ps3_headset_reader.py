#!/usr/bin/env python3
"""Native Windows HID input reader for the PS3 Wireless Stereo Headset.

Receive-only path:
    headset -> USB receiver -> Windows HID class driver -> this process

The reader deliberately uses the HID collection's *actual* InputReportByteLength
instead of an arbitrary large ReadFile buffer.  This matters for composite HID
interfaces such as the 12BA:0035 receiver, which exposes several collections.
No output, feature, or control requests are performed.
"""
from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes
from typing import Callable

if os.name == "nt":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _hid = ctypes.WinDLL("hid", use_last_error=True)
else:
    _kernel32 = None
    _hid = None

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
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
ULONG_PTR = ctypes.c_size_t


class OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ULONG_PTR),
        ("InternalHigh", ULONG_PTR),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


# HIDP_CAPS contains a USHORT for every field below.  The two reserved
# arrays are part of the Windows ABI and must be preserved for the offsets to
# match hidparse.dll's structure.
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
    _CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _CreateFileW.restype = wintypes.HANDLE
    _ReadFile = _kernel32.ReadFile
    _ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(OVERLAPPED)]
    _ReadFile.restype = wintypes.BOOL
    _GetOverlappedResult = _kernel32.GetOverlappedResult
    _GetOverlappedResult.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED), ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]
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


def native_windows_available() -> bool:
    return os.name == "nt" and _kernel32 is not None and _hid is not None


def windows_path(path: object) -> str:
    return path.decode("utf-8", errors="replace") if isinstance(path, bytes) else str(path)


def _winerr() -> int:
    return ctypes.get_last_error()


class NativeWindowsHIDReader:
    WAIT_SLICE_MS = 100

    def __init__(self, path: object, on_report: Callable[[bytes], None], on_error: Callable[[Exception], None] | None = None, on_log: Callable[[str], None] | None = None) -> None:
        if not native_windows_available():
            raise OSError("Native Windows HID backend is only available on Windows")
        self.path = windows_path(path)
        self.on_report = on_report
        self.on_error = on_error
        self.on_log = on_log
        self.stop_event = threading.Event()
        self.thread = None
        self.handle = None
        self.event = None
        self.input_report_length = 0
        self.output_report_length = 0
        self.feature_report_length = 0
        self._lock = threading.Lock()
        self._stopped = False
        self.report_count = 0

    def _log(self, message: str) -> None:
        if self.on_log:
            try:
                self.on_log(message)
            except Exception:
                pass

    def _get_caps(self, handle) -> HIDP_CAPS:
        preparsed = ctypes.c_void_p()
        if not _HidD_GetPreparsedData(handle, ctypes.byref(preparsed)):
            error = _winerr()
            raise OSError(error, f"HidD_GetPreparsedData failed (WinError {error})")
        try:
            caps = HIDP_CAPS()
            status = _HidP_GetCaps(preparsed, ctypes.byref(caps))
            # HIDP_STATUS_SUCCESS == 0x00110000.
            if status != 0x00110000:
                raise OSError(status, f"HidP_GetCaps failed (NTSTATUS 0x{status & 0xFFFFFFFF:08X})")
            return caps
        finally:
            _HidD_FreePreparsedData(preparsed)

    def open(self) -> None:
        self._log("[WINHID] CreateFileW: opening collection")
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
            error = _winerr()
            hint = " (access denied)" if error == ERROR_ACCESS_DENIED else ""
            raise OSError(error, f"CreateFileW failed (WinError {error}){hint}")

        self._log(f"[WINHID] CreateFileW SUCCESS handle=0x{int(handle):X}")
        try:
            caps = self._get_caps(handle)
            if caps.InputReportByteLength <= 0:
                raise OSError("HID collection reports InputReportByteLength=0")
            self.input_report_length = int(caps.InputReportByteLength)
            self.output_report_length = int(caps.OutputReportByteLength)
            self.feature_report_length = int(caps.FeatureReportByteLength)
            self._log(
                "[WINHID][CAPS] "
                f"usage=0x{int(caps.Usage):04X} page=0x{int(caps.UsagePage):04X} "
                f"input={self.input_report_length} output={self.output_report_length} "
                f"feature={self.feature_report_length}"
            )
        except Exception:
            _CloseHandle(handle)
            raise

        event = _CreateEventW(None, True, False, None)
        if not event:
            error = _winerr()
            _CloseHandle(handle)
            raise OSError(error, f"CreateEventW failed (WinError {error})")
        self._log(f"[WINHID] CreateEventW SUCCESS event=0x{int(event):X}")
        with self._lock:
            self.handle, self.event, self._stopped = handle, event, False

    def start(self) -> None:
        self.open()
        self.thread = threading.Thread(target=self._worker, name="ps3-headset-hid-reader", daemon=True)
        self.thread.start()
        self._log(
            f"[WINHID] reader thread STARTED; waiting for HID input "
            f"(exact report buffer={self.input_report_length} bytes)"
        )

    def _worker(self) -> None:
        with self._lock:
            handle, event = self.handle, self.event
        if handle is None or event is None:
            self._log("[WINHID][FATAL] invalid handle/event")
            return

        try:
            while not self.stop_event.is_set():
                if not _ResetEvent(event):
                    error = _winerr()
                    self._handle_error(OSError(error, f"ResetEvent failed (WinError {error})"))
                    return

                overlapped = OVERLAPPED()
                overlapped.hEvent = event
                buffer = ctypes.create_string_buffer(self.input_report_length)
                received = wintypes.DWORD(0)

                ok = _ReadFile(
                    handle,
                    buffer,
                    self.input_report_length,
                    ctypes.byref(received),
                    ctypes.byref(overlapped),
                )

                if ok:
                    # Synchronous completion is valid. Do not wait on the event.
                    count = int(received.value)
                    self._deliver(bytes(buffer.raw[:count]))
                    continue

                error = _winerr()
                if error != ERROR_IO_PENDING:
                    if error in {ERROR_OPERATION_ABORTED, ERROR_DEVICE_NOT_CONNECTED, ERROR_INVALID_HANDLE} and self.stop_event.is_set():
                        return
                    self._handle_error(OSError(error, f"ReadFile failed (WinError {error})"))
                    return

                while not self.stop_event.is_set():
                    result = _WaitForSingleObject(event, self.WAIT_SLICE_MS)
                    if result == WAIT_OBJECT_0:
                        break
                    if result != WAIT_TIMEOUT:
                        error = _winerr()
                        self._handle_error(OSError(error, f"WaitForSingleObject failed (WinError {error})"))
                        return

                if self.stop_event.is_set():
                    _CancelIoEx(handle, ctypes.byref(overlapped))
                    return

                received = wintypes.DWORD(0)
                if not _GetOverlappedResult(handle, ctypes.byref(overlapped), ctypes.byref(received), False):
                    error = _winerr()
                    if error == ERROR_OPERATION_IN_PROGRESS:
                        continue
                    if error in {ERROR_OPERATION_ABORTED, ERROR_DEVICE_NOT_CONNECTED, ERROR_INVALID_HANDLE}:
                        self._handle_error(OSError(error, f"HID read stopped (WinError {error})"))
                        return
                    self._handle_error(OSError(error, f"GetOverlappedResult failed (WinError {error})"))
                    return

                count = int(received.value)
                if count:
                    self._deliver(bytes(buffer.raw[:count]))

        except Exception as exc:
            self._handle_error(exc)

    def _deliver(self, report: bytes) -> None:
        self.report_count += 1
        self._log(
            f"[WINHID][REPORT] #{self.report_count} bytes={len(report)} "
            f"raw=[{' '.join(f'{b:02X}' for b in report)}]"
        )
        try:
            self.on_report(report)
        except Exception as exc:
            self._log(f"[WINHID][CALLBACK-ERROR] {type(exc).__name__}: {exc}")

    def _handle_error(self, exc: Exception) -> None:
        self._log(f"[WINHID][ERROR] {type(exc).__name__}: {exc}")
        if self.on_error and not self.stop_event.is_set():
            try:
                self.on_error(exc)
            except Exception:
                pass

    def stop(self) -> None:
        if self.stop_event.is_set() and self._stopped:
            return
        self._log("[WINHID] stopping reader")
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
        if event not in (None, INVALID_HANDLE_VALUE):
            try:
                _CloseHandle(event)
            except Exception:
                pass
        if handle not in (None, INVALID_HANDLE_VALUE):
            try:
                _CloseHandle(handle)
            except Exception:
                pass
        self._log("[WINHID] reader stopped; handles closed")
