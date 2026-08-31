#!/usr/bin/env python3
"""Live PS3 Wireless Stereo Headset monitor with detailed diagnostics.

The GUI is intentionally backed by a verbose console diagnostic stream. This
makes it possible to distinguish:
  1. USB receiver discovery
  2. HID collection enumeration
  3. Windows handle creation
  4. ReadFile/read-thread startup
  5. actual HID input reports
  6. B0 protocol decoding
  7. disconnects/errors

Direction is receive-only:
    headset -> USB receiver -> Windows HID -> this application

No HID output reports, feature reports, control commands, or polling commands
are sent.
"""

from __future__ import annotations

import threading
import time
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime
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


def log(message: str) -> None:
    """Print a timestamped diagnostic message immediately."""
    print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {message}", flush=True)


def hex_bytes(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)


def path_text(path: object) -> str:
    if isinstance(path, bytes):
        return path.decode("utf-8", errors="replace")
    return str(path)


def describe(info: dict[str, Any]) -> str:
    return (
        f"VID=0x{int(info.get('vendor_id') or 0):04X} "
        f"PID=0x{int(info.get('product_id') or 0):04X} "
        f"interface={info.get('interface_number', '-')} "
        f"usage_page=0x{int(info.get('usage_page') or 0):04X} "
        f"usage=0x{int(info.get('usage') or 0):04X} "
        f"manufacturer={info.get('manufacturer_string') or '-'} "
        f"product={info.get('product_string') or '-'}"
    )


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
        self.seen_paths: set[str] = set()
        self.report_count_by_path: dict[str, int] = {}

    @staticmethod
    def path_key(path: object) -> str:
        return path_text(path)

    def discover(self) -> list[dict[str, Any]]:
        try:
            devices = hid.enumerate(VID, PID)
        except Exception as exc:
            log(f"[ENUM] hid.enumerate FAILED: {type(exc).__name__}: {exc}")
            raise

        log(f"[ENUM] Target USB HID {VID:04X}:{PID:04X} -> {len(devices)} collection(s) found")
        for i, info in enumerate(devices):
            log(f"[ENUM] C{i:02d}: {describe(info)}")
            log(f"[ENUM] C{i:02d}: path={path_text(info.get('path', ''))}")
        return devices

    def reader_error(self, key: str, exc: Exception) -> None:
        with self.lock:
            self.state.last_error = f"{key}: {type(exc).__name__}: {exc}"
            self.readers.pop(key, None)
        log(f"[READ][ERROR] {key}: {type(exc).__name__}: {exc}")
        log("[READ][ERROR] The receiver may still be enumerated; this is a HID-open/read failure, not USB detection failure.")

    def reader_report(self, key: str, info: dict[str, Any], report: bytes) -> None:
        page = int(info.get("usage_page") or 0)
        usage = int(info.get("usage") or 0)
        collection = f"0x{page:04X}/0x{usage:04X}"
        count = self.report_count_by_path.get(key, 0) + 1
        self.report_count_by_path[key] = count

        decoded = decode_b0(report)
        log(
            f"[HID][REPORT] #{count} collection={collection} "
            f"length={len(report)} raw=[{hex_bytes(report)}]"
        )

        if decoded is None:
            log(
                f"[PROTO] NOT-B0: first_byte="
                f"0x{report[0]:02X}" if report else "[PROTO] EMPTY REPORT"
            )
        else:
            log(
                "[PROTO][B0] "
                f"volume={decoded['volume_raw']} "
                f"chat_balance={decoded['chat_balance_raw']} "
                f"battery_raw=0x{decoded['battery_raw']:02X} "
                f"charging={decoded['charging']} "
                f"flags=0x{decoded['flags']:02X} "
                f"vss={decoded['vss']} "
                f"mic_muted={decoded['mic_muted']} "
                f"headset_connected={decoded['headset_connected']} "
                f"family={decoded['family_flag']:02b}"
            )

        with self.lock:
            self.state.total_reports += 1
            self.state.total_bytes += len(report)
            self.state.last_report_age = 0.0
            self.state.last_report_hex = hex_bytes(report)
            self.state.last_report_collection = collection
            self.state.last_update = time.monotonic()

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
        current_paths: set[str] = set()

        with self.lock:
            previous = self.state.dongle_present
            self.state.dongle_present = bool(devices)

        if devices and not previous:
            log("[USB] RECEIVER DETECTED")
        elif not devices and previous:
            log("[USB] RECEIVER DISCONNECTED")

        for index, info in enumerate(devices):
            path = info.get("path")
            if not path:
                log(f"[ENUM][WARN] C{index:02d} has no HID path; cannot open")
                continue

            key = self.path_key(path)
            current_paths.add(key)

            if key not in self.seen_paths:
                self.seen_paths.add(key)
                log(f"[ENUM] NEW HID COLLECTION C{index:02d}")
                log(f"[ENUM]   {describe(info)}")
                log(f"[ENUM]   PATH={key}")

            if key in self.readers:
                continue

            if not native_windows_available():
                with self.lock:
                    self.state.last_error = "Native Windows HID backend unavailable"
                log("[READ][FATAL] Native Windows HID backend unavailable")
                continue

            log(f"[READ] Opening C{index:02d} with native Windows CreateFileW")
            reader = NativeWindowsHIDReader(
                path,
                on_report=lambda report, k=key, d=info: self.reader_report(k, d, report),
                on_error=lambda exc, k=key: self.reader_error(k, exc),
            )
            try:
                reader.start()
                self.readers[key] = reader
                log(
                    f"[READ] C{index:02d} STARTED successfully; "
                    "ReadFile is now waiting for Windows HID input"
                )
            except Exception as exc:
                self.reader_error(key, exc)

        stale = [key for key in self.readers if key not in current_paths]
        for key in stale:
            log(f"[READ] HID path disappeared; stopping reader: {key}")
            reader = self.readers.pop(key, None)
            if reader is not None:
                reader.stop()

        if not devices:
            with self.lock:
                self.state.headset_connected = False

    def diagnostic_heartbeat(self) -> None:
        with self.lock:
            dongle = self.state.dongle_present
            reports = self.state.total_reports
            b0 = self.state.b0_count
            readers = len(self.readers)
            last = self.state.last_report_hex or "NONE"
            error = self.state.last_error or "NONE"

        log(
            f"[HEARTBEAT] dongle={'YES' if dongle else 'NO'} "
            f"active_readers={readers} reports={reports} B0={b0} "
            f"last_raw=[{last}] last_error={error}"
        )

    def start(self) -> None:
        log("=" * 88)
        log("PS3 WIRELESS STEREO HEADSET - WINDOWS HID DIAGNOSTIC")
        log("=" * 88)
        log(f"Target VID/PID: 0x{VID:04X}:0x{PID:04X}")
        log("Expected status report: 8-byte input report beginning with 0xB0")
        log("Mode: PASSIVE RECEIVE ONLY")
        log("HID OUTPUT: DISABLED")
        log("FEATURE REPORTS: DISABLED")
        log("CONTROL TRANSFERS: DISABLED")
        log("BATTERY POLLING: DISABLED")
        log(f"Python: {__import__('sys').version.split()[0]}")
        log(f"Platform: {__import__('sys').platform}")
        log(f"Native Windows backend available: {native_windows_available()}")
        log("=" * 88)

        self.refresh()
        if self.state.dongle_present:
            log("[DIAG] USB dongle is visible to hidapi.")
            log("[DIAG] Now waiting for actual HID input reports from every enumerated collection.")
            log("[DIAG] IMPORTANT: the reference protocol is event-driven; no report may arrive until a headset property changes.")
            log("[DIAG] TEST: change volume, mute microphone, change game/chat balance, or toggle VSS.")
        else:
            log("[DIAG] USB dongle NOT detected. Check USB connection/driver/device state.")

        def loop() -> None:
            heartbeat_counter = 0
            while not self.stop_event.wait(SCAN_MS / 1000.0):
                try:
                    self.refresh()
                    heartbeat_counter += 1
                    if heartbeat_counter % 5 == 0:
                        self.diagnostic_heartbeat()
                except Exception as exc:
                    with self.lock:
                        self.state.last_error = f"scanner: {type(exc).__name__}: {exc}"
                    log(f"[SCAN][ERROR] {type(exc).__name__}: {exc}")

        thread = threading.Thread(target=loop, name="receiver-scanner", daemon=True)
        thread.start()
        self.reader_threads.append(thread)

    def stop(self) -> None:
        log("[SHUTDOWN] Stopping receiver monitor...")
        self.stop_event.set()
        for reader in list(self.readers.values()):
            reader.stop()
        self.readers.clear()
        log("[SHUTDOWN] All HID readers stopped.")


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
        ttk.Label(self, text="PS3 Wireless Stereo Headset", font=("Segoe UI", 21, "bold")).pack(anchor="w")
        ttk.Label(
            self,
            text="Passive information monitor • INPUT ONLY • detailed diagnostics are printed to the console",
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
                "Known B0 telemetry: byte 1=volume (0–5), byte 2=chat balance (0–100), "
                "byte 3=battery (0–100; 0x80=charging), byte 4=VSS/mic/link flags."
            ),
            wraplength=740,
        ).pack(anchor="w", pady=(9, 3))
        ttk.Label(
            info,
            text="Unknown reports are still counted and printed in full to the console.",
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

        self.set_card("dongle", "ON" if s.dongle_present else "OFF", "USB receiver detected" if s.dongle_present else "USB receiver not detected")

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
                self.set_card(key, "UNKNOWN" if key not in ("link", "power") else "OFF", detail)
        elif stale:
            self.set_card("link", "UNKNOWN", "No recent HID input")
            self.set_card("power", "UNKNOWN", "No recent status packet")
            self.set_card("vss", "UNKNOWN", "No recent B0 packet")
            self.set_card("mic", "UNKNOWN", "No recent B0 packet")
            self.set_card("battery", "CHARGING" if s.charging else f"{s.battery}%" if s.battery is not None else "UNKNOWN", "Last known value; status is stale")
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
