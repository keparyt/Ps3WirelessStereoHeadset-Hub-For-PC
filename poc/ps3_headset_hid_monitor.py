#!/usr/bin/env python3
"""PS3 Wireless Stereo Headset HID reverse-engineering PoC.

Read-only capture tool for the Sony Wireless Stereo Headset USB dongle.

Goals:
- Find the known PS3 headset HID device (VID 0x12BA, PID 0x0035).
- Monitor all HID collections from the device concurrently.
- Dump each collection's HID Report Descriptor.
- Record every raw input report to a timestamped JSONL capture.
- Print only changed reports to the console to keep it readable.
- Allow human-readable action markers (VSS, mute, volume, etc.) in the log.
- Produce a per-collection summary when the session ends.

This PoC intentionally does NOT send HID/output/feature commands.
Feature reports are not polled because this project is currently limited to
passive/read-only observation of data that the device sends on its own.

Install from repository root:
    python -m pip install -r requirements.txt

Run from repository root:
    python poc/ps3_headset_hid_monitor.py

Optional:
    python poc/ps3_headset_hid_monitor.py --all-hid

During a session, type one of these commands and press Enter to timestamp an
external action in the same capture log:
    vss       VSS button changed
    mute      microphone mute state changed
    vol+      volume up
    vol-      volume down
    power     headset power/connect event
    battery   battery observation/checkpoint
    idle      idle/baseline checkpoint
    mark TEXT custom marker
    stats     show live statistics
    help      show commands
    quit      stop capture
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import hid
except ImportError:
    print("Missing dependency: hidapi")
    print("Install with: python -m pip install -r requirements.txt")
    sys.exit(1)


PS3_VID = 0x12BA
PS3_PID = 0x0035

# Collections observed on the PS3 Wireless Stereo Headset family.
# We still monitor every HID collection by default; these values are only
# used to make the console output easier to scan.
INTERESTING_USAGE_PAGES = {
    0x000C: "Consumer Control",
    0xFF00: "Vendor 0xFF00",
    0xFF01: "Vendor 0xFF01",
    0xFF03: "Vendor 0xFF03",
}

ACTION_LABELS = {
    "vss": "VSS",
    "mute": "MIC_MUTE",
    "vol+": "VOLUME_UP",
    "vol-": "VOLUME_DOWN",
    "power": "POWER_OR_CONNECTION",
    "battery": "BATTERY_CHECKPOINT",
    "idle": "IDLE_BASELINE",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def timestamp() -> str:
    return utc_now().isoformat(timespec="milliseconds")


def fmt_hex(value: Any, width: int = 4) -> str:
    try:
        return f"{int(value or 0):0{width}X}"
    except (TypeError, ValueError):
        return "-"


def safe_filename(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value)
    return value.strip("._") or "unknown"


def hex_bytes(data: bytes | list[int]) -> str:
    return " ".join(f"{byte:02X}" for byte in data)


def descriptor_item_size(prefix: int) -> int:
    size_code = prefix & 0x03
    return 4 if size_code == 3 else size_code


def descriptor_item_type(prefix: int) -> int:
    return (prefix >> 2) & 0x03


def descriptor_item_tag(prefix: int) -> int:
    return (prefix >> 4) & 0x0F


def decode_descriptor(descriptor: bytes) -> list[str]:
    """Return a compact, human-readable HID report descriptor summary."""
    lines: list[str] = []
    i = 0
    usage_page: int | None = None
    usage: int | None = None
    report_size: int | None = None
    report_count: int | None = None
    report_id: int | None = None

    main_tags = {
        (0, 8): "INPUT",
        (0, 9): "OUTPUT",
        (0, 10): "COLLECTION",
        (0, 11): "FEATURE",
        (0, 12): "END_COLLECTION",
    }
    global_tags = {
        0: "USAGE_PAGE",
        7: "REPORT_SIZE",
        8: "REPORT_ID",
        9: "REPORT_COUNT",
    }
    local_tags = {
        0: "USAGE",
    }

    while i < len(descriptor):
        prefix = descriptor[i]
        i += 1

        # Long item. Keep the raw representation; long items are uncommon in
        # normal report descriptors but we should not misparse them.
        if prefix == 0xFE:
            if i + 1 >= len(descriptor):
                lines.append(f"@{i - 1:04X}: LONG_ITEM (truncated)")
                break
            length = descriptor[i]
            tag = descriptor[i + 1]
            i += 2
            data = descriptor[i : i + length]
            i += length
            lines.append(
                f"@{i - length - 3:04X}: LONG tag=0x{tag:02X} "
                f"len={length} data={hex_bytes(data)}"
            )
            continue

        size = descriptor_item_size(prefix)
        end = min(i + size, len(descriptor))
        data = descriptor[i:end]
        i = end
        item_type = descriptor_item_type(prefix)
        tag = descriptor_item_tag(prefix)
        value = int.from_bytes(bytes(data), "little", signed=False) if data else 0

        if item_type == 1:  # Global
            name = global_tags.get(tag)
            if tag == 0:
                usage_page = value
            elif tag == 7:
                report_size = value
            elif tag == 8:
                report_id = value
            elif tag == 9:
                report_count = value

            if name:
                if tag == 0:
                    lines.append(f"{name:<15} 0x{value:04X}")
                elif tag == 8:
                    lines.append(f"{name:<15} {value}")
                else:
                    lines.append(f"{name:<15} {value}")
            continue

        if item_type == 2:  # Local
            name = local_tags.get(tag)
            if tag == 0:
                usage = value
            if name:
                if usage_page is None:
                    lines.append(f"{name:<15} 0x{value:04X}")
                else:
                    lines.append(
                        f"{name:<15} 0x{value:04X} (page 0x{usage_page:04X})"
                    )
            continue

        if item_type == 0:  # Main
            name = main_tags.get((item_type, tag), f"MAIN_0x{tag:X}")
            if name in {"INPUT", "OUTPUT", "FEATURE"}:
                width = report_size if report_size is not None else "?"
                count = report_count if report_count is not None else "?"
                rid = report_id if report_id is not None else 0
                lines.append(
                    f"{name:<15} report_id={rid} size={width} count={count} "
                    f"flags=0x{value:02X}"
                )
            elif name == "COLLECTION":
                lines.append(f"{name:<15} type=0x{value:02X}")
            else:
                lines.append(name)
            # Local items are scoped to the next main item.
            usage = None
            continue

        lines.append(
            f"TYPE={item_type} TAG=0x{tag:X} SIZE={size} DATA={hex_bytes(data)}"
        )

    return lines


@dataclass
class CaptureStats:
    reports: int = 0
    bytes_received: int = 0
    unique_reports: int = 0
    errors: int = 0
    last_report_at: str | None = None
    last_report: bytes | None = None
    lengths: Counter[int] = field(default_factory=Counter)

    def update(self, report: bytes) -> bool:
        self.reports += 1
        self.bytes_received += len(report)
        self.lengths[len(report)] += 1
        self.last_report_at = timestamp()
        changed = report != self.last_report
        if changed:
            self.unique_reports += 1
        self.last_report = report
        return changed


class CaptureSession:
    def __init__(self, output_root: Path, devices: list[dict[str, Any]]):
        session_id = utc_now().strftime("%Y%m%d_%H%M%S")
        self.directory = output_root / session_id
        self.directory.mkdir(parents=True, exist_ok=True)
        self.devices = devices
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.stats: dict[str, CaptureStats] = {}
        self.threads: list[threading.Thread] = []
        self.log_path = self.directory / "capture.jsonl"
        self.events_path = self.directory / "events.jsonl"
        self.metadata_path = self.directory / "session.json"
        self.console_last: dict[str, bytes] = {}

        metadata = {
            "session_started": timestamp(),
            "vendor_id": PS3_VID,
            "product_id": PS3_PID,
            "device_count": len(devices),
            "devices": [self.serializable_device(d) for d in devices],
            "read_only": True,
            "notes": [
                "Input reports are captured passively.",
                "No HID output or feature writes are performed.",
                "A human action marker is correlation metadata, not an inferred headset state.",
            ],
        }
        self.metadata_path.write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    @staticmethod
    def serializable_device(device: dict[str, Any]) -> dict[str, Any]:
        result = dict(device)
        path = result.get("path")
        if isinstance(path, bytes):
            result["path"] = path.decode("utf-8", errors="replace")
        return result

    def append_jsonl(self, path: Path, payload: dict[str, Any]) -> None:
        with self.lock:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def add_event(self, action: str, detail: str | None = None) -> None:
        event = {
            "timestamp": timestamp(),
            "type": "action_marker",
            "action": action,
        }
        if detail:
            event["detail"] = detail
        self.append_jsonl(self.events_path, event)
        print(f"\n[ACTION] {action}" + (f" — {detail}" if detail else ""))

    def descriptor_path(self, index: int, device_info: dict[str, Any]) -> Path:
        page = fmt_hex(device_info.get("usage_page"))
        usage = fmt_hex(device_info.get("usage"))
        return self.directory / (
            f"collection_{index:02d}_page_{page}_usage_{usage}_descriptor.txt"
        )

    def dump_descriptor(self, index: int, device_info: dict[str, Any]) -> None:
        path = device_info.get("path")
        if not path:
            return

        device = hid.device()
        descriptor_file = self.descriptor_path(index, device_info)
        try:
            device.open_path(path)
            raw = bytes(device.get_report_descriptor())
            summary = decode_descriptor(raw)

            with descriptor_file.open("w", encoding="utf-8") as handle:
                handle.write("PS3 Wireless Stereo Headset HID Report Descriptor\n")
                handle.write("=" * 72 + "\n")
                handle.write(
                    f"VID/PID: {fmt_hex(device_info.get('vendor_id'))}:"
                    f"{fmt_hex(device_info.get('product_id'))}\n"
                )
                handle.write(f"Interface: {device_info.get('interface_number', '-')}\n")
                handle.write(f"Usage Page: {fmt_hex(device_info.get('usage_page'))}\n")
                handle.write(f"Usage: {fmt_hex(device_info.get('usage'))}\n")
                handle.write(f"Length: {len(raw)} bytes\n\n")
                handle.write("RAW DESCRIPTOR\n")
                handle.write("-" * 72 + "\n")
                handle.write(hex_bytes(raw) + "\n\n")
                handle.write("DECODED SUMMARY\n")
                handle.write("-" * 72 + "\n")
                handle.write("\n".join(summary) if summary else "No decoded items")
                handle.write("\n")

            print(
                f"  [DESC] collection={index:02d} "
                f"page=0x{fmt_hex(device_info.get('usage_page'))} "
                f"len={len(raw)} bytes -> {descriptor_file.name}"
            )
        except Exception as exc:
            descriptor_file.write_text(
                f"Unable to read HID report descriptor: {exc}\n",
                encoding="utf-8",
            )
            print(
                f"  [DESC] collection={index:02d} failed: {exc}"
            )
        finally:
            try:
                device.close()
            except Exception:
                pass

    def worker(self, index: int, device_info: dict[str, Any]) -> None:
        path = device_info.get("path")
        if not path:
            return

        key = f"{index}:{path!r}"
        stats = CaptureStats()
        self.stats[key] = stats
        hid_device = hid.device()

        page = int(device_info.get("usage_page") or 0)
        usage = int(device_info.get("usage") or 0)
        page_label = INTERESTING_USAGE_PAGES.get(page, f"0x{page:04X}")

        try:
            hid_device.open_path(path)
            hid_device.set_nonblocking(True)
            print(
                f"  [OPEN] collection={index:02d} "
                f"page=0x{page:04X} ({page_label}) usage=0x{usage:04X}"
            )

            while not self.stop_event.is_set():
                try:
                    report_list = hid_device.read(512)
                except Exception as exc:
                    stats.errors += 1
                    self.append_jsonl(
                        self.log_path,
                        {
                            "timestamp": timestamp(),
                            "type": "read_error",
                            "collection": index,
                            "usage_page": page,
                            "usage": usage,
                            "error": str(exc),
                        },
                    )
                    # Avoid a tight error loop if Windows stops exposing a path.
                    time.sleep(0.25)
                    continue

                if report_list:
                    report = bytes(report_list)
                    changed = stats.update(report)
                    self.append_jsonl(
                        self.log_path,
                        {
                            "timestamp": timestamp(),
                            "type": "input_report",
                            "collection": index,
                            "usage_page": page,
                            "usage": usage,
                            "report_length": len(report),
                            "hex": hex_bytes(report),
                            "changed_from_previous": changed,
                        },
                    )

                    if changed:
                        print(
                            f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] "
                            f"C{index:02d} PAGE=0x{page:04X} "
                            f"LEN={len(report):03d}  {hex_bytes(report)}"
                        )

                time.sleep(0.002)

        except Exception as exc:
            stats.errors += 1
            self.append_jsonl(
                self.log_path,
                {
                    "timestamp": timestamp(),
                    "type": "open_error",
                    "collection": index,
                    "usage_page": page,
                    "usage": usage,
                    "error": str(exc),
                },
            )
            print(f"  [OPEN] collection={index:02d} failed: {exc}")
        finally:
            try:
                hid_device.close()
            except Exception:
                pass

    def start(self) -> None:
        print(f"\nCapture directory: {self.directory}")
        print("Dumping HID report descriptors...\n")

        for index, device_info in enumerate(self.devices):
            self.dump_descriptor(index, device_info)

        print("\nStarting passive readers...\n")
        for index, device_info in enumerate(self.devices):
            thread = threading.Thread(
                target=self.worker,
                args=(index, device_info),
                name=f"hid-{index}",
                daemon=True,
            )
            thread.start()
            self.threads.append(thread)

    def stop(self) -> None:
        self.stop_event.set()
        for thread in self.threads:
            thread.join(timeout=1.0)
        self.write_summary()

    def write_summary(self) -> None:
        summary: dict[str, Any] = {
            "session_ended": timestamp(),
            "capture_directory": str(self.directory),
            "read_only": True,
            "collections": [],
        }

        for key, stats in sorted(self.stats.items()):
            summary["collections"].append(
                {
                    "collection_key": key,
                    "reports": stats.reports,
                    "bytes_received": stats.bytes_received,
                    "unique_reports": stats.unique_reports,
                    "errors": stats.errors,
                    "last_report_at": stats.last_report_at,
                    "length_distribution": {
                        str(length): count
                        for length, count in sorted(stats.lengths.items())
                    },
                }
            )

        (self.directory / "summary.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        print("\n" + "=" * 88)
        print("CAPTURE SUMMARY")
        print("=" * 88)
        for item in summary["collections"]:
            print(
                f"{item['collection_key']}: "
                f"reports={item['reports']} "
                f"unique={item['unique_reports']} "
                f"bytes={item['bytes_received']} "
                f"errors={item['errors']}"
            )
        print(f"\nSaved: {self.directory}")

    def live_stats(self) -> None:
        print("\n" + "-" * 88)
        print("LIVE STATISTICS")
        print("-" * 88)
        for key, stats in sorted(self.stats.items()):
            print(
                f"{key}: reports={stats.reports:<8} "
                f"unique={stats.unique_reports:<8} "
                f"bytes={stats.bytes_received:<10} "
                f"errors={stats.errors}"
            )


def find_ps3_hid_devices(all_hid: bool) -> list[dict[str, Any]]:
    if all_hid:
        devices = hid.enumerate()
    else:
        devices = hid.enumerate(PS3_VID, PS3_PID)

    if not devices:
        return []

    return devices


def print_devices(devices: list[dict[str, Any]]) -> None:
    print("=" * 88)
    print("PS3 WIRELESS STEREO HEADSET - HID COLLECTIONS")
    print("=" * 88)
    print(
        "Known target: VID/PID 12BA:0035 | "
        "all selected collections are monitored read-only."
    )

    for index, device in enumerate(devices):
        page = int(device.get("usage_page") or 0)
        usage = int(device.get("usage") or 0)
        label = INTERESTING_USAGE_PAGES.get(page, "Other")
        print(f"\n[{index}] {device.get('product_string') or 'Unknown HID device'}")
        print(
            f"  VID/PID     : {fmt_hex(device.get('vendor_id'))}:"
            f"{fmt_hex(device.get('product_id'))}"
        )
        print(f"  Manufacturer: {device.get('manufacturer_string') or '-'}")
        print(f"  Interface   : {device.get('interface_number', '-')}")
        print(f"  Usage Page  : {fmt_hex(page)} ({label})")
        print(f"  Usage       : {fmt_hex(usage)}")
        print(f"  Path        : {device.get('path')}")


def print_commands() -> None:
    print(
        "\nCommands:\n"
        "  vss       mark a VSS button change\n"
        "  mute      mark a microphone mute change\n"
        "  vol+      mark volume up\n"
        "  vol-      mark volume down\n"
        "  power     mark headset power/connect event\n"
        "  battery   mark a battery checkpoint\n"
        "  idle      mark an idle/baseline checkpoint\n"
        "  mark TEXT create a custom marker\n"
        "  stats     print live report statistics\n"
        "  help      show this help\n"
        "  quit      stop the capture\n"
    )


def command_loop(session: CaptureSession) -> None:
    print_commands()
    while not session.stop_event.is_set():
        try:
            raw = input("capture> ").strip()
        except (EOFError, KeyboardInterrupt):
            session.stop_event.set()
            return

        if not raw:
            continue

        command, _, remainder = raw.partition(" ")
        command = command.lower()

        if command in {"quit", "q", "exit"}:
            session.stop_event.set()
            return
        if command == "help":
            print_commands()
            continue
        if command == "stats":
            session.live_stats()
            continue
        if command in ACTION_LABELS:
            session.add_event(ACTION_LABELS[command])
            continue
        if command == "mark":
            detail = remainder.strip()
            if not detail:
                print("Usage: mark TEXT")
            else:
                session.add_event("CUSTOM", detail)
            continue

        print("Unknown command. Type 'help'.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only multi-interface HID analyzer for Sony PS3 Wireless Stereo Headset."
    )
    parser.add_argument(
        "--all-hid",
        action="store_true",
        help="Inspect every HID device instead of only VID/PID 12BA:0035.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent / "logs",
        help="Directory where capture sessions are stored.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    print("\nScanning HID devices...\n")
    devices = find_ps3_hid_devices(args.all_hid)

    if not devices:
        if args.all_hid:
            print("No HID devices found.")
        else:
            print(
                f"No HID device found for VID/PID "
                f"{PS3_VID:04X}:{PS3_PID:04X}.\n"
                "Make sure the USB dongle is connected."
            )
        return 1

    # Normal mode is intentionally restricted to the known Sony headset VID/PID.
    if not args.all_hid:
        devices = [
            d
            for d in devices
            if int(d.get("vendor_id") or 0) == PS3_VID
            and int(d.get("product_id") or 0) == PS3_PID
        ]

    print_devices(devices)

    session = CaptureSession(args.output, devices)
    session.start()

    try:
        command_loop(session)
    except KeyboardInterrupt:
        print("\nStopping capture...")
        session.stop_event.set()
    finally:
        session.stop()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
