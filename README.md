# PS3 Wireless Stereo Headset Hub for Windows

Windows tooling for the **Sony PlayStation Gold Wireless Stereo Headset** and its USB wireless receiver.

This project is based on the reverse-engineered `12BA:0035` HID behavior documented by `counter185/hid-playstation-headset`.

## Current status

The Windows implementation is currently **receive-only**. It can monitor the receiver without sending headset commands.

```text
Headset
   │ wireless
   ▼
Sony USB receiver
   │ HID input reports
   ▼
Windows HID class driver
   │
   ├── native overlapped ReadFile reader
   │
   └── Tk dashboard / raw monitor
```

### Known telemetry

The known `B0` status packet is 8 bytes:

```text
B0 VV CC BB FF XX 11 00
│  │  │  │  │  │  │  └─ observed constant
│  │  │  │  │  │  └──── observed constant
│  │  │  │  │  └─────── unknown/changing
│  │  │  │  └────────── flags
│  │  │  └───────────── battery (00-64, 80=charging)
│  │  └──────────────── sound/chat balance
│  └─────────────────── receiver volume (00-05)
└────────────────────── report ID
```

Flags currently decoded:

- bit 0: VSS enabled
- bit 1: microphone muted
- bit 3: headset linked to receiver
- bits 6-7: headset family/mode

## Windows reader

The native reader uses:

- `CreateFileW`
- `GENERIC_READ`
- shared read/write access
- overlapped `ReadFile`
- `CancelIoEx` when stopping

It never requests write access and never sends HID output/feature reports.

The overlapped implementation is important because a synchronous `ReadFile` can remain blocked when the headset is unplugged or the GUI closes.

## Run

Install the dependency:

```powershell
python -m pip install -r requirements.txt
```

Start the live dashboard:

```powershell
python poc/ps3_headset_panel.py
```

Start the raw HID monitor:

```powershell
python poc/ps3_headset_hid_monitor.py
```

If the direct HID reader does not receive reports, run the Windows Raw Input diagnostic:

```powershell
python poc/ps3_headset_rawinput_probe.py
```

That diagnostic uses a separate Windows input-delivery path and is useful for determining whether Windows itself is receiving the receiver's reports.

## Device

Known receiver:

```text
VID: 12BA
PID: 0035
Manufacturer: Sony Interactive Entertainment
```

The receiver exposes multiple HID collections, including vendor-defined collections. The monitor therefore records the collection, usage page, usage, raw report bytes, and report descriptor.

## Captures

The monitor writes sessions to:

```text
poc/logs/<timestamp>/
```

including:

- `session.json`
- `capture.jsonl`
- `summary.json`
- HID report descriptor dumps

## Development

Run protocol tests from the repository root:

```powershell
python -m pytest -q
```

The protocol decoder is deliberately isolated from device I/O so captured reports can be replayed and tested without hardware.

## Important limitation

This project does **not** yet implement headset control, pairing, audio routing, or microphone routing. The immediate goal is a reliable Windows HID telemetry layer. Once that layer is stable, additional behavior can be investigated separately and safely.

## Reference

- `counter185/hid-playstation-headset` — Linux HID driver documenting the `12BA:0035` receiver and `B0` status report.
