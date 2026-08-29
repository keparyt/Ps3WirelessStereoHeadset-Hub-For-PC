#!/usr/bin/env python3
"""PS3 Wireless Stereo Headset passive HID receiver.

This tool is intentionally ONE-WAY / READ-ONLY.

It does not send:
- HID output reports
- HID feature reports
- control commands
- battery polling commands
- headset control requests

It only opens the HID collections and reads data that the receiver sends to
Windows on its own. The purpose is to discover and document what information
is actually available from the Sony Wireless Stereo Headset receiver.

Known target:
    VID 0x12BA / PID 0x0035

The tool monitors all HID collections exposed by the receiver, dumps their
HID Report Descriptors, records incoming reports, and decodes the known B0
status report when present.

Run from repository root:
    python poc/ps3_headset_hid_monitor.py

Optional:
    python poc/ps3_headset_hid_monitor.py --all-hid

Stop with Ctrl+C. There is no command prompt because this is a passive
information receiver only.
"""

from __future__ import annotations

import argparse
import json
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
    raise SystemExit(1)


PS3_VID = 0x12BA
PS3_PID = 0x0035
STATUS_REPORT_ID = 0xB0
STATUS_MIN_LEN = 5

VSS_MASK = 0x01
MIC_MUTE_MASK = 0x02
CONNECTED_MASK = 0x08
MODEL_MASK = 0xC0

USAGE_PAGE_NAMES = {
    0x000C: "Consumer Control",
    0xFF00: "Vendor 0xFF00",
    0xFF01: "Vendor 0xFF01",
    0xFF03: "Vendor 0xFF03",
}


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def fmt_hex(value: Any, width: int = 4) -> str:
    try:
        return f"{int(value or 0):0{width}X}"
    except (TypeError, ValueError):
        return "-"


def hex_bytes(data: bytes | list[int]) -> str:
    return " ".join(f"{byte:02X}" for byte in data)


def serializable_device(device: dict[str, Any]) -> dict[str, Any]:
    result = dict(device)
    path = result.get("path")
    if isinstance(path, bytes):
        result["path"] = path.decode("utf-8", errors="replace")
    return result


def decode_b0_status(report: bytes) -> dict[str, Any] | None:
    """Decode the known 0xB0 status packet without sending anything."""
    if len(report) < STATUS_MIN_LEN or report[0] != STATUS_REPORT_ID:
        return None

    level = report[3]
    flags = report[4]

    if level == 0x80:
        battery = None
        charging = True
    else:
        battery = max(0, min(100, level))
        charging = False

    family = (flags & MODEL_MASK) >> 6
    if family == 0b01:
        model = "PlayStation Gold Wireless Headset"
    elif family:
        model = f"Sony headset (family flag {family:02b})"
    else:
        model = "Sony headset (family flag 00)"

    return {
        "battery_percent": battery,
        "charging": charging,
        "vss": bool(flags & VSS_MASK),
        "mic_muted": bool(flags & MIC_MUTE_MASK),
        "headset_connected": bool(flags & CONNECTED_MASK),
        "model": model,
        "flags": flags,
        "family_flag": family,
        "raw": hex_bytes(report),
    }


def descriptor_item_size(prefix: int) -> int:
    size_code = prefix & 0x03
    return 4 if size_code == 3 else size_code


def descriptor_item_type(prefix: int) -> int:
    return (prefix >> 2) & 0x03


def descriptor_item_tag(prefix: int) -> int:
    return (prefix >> 4) & 0x0F


def decode_descriptor(descriptor: bytes) -> list[str]:
    """Produce a compact structural summary of a HID report descriptor."""
    lines: list[str] = []
    i = 0
    usage_page: int | None = None
    report_size: int | None = None
    report_count: int | None = None
    report_id: int | None = None

    main_tags = {
        8: "INPUT",
        9: "OUTPUT",
        10: "COLLECTION",
        11: "FEATURE",
        12: "END_COLLECTION",
    }
    global_tags = {
        0: "USAGE_PAGE",
        7: "REPORT_SIZE",
        8: "REPORT_ID",
        9: "REPORT_COUNT",
    }

    while i < len(descriptor):
        start = i
        prefix = descriptor[i]
        i += 1

        if prefix == 0xFE:
            if i + 1 >= len(descriptor):
                lines.append(f"@{start:04X}: LONG_ITEM (truncated)")
                break
            length = descriptor[i]
            tag = descriptor[i + 1]
            i += 2
            data = descriptor[i : i + length]
            i += length
            lines.append(
                f"@{start:04X}: LONG tag=0x{tag:02X} len={length} "
                f"data={hex_bytes(data)}"
            )
            continue

        size = descriptor_item_size(prefix)
        data = descriptor[i : i + size]
        i += size
        item_type = descriptor_item_type(prefix)
        tag = descriptor_item_tag(prefix)
        value = int.from_bytes(bytes(data), "little", signed=False) if data else 0

        if item_type == 1:
            name = global_tags.get(tag, f"GLOBAL_0x{tag:X}")
            if tag == 0:
                usage_page = value
            elif tag == 7:
                report_size = value
            elif tag == 8:
                report_id = value
            elif tag == 9:
                report_count = value
            lines.append(f"{name:<15} {value if tag != 0 else f'0x{value:04X}'}")
            continue

        if item_type == 2:
            if tag == 0:
                if usage_page is None:
                    lines.append(f"USAGE           0x{value:04X}")
                else:
                    lines.append(
                        f"USAGE           0x{value:04X} (page 0x{usage_page:04X})"
                    )
            else:
                lines.append(f"LOCAL_0x{tag:X}      0x{value:04X}")
            continue

        if item_type == 0:
            name = main_tags.get(tag, f"MAIN_0x{tag:X}")
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
            continue

        lines.append(
            f"TYPE={item_type} TAG=0x{tag:X} SIZE={size} DATA={hex_bytes(data)}"
        )

    return lines


@dataclass
class CollectionStats:
    reports: int = 0
    bytes_received: int = 0
    changed_reports: int = 0
    errors: int = 0
    last_report_at: str | None = None
    last_report: bytes | None = None
    lengths: Counter[int] = field(default_factory=Counter)

    def update(self, report: bytes) -> bool:
        changed = report != self.last_report
        self.reports += 1
        self.bytes_received += len(report)
        self.lengths[len(report)] += 1
        self.last_report_at = timestamp()
        self.last_report = report
        if changed:
            self.changed_reports += 1
        return changed


class PassiveCapture:
    def __init__(self, output_root: Path, devices: list[dict[str, Any]]):
        session_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.directory = output_root / session_id
        self.directory.mkdir(parents=True, exist_ok=True)
        self.devices = devices
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.threads: list[threading.Thread] = []
        self.stats: dict[int, CollectionStats] = {}
        self.last_status: dict[str, Any] | None = None

        self.capture_path = self.directory / "capture.jsonl"
        self.metadata_path = self.directory / "session.json"

        metadata = {
            "session_started": timestamp(),
            "vendor_id": PS3_VID,
            "product_id": PS3_PID,
            "device_count": len(devices),
            "devices": [serializable_device(d) for d in devices],
            "mode": "PASSIVE_RECEIVE_ONLY",
            "outgoing_hid_reports": False,
            "outgoing_feature_reports": False,
            "outgoing_control_commands": False,
            "battery_polling_command": False,
            "notes": [
                "Only HID input reports are read.",
                "No HID output or feature writes are performed.",
                "0xB0 battery/status information is decoded from incoming telemetry.",
            ],
        }
        self.metadata_path.write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def log(self, payload: dict[str, Any]) -> None:
        with self.lock:
            with self.capture_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def dump_descriptor(self, index: int, device_info: dict[str, Any]) -> None:
        path = device_info.get("path")
        if not path:
            return

        page = int(device_info.get("usage_page") or 0)
        usage = int(device_info.get("usage") or 0)
        target = self.directory / (
            f"collection_{index:02d}_page_{page:04X}_usage_{usage:04X}_descriptor.txt"
        )

        device = hid.device()
        try:
            device.open_path(path)
            raw = bytes(device.get_report_descriptor())
            decoded = decode_descriptor(raw)

            with target.open("w", encoding="utf-8") as handle:
                handle.write("PS3 Wireless Stereo Headset HID Report Descriptor\n")
                handle.write("=" * 72 + "\n")
                handle.write(
                    f"VID/PID: {PS3_VID:04X}:{PS3_PID:04X}\n"
                    f"Interface: {device_info.get('interface_number', '-')}\n"
                    f"Usage Page: 0x{page:04X}\n"
                    f"Usage: 0x{usage:04X}\n"
                    f"Length: {len(raw)} bytes\n\n"
                )
                handle.write("RAW\n---\n")
                handle.write(hex_bytes(raw) + "\n\n")
                handle.write("DECODED STRUCTURE\n------------------\n")
                handle.write("\n".join(decoded) if decoded else "No decoded items")
                handle.write("\n")

            print(
                f"  [DESC] C{index:02d} page=0x{page:04X} "
                f"len={len(raw)} -> {target.name}"
            )
        except Exception as exc:
            target.write_text(f"Descriptor read failed: {exc}\n", encoding="utf-8")
            print(f"  [DESC] C{index:02d} FAILED: {exc}")
        finally:
            try:
                device.close()
            except Exception:
                pass

    def print_status(self, status: dict[str, Any]) -> None:
        changed = status != self.last_status
        if not changed:
            return
        self.last_status = dict(status)

        battery = "CHARGING" if status["charging"] else (
            f"{status['battery_percent']}%"
            if status["battery_percent"] is not None
            else "UNKNOWN"
        )

        print(
            "[STATUS] "
            f"HEADSET={'ON' if status['headset_connected'] else 'OFF'} | "
            f"VSS={'ON' if status['vss'] else 'OFF'} | "
            f"MIC={'MUTED' if status['mic_muted'] else 'ON'} | "
            f"BATTERY={battery} | "
            f"MODEL={status['model']} | "
            f"FLAGS=0x{status['flags']:02X} | "
            f"RAW={status['raw']}"
        )

    def worker(self, index: int, device_info: dict[str, Any]) -> None:
        path = device_info.get("path")
        if not path:
            return

        page = int(device_info.get("usage_page") or 0)
        usage = int(device_info.get("usage") or 0)
        stats = CollectionStats()
        self.stats[index] = stats
        device = hid.device()

        try:
            device.open_path(path)
            device.set_nonblocking(True)
            print(
                f"  [OPEN] C{index:02d} page=0x{page:04X} "
                f"({USAGE_PAGE_NAMES.get(page, 'Vendor/Other')}) usage=0x{usage:04X}"
            )

            while not self.stop_event.is_set():
                try:
                    reports = device.read(512)
                except Exception as exc:
                    stats.errors += 1
                    self.log({
                        "timestamp": timestamp(),
                        "type": "read_error",
                        "collection": index,
                        "usage_page": page,
                        "usage": usage,
                        "error": str(exc),
                    })
                    time.sleep(0.25)
                    continue

                if reports:
                    report = bytes(reports)
                    changed = stats.update(report)
                    record = {
                        "timestamp": timestamp(),
                        "type": "input_report",
                        "collection": index,
                        "usage_page": page,
                        "usage": usage,
                        "report_length": len(report),
                        "hex": hex_bytes(report),
                        "changed_from_previous": changed,
                    }

                    status = decode_b0_status(report)
                    if status is not None:
                        record["decoded_b0"] = status
                        with self.lock:
                            self.print_status(status)

                    self.log(record)

                    if changed:
                        print(
                            f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] "
                            f"C{index:02d} PAGE=0x{page:04X} "
                            f"LEN={len(report):03d}  {hex_bytes(report)}"
                        )

                time.sleep(0.003)

        except Exception as exc:
            stats.errors += 1
            self.log({
                "timestamp": timestamp(),
                "type": "open_error",
                "collection": index,
                "usage_page": page,
                "usage": usage,
                "error": str(exc),
            })
            print(f"  [OPEN] C{index:02d} FAILED: {exc}")
        finally:
            try:
                device.close()
            except Exception:
                pass

    def start(self) -> None:
        print(f"\nCapture directory: {self.directory}")
        print("Mode: PASSIVE RECEIVE ONLY")
        print("Outgoing HID reports: DISABLED")
        print("Outgoing feature/control commands: DISABLED")
        print("Battery polling command: DISABLED")
        print("\nDumping HID report descriptors...\n")

        for index, device_info in enumerate(self.devices):
            self.dump_descriptor(index, device_info)

        print("\nStarting passive HID readers...\n")
        for index, device_info in enumerate(self.devices):
            thread = threading.Thread(
                target=self.worker,
                args=(index, device_info),
                name=f"ps3-hid-reader-{index}",
                daemon=True,
            )
            thread.start()
            self.threads.append(thread)

        print("\nListening for incoming information. No command input is accepted.")
        print("Press Ctrl+C to stop and save the summary.\n")

    def stop(self) -> None:
        self.stop_event.set()
        for thread in self.threads:
            thread.join(timeout=1.0)

        summary = {
            "session_ended": timestamp(),
            "mode": "PASSIVE_RECEIVE_ONLY",
            "outgoing_hid_reports": False,
            "outgoing_feature_reports": False,
            "outgoing_control_commands": False,
            "battery_polling_command": False,
            "collections": [],
        }

        for index, stats in sorted(self.stats.items()):
            summary["collections"].append({
                "collection": index,
                "reports": stats.reports,
                "bytes_received": stats.bytes_received,
                "changed_reports": stats.changed_reports,
                "errors": stats.errors,
                "last_report_at": stats.last_report_at,
                "length_distribution": {
                    str(k): v for k, v in sorted(stats.lengths.items())
                },
            })

        (self.directory / "summary.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        print("\n" + "=" * 88)
        print("PASSIVE CAPTURE SUMMARY")
        print("=" * 88)
        for item in summary["collections"]:
            print(
                f"C{item['collection']:02d}: "
                f"reports={item['reports']} "
                f"changed={item['changed_reports']} "
                f"bytes={item['bytes_received']} "
                f"errors={item['errors']}"
            )
        print(f"\nSaved: {self.directory}")


def find_devices(all_hid: bool) -> list[dict[str, Any]]:
    if all_hid:
        return hid.enumerate()
    return hid.enumerate(PS3_VID, PS3_PID)


def print_devices(devices: list[dict[str, Any]]) -> None:
    print("=" * 88)
    print("PS3 WIRELESS STEREO HEADSET - PASSIVE INFORMATION RECEIVER")
    print("=" * 88)
    print(
        f"Target VID/PID: {PS3_VID:04X}:{PS3_PID:04X} | "
        "INPUT ONLY | NO HID OUTPUT"
    )

    for index, device in enumerate(devices):
        page = int(device.get("usage_page") or 0)
        usage = int(device.get("usage") or 0)
        print(f"\n[{index}] HID Interface")
        print(
            f"  VID/PID     : {fmt_hex(device.get('vendor_id'))}:"
            f"{fmt_hex(device.get('product_id'))}"
        )
        print(f"  Manufacturer: {device.get('manufacturer_string') or '-'}")
        print(f"  Interface   : {device.get('interface_number', '-')}")
        print(f"  Usage Page  : {fmt_hex(page)} ({USAGE_PAGE_NAMES.get(page, 'Vendor/Other')})")
        print(f"  Usage       : {fmt_hex(usage)}")
        print(f"  Path        : {device.get('path')}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Passive receive-only HID analyzer for the Sony PS3 Wireless Stereo Headset."
    )
    parser.add_argument(
        "--all-hid",
        action="store_true",
        help="Read all HID devices instead of only VID/PID 12BA:0035.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent / "logs",
        help="Directory for capture sessions.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    print("\nScanning HID devices...\n")
    devices = find_devices(args.all_hid)

    if not devices:
        if args.all_hid:
            print("No HID devices found.")
        else:
            print(
                f"No HID device found for VID/PID {PS3_VID:04X}:{PS3_PID:04X}."
            )
        return 1

    print_devices(devices)

    capture = PassiveCapture(args.output, devices)
    capture.start()

    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nStopping passive receiver...")
    finally:
        capture.stop()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
