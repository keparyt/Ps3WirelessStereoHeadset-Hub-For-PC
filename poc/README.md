# PS3 Wireless Stereo Headset HID PoC

This project is a **receive/observability PoC** for investigating what the Sony PS3 Wireless Stereo Headset USB receiver exposes to a Windows PC.

The purpose is to discover and display incoming headset telemetry. It is **not a controller application**.

## Receive-only architecture

```text
Headset ──wireless──> USB receiver ──HID input──> PC
                                          │
                           ┌──────────────┴──────────────┐
                           ▼                             ▼
                       live panel                  raw analyzer
```

The tools do **not** send headset commands. They do not send HID output reports, write feature reports, issue headset control requests, or transmit a battery-polling command.

They only open the HID interfaces and read input reports exposed by Windows.

## Known device

- VID: `12BA`
- PID: `0035`
- Manufacturer: Sony Interactive Entertainment

The receiver exposes multiple HID collections, including vendor-defined usage pages. All of them are monitored because they may contain additional incoming headset information.

## Live information panel

Run:

```powershell
python poc/ps3_headset_panel.py
```

The panel is display-only and has no headset control buttons.

```text
┌────────────────────────┬────────────────────────┐
│ USB Dongle             │ Headset Link           │
│ ON / OFF               │ ON / OFF / UNKNOWN     │
├────────────────────────┼────────────────────────┤
│ Headset Power*         │ VSS                    │
│ ON / OFF / UNKNOWN     │ ON / OFF / UNKNOWN     │
├────────────────────────┼────────────────────────┤
│ Microphone             │ Battery                │
│ ON / MUTED / UNKNOWN   │ xx% / CHARGING         │
└────────────────────────┴────────────────────────┘

Status pings (B0): 1532
Last B0 report: 0.2s ago
Last B0: B0 ...
```

`* Headset Power` is shown as a derived value from the live headset-link flag. The known `0xB0` report does not independently identify a separate hardware power bit, so the panel uses `UNKNOWN` when live telemetry is stale.

## Known incoming 0xB0 telemetry

The current decoder follows the publicly available Linux HID driver for this headset family.

```text
byte 0       0xB0                    status report identifier
byte 1-2     device-specific          preserved, not interpreted here
byte 3       battery level            percentage-like value
byte 4       flags

byte 4 bit 0      VSS enabled
byte 4 bit 1      microphone mute enabled
byte 4 bit 3      headset/device link connected
byte 4 bits 6-7   family/model flag
```

A battery value of `0x80` is treated as `CHARGING / level unavailable`, matching the reference implementation. citeturn10file0

The panel counts incoming `0xB0` reports as **status pings** and shows the age of the latest report. Nothing is transmitted to make the headset produce those reports.

## Passive HID information analyzer

Run:

```powershell
python poc/ps3_headset_hid_monitor.py
```

There is **no interactive command prompt** in the analyzer.

The analyzer only:

1. Finds the HID collections for `12BA:0035`.
2. Dumps each HID Report Descriptor.
3. Opens each collection for passive input reading.
4. Records every incoming HID input report to JSONL.
5. Prints changed incoming reports.
6. Detects and decodes known incoming `0xB0` status packets.
7. Records per-collection report statistics.

The only local interaction while it is running is `Ctrl+C` to stop the capture.

## What we are trying to discover

The known `0xB0` report already exposes useful information:

```text
             incoming B0 report
                     │
        ┌────────────┼────────────┐
        ▼            ▼            ▼
      byte 3       byte 4       other bytes
     battery       flags          ?
                    │
             ┌──────┼───────┐
             ▼      ▼       ▼
            VSS    mic     link
```

The other HID collections are intentionally captured as raw input so we can determine whether the receiver sends additional telemetry. Volume percentage is currently **not decoded**; no value is guessed from unrelated reports.

## Capture output

Sessions are stored under:

```text
poc/logs/<timestamp>/
```

Typical files:

```text
poc/logs/20260829_185531/
├── session.json
├── capture.jsonl
├── summary.json
├── collection_00_page_000C_usage_0001_descriptor.txt
├── collection_01_page_FF00_usage_0001_descriptor.txt
├── collection_02_page_FF03_usage_0020_descriptor.txt
└── collection_03_page_FF01_usage_0020_descriptor.txt
```

`capture.jsonl` contains raw incoming reports and the decoded `0xB0` fields when applicable.

## Receive-only guarantee

The analyzer and panel are designed around:

```text
INPUT FROM HEADSET → INFORMATION ON PC
```

and not:

```text
PC → HEADSET CONTROL
```

No HID output/feature writes or control commands are performed by these PoCs.

## Install

From the repository root:

```powershell
python -m pip install -r requirements.txt
```

Only `hidapi` is required. The panel uses Python's built-in Tkinter GUI toolkit.
