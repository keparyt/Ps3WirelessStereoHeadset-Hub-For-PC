#!/usr/bin/env python3
"""Enumerate and capture every HID collection for a Sony headset receiver.

Receive-only diagnostic tool. It does not send output, feature, or control
reports. It is intentionally model-agnostic and records raw bytes so unknown
CUHYA/CECHYA layouts can be mapped safely.
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import hid


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def text(value: object) -> str:
    return value.decode(errors="replace") if isinstance(value, bytes) else str(value)


def serializable(info: dict[str, Any]) -> dict[str, Any]:
    result = dict(info)
    result["path"] = text(result.get("path", ""))
    return result


def hex_bytes(data: list[int] | bytes) -> str:
    return " ".join(f"{int(value):02X}" for value in data)


def is_vendor(info: dict[str, Any]) -> bool:
    return int(info.get("usage_page") or 0) >= 0xFF00


def matches(info: dict[str, Any], vid: int | None, pid: int | None) -> bool:
    return (vid is None or int(info.get("vendor_id") or 0) == vid) and (
        pid is None or int(info.get("product_id") or 0) == pid
    )


def capture(info: dict[str, Any], output: Path, stop: threading.Event) -> None:
    path = text(info["path"])
    collection = {
        "vendor_id": int(info.get("vendor_id") or 0),
        "product_id": int(info.get("product_id") or 0),
        "usage_page": int(info.get("usage_page") or 0),
        "usage": int(info.get("usage") or 0),
        "path": path,
    }
    device = hid.device()
    try:
        device.open_path(info["path"])
        device.set_nonblocking(True)
        print(
            f"[OPEN] VID:PID={collection['vendor_id']:04X}:{collection['product_id']:04X} "
            f"PAGE=0x{collection['usage_page']:04X} USAGE=0x{collection['usage']:04X}"
        )
        while not stop.is_set():
            report = device.read(1024)
            if report:
                raw = bytes(report)
                record = {
                    "timestamp": now(),
                    "type": "input_report",
                    **collection,
                    "length": len(raw),
                    "hex": hex_bytes(raw),
                    "report_id": raw[0] if raw else None,
                    "starts_with_b0": bool(raw and raw[0] == 0xB0),
                    "contains_b0": 0xB0 in raw,
                }
                with output.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record) + "\n")
                marker = " <== B0 CANDIDATE" if raw and raw[0] == 0xB0 else ""
                print(f"[{record['timestamp']}] LEN={len(raw):03d} {hex_bytes(raw)}{marker}")
            time.sleep(0.005)
    except Exception as exc:
        record = {"timestamp": now(), "type": "error", **collection, "error": str(exc)}
        with output.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")
        print(f"[ERROR] {path}: {exc}")
    finally:
        try:
            device.close()
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vid", type=lambda value: int(value, 0), default=None, help="Optional VID, e.g. 0x12BA")
    parser.add_argument("--pid", type=lambda value: int(value, 0), default=None, help="Optional PID, e.g. 0x0035")
    parser.add_argument("--all", action="store_true", help="Include standard non-vendor HID collections")
    parser.add_argument("--output", type=Path, default=Path("captures"), help="Capture directory")
    args = parser.parse_args()

    devices = [serializable(item) for item in hid.enumerate() if matches(item, args.vid, args.pid)]
    devices.sort(key=lambda item: (int(item.get("vendor_id") or 0), int(item.get("product_id") or 0), int(item.get("usage_page") or 0), int(item.get("usage") or 0), item.get("path", "")))

    print(f"Found {len(devices)} matching HID collections")
    for index, info in enumerate(devices):
        print(
            f"[{index:02d}] VID:PID={int(info.get('vendor_id') or 0):04X}:{int(info.get('product_id') or 0):04X} "
            f"PAGE=0x{int(info.get('usage_page') or 0):04X} USAGE=0x{int(info.get('usage') or 0):04X} "
            f"INPUT={info.get('max_input_report_length', 0)} FEATURE={info.get('max_feature_report_length', 0)} "
            f"PATH={info.get('path', '')}"
        )

    selected = [item for item in devices if args.all or is_vendor(item)]
    if not selected:
        print("No vendor-defined collections selected. Use --all to capture every collection.")
        return 1

    session = args.output / datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    session.mkdir(parents=True, exist_ok=True)
    (session / "devices.json").write_text(json.dumps(devices, indent=2), encoding="utf-8")
    capture_path = session / "capture.jsonl"
    capture_path.touch()
    print(f"\nCapture directory: {session}")
    print("Mode: PASSIVE RECEIVE ONLY")
    print("Selected vendor-defined collections:")
    for info in selected:
        print(f"  PAGE=0x{int(info.get('usage_page') or 0):04X} USAGE=0x{int(info.get('usage') or 0):04X}")

    stop = threading.Event()
    threads = [threading.Thread(target=capture, args=(info, capture_path, stop), daemon=True) for info in selected]
    for thread in threads:
        thread.start()
    print("\nPress Ctrl+C after testing connection, power, volume, mute, VSS and chat controls.\n")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        stop.set()
        for thread in threads:
            thread.join(timeout=1.0)
        print(f"\nSaved: {capture_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
