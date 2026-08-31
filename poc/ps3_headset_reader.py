#!/usr/bin/env python3
"""Robust receive-only Windows HID reader."""
from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes
from typing import Callable

if os.name == "nt":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
else:
    _kernel32 = None

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

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
# ULONG_PTR is pointer-sized. wintypes does not define it on all Python builds.
ULONG_PTR = ctypes.c_size_t

class OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ULONG_PTR),
        ("InternalHigh", ULONG_PTR),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
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

    _WaitForSingleObject = _kernel32.WaitForSingleObject
    _WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _WaitForSingleObject.restype = wintypes.DWORD

    _CancelIoEx = _kernel32.CancelIoEx
    _CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED)]
    _CancelIoEx.restype = wintypes.BOOL

    _CloseHandle = _kernel32.CloseHandle
    _CloseHandle.argtypes = [wintypes.HANDLE]
    _CloseHandle.restype = wintypes.BOOL


def windows_path(path: object) -> str:
    return path.decode("utf-8", errors="replace") if isinstance(path, bytes) else str(path)


def native_windows_available() -> bool:
    return os.name == "nt" and _kernel32 is not None


class NativeWindowsHIDReader:
    """Receive-only reader for one Windows HID collection."""
    def __init__(self, path: object, on_report: Callable[[bytes], None], on_error: Callable[[Exception], None] | None = None) -> None:
        if not native_windows_available():
            raise OSError("Native Windows HID backend is only available on Windows.")
        self.path = windows_path(path)
        self.on_report = on_report
        self.on_error = on_error
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.handle: wintypes.HANDLE | None = None
        self.event: wintypes.HANDLE | None = None
        self._lock = threading.Lock()

    def open(self) -> None:
        handle = _CreateFileW(self.path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OVERLAPPED, None)
        if handle in (None, INVALID_HANDLE_VALUE):
            error = ctypes.get_last_error()
            raise OSError(error, f"CreateFileW failed for HID path (WinError {error})")
        event = _CreateEventW(None, True, False, None)
        if not event:
            error = ctypes.get_last_error()
            _CloseHandle(handle)
            raise OSError(error, f"CreateEventW failed (WinError {error})")
        with self._lock:
            self.handle = handle
            self.event = event

    def start(self) -> None:
        self.open()
        self.thread = threading.Thread(target=self._worker, name="native-hid-reader", daemon=True)
        self.thread.start()

    def _worker(self) -> None:
        with self._lock:
            handle, event = self.handle, self.event
        if handle is None or event is None:
            return
        try:
            while not self.stop_event.is_set():
                overlapped = OVERLAPPED()
                overlapped.hEvent = event
                buffer = ctypes.create_string_buffer(4096)
                received = wintypes.DWORD(0)
                ok = _ReadFile(handle, buffer, ctypes.sizeof(buffer), ctypes.byref(received), ctypes.byref(overlapped))
                if not ok:
                    error = ctypes.get_last_error()
                    if error != ERROR_IO_PENDING:
                        if self.stop_event.is_set() and error in (ERROR_OPERATION_ABORTED, ERROR_INVALID_HANDLE):
                            return
                        self._handle_error(OSError(error, f"HID ReadFile failed (WinError {error})"))
                        return
                while not self.stop_event.is_set():
                    result = _WaitForSingleObject(event, 100)
                    if result == WAIT_OBJECT_0:
                        break
                    if result != WAIT_TIMEOUT:
                        error = ctypes.get_last_error()
                        self._handle_error(OSError(error, f"WaitForSingleObject failed (WinError {error})"))
                        return
                if self.stop_event.is_set():
                    _CancelIoEx(handle, ctypes.byref(overlapped))
                    return
                received = wintypes.DWORD(0)
                if not _GetOverlappedResult(handle, ctypes.byref(overlapped), ctypes.byref(received), False):
                    error = ctypes.get_last_error()
                    if error in (ERROR_OPERATION_ABORTED, ERROR_DEVICE_NOT_CONNECTED, ERROR_INVALID_HANDLE):
                        self._handle_error(OSError(error, f"HID read stopped (WinError {error})"))
                        return
                    self._handle_error(OSError(error, f"GetOverlappedResult failed (WinError {error})"))
                    return
                if received.value:
                    self.on_report(buffer.raw[:received.value])
        except Exception as exc:
            self._handle_error(exc)

    def _handle_error(self, exc: Exception) -> None:
        if self.on_error is not None and not self.stop_event.is_set():
            self.on_error(exc)

    def stop(self) -> None:
        self.stop_event.set()
        with self._lock:
            handle, event = self.handle, self.event
            self.handle = None
            self.event = None
        if handle not in (None, INVALID_HANDLE_VALUE):
            try:
                _CancelIoEx(handle, None)
            except Exception:
                pass
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=1.0)
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

    def __enter__(self) -> "NativeWindowsHIDReader":
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.stop()
