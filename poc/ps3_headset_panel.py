#!/usr/bin/env python3
"""Live Windows panel for the Sony PS3 Wireless Stereo Headset receiver.

This panel is intentionally read-only. It listens to HID input reports from
Sony VID 0x12BA / PID 0x0035 and decodes the known 0xB0 status report:

    byte 0 == 0xB0
    byte 3 = battery level (0x80 is treated as charging/unknown)
    byte 4 bit 0 = VSS enabled
    byte 4 bit 1 = microphone mute enabled
    byte 4 bit 3 = headset/receiver link connected
    byte 4 bits 6-7 = device family flag (01 = Gold in the reference driver)

The reference driver this implementation follows is the Linux
hid-playstation-headset driver by counter185. This project does not send
output reports or feature writes.

Install from repository root:
    python -m pip install -r requirements.txt

Run:
    python poc/ps3_headset_panel.py
"""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
import tkinter as tk
from tkinter import ttk

try:
    import hid
except ImportError:
    print("Missing dependency: hidapi")
    print("Install with: python -m pip install -r requirements.txt")
    raise SystemExit(1)


VID = 0x12BA
PID = 0x0035
STATUS_REPORT_ID = 0xB0
STATUS_MIN_LEN = 5
STALE_AFTER_SECONDS = 4.0
SCAN_INTERVAL_SECONDS = 1.0
POLL_INTERVAL_SECONDS = 0.01

VSS_MASK = 0x01
MIC_MUTE_MASK = 0x02
CONNECTED_MASK = 0x08
MODEL_MASK = 0xC0


@dataclass
class HeadsetState:
    dongle_present: bool = False
    headset_connected: bool | None = None
    vss_enabled: bool | None = None
    mic_muted: bool | None = None
    battery_percent: int | None = None
    charging: bool = False
    model: str = "Unknown"
    status_reports: int = 0
    last_status_time: float | None = None
    last_status_hex: str = ""
    last_error: str = ""

    def update_from_status(self, report: bytes) -> None:
        if len(report) < STATUS_MIN_LEN or report[0] != STATUS_REPORT_ID:
            return

        level = report[3]
        flags = report[4]

        self.headset_connected = bool(flags & CONNECTED_MASK)
        self.vss_enabled = bool(flags & VSS_MASK)
        self.mic_muted = bool(flags & MIC_MUTE_MASK)
        self.status_reports += 1
        self.last_status_time = time.monotonic()
        self.last_status_hex = " ".join(f"{b:02X}" for b in report)

        model_flags = (flags & MODEL_MASK) >> 6
        if model_flags == 0b01:
            self.model = "PlayStation Gold Wireless Headset"
        elif model_flags:
            self.model = f"Sony headset (family flag {model_flags:02b})"
        else:
            self.model = "Sony headset (family flag 00)"

        # The known reference driver treats 0x80 as a special charging state.
        if level == 0x80:
            self.charging = True
            self.battery_percent = None
        else:
            self.charging = False
            # The known device reports the battery as a percentage-like byte.
            # Clamp only to keep the UI sane if a malformed report is received.
            self.battery_percent = max(0, min(100, level))


class HeadsetMonitor:
    def __init__(self) -> None:
        self.state = HeadsetState()
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []
        self.paths: set[str] = set()

    @staticmethod
    def path_key(path: object) -> str:
        if isinstance(path, bytes):
            return path.decode("utf-8", errors="replace")
        return str(path)

    def discover(self) -> list[dict]:
        devices = hid.enumerate(VID, PID)
        with self.lock:
            self.state.dongle_present = bool(devices)
        return devices

    def worker(self, device_info: dict) -> None:
        path = device_info.get("path")
        if not path:
            return

        device = hid.device()
        try:
            device.open_path(path)
            device.set_nonblocking(True)

            while not self.stop_event.is_set():
                reports = device.read(512)
                if reports:
                    report = bytes(reports)
                    if len(report) >= STATUS_MIN_LEN and report[0] == STATUS_REPORT_ID:
                        with self.lock:
                            self.state.update_from_status(report)
                time.sleep(POLL_INTERVAL_SECONDS)
        except Exception as exc:
            with self.lock:
                self.state.last_error = str(exc)
        finally:
            try:
                device.close()
            except Exception:
                pass

    def refresh_devices(self) -> None:
        devices = self.discover()
        new_paths = {self.path_key(d.get("path")) for d in devices if d.get("path")}

        for device in devices:
            path_key = self.path_key(device.get("path"))
            if not path_key:
                continue
            if path_key in self.paths:
                continue

            thread = threading.Thread(
                target=self.worker,
                args=(device,),
                name="ps3-hid-reader",
                daemon=True,
            )
            thread.start()
            self.threads.append(thread)

        self.paths.update(new_paths)
        self.paths.intersection_update(new_paths)

        # If the dongle disappears, clear live transport status. Keep the last
        # decoded values visible only until the UI marks them stale.
        if not devices:
            with self.lock:
                self.state.headset_connected = False
                self.state.last_status_time = None


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("PS3 Wireless Stereo Headset Hub")
        self.geometry("680x560")
        self.minsize(620, 500)
        self.configure(padx=18, pady=18)

        self.monitor = HeadsetMonitor()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self._build_ui()
        self.after(50, self.refresh_ui)
        self.after(1000, self.refresh_connection)

    def _build_ui(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass

        title = ttk.Label(
            self,
            text="PS3 Wireless Stereo Headset",
            font=("Segoe UI", 20, "bold"),
        )
        title.pack(anchor="w")

        subtitle = ttk.Label(
            self,
            text="Sony USB receiver • read-only HID telemetry",
            font=("Segoe UI", 10),
        )
        subtitle.pack(anchor="w", pady=(0, 16))

        self.cards = ttk.Frame(self)
        self.cards.pack(fill="x")

        self.card_labels: dict[str, tuple[ttk.Label, ttk.Label]] = {}
        cards = [
            ("Dongle", "dongle"),
            ("Headset Link", "connected"),
            ("Headset Power", "power"),
            ("VSS", "vss"),
            ("Microphone", "mic"),
            ("Battery", "battery"),
        ]

        for row, (caption, key) in enumerate(cards):
            frame = ttk.LabelFrame(self.cards, text=caption, padding=12)
            frame.grid(row=row // 2, column=row % 2, sticky="nsew", padx=5, pady=5)
            self.cards.columnconfigure(0, weight=1)
            self.cards.columnconfigure(1, weight=1)

            value = ttk.Label(frame, text="UNKNOWN", font=("Segoe UI", 17, "bold"))
            value.pack(anchor="w")
            detail = ttk.Label(frame, text="Waiting for telemetry…")
            detail.pack(anchor="w", pady=(4, 0))
            self.card_labels[key] = (value, detail)

        info = ttk.LabelFrame(self, text="Telemetry", padding=12)
        info.pack(fill="both", expand=True, pady=(12, 0))

        self.model_var = tk.StringVar(value="Model: Unknown")
        self.ping_var = tk.StringVar(value="Status pings: 0")
        self.age_var = tk.StringVar(value="Last B0 report: —")
        self.raw_var = tk.StringVar(value="Last B0: —")
        self.volume_var = tk.StringVar(value="Headset volume telemetry: not reported by the decoded B0 status packet")
        self.error_var = tk.StringVar(value="")

        for var in (self.model_var, self.ping_var, self.age_var, self.raw_var, self.volume_var):
            ttk.Label(info, textvariable=var, wraplength=610).pack(anchor="w", pady=3)

        self.error_label = ttk.Label(info, textvariable=self.error_var, wraplength=610)
        self.error_label.pack(anchor="w", pady=(8, 0))

        footer = ttk.Frame(self)
        footer.pack(fill="x", pady=(12, 0))
        ttk.Label(
            footer,
            text="Battery is read from incoming 0xB0 status reports; no polling/write command is sent.",
            wraplength=610,
        ).pack(anchor="w")

    @staticmethod
    def age_text(last_time: float | None) -> str:
        if last_time is None:
            return "—"
        age = max(0.0, time.monotonic() - last_time)
        return f"{age:.1f}s ago"

    def set_card(self, key: str, value: str, detail: str) -> None:
        value_label, detail_label = self.card_labels[key]
        value_label.config(text=value)
        detail_label.config(text=detail)

    def refresh_connection(self) -> None:
        if self.monitor.stop_event.is_set():
            return
        try:
            self.monitor.refresh_devices()
        except Exception as exc:
            with self.monitor.lock:
                self.monitor.state.last_error = str(exc)
        self.after(int(SCAN_INTERVAL_SECONDS * 1000), self.refresh_connection)

    def refresh_ui(self) -> None:
        with self.monitor.lock:
            snapshot = HeadsetState(**vars(self.monitor.state))

        stale = (
            snapshot.last_status_time is None
            or (time.monotonic() - snapshot.last_status_time) > STALE_AFTER_SECONDS
        )

        if snapshot.dongle_present:
            self.set_card("dongle", "ON", "USB receiver detected")
        else:
            self.set_card("dongle", "OFF", "USB receiver not detected")

        if not snapshot.dongle_present:
            self.set_card("connected", "OFF", "Receiver not connected")
            self.set_card("power", "OFF", "No live headset status")
            self.set_card("vss", "UNKNOWN", "No live status report")
            self.set_card("mic", "UNKNOWN", "No live status report")
            self.set_card("battery", "UNKNOWN", "No receiver telemetry")
        elif stale:
            self.set_card("connected", "UNKNOWN", "Status report is stale")
            self.set_card("power", "UNKNOWN", "Cannot confirm headset power")
            self.set_card("vss", "UNKNOWN", "Status report is stale")
            self.set_card("mic", "UNKNOWN", "Status report is stale")
            if snapshot.charging:
                self.set_card("battery", "CHARGING", "Last known battery state")
            elif snapshot.battery_percent is not None:
                self.set_card("battery", f"{snapshot.battery_percent}%", "Last known value (stale)")
            else:
                self.set_card("battery", "UNKNOWN", "Status report is stale")
        else:
            connected = bool(snapshot.headset_connected)
            self.set_card(
                "connected",
                "ON" if connected else "OFF",
                "Wireless headset link",
            )
            self.set_card(
                "power",
                "ON" if connected else "OFF",
                "Derived from the live headset-link flag",
            )
            self.set_card(
                "vss",
                "ON" if snapshot.vss_enabled else "OFF",
                "VIRTUAL SURROUND flag",
            )
            self.set_card(
                "mic",
                "MUTED" if snapshot.mic_muted else "ON",
                "Microphone input state",
            )
            if snapshot.charging:
                self.set_card("battery", "CHARGING", "Battery level reported as 0x80")
            elif snapshot.battery_percent is not None:
                self.set_card("battery", f"{snapshot.battery_percent}%", "Live B0 telemetry")
            else:
                self.set_card("battery", "UNKNOWN", "No battery percentage decoded")

        self.model_var.set(f"Model: {snapshot.model}")
        self.ping_var.set(f"Status pings (B0): {snapshot.status_reports}")
        self.age_var.set(f"Last B0 report: {self.age_text(snapshot.last_status_time)}")
        self.raw_var.set(f"Last B0: {snapshot.last_status_hex or '—'}")
        self.volume_var.set(
            "Headset volume telemetry: not decoded from the known B0 status packet; "
            "the UI does not guess a volume percentage."
        )

        if snapshot.last_error:
            self.error_var.set(f"Reader: {snapshot.last_error}")
        else:
            self.error_var.set("")

        self.after(100, self.refresh_ui)

    def on_close(self) -> None:
        self.monitor.stop_event.set()
        self.destroy()


def main() -> int:
    app = App()
    app.mainloop()
    app.monitor.stop_event.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
