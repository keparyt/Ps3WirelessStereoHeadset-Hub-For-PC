#!/usr/bin/env python3
"""Robust receive-only Windows HID reader.

The PS3 Wireless Stereo Headset receiver exposes several HID collections. The
Windows HID class driver expects applications to keep an input read pending;
this module uses overlapped ReadFile so the pending read can be cancelled
cleanly when the device disappears or the application exits.

No HID output reports, feature reports, control transfers, or headset commands
are sent by this module.
"""

from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes
from typing import Callable


if os.name == "nt":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
else:  # pragma: no cover
    _kernel32 = None


GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x00000080
FILE_FLAG_OVERLAPPED = 0x40000000

WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
INFINITE = 0xFFFFFFFF
ERROR_IO_PENDING = 997
ERROR_OPERATION_ABORTED = 995
ERROR_DEVICE_NOT_CONNECTED = 1167
ERROR_INVALID_HANDLE = 6
ERROR_FILE_NOT_FOUND = 2
ERROR_ACCESS_DENIED = 5

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", wintypes.ULONG_PTR),
        ("InternalHigh", wintypes.ULONG_PTR),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
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

    _WaitForSingleObject = _kernel32.WaitForSingleObject
    _WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _WaitForSingleObject.restype = wintypes.DWORD

    _CancelIoEx = getattr(_kernel32, "CancelIoEx", None)
    if _CancelIoEx is not None:
        _CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED)]
        _CancelIoEx.restype = wintypes.BOOL

    _CloseHandle = _kernel32.CloseHandle
    _CloseHandle.argtypes = [wintypes.HANDLE]
    _CloseHandle.restype = wintypes.BOOL


def windows_path(path: object) -> str:
    if isinstance(path, bytes):
        return path.decode("utf-8", errors="replace")
    return str(path)


def native_windows_available() -> bool:
    return os.name == "nt" and _kernel32 is not None


def _winerror(prefix: str, error: int) -> OSError:
    return OSError(error, f"{prefix} (WinError {error})")


class NativeWindowsHIDReader:
    """One-way reader for one Windows HID collection path."""

    def __init__(
        self,
        path: object,
        on_report: Callable[[bytes], None],
        on_error: Callable[[Exception], None] | None = None,
        report_buffer_size: int = 4096,
    ) -> None:
        if not native_windows_available():
            raise OSError("Native Windows HID backend is only available on Windows.")
        self.path = windows_path(path)
        self.on_report = on_report
        self.on_error = on_error
        self.report_buffer_size = max(64, int(report_buffer_size))
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.handle: wintypes.HANDLE | None = None
        self.event: wintypes.HANDLE | None = None
        self._lock = threading.Lock()

    def open(self) -> None:
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
            error = ctypes.get_last_error()
            raise _winerror("CreateFileW failed for HID collection", error)

        event = _CreateEventW(None, True, False, None)
        if event in (None, INVALID_HANDLE_VALUE):
            error = ctypes.get_last_error()
            _CloseHandle(handle)
            raise _winerror("CreateEventW failed", error)

        with self._lock:
            self.handle = handle
            self.event = event

    def start(self) -> None:
        self.stop_event.clear()
        self.open()
        self.thread = threading.Thread(
            target=self._worker,
            name="native-hid-reader",
            daemon=True,
        )
        self.thread.start()

    def _read_once(self) -> tuple[bytes | None, int]:
        with self._lock:
            handle = self.handle
            event = self.event
        if handle in (None, INVALID_HANDLE_VALUE) or event in (None, INVALID_HANDLE_VALUE):
            return None, ERROR_INVALID_HANDLE

        overlapped = OVERLAPPED()
        overlapped.hEvent = event
        ctypes.memset(ctypes.byref(overlapped), 0, ctypes.sizeof(overlapped))
        overlapped.hEvent = event
        _WaitForSingleObject(event, 0)  # consume a previous signalled state

        buffer = ctypes.create_string_buffer(self.report_buffer_size)
        received = wintypes.DWORD(0)

        ok = _ReadFile(
            handle,
            buffer,
            self.report_buffer_size,
            ctypes.byref(received),
            ctypes.byref(overlapped),
        )

        if ok:
            return buffer.raw[: received.value], 0

        error = ctypes.get_last_error()
        if error != ERROR_IO_PENDING:
            return None, error

        while not self.stop_event.is_set():
            result = _WaitForSingleObject(event, 100)
            if result == WAIT_TIMEOUT:
                continue
            if result != WAIT_OBJECT_0:
                return None, ctypes.get_last_error() or ERROR_OPERATION_ABORTED

            received = wintypes.DWORD(0)
            if _GetOverlappedResult(handle, ctypes.byref(overlapped), ctypes.byref(received), False):
                return buffer.raw[: received.value], 0
            return None, ctypes.get_last_error()

        # Stop requested: cancel this exact pending read.
        if _CancelIoEx is not None:
            _CancelIoEx(handle, ctypes.byref(overlapped))
        return None, ERROR_OPERATION_ABORTED

    def _worker(self) -> None:
        try:
            while not self.stop_event.is_set():
                report, error = self._read_once()
                if report is not None:
                    if report:
                        try:
                            self.on_report(report)
                        except Exception as exc:
                            self._handle_error(exc)
                    continue

                if self.stop_event.is_set() or error == ERROR_OPERATION_ABORTED:
                    return

                if error in {
                    ERROR_DEVICE_NOT_CONNECTED,
                    ERROR_INVALID_HANDLE,
                    ERROR_FILE_NOT_FOUND,
                    ERROR_ACCESS_DENIED,
                }:
                    self._handle_error(_winerror("HID read stopped", error))
                    return

                self._handle_error(_winerror("HID ReadFile failed", error))
                return
        except Exception as exc:  # defensive runtime boundary
            self._handle_error(exc)
        finally:
            self._close_handles()

    def _handle_error(self, exc: Exception) -> None:
        if not self.stop_event.is_set() and self.on_error is not None:
            self.on_error(exc)

    def _close_handles(self) -> None:
        with self._lock:
            event = self.event
            handle = self.handle
            self.event = None
            self.handle = None
        if event not in (None, INVALID_HANDLE_VALUE):
            _CloseHandle(event)
        if handle not in (None, INVALID_HANDLE_VALUE):
            _CloseHandle(handle)

    def stop(self) -> None:
        self.stop_event.set()
        with self._lock:
            handle = self.handle
            event = self.event

        if handle not in (None, INVALID_HANDLE_VALUE) and _CancelIoEx is not None:
            try:
                _CancelIoEx(handle, None)
            except Exception:
                pass

        if event not in (None, INVALID_HANDLE_VALUE):
            # Wake the 100 ms wait immediately when possible.
            try:
                _kernel32.SetEvent(event)
            except Exception:
                pass

        thread = self.thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

        self._close_handles()
        self.thread = None

    def __enter__(self) -> "NativeWindowsHIDReader":
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.stop()
