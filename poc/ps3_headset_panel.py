#!/usr/bin/env python3
"""Live receive-only panel for the Sony PS3 Wireless Stereo Headset receiver.

This application is an observation tool, not a controller.

It ONLY:
- discovers the Sony USB receiver (VID 0x12BA / PID 0x0035),
- opens its HID collections read-only,
- receives input reports,
- decodes the known 0xB0 status packet,
- shows raw incoming packets and per-collection receive statistics.

It NEVER sends HID output reports, feature reports, control commands, or
battery polling commands.

Known 0xB0 status packet fields used by this PoC:
    byte 0 == 0xB0
    byte 3 = battery level (0x80 is treated as charging/level unavailable)
    byte 4 bit 0 = VSS enabled
    byte 4 bit 1 = microphone mute enabled
    byte 4 bit 3 = headset/device link connected
    byte 4 bits 6-7 = device family flag

The panel deliberately labels headset power as DERIVED from the live link
flag because the known packet does not provide a separate independently
verified power-on bit.

Install from repository root:
    python -m pip install -r requirements.txt

Run:
    python poc/ps3_headset_panel.py
"""

from __future__ import annotations

import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from datetime import datetime
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
MAX_RECEIVE_LOG_LINES = 250

VSS_MASK = 0x01
MIC_MUTE_MASK = 0x02
CONNECTED_MASK = 0x08
MODEL_MASK = 0xC0


@dataclass
class CollectionStats:
    usage_page: int = 0
    usage: int = 0
    interface: int | None = None
    reports: int = 0
    bytes_received: int = 0
    last_hex: str = ""
    last_time: float | None = None
    errors: int = 0


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
    total_reports: int = 0
    total_bytes: int = 0
    last_status_time: float | None = None
    last_status_hex: str = ""
    last_error: str = ""
    collections: dict[str, CollectionStats] = field(default_factory=dict)
    receive_log: list[str] = field(default_factory=list)

    def update_from_report(
        self,
        report: bytes,
        collection_key: str,
        usage_page: int,
        usage: int,
        interface: int | None,
    ) -> None:
        now = time.monotonic()
        collection = self.collections.setdefault(
            collection_key,
            CollectionStats(
                usage_page=usage_page,
                usage=usage,
                interface=interface,
            ),
        )
        collection.reports += 1
        collection.bytes_received += len(report)
        collection.last_hex = " ".join(f"{b:02X}" for b in report)
        collection.last_time = now

        self.total_reports += 1
        self.total_bytes += len(report)
        self.receive_log.append(
            f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]}  "
            f"{collection_key:<18}  LEN={len(report):03d}  {collection.last_hex}"
        )
        if len(self.receive_log) > MAX_RECEIVE_LOG_LINES:
            del self.receive_log[:-MAX_RECEIVE_LOG_LINES]

        if len(report) < STATUS_MIN_LEN or report[0] != STATUS_REPORT_ID:
            return

        level = report[3]
        flags = report[4]

        self.headset_connected = bool(flags & CONNECTED_MASK)
        self.vss_enabled = bool(flags & VSS_MASK)
        self.mic_muted = bool(flags & MIC_MUTE_MASK)
        self.status_reports += 1
        self.last_status_time = now
        self.last_status_hex = " ".join(f"{b:02X}" for b in report)

        model_flags = (flags & MODEL_MASK) >> 6
        if model_flags == 0b01:
            self.model = "PlayStation Gold Wireless Headset"
        elif model_flags:
            self.model = f"Sony headset (family flag {model_flags:02b})"
        else:
            self.model = "Sony headset (family flag 00)"

        # In the known Linux reference driver, 0x80 is the special state.
        if level == 0x80:
            self.charging = True
            self.battery_percent = None
        else:
            self.charging = False
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

    @staticmethod
    def collection_key(device_info: dict) -> str:
        page = int(device_info.get("usage_page") or 0)
        usage = int(device_info.get("usage") or 0)
        interface = device_info.get("interface_number")
        return f"IF{interface if interface is not None else '-'} FF{page:04X} U{usage:04X}"

    def discover(self) -> list[dict]:
        devices = hid.enumerate(VID, PID)
        with self.lock:
            self.state.dongle_present = bool(devices)
        return devices

    def worker(self, device_info: dict) -> None:
        path = device_info.get("path")
        if not path:
            return

        key = self.collection_key(device_info)
        page = int(device_info.get("usage_page") or 0)
        usage = int(device_info.get("usage") or 0)
        interface = device_info.get("interface_number")
        device = hid.device()

        try:
            device.open_path(path)
            device.set_nonblocking(True)

            while not self.stop_event.is_set():
                try:
                    reports = device.read(512)
                except Exception as exc:
                    with self.lock:
                        collection = self.state.collections.setdefault(
                            key,
                            CollectionStats(
                                usage_page=page,
                                usage=usage,
                                interface=interface,
                            ),
                        )
                        collection.errors += 1
                        self.state.last_error = f"{key}: {exc}"
                    time.sleep(0.25)
                    continue

                if reports:
                    report = bytes(reports)
                    with self.lock:
                        self.state.update_from_report(
                            report,
                            key,
                            page,
                            usage,
                            interface,
                        )

                time.sleep(POLL_INTERVAL_SECONDS)
        except Exception as exc:
            with self.lock:
                self.state.last_error = f"{key}: {exc}"
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
            if not path_key or path_key in self.paths:
                continue

            thread = threading.Thread(
                target=self.worker,
                args=(device,),
                name=f"ps3-hid-reader-{len(self.threads)}",
                daemon=True,
            )
            thread.start()
            self.threads.append(thread)

        self.paths.update(new_paths)
        self.paths.intersection_update(new_paths)

        if not devices:
            with self.lock:
                self.state.headset_connected = None
                self.state.vss_enabled = None
                self.state.mic_muted = None
                self.state.battery_percent = None
                self.state.charging = False
                self.state.last_status_time = None


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("PS3 Wireless Stereo Headset — Receive Monitor")
        self.geometry("920x760")
        self.minsize(820, 650)
        self.configure(padx=16, pady=16)

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

        ttk.Label(
            self,
            text="PS3 Wireless Stereo Headset",
            font=("Segoe UI", 20, "bold"),
        ).pack(anchor="w")

        ttk.Label(
            self,
            text="Receive-only HID telemetry monitor • no headset control",
            font=("Segoe UI", 10),
        ).pack(anchor="w", pady=(0, 12))

        notice = ttk.LabelFrame(self, text="Mode", padding=10)
        notice.pack(fill="x", pady=(0, 10))
        ttk.Label(
            notice,
            text="READ ONLY — this application opens HID input paths and receives data only. "
                 "It sends no output reports, feature reports, or polling commands.",
            wraplength=850,
        ).pack(anchor="w")

        self.cards = ttk.Frame(self)
        self.cards.pack(fill="x")
        self.card_labels: dict[str, tuple[ttk.Label, ttk.Label]] = {}

        cards = [
            ("USB Dongle", "dongle"),
            ("Headset Link", "connected"),
            ("Headset Power", "power"),
            ("VSS", "vss"),
            ("Microphone", "mic"),
            ("Battery", "battery"),
        ]

        for row, (caption, key) in enumerate(cards):
            frame = ttk.LabelFrame(self.cards, text=caption, padding=10)
            frame.grid(row=row // 3, column=row % 3, sticky="nsew", padx=4, pady=4)
            value = ttk.Label(frame, text="UNKNOWN", font=("Segoe UI", 16, "bold"))
            value.pack(anchor="w")
            detail = ttk.Label(frame, text="Waiting for incoming telemetry…")
            detail.pack(anchor="w", pady=(3, 0))
            self.card_labels[key] = (value, detail)

        for column in range(3):
            self.cards.columnconfigure(column, weight=1)

        telemetry = ttk.LabelFrame(self, text="Receive statistics", padding=10)
        telemetry.pack(fill="x", pady=(10, 8))

        self.model_var = tk.StringVar(value="Model: Unknown")
        self.ping_var = tk.StringVar(value="B0 status packets: 0")
        self.total_var = tk.StringVar(value="Total HID input reports: 0 (0 bytes)")
        self.age_var = tk.StringVar(value="Last B0 report: —")
        self.raw_var = tk.StringVar(value="Last B0: —")
        self.volume_var = tk.StringVar(
            value="Volume: not decoded from the known B0 status packet"
        )

        for var in (
            self.model_var,
            self.ping_var,
            self.total_var,
            self.age_var,
            self.raw_var,
            self.volume_var,
        ):
            ttk.Label(telemetry, textvariable=var, wraplength=850).pack(anchor="w", pady=2)

        collections_frame = ttk.LabelFrame(self, text="Incoming HID collections", padding=8)
        collections_frame.pack(fill="x", pady=(0, 8))

        columns = ("collection", "reports", "bytes", "last")
        self.collection_tree = ttk.Treeview(
            collections_frame,
            columns=columns,
            show="headings",
            height=5,
        )
        headings = {
            "collection": "Collection",
            "reports": "Reports",
            "bytes": "Bytes",
            "last": "Last received report",
        }
        widths = {"collection": 180, "reports": 90, "bytes": 100, "last": 500}
        for column in columns:
            self.collection_tree.heading(column, text=headings[column])
            self.collection_tree.column(column, width=widths[column], anchor="w")
        self.collection_tree.pack(fill="x")

        raw_frame = ttk.LabelFrame(self, text="Raw incoming report stream", padding=8)
        raw_frame.pack(fill="both", expand=True)

        text_frame = ttk.Frame(raw_frame)
        text_frame.pack(fill="both", expand=True)
        self.raw_text = tk.Text(
            text_frame,
            height=12,
            wrap="none",
            font=("Consolas", 9),
            state="disabled",
        )
        scrollbar_y = ttk.Scrollbar(text_frame, orient="vertical", command=self.raw_text.yview)
        scrollbar_x = ttk.Scrollbar(text_frame, orient="horizontal", command=self.raw_text.xview)
        self.raw_text.configure(yscrollcommand=scrollbar_y.set, xscrollcommand=scrollbar_x.set)
        self.raw_text.grid(row=0, column=0, sticky="nsew")
        scrollbar_y.grid(row=0, column=1, sticky="ns")
        scrollbar_x.grid(row=1, column=0, sticky="ew")
        text_frame.rowconfigure(0, weight=1)
        text_frame.columnconfigure(0, weight=1)

        footer = ttk.Label(
            self,
            text="The panel shows what the receiver sends spontaneously. Unknown reports remain raw so they can be analyzed later.",
            wraplength=850,
        )
        footer.pack(anchor="w", pady=(8, 0))

        self.last_log_length = 0

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

    def refresh_collection_table(self, snapshot: HeadsetState) -> None:
        existing = set(self.collection_tree.get_children())
        used: set[str] = set()

        for key, info in sorted(snapshot.collections.items()):
            values = (
                key,
                info.reports,
                info.bytes_received,
                info.last_hex or "—",
            )
            iid = key
            if iid in existing:
                self.collection_tree.item(iid, values=values)
            else:
                try:
                    self.collection_tree.insert("", "end", iid=iid, values=values)
                except tk.TclError:
                    safe_iid = f"row{len(existing) + len(used)}"
                    self.collection_tree.insert("", "end", iid=safe_iid, values=values)
                    iid = safe_iid
            used.add(iid)

    def refresh_raw_log(self, snapshot: HeadsetState) -> None:
        log = snapshot.receive_log
        if len(log) == self.last_log_length:
            return
        self.last_log_length = len(log)
        self.raw_text.configure(state="normal")
        self.raw_text.delete("1.0", "end")
        self.raw_text.insert("1.0", "\n".join(log))
        self.raw_text.see("end")
        self.raw_text.configure(state="disabled")

    def refresh_ui(self) -> None:
        with self.monitor.lock:
            snapshot = HeadsetState(
                dongle_present=self.monitor.state.dongle_present,
                headset_connected=self.monitor.state.headset_connected,
                vss_enabled=self.monitor.state.vss_enabled,
                mic_muted=self.monitor.state.mic_muted,
                battery_percent=self.monitor.state.battery_percent,
                charging=self.monitor.state.charging,
                model=self.monitor.state.model,
                status_reports=self.monitor.state.status_reports,
                total_reports=self.monitor.state.total_reports,
                total_bytes=self.monitor.state.total_bytes,
                last_status_time=self.monitor.state.last_status_time,
                last_status_hex=self.monitor.state.last_status_hex,
                last_error=self.monitor.state.last_error,
                collections={k: CollectionStats(**vars(v)) for k, v in self.monitor.state.collections.items()},
                receive_log=list(self.monitor.state.receive_log),
            )

        stale = (
            snapshot.last_status_time is None
            or (time.monotonic() - snapshot.last_status_time) > STALE_AFTER_SECONDS
        )

        if snapshot.dongle_present:
            self.set_card("dongle", "ON", "USB receiver detected")
        else:
            self.set_card("dongle", "OFF", "USB receiver not detected")

        if not snapshot.dongle_present:
            self.set_card("connected", "UNKNOWN", "No receiver present")
            self.set_card("power", "UNKNOWN", "No receiver present")
            self.set_card("vss", "UNKNOWN", "No incoming status")
            self.set_card("mic", "UNKNOWN", "No incoming status")
            self.set_card("battery", "UNKNOWN", "No receiver telemetry")
        elif stale:
            self.set_card("connected", "UNKNOWN", "No fresh B0 status packet")
            self.set_card("power", "UNKNOWN", "Power is not independently reported")
            self.set_card("vss", "UNKNOWN", "No fresh B0 status packet")
            self.set_card("mic", "UNKNOWN", "No fresh B0 status packet")
            if snapshot.charging:
                self.set_card("battery", "CHARGING", "Last known state; report is stale")
            elif snapshot.battery_percent is not None:
                self.set_card("battery", f"{snapshot.battery_percent}%", "Last known value; stale")
            else:
                self.set_card("battery", "UNKNOWN", "No fresh battery telemetry")
        else:
            connected = bool(snapshot.headset_connected)
            self.set_card("connected", "ON" if connected else "OFF", "Live wireless link flag")
            self.set_card(
                "power",
                "ON" if connected else "OFF",
                "Derived from live link flag; not a separate power bit",
            )
            self.set_card("vss", "ON" if snapshot.vss_enabled else "OFF", "Live B0 bit 0")
            self.set_card(
                "mic",
                "MUTED" if snapshot.mic_muted else "ON",
                "Live B0 bit 1",
            )
            if snapshot.charging:
                self.set_card("battery", "CHARGING", "B0 byte 3 = 0x80")
            elif snapshot.battery_percent is not None:
                self.set_card("battery", f"{snapshot.battery_percent}%", "Live B0 byte 3")
            else:
                self.set_card("battery", "UNKNOWN", "Battery value unavailable")

        self.model_var.set(f"Model: {snapshot.model}")
        self.ping_var.set(f"B0 status packets: {snapshot.status_reports}")
        self.total_var.set(
            f"Total HID input reports: {snapshot.total_reports} "
            f"({snapshot.total_bytes} bytes)"
        )
        self.age_var.set(f"Last B0 report: {self.age_text(snapshot.last_status_time)}")
        self.raw_var.set(f"Last B0: {snapshot.last_status_hex or '—'}")
        self.volume_var.set(
            "Volume: not decoded from the known B0 status packet; raw reports are shown below for discovery."
        )

        self.refresh_collection_table(snapshot)
        self.refresh_raw_log(snapshot)
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
