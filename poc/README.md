# PS3 Wireless Stereo Headset HID PoC

This PoC is for investigating the HID traffic exposed by the Sony PS3 Wireless Stereo Headset USB dongle on Windows.

## Target device

The normal capture mode targets:

- VID: `12BA`
- PID: `0035`
- Manufacturer: Sony Interactive Entertainment
- Product: Wireless Stereo Headset

The dongle exposes multiple HID collections. In particular, vendor-defined usage pages such as `FF00`, `FF01`, and `FF03` are useful candidates for headset-specific telemetry or controls. The tool does **not** assume what any byte means yet; it records raw evidence first.

## What the PoC does

```text
USB dongle
    │
    ├── Consumer Control (0x000C)
    ├── Vendor collection (0xFF00)
    ├── Vendor collection (0xFF03)
    └── Vendor collection (0xFF01)
             │
             ▼
     Passive HID readers
             │
       ┌─────┴─────┐
       ▼           ▼
   console      JSONL logs
```

Each capture session:

1. Finds all HID collections belonging to `12BA:0035`.
2. Dumps the HID Report Descriptor for every collection.
3. Creates one passive reader thread per HID collection.
4. Records **every input report** with UTC timestamps.
5. Prints only changed reports to the console for easier live analysis.
6. Lets you add timestamped action markers for correlation.
7. Writes a summary containing report counts, byte totals, errors, and report-length distributions.

No HID output reports are sent. No feature reports are written. The PoC is intentionally passive/read-only.

## Run

From the repository root:

```powershell
python -m pip install -r requirements.txt
python poc/ps3_headset_hid_monitor.py
```

The script will automatically select the known `12BA:0035` device and all of its HID collections.

To inspect every HID device instead:

```powershell
python poc/ps3_headset_hid_monitor.py --all-hid
```

## Action correlation

While the capture is running, use the prompt to create markers immediately before or after a physical headset action:

```text
capture> idle
capture> vss
capture> mute
capture> vol+
capture> vol-
capture> power
capture> battery
capture> mark headset is connected
```

These are **human annotations**, not decoded device state.

A good first test sequence is:

```text
idle
wait 5-10 seconds
vss
press VSS
wait 2 seconds
vss
mute
press microphone mute
wait 2 seconds
mute
vol+
press volume up
vol-
press volume down
battery
continue observing without touching anything
```

Repeat each experiment individually where possible so report changes can be correlated with a single physical action.

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

### `capture.jsonl`

Contains every input report, for example:

```json
{"timestamp":"2026-08-29T18:00:00.123+00:00","type":"input_report","collection":1,"usage_page":65280,"usage":1,"report_length":8,"hex":"01 00 00 00 00 00 00 00","changed_from_previous":true}
```

### `events.jsonl`

Contains your manual action markers and their timestamps.

### Descriptor files

Each collection gets both the raw descriptor and a compact decoded summary. The decoded summary is only structural information (usage pages, report IDs, report sizes/counts, input/output/feature items, etc.). It does not claim that a particular byte is battery, VSS, mute, or another headset state.

## Reverse-engineering workflow

Use the capture data to identify correlations:

```text
Physical action
      │
      ▼
Action marker timestamp
      │
      ├───────────────┐
      ▼               ▼
Collection FF00    Collection FF03    Collection FF01
      │               │               │
      └───────────────┼───────────────┘
                      ▼
               changed reports
                      │
                      ▼
              candidate state bits
```

Do not label a byte as `battery`, `VSS`, or `mute` until repeated captures show that the same field changes consistently with the corresponding action and does not change for unrelated actions.

## Important limitation

A HID collection being present does **not** prove that it contains battery or headset telemetry. This PoC establishes whether useful data is actually being transmitted and gives us the raw evidence needed to decode it safely.
