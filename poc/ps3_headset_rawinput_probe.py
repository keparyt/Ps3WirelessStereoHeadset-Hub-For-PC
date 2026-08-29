#!/usr/bin/env python3
"""Windows Raw Input probe for Sony PS3 Wireless Stereo Headset.

RECEIVE ONLY. This probe registers for Windows Raw Input HID messages and
prints/logs reports received from VID 0x12BA / PID 0x0035.

It does NOT open the headset with write access and does NOT send HID output,
feature, control, or battery-polling requests.

Why this exists:
    The direct hidapi/ReadFile readers can successfully enumerate/open the
    four HID collections yet receive zero reports on some Windows setups.
    Raw Input is a separate Windows delivery path. This probe tells us whether
    Windows is receiving HID input messages at all.

Run from repository root:
    python poc/ps3_headset_rawinput_probe.py

Stop with Ctrl+C.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import time
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path

from ps3_headset_protocol import decode_b0, hex_bytes


if os.name != "nt":
    print("This probe requires Windows.")
    raise SystemExit(1)


user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WM_INPUT = 0x00FF
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
PM_REMOVE = 0x0001
RID_INPUT = 0x10000003
RIM_TYPEHID = 2
RIDEV_INPUTSINK = 0x00000100
RIDI_DEVICEINFO = 0x2000000B
GWLP_USERDATA = -21
ERROR_INSUFFICIENT_BUFFER = 122

VID = 0x12BA
PID = 0x0035


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", wintypes.USHORT),
        ("usUsage", wintypes.USHORT),
        ("dwFlags", wintypes.DWORD),
        ("hwndTarget", wintypes.HWND),
    ]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [
        ("dwType", wintypes.DWORD),
        ("dwSize", wintypes.DWORD),
        ("hDevice", wintypes.HANDLE),
        ("wParam", wintypes.WPARAM),
    ]


class RID_DEVICE_INFO_HID(ctypes.Structure):
    _fields_ = [
        ("dwVendorId", wintypes.DWORD),
        ("dwProductId", wintypes.DWORD),
        ("usUsagePage", wintypes.USHORT),
        ("usUsage", wintypes.USHORT),
    ]


class RID_DEVICE_INFO(ctypes.Structure):
    class _Union(ctypes.Union):
        _fields_ = [
            ("hid", RID_DEVICE_INFO_HID),
            ("raw", ctypes.c_ubyte * 32),
        ]

    _anonymous_ = ("u",)
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("dwType", wintypes.DWORD),
        ("u", _Union),
    ]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HCURSOR),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


user32.GetRawInputData.argtypes = [
    wintypes.HRAWINPUT,
    wintypes.UINT,
    wintypes.LPVOID,
    ctypes.POINTER(wintypes.UINT),
    wintypes.UINT,
]
user32.GetRawInputData.restype = wintypes.UINT

user32.GetRawInputDeviceInfoW.argtypes = [
    wintypes.HANDLE,
    wintypes.UINT,
    wintypes.LPVOID,
    ctypes.POINTER(wintypes.UINT),
]
user32.GetRawInputDeviceInfoW.restype = wintypes.UINT

user32.RegisterRawInputDevices.argtypes = [
    ctypes.POINTER(RAWINPUTDEVICE),
    wintypes.UINT,
    wintypes.UINT,
]
user32.RegisterRawInputDevices.restype = wintypes.BOOL

user32.CreateWindowExW.argtypes = [
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    wintypes.HMENU,
    wintypes.HINSTANCE,
    wintypes.LPVOID,
]
user32.CreateWindowExW.restype = wintypes.HWND

user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wintypes.ATOM

user32.DefWindowProcW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
user32.DefWindowProcW.restype = wintypes.LRESULT

user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.restype = wintypes.BOOL

user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.PostQuitMessage.restype = None

user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = wintypes.BOOL

user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.TranslateMessage.restype = wintypes.BOOL

user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = wintypes.LRESULT

kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE


WndProcType = ctypes.WINFUNCTYPE(
    wintypes.LRESULT,
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
)


class Probe:
    def __init__(self) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.directory = Path(__file__).resolve().parent / "logs" / f"rawinput_{stamp}"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.capture_path = self.directory / "rawinput.jsonl"
        self.summary_path = self.directory / "summary.json"
        self.packet_count = 0
        self.b0_count = 0
        self.bytes_received = 0
        self.by_usage: dict[str, int] = {}
        self.last_status: dict | None = None
        self.start_time = time.time()

        self.proc = WndProcType(self.window_proc)
        self.class_name = f"PS3HeadsetRawInput_{os.getpid()}_{int(time.time())}"
        self.hwnd: wintypes.HWND | None = None

    def log(self, record: dict) -> None:
        with self.capture_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def device_info(self, handle: wintypes.HANDLE) -> tuple[int, int, int, int]:
        info = RID_DEVICE_INFO()
        info.cbSize = ctypes.sizeof(RID_DEVICE_INFO)
        size = wintypes.UINT(ctypes.sizeof(RID_DEVICE_INFO))
        result = user32.GetRawInputDeviceInfoW(
            handle,
            RIDI_DEVICEINFO,
            ctypes.byref(info),
            ctypes.byref(size),
        )
        if result == 0xFFFFFFFF:
            error = ctypes.get_last_error()
            raise OSError(error, f"GetRawInputDeviceInfoW failed (WinError {error})")
        return (
            int(info.dwType),
            int(info.hid.dwVendorId),
            int(info.hid.dwProductId),
            int(info.hid.usUsagePage),
        )

    def matches_device(self, handle: wintypes.HANDLE) -> tuple[bool, tuple[int, int, int, int] | None]:
        try:
            info = self.device_info(handle)
        except Exception as exc:
            print(f"[DEVICE] unable to query device: {exc}")
            return False, None
        return info[0] == RIM_TYPEHID and info[1] == VID and info[2] == PID, info

    def extract_input_report(self, hrawinput: wintypes.HRAWINPUT) -> tuple[RAWINPUTHEADER, bytes]:
        size = wintypes.UINT(0)
        header_size = ctypes.sizeof(RAWINPUTHEADER)
        result = user32.GetRawInputData(
            hrawinput,
            RID_INPUT,
            None,
            ctypes.byref(size),
            header_size,
        )
        if result == 0xFFFFFFFF:
            error = ctypes.get_last_error()
            raise OSError(error, f"GetRawInputData(size) failed (WinError {error})")

        buffer = ctypes.create_string_buffer(size.value)
        result = user32.GetRawInputData(
            hrawinput,
            RID_INPUT,
            buffer,
            ctypes.byref(size),
            header_size,
        )
        if result == 0xFFFFFFFF:
            error = ctypes.get_last_error()
            raise OSError(error, f"GetRawInputData failed (WinError {error})")

        header = RAWINPUTHEADER.from_buffer_copy(buffer.raw[:header_size])
        if header.dwType != RIM_TYPEHID:
            return header, b""

        offset = header_size
        if len(buffer) < offset + 8:
            return header, b""

        size_hid = int.from_bytes(buffer.raw[offset:offset + 4], "little")
        count = int.from_bytes(buffer.raw[offset + 4:offset + 8], "little")
        data_start = offset + 8
        data_end = min(len(buffer), data_start + size_hid * count)
        return header, buffer.raw[data_start:data_end]

    def handle_raw_input(self, hrawinput: wintypes.HRAWINPUT) -> None:
        header, payload = self.extract_input_report(hrawinput)
        if header.dwType != RIM_TYPEHID or not payload:
            return

        matched, info = self.matches_device(header.hDevice)
        if not matched or info is None:
            return

        _dw_type, vendor, product, usage_page = info
        self.packet_count += 1
        self.bytes_received += len(payload)
        usage_key = f"0x{usage_page:04X}"
        self.by_usage[usage_key] = self.by_usage.get(usage_key, 0) + 1

        decoded = decode_b0(payload)
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "type": "raw_input_hid",
            "vendor_id": vendor,
            "product_id": product,
            "usage_page": usage_page,
            "payload_length": len(payload),
            "hex": hex_bytes(payload),
        }
        if decoded is not None:
            self.b0_count += 1
            self.last_status = decoded
            record["decoded_b0"] = decoded

        self.log(record)

        print(
            f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] "
            f"RAWINPUT PAGE=0x{usage_page:04X} LEN={len(payload):03d} "
            f"{hex_bytes(payload)}"
        )
        if decoded is not None:
            battery = "CHARGING" if decoded["charging"] else f"{decoded['battery_percent']}%" if decoded["battery_percent"] is not None else "UNKNOWN"
            volume = f"{decoded['volume_level']}/5" if decoded["volume_level"] is not None else f"raw={decoded['volume_raw']}"
            chat = f"{decoded['chat_balance']}%" if decoded["chat_balance"] is not None else f"raw={decoded['chat_balance_raw']}"
            print(
                "[STATUS] "
                f"LINK={'ON' if decoded['headset_connected'] else 'OFF'} | "
                f"VSS={'ON' if decoded['vss'] else 'OFF'} | "
                f"MIC={'MUTED' if decoded['mic_muted'] else 'ON'} | "
                f"BATTERY={battery} | VOLUME={volume} | CHAT={chat}"
            )

    def window_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_INPUT:
            try:
                self.handle_raw_input(wintypes.HRAWINPUT(lparam))
            except Exception as exc:
                self.log({
                    "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                    "type": "raw_input_error",
                    "error": str(exc),
                })
                print(f"[RAWINPUT] ERROR: {exc}")
            return 0

        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0

        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def register_raw_input(self) -> None:
        usages = [
            (0x000C, 0x0001, "Consumer Control"),
            (0xFF00, 0x0001, "Vendor 0xFF00"),
            (0xFF03, 0x0020, "Vendor 0xFF03"),
            (0xFF01, 0x0020, "Vendor 0xFF01"),
        ]

        devices = (RAWINPUTDEVICE * len(usages))()
        for i, (page, usage, _label) in enumerate(usages):
            devices[i].usUsagePage = page
            devices[i].usUsage = usage
            devices[i].dwFlags = RIDEV_INPUTSINK
            devices[i].hwndTarget = self.hwnd

        if not user32.RegisterRawInputDevices(
            devices,
            len(usages),
            ctypes.sizeof(RAWINPUTDEVICE),
        ):
            error = ctypes.get_last_error()
            raise OSError(error, f"RegisterRawInputDevices failed (WinError {error})")

    def create_window(self) -> None:
        hinstance = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = ctypes.cast(self.proc, ctypes.c_void_p)
        wc.hInstance = hinstance
        wc.lpszClassName = self.class_name

        if not user32.RegisterClassW(ctypes.byref(wc)):
            error = ctypes.get_last_error()
            # ERROR_CLASS_ALREADY_EXISTS is harmless for our unique class.
            if error != 1410:
                raise OSError(error, f"RegisterClassW failed (WinError {error})")

        self.hwnd = user32.CreateWindowExW(
            0,
            self.class_name,
            "PS3 Headset Raw Input Probe",
            0,
            0,
            0,
            0,
            0,
            wintypes.HWND(-3),  # HWND_MESSAGE
            None,
            hinstance,
            None,
        )
        if not self.hwnd:
            error = ctypes.get_last_error()
            raise OSError(error, f"CreateWindowExW failed (WinError {error})")

    def run(self) -> None:
        print("=" * 88)
        print("PS3 WIRELESS STEREO HEADSET - WINDOWS RAW INPUT PROBE")
        print("=" * 88)
        print("Mode: PASSIVE RECEIVE ONLY")
        print(f"Target VID/PID: {VID:04X}:{PID:04X}")
        print("Raw Input usages: 000C/0001, FF00/0001, FF03/0020, FF01/0020")
        print("NO HID OUTPUT | NO FEATURE WRITES | NO CONTROL REQUESTS")
        print()
        print(f"Capture directory: {self.directory}")
        print("Waiting for Windows RAWINPUT HID packets... Press Ctrl+C to stop.\n")

        self.create_window()
        self.register_raw_input()

        msg = wintypes.MSG()
        try:
            while True:
                result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result == 0:
                    break
                if result == -1:
                    error = ctypes.get_last_error()
                    raise OSError(error, f"GetMessageW failed (WinError {error})")
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except KeyboardInterrupt:
            pass
        finally:
            self.write_summary()
            if self.hwnd:
                user32.DestroyWindow(self.hwnd)

    def write_summary(self) -> None:
        elapsed = max(0.0, time.time() - self.start_time)
        self.summary_path.write_text(
            json.dumps(
                {
                    "mode": "PASSIVE_RECEIVE_ONLY",
                    "vendor_id": VID,
                    "product_id": PID,
                    "elapsed_seconds": elapsed,
                    "raw_input_packets": self.packet_count,
                    "b0_status_packets": self.b0_count,
                    "bytes_received": self.bytes_received,
                    "packets_by_usage_page": self.by_usage,
                    "last_decoded_b0": self.last_status,
                    "outgoing_hid_reports": False,
                    "outgoing_feature_reports": False,
                    "outgoing_control_requests": False,
                    "battery_polling_command": False,
                },
                indent=2,
            ),
            encoding="utf-8",
        )


def main() -> int:
    Probe().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
