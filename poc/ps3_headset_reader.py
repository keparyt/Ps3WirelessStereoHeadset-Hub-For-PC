#!/usr/bin/env python3
"""Windows receive-only HID reader for Sony 12BA:0035.

Windows equivalent of counter185/hid-playstation-headset's HID raw_event:
wait for USB HID interrupt-IN reports and pass the bytes to the protocol layer.
No output reports, feature reports, control transfers, or polling commands.
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
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
ULONG_PTR = ctypes.c_size_t

REFERENCE_REPORT_ID = 0xB0
REFERENCE_REPORT_SIZE = 8

class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ULONG_PTR), ("InternalHigh", ULONG_PTR),
                ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE)]

class HIDP_CAPS(ctypes.Structure):
    _fields_ = [
        ("Usage", wintypes.USHORT), ("UsagePage", wintypes.USHORT),
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
    _CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                             wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                             wintypes.HANDLE]
    _CreateFileW.restype = wintypes.HANDLE
    _ReadFile = _kernel32.ReadFile
    _ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                          ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(OVERLAPPED)]
    _ReadFile.restype = wintypes.BOOL
    _GetOverlappedResult = _kernel32.GetOverlappedResult
    _GetOverlappedResult.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED),
                                     ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]
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

def windows_path(path: object) -> str:
    return path.decode("utf-8", errors="replace") if isinstance(path, bytes) else str(path)

def _winerr() -> int:
    return ctypes.get_last_error()

def _hv(handle: object) -> int:
    try:
        return int(handle)
    except (TypeError, ValueError):
        return 0

class NativeWindowsHIDReader:
    WAIT_SLICE_MS = 100
    INPUT_BUFFER_COUNT = 64

    def __init__(self, path: object, on_report: Callable[[bytes], None],
                 on_error: Callable[[Exception], None] | None = None,
                 on_log: Callable[[str], None] | None = None) -> None:
        if not native_windows_available():
            raise OSError("Native Windows HID backend is only available on Windows")
        self.path = windows_path(path)
        self.on_report, self.on_error, self.on_log = on_report, on_error, on_log
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.handle = self.event = None
        self.input_report_length = self.output_report_length = self.feature_report_length = 0
        self.usage = self.usage_page = 0
        self._lock = threading.Lock()
        self._stopped = False
        self.report_count = 0

    def _log(self, message: str) -> None:
        if self.on_log:
            try: self.on_log(message)
            except Exception: pass

    def _get_caps(self, handle) -> HIDP_CAPS:
        preparsed = ctypes.c_void_p()
        if not _HidD_GetPreparsedData(handle, ctypes.byref(preparsed)):
            e = _winerr(); raise OSError(e, f"HidD_GetPreparsedData failed (WinError {e})")
        try:
            caps = HIDP_CAPS()
            status = _HidP_GetCaps(preparsed, ctypes.byref(caps))
            if status != 0x00110000:
                raise OSError(status, f"HidP_GetCaps failed (NTSTATUS 0x{status & 0xFFFFFFFF:08X})")
            return caps
        finally:
            _HidD_FreePreparsedData(preparsed)

    def open(self) -> None:
        self._log(f"[WINHID] opening collection: {self.path}")
        handle = _CreateFileW(self.path, GENERIC_READ,
                              FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                              OPEN_EXISTING,
                              FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OVERLAPPED, None)
        if handle in (None, INVALID_HANDLE_VALUE):
            e = _winerr(); hint = " (access denied)" if e == ERROR_ACCESS_DENIED else ""
            raise OSError(e, f"CreateFileW failed (WinError {e}){hint}")
        self._log(f"[WINHID] CreateFileW SUCCESS handle=0x{_hv(handle):X}")
        try:
            if not _HidD_SetNumInputBuffers(handle, self.INPUT_BUFFER_COUNT):
                e = _winerr(); self._log(f"[WINHID][WARN] HidD_SetNumInputBuffers failed (WinError {e}); continuing")
            caps = self._get_caps(handle)
            if caps.InputReportByteLength <= 0:
                raise OSError("HID collection reports InputReportByteLength=0")
            self.input_report_length = int(caps.InputReportByteLength)
            self.output_report_length = int(caps.OutputReportByteLength)
            self.feature_report_length = int(caps.FeatureReportByteLength)
            self.usage, self.usage_page = int(caps.Usage), int(caps.UsagePage)
            self._log(f"[WINHID][CAPS] usage_page=0x{self.usage_page:04X} usage=0x{self.usage:04X} input={self.input_report_length} output={self.output_report_length} feature={self.feature_report_length}")
            if self.input_report_length == REFERENCE_REPORT_SIZE:
                self._log("[WINHID][REFERENCE] input size=8, matching counter185 12BA:0035")
        except Exception:
            _CloseHandle(handle); raise
        event = _CreateEventW(None, True, False, None)
        if not event:
            e = _winerr(); _CloseHandle(handle); raise OSError(e, f"CreateEventW failed (WinError {e})")
        self._log(f"[WINHID] CreateEventW SUCCESS event=0x{_hv(event):X}")
        with self._lock:
            self.handle, self.event, self._stopped = handle, event, False

    def start(self) -> None:
        self.stop_event.clear()
        self.open()
        self.thread = threading.Thread(target=self._worker, name="ps3-headset-hid-reader", daemon=True)
        self.thread.start()
        self._log(f"[WINHID] reader thread STARTED; waiting for HID interrupt-IN (buffer={self.input_report_length}, reference=8)")

    def _worker(self) -> None:
        with self._lock: handle, event = self.handle, self.event
        if handle is None or event is None:
            self._log("[WINHID][FATAL] invalid handle/event"); return
        try:
            while not self.stop_event.is_set():
                _ResetEvent(event)
                overlapped = OVERLAPPED(); overlapped.hEvent = event
                buffer = ctypes.create_string_buffer(self.input_report_length)
                received = wintypes.DWORD(0)
                ok = _ReadFile(handle, buffer, self.input_report_length,
                               ctypes.byref(received), ctypes.byref(overlapped))
                if ok:
                    n = int(received.value)
                    if n: self._deliver(bytes(buffer.raw[:n]))
                    continue
                e = _winerr()
                if e != ERROR_IO_PENDING:
                    if e in {ERROR_OPERATION_ABORTED, ERROR_DEVICE_NOT_CONNECTED, ERROR_INVALID_HANDLE} and self.stop_event.is_set(): return
                    self._handle_error(OSError(e, f"ReadFile failed (WinError {e})")); return
                while not self.stop_event.is_set():
                    result = _WaitForSingleObject(event, self.WAIT_SLICE_MS)
                    if result == WAIT_OBJECT_0: break
                    if result != WAIT_TIMEOUT:
                        e = _winerr(); self._handle_error(OSError(e, f"WaitForSingleObject failed (WinError {e})")); return
                if self.stop_event.is_set():
                    _CancelIoEx(handle, ctypes.byref(overlapped)); return
                received = wintypes.DWORD(0)
                if not _GetOverlappedResult(handle, ctypes.byref(overlapped), ctypes.byref(received), False):
                    e = _winerr()
                    if e == ERROR_OPERATION_IN_PROGRESS: continue
                    if e in {ERROR_OPERATION_ABORTED, ERROR_DEVICE_NOT_CONNECTED, ERROR_INVALID_HANDLE}:
                        self._handle_error(OSError(e, f"HID read stopped (WinError {e})")); return
                    self._handle_error(OSError(e, f"GetOverlappedResult failed (WinError {e})")); return
                n = int(received.value)
                if n: self._deliver(bytes(buffer.raw[:n]))
        except Exception as exc:
            self._handle_error(exc)

    def _deliver(self, report: bytes) -> None:
        self.report_count += 1
        raw = " ".join(f"{b:02X}" for b in report)
        self._log(f"[WINHID][REPORT] #{self.report_count} bytes={len(report)} raw=[{raw}]")
        if len(report) == REFERENCE_REPORT_SIZE and report[0] == REFERENCE_REPORT_ID:
            self._log("[WINHID][REFERENCE B0] received documented 8-byte 12BA:0035 status report")
        try: self.on_report(report)
        except Exception as exc: self._log(f"[WINHID][CALLBACK-ERROR] {type(exc).__name__}: {exc}")

    def _handle_error(self, exc: Exception) -> None:
        self._log(f"[WINHID][ERROR] {type(exc).__name__}: {exc}")
        if self.on_error and not self.stop_event.is_set():
            try: self.on_error(exc)
            except Exception: pass

    def stop(self) -> None:
        if self.stop_event.is_set() and self._stopped: return
        self._log("[WINHID] stopping reader")
        self.stop_event.set()
        with self._lock:
            handle, event = self.handle, self.event
            self.handle = self.event = None
            self._stopped = True
        if handle not in (None, INVALID_HANDLE_VALUE):
            try: _CancelIoEx(handle, None)
            except Exception: pass
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=1.5)
        if event not in (None, INVALID_HANDLE_VALUE):
            try: _CloseHandle(event)
            except Exception: pass
        if handle not in (None, INVALID_HANDLE_VALUE):
            try: _CloseHandle(handle)
            except Exception: pass
        self._log("[WINHID] reader stopped; handles closed")
