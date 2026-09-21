# PS3 Wireless Stereo Headset Hub for Windows — Technical Documentation

This document describes the architecture, reverse-engineering work, HID protocol research, diagnostic tools, capture system, and development methodology used by the project.

It is intended for developers, reverse engineers, contributors, and AI coding agents who need to understand the project before modifying it.

> **Important:** This document distinguishes observed behavior from interpretations. Reverse-engineered fields marked unknown or provisional should not be treated as confirmed protocol specifications.

---

# 1. Project background

This project exists because the developer owns a collection of Sony PlayStation Wireless Stereo Headsets and wanted to use them on Windows with an experience closer to what the hardware had on PlayStation.

The original motivation was simple:

**The headset should feel like a real device, not merely a USB peripheral.**

The long-term concept is therefore a Windows headset hub capable of exposing headset information and controls, while potentially extending the original console experience with PC functionality such as media/music controls and Windows integration.

The first technical problem was that the headset's USB receiver had to be understood before a reliable application could be built.

The project therefore started as a passive HID reverse-engineering PoC.

---

# 2. Design principles

The current research follows several principles:

1. **Observe before writing.**
2. **Separate protocol decoding from hardware I/O.**
3. **Preserve raw captures.**
4. **Do not call an interpretation confirmed without evidence.**
5. **Keep diagnostic paths independent where possible.**
6. **Make captured behavior reproducible through tests.**
7. **Build application features on top of verified protocol behavior.**

Architecture:

```
                 ┌───────────────────────┐
                 │       Hardware        │
                 │ Sony Headset + Dongle │
                 └───────────┬───────────┘
                             │
                             ▼
                 ┌───────────────────────┐
                 │ Windows USB / HID     │
                 └───────────┬───────────┘
                             │
              ┌──────────────┴──────────────┐
              │                             │
              ▼                             ▼
       Direct HID path               Raw Input path
              │                             │
              └──────────────┬──────────────┘
                             ▼
                    Raw HID reports
                             │
                             ▼
                    Protocol decoder
                             │
                             ▼
                     Application state
                             │
                             ▼
                    Windows application
```

---

# 3. Known hardware

The primary known receiver is:

| Property | Value |
|---|---|
| Vendor ID | `0x12BA` |
| Product ID | `0x0035` |
| Manufacturer | Sony Interactive Entertainment |
| Transport | USB |
| Device class of interest | HID |

The receiver exposes multiple HID collections.

This matters because the USB receiver is not necessarily represented by one simple HID endpoint. Different collections can have different usage pages, usages, descriptors, and behavior.

---

# 4. HID collection discovery

The PoC uses HID enumeration to discover all collections belonging to the target VID/PID.

Conceptually:

```
hid.enumerate(0x12BA, 0x0035)
             │
             ▼
       collection list
             │
      ┌──────┼──────┐
      ▼      ▼      ▼
    HID C0  HID C1  HID C2 ...
      │
      ├── interface number
      ├── usage page
      ├── usage
      ├── HID path
      ├── manufacturer
      └── product
```

The diagnostic output intentionally records these values because the usage page and usage are essential when determining which collection carries a particular report.

---

# 5. Two Windows input paths

The project contains two conceptually different ways to investigate incoming reports.

## 5.1 Direct HID access

The native Windows reader uses Windows file APIs against the HID device path.

The intended flow is:

```
HID path
   │
   ▼
CreateFileW()
   │
   ├── GENERIC_READ
   ├── shared read
   ├── shared write
   └── FILE_FLAG_OVERLAPPED
   │
   ▼
ReadFile()
   │
   ▼
incoming report
```

The reader uses overlapped I/O so that shutdown and device removal do not leave the application permanently blocked inside a synchronous read.

When stopping:

```
stop()
  │
  ▼
CancelIoEx()
  │
  ▼
reader exits
  │
  ▼
handle cleanup
```

The PoC does not request HID write access for the receive-only research path.

---

# 6. Windows Raw Input path

The Raw Input probe provides an independent diagnostic path.

It registers HID usages with:

```
0x000C / 0x0001
0xFF00 / 0x0001
0xFF03 / 0x0020
0xFF01 / 0x0020
```

The simplified flow is:

```
RegisterRawInputDevices()
          │
          ▼
       Windows
          │
          ▼
       WM_INPUT
          │
          ▼
GetRawInputData()
          │
          ▼
   device information
          │
          ▼
 VID/PID filtering
          │
          ▼
      raw payload
```

This path is particularly useful when direct HID enumeration/opening succeeds but the direct reader receives no reports.

The two paths should therefore not be treated as identical implementations. They are diagnostic views into different Windows input mechanisms.

---

# 7. Receive-only safety boundary

The initial reverse-engineering implementation intentionally has a strict direction:

```
HEADSET → RECEIVER → WINDOWS → PROJECT
```

Not:

```
PROJECT → WINDOWS → RECEIVER → HEADSET
```

The PoC does not currently send:

- HID output reports
- HID feature reports
- arbitrary control commands
- battery polling commands

This is intentional.

The project first establishes what the hardware sends naturally before investigating commands that alter device state.

---

# 8. B0 status report

The known status packet is an 8-byte input report beginning with `0xB0`.

```
Offset       0     1     2     3     4     5     6     7
            ┌─────┬─────┬─────┬─────┬─────┬─────┬─────┬─────┐
            │ B0  │ VV  │ CC  │ BB  │ FF  │ XX  │ 11  │ 00  │
            └─────┴─────┴─────┴─────┴─────┴─────┴─────┴─────┘
```

Current interpretation:

| Offset | Field | Current interpretation |
|---:|---|---|
| 0 | Report ID | `0xB0` |
| 1 | VV | Volume raw value |
| 2 | CC | Chat/game balance raw value |
| 3 | BB | Battery / charging value |
| 4 | FF | State flags |
| 5 | XX | Unknown |
| 6 | `0x11` | Observed constant |
| 7 | `0x00` | Observed constant |

---

# 9. Volume

The current protocol implementation recognizes a raw volume range of:

```
00
01
02
03
04
05
```

The application may present this as a user-friendly scale.

Important distinction:

```
raw protocol value ≠ necessarily UI percentage
```

A protocol field should remain represented in its raw form internally whenever possible, with presentation conversion happening at the application layer.

---

# 10. Chat/game balance

Byte 2 is currently interpreted as chat/game balance.

The protocol layer keeps both the raw value and the decoded representation.

This is important because the exact semantics and scaling should remain traceable to the packet instead of being hidden by UI formatting.

---

# 11. Battery and charging

Byte 3 represents battery information.

The currently observed range is:

```
0x00 → 0%
...
0x64 → 100%
```

A special observed value:

```
0x80 → charging
```

Therefore:

```
0x80 ≠ 128%
```

The decoder explicitly handles this state separately.

Future captures should be used to determine whether additional charging states exist.

---

# 12. Flag byte

Byte 4 contains several state flags.

Current decoder interpretation:

| Bit | Meaning |
|---:|---|
| 0 | VSS enabled |
| 1 | Microphone muted |
| 2 | Unknown |
| 3 | Headset linked |
| 4 | Unknown |
| 5 | Unknown |
| 6–7 | Headset family/mode information |

Visual representation:

```
FF
│
├── bit 0 ── VSS
├── bit 1 ── microphone mute
├── bit 2 ── unknown
├── bit 3 ── headset link
├── bit 4 ── unknown
├── bit 5 ── unknown
└── bits 6-7 ── family/mode
```

The presence of a decoded field does not automatically mean every semantic detail is permanently confirmed. New captures should be compared against the existing interpretation.

---

# 13. Unknown byte 5

Byte 5 remains unresolved.

This is exactly the kind of field that should not be silently assigned a meaning.

The preferred workflow is:

```
Record raw value
      ↓
Change one headset property
      ↓
Record again
      ↓
Compare
      ↓
Determine whether byte changes
      ↓
Repeat across multiple states
      ↓
Form hypothesis
      ↓
Test hypothesis
```

A future protocol change should only assign a semantic name once there is sufficient evidence.

---

# 14. Reverse-engineering methodology

The project's research method is based on controlled experiments.

For each experiment:

### Step 1 — Capture baseline

Record several packets while the headset is idle.

### Step 2 — Change exactly one state

Examples:

- change volume
- mute microphone
- unmute microphone
- change game/chat balance
- toggle VSS
- connect/disconnect headset
- connect charger
- remove charger

### Step 3 — Capture again

Record the exact bytes.

### Step 4 — Diff

Compare the reports byte-by-byte.

### Step 5 — Repeat

Repeat the same experiment several times.

### Step 6 — Cross-check

Verify the behavior across different headset states and, where possible, different hardware.

### Step 7 — Encode

Only then add the interpretation to the protocol decoder.

---

# 15. Capture architecture

The PoC saves sessions to:

```
poc/logs/
```

A typical session:

```
poc/logs/<timestamp>/
├── session.json
├── capture.jsonl
├── summary.json
└── descriptor data
```

## JSONL

Each incoming report can be represented as an individual JSON object.

Conceptually:

```json
{
  "timestamp": "...",
  "type": "raw_input_hid",
  "vendor_id": 4778,
  "product_id": 53,
  "usage_page": 65281,
  "payload_length": 8,
  "hex": "B0 ..."
}
```

The raw hexadecimal packet is intentionally preserved.

---

# 16. Why raw captures matter

Decoded data is convenient:

```
Battery = 73%
VSS = ON
Mic = MUTED
```

But the raw packet is more valuable for research:

```
B0 04 4A 49 0B XX 11 00
```

A future decoder may discover that `XX` contains another state.

If the raw data was discarded, that discovery would require reproducing the original experiment.

Therefore:

**Never rely exclusively on decoded telemetry when creating research captures.**

---

# 17. Protocol decoder architecture

The protocol decoder is deliberately kept separate from device access.

```
               HID reader
                   │
                   ▼
              bytes: B0 ...
                   │
                   ▼
             decode_b0()
                   │
                   ▼
       structured Python state
                   │
          ┌────────┴────────┐
          ▼                 ▼
        GUI               tests
```

This provides three major benefits:

1. Hardware access can change without rewriting protocol logic.
2. Captures can be replayed offline.
3. Protocol behavior can be unit-tested.

---

# 18. Application architecture

The intended application architecture is layered.

```
┌─────────────────────────────────┐
│          Presentation           │
│          Windows GUI             │
├─────────────────────────────────┤
│        Application State         │
├─────────────────────────────────┤
│       Headset capabilities       │
├─────────────────────────────────┤
│        Protocol decoder          │
├─────────────────────────────────┤
│          HID backend             │
├─────────────────────────────────┤
│         Windows HID API          │
└─────────────────────────────────┘
```

The important rule is:

**The GUI should not contain protocol parsing logic.**

Protocol behavior belongs in the protocol layer.

---

# 19. Diagnostic GUI

The PoC dashboard is intentionally verbose.

Its job is to distinguish:

```
USB detected?
     ↓
HID collection discovered?
     ↓
HID handle opened?
     ↓
Reader running?
     ↓
Reports arriving?
     ↓
B0 reports arriving?
     ↓
Protocol decoded?
```

This is much more useful than a simple:

```
Headset: Connected
```

when debugging a device.

---

# 20. Diagnostic state

The live monitor tracks information such as:

- dongle presence
- headset connection
- VSS
- microphone state
- battery
- charging
- volume
- chat balance
- model/family
- total HID reports
- B0 reports
- bytes received
- last report
- report collection
- last error
- report age

The GUI can therefore distinguish a disconnected receiver from an enumerated receiver that simply has not produced a new status packet.

---

# 21. Event-driven behavior

An important development detail is that the protocol should not be assumed to continuously poll the headset.

The PoC explicitly treats incoming reports as event-driven.

For example:

```
Headset idle
    │
    └── possibly no new report

Change volume
    │
    ▼
receiver sends report
    │
    ▼
application decodes it
```

This is why testing should deliberately change headset properties rather than waiting indefinitely for traffic.

---

# 22. Raw Input device filtering

The Raw Input probe receives Windows HID messages and then checks the originating device.

The filtering concept is:

```
WM_INPUT
   │
   ▼
GetRawInputData()
   │
   ▼
GetRawInputDeviceInfoW()
   │
   ▼
VID/PID
   │
   ├── 12BA:0035 → process
   │
   └── other → ignore
```

This prevents unrelated keyboards, mice, controllers, and other HID devices from polluting the headset capture.

---

# 23. Logging philosophy

Diagnostic messages should make the failure stage obvious.

Examples:

```
[ENUM]
[READ]
[HID]
[PROTO]
[USB]
[SCAN]
[SHUTDOWN]
```

This allows a log to be read chronologically:

```
[ENUM] receiver found
[ENUM] collection found
[READ] opening collection
[READ] reader started
[HID] report received
[PROTO] B0 decoded
```

or:

```
[ENUM] receiver found
[READ] opening collection
[READ][ERROR] ...
```

which immediately narrows the problem to the HID read stage.

---

# 24. Testing

Protocol tests should operate independently from physical hardware.

Preferred structure:

```
Captured bytes
      │
      ▼
decode_b0()
      │
      ▼
expected structured state
      │
      ▼
pytest
```

Tests should cover:

- valid B0 reports
- invalid lengths
- non-B0 reports
- battery percentage
- charging
- volume
- chat balance
- VSS flag
- microphone mute flag
- headset link flag
- unknown bits
- edge values

Hardware integration tests can then be separate from pure decoder tests.

---

# 25. What is known vs unknown

## High-confidence observed behavior

- Target receiver VID/PID
- Presence of multiple HID collections
- 8-byte B0 status reports
- B0 report identifier
- battery field behavior
- charging marker
- volume field
- chat/game balance field
- several flag meanings

## Still requiring investigation

- byte 5
- all unused flag bits
- complete family/mode semantics
- every HID collection
- complete output protocol
- pairing protocol
- command/feature reports
- whether all headset revisions behave identically
- full audio integration behavior

The distinction is important.

A reverse-engineering project becomes difficult to maintain when guesses gradually become documented as facts.

---

# 26. Output protocol research

The next major research stage is understanding commands sent in the opposite direction.

Current receive-only work:

```
HEADSET
   ↓
RECEIVER
   ↓
PC
```

Future control research:

```
PC
   ↓
RECEIVER
   ↓
HEADSET
```

Potential targets include:

- volume
- VSS
- microphone mute
- game/chat balance
- other headset settings

Before implementing a command, the project should establish:

1. report ID
2. report length
3. collection
4. required fields
5. valid ranges
6. state transitions
7. response/acknowledgement behavior
8. behavior on invalid values
9. behavior across headset revisions

---

# 27. Media control direction

Music/media control is a deliberate future idea.

The motivation is not necessarily that the original headset protocol contains a music-control protocol.

Instead, the Windows application can act as a bridge:

```
Headset button / detected event
          │
          ▼
Windows application
          │
          ▼
Windows media APIs
          │
          ▼
Spotify / browser / media player / etc.
```

This would allow the project to extend the headset with PC-specific functionality even where the original PlayStation implementation did not provide it.

Possible controls:

- Play/pause
- Next
- Previous
- volume
- mute
- configurable shortcuts

The exact mechanism depends on what physical controls/events can be reliably detected.

---

# 28. Future PC integration

Potential architecture:

```
             Headset
                │
                ▼
          HID receiver
                │
                ▼
        Headset Hub backend
          │      │      │
          │      │      └── Notifications
          │      │
          │      └───────── Windows media
          │
          └──────────────── GUI
```

The backend should become the central source of device state.

The UI should consume state rather than directly interrogating HID packets.

---

# 29. Recommended contributor workflow

Before changing protocol code:

1. Read this document.
2. Read `poc/ps3_headset_protocol.py`.
3. Read the HID reader implementation.
4. Inspect existing tests.
5. Inspect existing captures.
6. Determine whether the behavior is confirmed or provisional.
7. Add a regression test.
8. Only then change the decoder.

For new protocol discoveries:

```
capture
  ↓
document
  ↓
reproduce
  ↓
test
  ↓
decode
  ↓
document again
```

---

# 30. Recommended AI-agent workflow

An AI coding agent working on this repository should:

### First

Understand the distinction between:

- `app/`
- `poc/`
- protocol code
- HID I/O
- captures
- tests

### Second

Never rewrite the protocol decoder based solely on UI requirements.

### Third

Preserve raw HID values.

### Fourth

Do not invent undocumented Sony commands.

### Fifth

When adding a feature, identify the lowest layer responsible:

```
UI problem       → application/UI
state problem    → state model
decode problem   → protocol
read problem     → HID backend
Windows problem  → platform layer
```

### Sixth

Add tests for protocol changes.

---

# 31. Debugging decision tree

When the application says the headset is not working:

```
                  Start
                    │
                    ▼
          Is USB receiver visible?
              /            \
            NO              YES
            │                │
            ▼                ▼
       USB/device       Are HID collections
        problem            enumerated?
                         /          \
                       NO            YES
                       │              │
                       ▼              ▼
                    HID/driver    Can reader open?
                                    /       \
                                  NO         YES
                                  │           │
                                  ▼           ▼
                               handle      Reports?
                               problem     /    \
                                         NO      YES
                                         │        │
                                         ▼        ▼
                                     delivery   decoder
                                      issue      issue
```

This prevents jumping directly to protocol assumptions when the actual issue is device access.

---

# 32. Data model philosophy

Keep three levels separate:

### Raw

```
B0 04 4A 64 09 XX 11 00
```

### Protocol

```
{
    volume_raw: 4,
    chat_balance_raw: 74,
    battery_raw: 100,
    flags: 9
}
```

### User-facing

```
Volume: 80%
Chat balance: 74%
Battery: 100%
VSS: ON
Microphone: ON
Headset: Connected
```

This separation makes debugging substantially easier.

---

# 33. Capture-driven development

A strong future development pattern is to turn interesting captures into fixtures.

```
Real headset
     │
     ▼
Capture
     │
     ▼
fixture
     │
     ▼
unit test
     │
     ▼
decoder
     │
     ▼
application
```

This means contributors can work on protocol decoding without physically owning the exact headset revision used for the original capture.

---

# 34. Hardware compatibility

The project should not assume that every headset in the PlayStation Wireless Stereo Headset family is identical.

Future compatibility work should record:

- headset model
- headset revision
- receiver revision
- VID/PID
- HID collections
- report descriptors
- B0 captures
- observed behavior

A compatibility matrix should eventually look like:

| Hardware | Receiver | B0 | Telemetry | Output | Notes |
|---|---|---|---|---|---|
| Headset A | 12BA:0035 | ✅ | ✅ | ? | Reference |
| Headset B | 12BA:0035 | ? | ? | ? | To test |
| Headset C | ? | ? | ? | ? | To test |

---

# 35. Long-term architecture

The eventual application should ideally look like:

```
┌──────────────────────────────────────────────┐
│                 Windows App                  │
├──────────────────────────────────────────────┤
│ Dashboard │ Controls │ Media │ Diagnostics   │
├──────────────────────────────────────────────┤
│             Device State Manager             │
├──────────────────────────────────────────────┤
│              Headset Protocol                │
├──────────────────────────────────────────────┤
│          HID / Windows Backend               │
├──────────────────────────────────────────────┤
│                Windows HID                   │
└──────────────────────────────────────────────┘
                       │
                       ▼
              Sony USB Receiver
                       │
                       ▼
                Wireless Headset
```

The key idea is that the application should eventually become a real **device hub**, not simply a packet viewer.

---

# 36. Research references

The current reverse-engineering work is informed in part by:

**counter185/hid-playstation-headset**

That project documents the Sony `12BA:0035` receiver and provides an important reference for the known B0 status behavior.

The Windows implementation in this repository should continue to validate behavior against actual captures rather than blindly assuming another implementation is correct for every hardware revision.

---

# 37. Final project vision

The technical work ultimately serves a simple goal.

These headsets were designed to be more than anonymous audio hardware.

The project aims to make them feel like proper devices on Windows:

```
        OLD EXPERIENCE
             │
             ▼
   PlayStation headset UI
             │
             │  inspiration
             ▼
┌───────────────────────────┐
│ Windows Headset Hub       │
├───────────────────────────┤
│ Status                    │
│ Battery                   │
│ Volume                    │
│ Microphone                │
│ VSS                       │
│ Game / Chat               │
│ Diagnostics               │
│ Media Controls            │
│ Windows integration       │
│ Future discoveries        │
└───────────────────────────┘
```

The project is therefore both:

1. a reverse-engineering effort to understand the receiver, and
2. an application project intended to give these headsets a modern Windows experience.

The first makes the second possible.
