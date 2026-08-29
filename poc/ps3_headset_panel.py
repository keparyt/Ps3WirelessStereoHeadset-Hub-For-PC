#!/usr/bin/env python3
"""Live receive-only dashboard for the Sony PS3 Wireless Stereo Headset.

This is an OBSERVABILITY tool only.

Direction:
    headset -> USB receiver -> Windows HID input -> this application

The application never sends HID output reports, feature reports, control
commands, or battery polling requests. On Windows it reads the HID collection
with GENERIC_READ + ReadFile so we can observe reports independently of the
previous hidapi polling backend.
"""

from __future__ import annotations

import threading
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import ttk
from typing import Any

import hid

from ps3_headset_protocol import decode_b0
from ps3_headset_reader import NativeWindowsHIDReader, native_windows_available


VID = 0x12BA
PID = 0x0035
STALE_SECONDS = 5.0
SCAN_MS = 1000
UI_MS = 100


@dataclass
class State:
    dongle_present: bool = False
    headset_connected: bool | None = None
    vss: bool | None = None
    mic_muted: bool | None = None
    battery: int | None = None
    charging: bool = False
    volume: int | None = None
    chat_balance: int | None = None
    model: str = "Unknown"
    b0_count: int = 0
    total_reports: int = 0
    total_bytes: int = 0
    last_report_age: float | None = None
    last_report_hex: str = ""
    last_report_collection: str = ""
    last_error: str = ""
    last_update: float | None = None


class ReceiverMonitor:
    def __init__(self) -> None:
        self.state = State()
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.readers: dict[str, NativeWindowsHIDReader] = {}
        self.reader_threads: list[threading.Thread] = []
        self.collection_names: dict[str, str] = {}

    @staticmethod
    def path_key(path: object) -> str:
        if isinstance(path, bytes):
            return path.decode("utf-8", errors="replace")
        return str(path)

    def discover(self) -> list[dict[str, Any]]:
        return hid.enumerate(VID, PID)

    def reader_error(self, key: str, exc: Exception) -> None:
        with self.lock:
            self.state.last_error = f"{key}: {exc}"
            self.readers.pop(key, None)

    def reader_report(self, key: str, info: dict[str, Any], report: bytes) -> None:
        page = int(info.get("usage_page") or 0)
        usage = int(info.get("usage") or 0)
        collection = f"0x{page:04X}/0x{usage:04X}"

        with self.lock:
            self.state.total_reports += 1
            self.state.total_bytes += len(report)
            self.state.last_report_age = 0.0
            self.state.last_report_hex = " ".join(f"{b:02X}" for b in report)
            self.state.last_report_collection = collection
            self.state.last_update = time.monotonic()

            decoded = decode_b0(report)
            if decoded is None:
                return

            self.state.b0_count += 1
            self.state.headset_connected = decoded["headset_connected"]
            self.state.vss = decoded["vss"]
            self.state.mic_muted = decoded["mic_muted"]
            self.state.battery = decoded["battery_percent"]
            self.state.charging = decoded["charging"]
            self.state.volume = decoded["volume_level"]
            self.state.chat_balance = decoded["chat_balance"]
            self.state.model = decoded["model"]

    def refresh(self) -> None:
        devices = self.discover()
        current_paths = set()

        with self.lock:
            self.state.dongle_present = bool(devices)

        for index, info in enumerate(devices):
            path = info.get("path")
            if not path:
                continue
            key = self.path_key(path)
            current_paths.add(key)
            self.collection_names[key] = (
                f"C{index:02d} / page 0x{int(info.get('usage_page') or 0):04X}"
            )

            if key in self.readers:
                continue

            if not native_windows_available():
                with self.lock:
                    self.state.last_error = "Native Windows HID backend unavailable"
                continue

            reader = NativeWindowsHIDReader(
                path,
                on_report=lambda report, k=key, d=info: self.reader_report(k, d, report),
                on_error=lambda exc, k=key: self.reader_error(k, exc),
            )
            try:
                reader.start()
                self.readers[key] = reader
            except Exception as exc:
                self.reader_error(key, exc)

        # Remove reader objects whose HID path has disappeared.
        stale = [key for key in self.readers if key not in current_paths]
        for key in stale:
            reader = self.readers.pop(key, None)
            if reader is not None:
                reader.stop()

        if not devices:
            with self.lock:
                self.state.headset_connected = False

    def start(self) -> None:
        self.refresh()

        def loop() -> None:
            while not self.stop_event.wait(SCAN_MS / 1000.0):
                try:
                    self.refresh()
                except Exception as exc:
                    with self.lock:
                        self.state.last_error = str(exc)

        thread = threading.Thread(target=loop, name="receiver-scanner", daemon=True)
        thread.start()
        self.reader_threads.append(thread)

    def stop(self) -> None:
        self.stop_event.set()
        for reader in list(self.readers.values()):
            reader.stop()
        self.readers.clear()


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("PS3 Wireless Stereo Headset — Receive Monitor")
        self.geometry("820x680")
        self.minsize(720, 590)
        self.configure(padx=18, pady=18)

        self.monitor = ReceiverMonitor()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.build()
        self.monitor.start()
        self.after(UI_MS, self.update_ui)

    def build(self) -> None:
        ttk.Label(
            self,
            text="PS3 Wireless Stereo Headset",
            font=("Segoe UI", 21, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            self,
            text="Passive information monitor • INPUT ONLY • no controls are sent",
            font=("Segoe UI", 10),
        ).pack(anchor="w", pady=(2, 16))

        cards = ttk.Frame(self)
        cards.pack(fill="x")
        for col in range(2):
            cards.columnconfigure(col, weight=1)

        self.cards: dict[str, tuple[ttk.Label, ttk.Label]] = {}
        definitions = [
            ("USB Dongle", "dongle"),
            ("Headset Link", "link"),
            ("VSS", "vss"),
            ("Microphone", "mic"),
            ("Battery", "battery"),
            ("Headset Volume", "volume"),
            ("Chat Balance", "chat"),
            ("Headset Power", "power"),
        ]

        for i, (title, key) in enumerate(definitions):
            frame = ttk.LabelFrame(cards, text=title, padding=12)
            frame.grid(row=i // 2, column=i % 2, sticky="nsew", padx=5, pady=5)
            value = ttk.Label(frame, text="UNKNOWN", font=("Segoe UI", 17, "bold"))
            value.pack(anchor="w")
            detail = ttk.Label(frame, text="Waiting for incoming telemetry…")
            detail.pack(anchor="w", pady=(4, 0))
            self.cards[key] = (value, detail)

        info = ttk.LabelFrame(self, text="Incoming telemetry", padding=12)
        info.pack(fill="both", expand=True, pady=(12, 0))

        self.vars = {
            "model": tk.StringVar(value="Model: Unknown"),
            "b0": tk.StringVar(value="B0 status packets: 0"),
            "reports": tk.StringVar(value="All HID input reports: 0"),
            "bytes": tk.StringVar(value="Bytes received: 0"),
            "age": tk.StringVar(value="Last report: —"),
            "collection": tk.StringVar(value="Last collection: —"),
            "raw": tk.StringVar(value="Last raw report: —"),
            "error": tk.StringVar(value=""),
        }

        for key in ("model", "b0", "reports", "bytes", "age", "collection", "raw"):
            ttk.Label(info, textvariable=self.vars[key], wraplength=740).pack(anchor="w", pady=3)

        ttk.Label(
            info,
            text=(
                "Known B0 telemetry: byte 1=volume level (0–5), byte 2=chat balance "
                "(0–100), byte 3=battery (0–100; 0x80=charging), byte 4=VSS/mic/link flags."
            ),
            wraplength=740,
        ).pack(anchor="w", pady=(9, 3))

        ttk.Label(
            info,
            text=(
                "Unknown reports are still counted and displayed. Nothing is sent to the receiver."
            ),
            wraplength=740,
        ).pack(anchor="w", pady=3)

        ttk.Label(info, textvariable=self.vars["error"], wraplength=740).pack(anchor="w", pady=(9, 0))

    def set_card(self, key: str, value: str, detail: str) -> None:
        value_label, detail_label = self.cards[key]
        value_label.config(text=value)
        detail_label.config(text=detail)

    def update_ui(self) -> None:
        with self.monitor.lock:
            s = State(**vars(self.monitor.state))

        age = None
        if s.last_update is not None:
            age = max(0.0, time.monotonic() - s.last_update)
        stale = age is None or age > STALE_SECONDS

        if s.dongle_present:
            self.set_card("dongle", "ON", "USB receiver detected")
        else:
            self.set_card("dongle", "OFF", "USB receiver not detected")

        if not s.dongle_present:
            for key, detail in {
                "link": "No receiver",
                "vss": "No incoming status",
                "mic": "No incoming status",
                "battery": "No incoming telemetry",
                "volume": "No incoming telemetry",
                "chat": "No incoming telemetry",
                "power": "Cannot determine",
            }.items():
                self.set_card(key, "UNKNOWN" if key != "link" and key != "power" else "OFF", detail)
        elif stale:
            self.set_card("link", "UNKNOWN", "No recent HID input")
            self.set_card("power", "UNKNOWN", "No recent status packet")
            self.set_card("vss", "UNKNOWN", "No recent B0 packet")
            self.set_card("mic", "UNKNOWN", "No recent B0 packet")
            self.set_card(
                "battery",
                "CHARGING" if s.charging else (f"{s.battery}%" if s.battery is not None else "UNKNOWN"),
                "Last known value; status is stale",
            )
            self.set_card("volume", f"{s.volume}/5" if s.volume is not None else "UNKNOWN", "Last known B0 value")
            self.set_card("chat", f"{s.chat_balance}%" if s.chat_balance is not None else "UNKNOWN", "Last known B0 value")
        else:
            connected = bool(s.headset_connected)
            self.set_card("link", "ON" if connected else "OFF", "Incoming receiver link flag")
            self.set_card("power", "ON" if connected else "OFF", "Derived from incoming link flag")
            self.set_card("vss", "ON" if s.vss else "OFF", "Incoming VSS flag")
            self.set_card("mic", "MUTED" if s.mic_muted else "ON", "Incoming microphone flag")
            self.set_card("battery", "CHARGING" if s.charging else f"{s.battery}%" if s.battery is not None else "UNKNOWN", "Incoming B0 telemetry")
            self.set_card("volume", f"{s.volume}/5" if s.volume is not None else "UNKNOWN", "Incoming B0 telemetry")
            self.set_card("chat", f"{s.chat_balance}%" if s.chat_balance is not None else "UNKNOWN", "Incoming B0 telemetry")

        self.vars["model"].set(f"Model: {s.model}")
        self.vars["b0"].set(f"B0 status packets received: {s.b0_count}")
        self.vars["reports"].set(f"All HID input reports received: {s.total_reports}")
        self.vars["bytes"].set(f"Bytes received: {s.total_bytes}")
        self.vars["age"].set(f"Last HID report: {f'{age:.2f}s ago' if age is not None else '—'}")
        self.vars["collection"].set(f"Last collection: {s.last_report_collection or '—'}")
        self.vars["raw"].set(f"Last raw report: {s.last_report_hex or '—'}")
        self.vars["error"].set(f"Reader error: {s.last_error}" if s.last_error else "")

        self.after(UI_MS, self.update_ui)

    def close(self) -> None:
        self.monitor.stop()
        self.destroy()


def main() -> int:
    if not native_windows_available():
        print("This dashboard currently requires Windows native HID reading.")
        return 1

    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
