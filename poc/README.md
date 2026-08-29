# PS3 Wireless Stereo Headset HID PoC

This PoC investigates the HID traffic exposed by the Sony PS3 Wireless Stereo Headset USB dongle on Windows.

## Known device

The normal target is:

- VID: `12BA`
- PID: `0035`
- Manufacturer: Sony Interactive Entertainment
- Product: Wireless Stereo Headset

The receiver exposes multiple HID collections, including vendor-defined usage pages. The project also contains a live Windows panel that decodes the known `0xB0` status packet used by this headset family.

## Live status panel

Run:

```powershell
python poc/ps3_headset_panel.py
```

The panel displays the requested live state at startup and continuously updates it:

```text
┌──────────────────────┬──────────────────────┐
│ Dongle               │ Headset Link         │
│ ON / OFF             │ ON / OFF / UNKNOWN   │
├──────────────────────┼──────────────────────┤
│ Headset Power        │ VSS                  │
│ ON / OFF / UNKNOWN   │ ON / OFF / UNKNOWN   │
├──────────────────────┼──────────────────────┤
│ Microphone           │ Battery              │
│ ON / MUTED / UNKNOWN │ xx% / CHARGING       │
└──────────────────────┴──────────────────────┘
```

It also shows:

- headset model/family flag
- number of received `0xB0` status reports (`Status pings`)
- age of the last `0xB0` report
- last raw `0xB0` packet
- whether the current status is live or stale
- an explicit note that headset volume percentage is not decoded from the known status packet

### `0xB0` status format

The panel follows the public Linux HID driver for PlayStation wireless headsets:

```text
byte 0       0xB0                    status report ID
byte 1-2     device-specific          not decoded by this panel
byte 3       battery level            percentage-like value
byte 4       flags

byte 4 bit 0     VSS enabled
byte 4 bit 1     microphone mute enabled
byte 4 bit 3     headset/device link connected
byte 4 bits 6-7  family/model flag
```

The reference driver identifies `12BA:0035` as the Gold receiver and uses the `0xB0` packet to expose battery and connection information. citeturn10file0

A battery value of `0x80` is shown as `CHARGING` with no percentage because that value is treated specially by the reference implementation. citeturn10file0

### Headset power vs. link state

The known `0xB0` packet exposes a headset/device link flag, not a separately decoded power bit. Therefore the panel shows **Headset Power = ON** when a live status report says the headset is connected, and **OFF** when that live link flag is clear. When reports stop arriving, the panel changes the value to `UNKNOWN` rather than pretending it knows the exact hardware power state.

This keeps the UI useful while avoiding a false claim about a field that has not been independently decoded.

### Battery pings

The panel does not transmit a guessed battery command. It counts incoming `0xB0` status reports as `Status pings` and shows the age of the last one.

That gives a useful live diagnostic:

```text
Status pings (B0): 1532
Last B0 report: 0.2s ago
Battery: 84%
```

If the receiver stops producing status reports, the panel marks telemetry as stale.

## Volume

The known `0xB0` status packet does not provide a decoded absolute headset-volume field in this PoC. The panel therefore displays:

```text
Headset volume telemetry: not decoded
```

It does not guess a percentage from unrelated HID activity. The passive analyzer can still capture Consumer Control/other HID reports for future investigation.

## Passive HID analyzer

For full reverse-engineering captures:

```powershell
python poc/ps3_headset_hid_monitor.py
```

The analyzer:

1. Finds all HID collections for `12BA:0035`.
2. Dumps each HID report descriptor.
3. Starts one passive reader per HID collection.
4. Stores every input report in JSONL.
5. Prints changed reports live.
6. Allows timestamped action markers for correlation.
7. Writes per-collection statistics.

Capture output is saved under `poc/logs/<timestamp>/`.

Example markers:

```text
capture> idle
capture> vss
capture> mute
capture> vol+
capture> vol-
capture> power
capture> battery
capture> mark headset connected
```

These markers are human annotations; they are not decoded device states.

## Recommended test sequence

```text
idle
wait 5-10 seconds
vss
press VSS
wait 2 seconds
mute
press microphone mute
wait 2 seconds
vol+
press volume up
vol-
press volume down
battery
continue observing without touching anything
```

The live panel is useful for confirming the already-known fields immediately; the passive analyzer is useful for discovering additional fields and volume behavior.

## Capture files

Sessions are stored under `poc/logs/<timestamp>/` by default:

```text
poc/logs/
└── 20260829_150000/
    ├── session.json
    ├── capture.jsonl
    ├── events.jsonl
    ├── summary.json
    ├── collection_00_page_000C_usage_0001_descriptor.txt
    ├── collection_01_page_FF00_usage_0001_descriptor.txt
    ├── collection_02_page_FF03_usage_0020_descriptor.txt
    └── collection_03_page_FF01_usage_0020_descriptor.txt
```

## Read-only behavior

The live panel and analyzer only consume HID input data. They do not send HID output reports or feature writes. Battery percentage comes from the incoming `0xB0` status telemetry rather than from a guessed command.

## Install

From the repository root:

```powershell
python -m pip install -r requirements.txt
```

Only `hidapi` is required; the panel uses Python's built-in Tkinter GUI toolkit.
