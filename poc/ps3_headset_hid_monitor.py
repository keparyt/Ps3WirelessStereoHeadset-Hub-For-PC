#!/usr/bin/env python3
"""Passive Windows monitor for the Sony 12BA:0035 headset receiver.

Reference protocol: counter185/hid-playstation-headset.
The Linux driver receives 8-byte B0 status packets from the receiver whenever
headset properties change. This monitor mirrors that receive-only behavior on
Windows by opening each HID collection read-only and waiting for interrupt-IN
reports. No output/feature/control requests are performed.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import hid
except ImportError:
    print("Missing dependency: hidapi")
    print("Install with: python -m pip install -r requirements.txt")
    raise SystemExit(1)

from ps3_headset_protocol import decode_b0, hex_bytes
from ps3_headset_reader import NativeWindowsHIDReader, native_windows_available

VID = 0x12BA
PID = 0x0035

USAGE_NAMES = {
    0x000C: "Consumer Control",
    0xFF00: "Vendor 0xFF00",
    0xFF01: "Vendor 0xFF01",
    0xFF03: "Vendor 0xFF03",
}


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def fmt_hex(value: Any, width: int = 4) -> str:
    try:
        return f"{int(value or 0):0{width}X}"
    except (TypeError, ValueError):
        return "-"


def path_text(path: object) -> str:
    return path.decode("utf-8", errors="replace") if isinstance(path, bytes) else str(path)


def serializable_device(device: dict[str, Any]) -> dict[str, Any]:
    result = dict(device)
    result["path"] = path_text(result.get("path", ""))
    return result


class Monitor:
    def __init__(self, devices: list[dict[str, Any]], output: Path, backend: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.directory = output / stamp
        self.directory.mkdir(parents=True, exist_ok=True)
        self.devices = devices
        self.backend = backend
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.threads: list[threading.Thread] = []
        self.readers: list[NativeWindowsHIDReader] = []
        self.stats: dict[int, Counter[str]] = {}
        self.last_reports: dict[int, bytes] = {}
        self.status_count = 0
        self.last_status: dict[str, Any] | None = None
        self.last_status_time: str | None = None
        self._last_printed_status: dict[str, Any] | None = None
        self.capture_path = self.directory / "capture.jsonl"
        self.summary_path = self.directory / "summary.json"
        self.session_path = self.directory / "session.json"
        self.session_path.write_text(json.dumps({
            "session_started": utc_timestamp(),
            "mode": "PASSIVE_RECEIVE_ONLY",
            "reference": "counter185/hid-playstation-headset",
            "backend": backend,
            "vendor_id": VID,
            "product_id": PID,
            "reference_report": "B0 VV CC BB FF XX 11 00",
            "devices": [serializable_device(d) for d in devices],
            "outgoing_hid_reports": False,
            "outgoing_feature_reports": False,
            "outgoing_control_commands": False,
            "battery_polling_command": False,
        }, indent=2), encoding="utf-8")

    def log(self, data: dict[str, Any]) -> None:
        with self.lock:
            with self.capture_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(data, ensure_ascii=False) + "\n")

    def native_log(self, index: int, message: str) -> None:
        self.log({"timestamp": utc_timestamp(), "type": "native_reader_log",
                  "collection": index, "message": message})
        print(f"  [C{index:02d}] {message}")

    def descriptor_dump(self, index: int, info: dict[str, Any]) -> None:
        page = int(info.get("usage_page") or 0)
        usage = int(info.get("usage") or 0)
        target = self.directory / f"collection_{index:02d}_page_{page:04X}_usage_{usage:04X}_descriptor.txt"
        device = hid.device()
        try:
            device.open_path(info["path"])
            descriptor = bytes(device.get_report_descriptor())
            target.write_text(
                "PS3 WIRELESS STEREO HEADSET HID REPORT DESCRIPTOR\n" + "=" * 72 + "\n"
                + f"VID/PID: {VID:04X}:{PID:04X}\nUsage Page: 0x{page:04X}\nUsage: 0x{usage:04X}\n"
                + f"Length: {len(descriptor)} bytes\n\nRAW\n---\n{hex_bytes(descriptor)}\n",
                encoding="utf-8",
            )
            print(f"  [DESC] C{index:02d} page=0x{page:04X} len={len(descriptor)} -> {target.name}")
        except Exception as exc:
            target.write_text(f"Descriptor read failed: {exc}\n", encoding="utf-8")
            print(f"  [DESC] C{index:02d} FAILED: {exc}")
        finally:
            try: device.close()
            except Exception: pass

    def process_report(self, index: int, info: dict[str, Any], report: bytes) -> None:
        page = int(info.get("usage_page") or 0)
        usage = int(info.get("usage") or 0)
        stats = self.stats[index]
        stats["reports"] += 1
        stats["bytes"] += len(report)
        stats[f"len_{len(report)}"] += 1
        changed = report != self.last_reports.get(index)
        if changed:
            stats["changed"] += 1
            self.last_reports[index] = report

        decoded = decode_b0(report)
        event: dict[str, Any] = {
            "timestamp": utc_timestamp(), "type": "input_report",
            "collection": index, "usage_page": page, "usage": usage,
            "report_length": len(report), "hex": hex_bytes(report),
            "changed_from_previous": changed, "backend": self.backend,
        }
        if decoded is not None:
            event["decoded_b0"] = decoded
            self.status_count += 1
            self.last_status_time = event["timestamp"]
            self.last_status = decoded
        self.log(event)

        if changed:
            print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] C{index:02d} PAGE=0x{page:04X} LEN={len(report):03d}  {hex_bytes(report)}")

        if decoded is not None and decoded != self._last_printed_status:
            self._last_printed_status = dict(decoded)
            battery = "CHARGING" if decoded["charging"] else f"{decoded['battery_percent']}%" if decoded["battery_percent"] is not None else "UNKNOWN"
            volume = f"{decoded['volume_level']}/5" if decoded["volume_level"] is not None else f"raw={decoded['volume_raw']}"
            chat = f"{decoded['chat_balance']}%" if decoded["chat_balance"] is not None else f"raw={decoded['chat_balance_raw']}"
            print("[STATUS] "
                  f"LINK={'ON' if decoded['headset_connected'] else 'OFF'} | "
                  f"VSS={'ON' if decoded['vss'] else 'OFF'} | "
                  f"MIC={'MUTED' if decoded['mic_muted'] else 'ON'} | "
                  f"BATTERY={battery} | VOLUME={volume} | CHAT={chat} | RAW={decoded['raw']}")

    def hidapi_worker(self, index: int, info: dict[str, Any]) -> None:
        device = hid.device()
        try:
            device.open_path(info["path"])
            device.set_nonblocking(True)
            print(f"  [OPEN/HIDAPI] C{index:02d} page=0x{int(info.get('usage_page') or 0):04X}")
            while not self.stop_event.is_set():
                report = device.read(512)
                if report: self.process_report(index, info, bytes(report))
                time.sleep(0.002)
        except Exception as exc:
            self.stats[index]["errors"] += 1
            self.log({"timestamp": utc_timestamp(), "type": "reader_error", "collection": index,
                      "backend": "hidapi", "error": str(exc)})
            print(f"  [OPEN/HIDAPI] C{index:02d} FAILED: {exc}")
        finally:
            try: device.close()
            except Exception: pass

    def native_error(self, index: int, exc: Exception) -> None:
        self.stats[index]["errors"] += 1
        self.log({"timestamp": utc_timestamp(), "type": "reader_error", "collection": index,
                  "backend": "native-windows", "error": str(exc)})
        print(f"  [READ/NATIVE] C{index:02d} ERROR: {exc}")

    def start(self) -> None:
        print(f"\nCapture directory: {self.directory}")
        print(f"Backend: {self.backend.upper()}")
        print("Reference: counter185/hid-playstation-headset")
        print("Mode: PASSIVE RECEIVE ONLY")
        print("Expected status report: B0 VV CC BB FF XX 11 00 (8 bytes)")
        print("Outgoing HID reports: DISABLED")
        print("Outgoing feature/control requests: DISABLED")
        print("Battery polling command: DISABLED")
        print("\nDumping HID report descriptors...\n")
        for index, info in enumerate(self.devices):
            self.stats[index] = Counter()
            self.descriptor_dump(index, info)

        print("\nStarting passive readers...\n")
        if self.backend == "native" and not native_windows_available():
            print("Native Windows backend unavailable; use --backend hidapi.")
            return

        for index, info in enumerate(self.devices):
            if self.backend == "native":
                def on_report(report: bytes, idx=index, device_info=info) -> None:
                    self.process_report(idx, device_info, report)
                reader = NativeWindowsHIDReader(
                    info["path"], on_report=on_report,
                    on_error=lambda exc, idx=index: self.native_error(idx, exc),
                    on_log=lambda msg, idx=index: self.native_log(idx, msg),
                )
                try:
                    reader.start()
                    self.readers.append(reader)
                    print(f"  [OPEN/NATIVE] C{index:02d} page=0x{int(info.get('usage_page') or 0):04X} usage=0x{int(info.get('usage') or 0):04X}")
                except Exception as exc:
                    self.native_error(index, exc)
            else:
                thread = threading.Thread(target=self.hidapi_worker, args=(index, info), name=f"hidapi-{index}", daemon=True)
                thread.start(); self.threads.append(thread)

        print("\nListening for incoming information. The reference driver only receives packets when headset properties change.")
        print("Try changing volume, mic mute, VSS, chat balance, or headset power state.")
        print("Press Ctrl+C to stop and save the summary.\n")

    def wait(self) -> None:
        try:
            while not self.stop_event.is_set(): time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self) -> None:
        self.stop_event.set()
        for reader in self.readers: reader.stop()
        for thread in self.threads: thread.join(timeout=0.5)
        collections = {str(i): dict(s) for i, s in self.stats.items()}
        self.summary_path.write_text(json.dumps({
            "session_ended": utc_timestamp(), "mode": "PASSIVE_RECEIVE_ONLY",
            "reference": "counter185/hid-playstation-headset",
            "backend": self.backend, "status_reports_b0": self.status_count,
            "last_status_time": self.last_status_time,
            "last_decoded_status": self.last_status,
            "collections": collections,
        }, indent=2), encoding="utf-8")
        print(f"\nSaved capture: {self.directory}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Receive-only PS3 Wireless Stereo Headset HID information monitor.")
    parser.add_argument("--backend", choices=("auto", "native", "hidapi"), default="auto")
    parser.add_argument("--all-hid", action="store_true", help="Enumerate every HID device instead of only 12BA:0035.")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent / "logs")
    return parser.parse_args()


def enumerate_devices(all_hid: bool) -> list[dict[str, Any]]:
    return hid.enumerate() if all_hid else hid.enumerate(VID, PID)


def print_devices(devices: list[dict[str, Any]]) -> None:
    print("=" * 88)
    print("PS3 WIRELESS STEREO HEADSET - PASSIVE INFORMATION RECEIVER")
    print("=" * 88)
    print(f"Target VID/PID: {VID:04X}:{PID:04X} | INPUT ONLY | NO HID OUTPUT\n")
    for index, info in enumerate(devices):
        page = int(info.get("usage_page") or 0); usage = int(info.get("usage") or 0)
        print(f"[{index}] HID Interface")
        print(f"  VID/PID     : {fmt_hex(info.get('vendor_id'))}:{fmt_hex(info.get('product_id'))}")
        print(f"  Manufacturer: {info.get('manufacturer_string') or '-'}")
        print(f"  Interface   : {info.get('interface_number', '-')}")
        print(f"  Usage Page  : {fmt_hex(page)} ({USAGE_NAMES.get(page, 'Other/Vendor')})")
        print(f"  Usage       : {fmt_hex(usage)}")
        print(f"  Path        : {info.get('path')}\n")


def main() -> int:
    args = parse_args()
    backend = ("native" if native_windows_available() else "hidapi") if args.backend == "auto" else args.backend
    print("\nScanning HID devices...\n")
    devices = enumerate_devices(args.all_hid)
    if not devices:
        print(f"No HID device found for {VID:04X}:{PID:04X}.")
        return 1
    if not args.all_hid:
        devices = [d for d in devices if int(d.get("vendor_id") or 0) == VID and int(d.get("product_id") or 0) == PID]
    print_devices(devices)
    Monitor(devices, args.output, backend).wait() if False else None
    monitor = Monitor(devices, args.output, backend)
    monitor.start(); monitor.wait()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
