#!/usr/bin/env python3
"""Receive-only readers for the Sony PS3 Wireless Stereo Headset receiver.

Windows users can use the native Windows HID reader in this module when the
hidapi backend opens the collections successfully but receives no reports.
The native backend uses CreateFileW + ReadFile on the HID collection path and
requests GENERIC_READ only. It never sends HID output/feature reports or
application control commands.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
from ctypes import wintypes
from typing import Callable


if os.name == "nt":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
else:  # pragma: no cover - Windows-only backend
    _kernel32 = None


GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x00000080
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
ERROR_DEVICE_NOT_CONNECTED = 1167
ERROR_OPERATION_ABORTED = 995
ERROR_INVALID_HANDLE = 6


if _kernel32 is not None:
    _CreateFileW = _kernel32.CreateFileW
    _CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _CreateFileW.restype = wintypes.HANDLE

    _ReadFile = _kernel32.ReadFile
    _ReadFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    _ReadFile.restype = wintypes.BOOL

    _CloseHandle = _kernel32.CloseHandle
    _CloseHandle.argtypes = [wintypes.HANDLE]
    _CloseHandle.restype = wintypes.BOOL


def windows_path(path: object) -> str:
    if isinstance(path, bytes):
        return path.decode("utf-8", errors="replace")
    return str(path)


def native_windows_available() -> bool:
    return os.name == "nt" and _kernel32 is not None


def read_file_once(handle: wintypes.HANDLE, size: int = 4096) -> tuple[bytes | None, int]:
    """Blocking read of one HID input report. No write/control transfer occurs."""
    buffer = ctypes.create_string_buffer(size)
    received = wintypes.DWORD(0)

    ok = _ReadFile(
        handle,
        buffer,
        size,
        ctypes.byref(received),
        None,
    )
    if not ok:
        return None, ctypes.get_last_error()

    return buffer.raw[: received.value], 0


class NativeWindowsHIDReader:
    """One-way reader for a Windows HID collection path."""

    def __init__(
        self,
        path: object,
        on_report: Callable[[bytes], None],
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        if not native_windows_available():
            raise OSError("Native Windows HID backend is only available on Windows.")

        self.path = windows_path(path)
        self.on_report = on_report
        self.on_error = on_error
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.handle: wintypes.HANDLE | None = None

    def open(self) -> None:
        handle = _CreateFileW(
            self.path,
            GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL,
            None,
        )

        if handle in (None, INVALID_HANDLE_VALUE):
            error = ctypes.get_last_error()
            raise OSError(error, f"CreateFileW failed for HID path (WinError {error})")

        self.handle = handle

    def start(self) -> None:
        self.open()
        self.thread = threading.Thread(
            target=self._worker,
            name="native-hid-reader",
            daemon=True,
        )
        self.thread.start()

    def _worker(self) -> None:
        assert self.handle is not None

        try:
            while not self.stop_event.is_set():
                report, error = read_file_once(self.handle)

                if report is not None:
                    if report:
                        self.on_report(report)
                    continue

                if self.stop_event.is_set():
                    return

                if error in {
                    ERROR_DEVICE_NOT_CONNECTED,
                    ERROR_OPERATION_ABORTED,
                    ERROR_INVALID_HANDLE,
                }:
                    self._handle_error(
                        OSError(error, f"HID read stopped (WinError {error})")
                    )
                    return

                self._handle_error(OSError(error, f"HID ReadFile failed (WinError {error})"))
                return

        except Exception as exc:  # pragma: no cover - defensive runtime path
            self._handle_error(exc)

    def _handle_error(self, exc: Exception) -> None:
        if self.on_error is not None:
            self.on_error(exc)

    def stop(self) -> None:
        self.stop_event.set()
        handle = self.handle
        self.handle = None

        if handle not in (None, INVALID_HANDLE_VALUE):
            try:
                _CloseHandle(handle)
            except Exception:
                pass

        thread = self.thread
        if thread is not None and thread is not threading.current_thread():
            # ReadFile is allowed to be blocking; the process is not held open by
            # daemon reader threads. A short join is useful when CloseHandle wakes it.
            thread.join(timeout=0.5)

    def __enter__(self) -> "NativeWindowsHIDReader":
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.stop()
