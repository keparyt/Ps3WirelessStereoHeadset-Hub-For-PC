# PS3 Wireless Stereo Headset HID PoC

This project is a **receive/observability PoC** for investigating what the Sony PS3 Wireless Stereo Headset USB receiver exposes to a Windows PC.

The purpose is to discover and display incoming headset telemetry. It is **not a controller application**.

## Receive-only design

The PoC does not attempt to change headset settings.

```text
PS3 Wireless Headset
        │
        │ wireless
        ▼
   Sony USB receiver
        │
        │ HID input reports
        ▼
┌─────────────────────────┐
│ PC receive-only monitor │
└────────────┬────────────┘
             │
     ┌───────┼────────┐
     ▼       ▼        ▼
 decoded   raw      statistics
 status    reports   / discovery
```

The tools:

- discover the receiver and its HID collections,
- receive HID input reports,
- decode only fields that have an identified protocol mapping,
- show unknown data as raw bytes for further investigation,
- record receive statistics and timestamps.

They do **not** send HID output reports, feature reports, control commands, or battery polling commands.

## Target device

Normal mode targets:

- VID: `12BA`
- PID: `0035`
- Manufacturer: Sony Interactive Entertainment
- Product: Wireless Stereo Headset

The receiver exposes multiple HID collections, including vendor-defined usage pages such as `FF00`, `FF01`, and `FF03`.

## Live receive panel

Run from the repository root:

```powershell
python -m pip install -r requirements.txt
python poc/ps3_headset_panel.py
```

The panel is intentionally passive and has no control actions.

It displays:

```text
┌────────────────┬────────────────┬────────────────┐
│ USB Dongle     │ Headset Link   │ Headset Power  │
│ ON / OFF       │ ON/OFF/UNKNOWN │ ON/OFF/UNKNOWN │
├────────────────┼────────────────┼────────────────┤
│ VSS            │ Microphone     │ Battery        │
│ ON/OFF/UNKNOWN │ ON/MUTED/UNK   │ xx%/CHARGING   │
└────────────────┴────────────────┴────────────────┘

B0 status packets: 1234
Total HID input reports: 4567
Last B0 report: 0.2s ago
```

It also shows the individual incoming HID collections, report counters, last received packet, and a live raw report stream. This is important because the main goal is discovering **what the receiver sends**, not controlling the headset.

## Known incoming `0xB0` status report

For `12BA:0035`, the current decoder follows the public Linux HID driver reference for this headset family.

```text
byte 0     0xB0                    status report ID
byte 1-2   device-specific          not decoded here
byte 3     battery level            percentage-like value
byte 4     flags

byte 4 bit 0 = VSS enabled
byte 4 bit 1 = microphone mute enabled
byte 4 bit 3 = headset/device link connected
byte 4 bits 6-7 = headset family/model flag
```

The special battery value `0x80` is displayed as `CHARGING` / `level unavailable` rather than as `128%`.

The panel derives **Headset Power** from the live headset-link flag. This is intentionally shown as a derived state because the known `0xB0` packet does not provide a separately verified power-on bit in this PoC.

## What we can receive

The UI separates **confirmed decoded telemetry** from **raw observed telemetry**:

| Data | Current status |
|---|---|
| USB receiver connected | ✅ Detected from HID enumeration |
| Headset wireless link | ✅ Decoded from B0 bit 3 |
| VSS | ✅ Decoded from B0 bit 0 |
| Microphone mute | ✅ Decoded from B0 bit 1 |
| Battery percentage | ✅ Decoded from B0 byte 3 |
| Charging indication | ✅ `0x80` special state |
| Headset family/model | ✅ Decoded from B0 bits 6-7 |
| Volume percentage | ❓ Not decoded yet |
| Other vendor telemetry | 🔎 Raw reports exposed for discovery |

The unknown states are deliberate. The application does not guess the meaning of unrelated bytes just because they change.

## Battery receive / ping behavior

The panel counts incoming `0xB0` reports as **status packets**. This provides a live indication that the receiver is transmitting status telemetry.

No special battery request is sent by the application. The battery display therefore represents what the receiver spontaneously provides through incoming status reports.

## Passive HID analyzer

For deeper reverse-engineering, run:

```powershell
python poc/ps3_headset_hid_monitor.py
```

This analyzer:

1. Finds all HID collections for `12BA:0035`.
2. Dumps every HID report descriptor.
3. Starts one passive reader per HID collection.
4. Stores every incoming report in JSONL.
5. Prints changed reports live.
6. Allows human action markers for correlation.
7. Produces receive statistics.

Capture output is saved under:

```text
poc/logs/<timestamp>/
```

## Discovery workflow

The best way to find more telemetry is to watch the raw input stream while performing one physical action at a time:

```text
Idle baseline
     │
     ├── press VSS ─────────► compare incoming bytes
     │
     ├── mute/unmute ───────► compare incoming bytes
     │
     ├── volume +/- ────────► compare incoming bytes
     │
     ├── power on/off ──────► compare incoming bytes
     │
     └── leave idle ────────► compare periodic reports
```

The physical actions are performed on the headset itself; the software only observes the resulting incoming HID traffic.

## Read-only protocol behavior

```text
HID device ───────► Python application

No HID report is sent back.
No feature report is written.
No control transfer is issued by the PoC.
No guessed battery request is transmitted.
```

This makes the project focused on answering one question: **what data can the PS3 Wireless Stereo Headset receiver actually provide to the PC?**
