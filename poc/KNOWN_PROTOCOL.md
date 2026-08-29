# Known protocol: Sony PlayStation Gold Wireless Stereo Headset CUHYA-0080 [NO60]

This document records the currently known **receive-side** information for the hardware being investigated.

## Hardware identity

- Headset: **Sony PlayStation Gold Wireless Stereo Headset**
- Headset model: **CUHYA-0080**
- User-provided marking: **[NO60]**
- Sony wireless adapter: **CUHYA-0081**
- USB receiver seen by Windows: **VID 0x12BA / PID 0x0035**
- Receiver manufacturer string: **Sony Interactive Entertainment**

Sony's support documentation lists CUHYA-0080 as the Gold Wireless Headset model, and the CUHYA-0080 manual identifies the supplied wireless adapter as CUHYA-0081.

## Known incoming status report

A public reverse-engineered Linux HID driver for receiver `12BA:0035` documents an 8-byte status report sent by the receiver when headset properties change.

```text
B0 VV CC BB FF XX 11 00
│  │  │  │  │  │  │  └── observed constant 0x00
│  │  │  │  │  │  └───── observed constant 0x11
│  │  │  │  │  └──────── unknown changing byte
│  │  │  │  └─────────── flags
│  │  │  └────────────── battery
│  │  └───────────────── sound/chat balance
│  └──────────────────── volume level
└──────────────────────── status report ID
```

### Byte meanings

| Byte | Known meaning | Values |
|---|---|---|
| 0 | Status report ID | `0xB0` |
| 1 | Receiver-reported volume level | `0x00`–`0x05` (6 levels; headset itself has more physical steps) |
| 2 | Sound/chat balance | `0x00`–`0x64` (100 max; documented as changing in `0x08` steps) |
| 3 | Battery level | `0x00`–`0x64` = 0–100%; `0x80` while charging |
| 4 bit 0 | VSS enabled | `0` / `1` |
| 4 bit 1 | Microphone muted | `0` / `1` |
| 4 bit 3 | Headset connected to receiver | `0` / `1` |
| 4 bits 6–7 | Family/mode flags | Gold V2 uses the `01` family value in the reference driver |
| 5 | Unknown | Changes; exact purpose not established |
| 6 | Unknown/observed constant | `0x11` in the reference sample |
| 7 | Unknown/observed constant | `0x00` in the reference sample |

The repository decoder exposes both the interpreted values and the raw bytes so unknown fields are not lost.

## Important receive behavior

The reference driver describes the `B0` status report as **receiver-generated telemetry when a property changes**. It should therefore not be treated as a periodic battery heartbeat.

That means a passive listener can correctly sit idle for a while without receiving anything. Turning the headset on, changing VSS, changing microphone mute, changing volume, or changing sound/chat balance are useful opportunities for a status packet to appear.

No command is required by this project to obtain the known status packet. The project does not transmit a battery query or any other headset control request.

## What the project can currently display

```text
USB receiver       detected / not detected
Headset link       ON / OFF / UNKNOWN
Headset power      derived from incoming link status
VSS                ON / OFF / UNKNOWN
Microphone         ON / MUTED / UNKNOWN
Battery            0–100% / CHARGING / UNKNOWN
Volume             0–5 / UNKNOWN
Sound/Chat balance 0–100% / UNKNOWN
Raw B0             complete packet
Raw HID            every received HID input report
```

## Sources

1. Sony PlayStation support: CUHYA-0080 is listed as the Gold Wireless Headset model.
2. Sony CUHYA-0080 instruction manual: the headset status display includes VSS, volume, microphone state, and battery charge level; the supplied wireless adapter is CUHYA-0081.
3. `counter185/hid-playstation-headset`: public reverse-engineered Linux HID driver documenting receiver `12BA:0035` and the 8-byte `0xB0` status packet.

Reference repository:
https://github.com/counter185/hid-playstation-headset
