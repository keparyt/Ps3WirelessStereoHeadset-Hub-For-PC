# HID interface diagnostic workflow

Run from the repository root with the existing Python 3.11 environment:

```bat
python -m pip install -r requirements.txt
python poc\ps3_headset_interface_diagnostic.py --vid 0x12BA
```

To inspect every collection, including Consumer Control and Generic Desktop:

```bat
python poc\ps3_headset_interface_diagnostic.py --vid 0x12BA --all
```

To scan all HID devices (use carefully because this can include keyboards,
mice, and controllers):

```bat
python poc\ps3_headset_interface_diagnostic.py --all
```

The tool prints and saves:

- VID/PID
- `usage_page`
- `usage`
- input and feature report lengths
- HID path
- raw input reports as hexadecimal
- report length and report ID
- whether the report starts with or contains `B0`

Each run creates a timestamped directory under `captures/` containing
`devices.json` and `capture.jsonl`. The tool is receive-only: it does not send
output reports, feature reports, or control requests.

For the CUHYA-0080 receiver, test headset connection/power, volume +/-,
microphone mute, VSS, and chat balance while the tool is running. Share the
`devices.json` output and relevant `capture.jsonl` lines for protocol mapping.
