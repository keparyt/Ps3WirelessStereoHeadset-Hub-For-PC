#!/usr/bin/env python3
"""PS3 Wireless Stereo Headset USB/HID read-only PoC.

Enumerates HID devices and monitors raw input reports from a selected device.
This is intentionally read-only: it does not send HID reports or control the headset.

Install:
    py -m pip install hidapi

Run:
    py poc/ps3_headset_hid_monitor.py
"""

import sys
import time

try:
    import hid
except ImportError:
    print("Missing dependency: hidapi")
    print("Install with: py -m pip install hidapi")
    sys.exit(1)


def fmt_hex(value):
    return f"{int(value or 0):04X}"


def enumerate_devices():
    devices = hid.enumerate()

    print("=" * 88)
    print("PS3 WIRELESS STEREO HEADSET - HID ENUMERATION")
    print("=" * 88)

    if not devices:
        print("No HID devices found.")
        return []

    for index, device in enumerate(devices):
        print(f"\n[{index}] {device.get('product_string') or 'Unknown HID device'}")
        print(f"  VID/PID     : {fmt_hex(device.get('vendor_id'))}:{fmt_hex(device.get('product_id'))}")
        print(f"  Manufacturer: {device.get('manufacturer_string') or '-'}")
        print(f"  Serial      : {device.get('serial_number') or '-'}")
        print(f"  Interface   : {device.get('interface_number', '-')}")
        print(f"  Usage Page  : {fmt_hex(device.get('usage_page'))}")
        print(f"  Usage       : {fmt_hex(device.get('usage'))}")
        print(f"  Path        : {device.get('path')}")

    return devices


def monitor(device_info):
    path = device_info.get("path")
    if not path:
        raise RuntimeError("Selected HID device has no path.")

    print("\n" + "=" * 88)
    print("RAW HID REPORT MONITOR (READ-ONLY)")
    print("=" * 88)
    print(f"Product : {device_info.get('product_string') or '-'}")
    print(f"VID/PID : {fmt_hex(device_info.get('vendor_id'))}:{fmt_hex(device_info.get('product_id'))}")
    print("\nTry pressing VSS, mute, volume, or other headset controls.")
    print("Reports are printed only when they change. Ctrl+C stops the monitor.\n")

    device = hid.device()
    previous = None

    try:
        device.open_path(path)
        device.set_nonblocking(True)

        while True:
            report = device.read(256)
            if report:
                raw = bytes(report)
                if raw != previous:
                    timestamp = time.strftime("%H:%M:%S.%f")[:-3]
                    hex_report = " ".join(f"{byte:02X}" for byte in raw)
                    print(f"[{timestamp}] LEN={len(raw):03d}  {hex_report}")
                    previous = raw

            time.sleep(0.005)

    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        try:
            device.close()
        except Exception:
            pass


def main():
    devices = enumerate_devices()
    if not devices:
        return 1

    print("\nSelect the HID interface belonging to the PS3 headset/dongle.")
    try:
        index = int(input("Device number: ").strip())
        device = devices[index]
    except (ValueError, IndexError):
        print("Invalid device number.")
        return 1

    try:
        monitor(device)
    except Exception as exc:
        print(f"\nUnable to monitor device: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
