# Engineering Report — Headset Hub upgrade

**Date:** 2026-09-29 · **Branch:** `main` · **Suite:** 136 passed, 0 failed (was 69/3)

This report separates what is **verified** (ran on this machine against real
hardware or deterministic tests) from what is **implemented but not
hardware-verified**. Nothing in it is aspirational.

---

## 1. IMPLEMENTED

### Volume system (fix)
The receiver reports **six** levels (`0x00`–`0x05`), not ten — verified against
the counter185/hid-playstation-headset reference driver and live captures; the
headset's finer internal scale is halved by the receiver.

- `ps3hub/state.py` (new): `raw_to_logical` maps raw 0..5 → logical 0, 2, 4,
  6, 8, 10; `logical_to_percent` = step × 10 exactly; `HeadsetState` (frozen
  dataclass) + `HeadsetStateTracker` with change flags.
- `HeadsetStateTracker.update` **baseline-seeds**: the first snapshot and the
  first snapshot after a re-link fire no value events (no "toasts about the
  volume the headset already had").
- The dashboard's private `volume_level * 2` duplicate is gone; both render
  paths read `state.py`.
- `poc/ps3_headset_protocol.py` corrected (`VOLUME_MAX 0x0A → 0x05` with the
  evidence trail); `TECHNICAL_README.md` §9 rewritten; `app/README.md` fixed.

### Event-driven notifications (anti-spam)
- `ps3hub/events.py` (new): `AppEvent` constants, `Event`, synchronous
  `EventBus` with per-subscriber failure isolation and bounded history.
- `ps3hub/notify_rules.py` (new): pure policy — per-type throttle (2 s
  default), battery warn-once + 15 min reminder + charging resets the latch,
  per-category switches, 10-segment `volume_meter()` block-glyph renderer.
- `ps3hub/notify_service.py` (new): bus → policy → `DesktopNotifier` wiring.
- `ps3hub/device.py`: the dispatch thread owns the tracker and publishes
  value events **only on change** (change-only publication), plus link /
  receiver / charging transitions. Identical status reports produce zero bus
  traffic.

### Windows audio DSP
- `ps3hub/audio/loopback_dsp.py`: WASAPI loopback capture (raw-ctypes COM,
  no comtypes) → biquad chain → sounddevice render. The capture client's
  `GetBuffer` prototype takes **five** out-params; the missing
  `qpcPosition` pointer corrupted the stack (fail-fast `0xC0000409`) and is
  the root cause of the earlier crashes.
- `ps3hub/audio/engine.py` (new): `AudioEngine` facade — the only audio API
  the app sees (endpoints, profiles, enable/disable, FxSound status,
  config export/import).
- `ps3hub/audio/dsp.py`: RBJ biquads; `spectrum_peak_db` now averages
  multi-channel blocks (stereo loopback can be measured directly).
- **FxSound integration** stays CLI-only (documented flags; no source
  copying, AGPL respected) — unchanged from its verified state.

### Config schema v2 + migration
`config.py`: `version: 2` adds the `audio` key (enabled, backend, per-device
profiles). v1 files migrate losslessly (only the missing key is added);
unknown future versions still load key-by-key; invalid backends degrade to
`native`. Round-trip + migration + sanitisation covered by tests.

### UI
- New **Audio** view (`view_audio.py`): enable processing, effect sliders
  (bass/clarity/ambience/surround/dynamic boost), master gain −12…+12 dB,
  live engine status, FxSound probe.
- Dashboard: new audio card (status pill + detail line fed by
  `AudioEngine.as_dict()`); volume ladder now fed by `state.py`.
- Tray tooltip shows logical **Volume N%** from the same source of truth.
- Settings: notification category toggles (connect / volume / audio).

---

## 2. AUDIO ARCHITECTURE

```
apps → Windows mixer → [IAudioClient LOOPBACK capture, default endpoint]
      → bounded queue (8 × 10 ms, drop-oldest)
      → DSP: shelf/peaking biquads (RBJ) + master gain + clip guard
      → sounddevice render (PortAudio)

device_monitor: IMMDeviceEnumerator + IMMNotificationClient (raw-vtable
COM) — endpoint list, default-device changes, push notifications.

Backends: "native" (the pipeline above) or "fxsound" (drive an installed
FxSound via CLI instead; loopback engine stays idle).
```

Real-time rules: no logging/allocations in the audio path; coefficients
recomputed only on profile change; capture and render on separate threads;
effect value 0 = filter omitted entirely (flat profile is bit-transparent).

---

## 3. VOLUME FIX — verification

| Claim | Evidence |
|---|---|
| Receiver range is 0x00–0x05 | reference driver + live captures; documented in README/TECHNICAL_README/PoC |
| Mapping raw→logical→percent | 14 unit tests in `test_state.py` (0→0 %, 3→60 %, 5→100 %) |
| Change-only events | `test_events.py`: 10 identical reports → 0 events; 1 move → exactly 1 event |
| Baseline seeding | `test_first_snapshot_is_a_seed_not_a_change` |
| Single source of truth | UI smoke drives dashboard + tray from tracker state |

---

## 4. NOTIFICATION FIX — verification

`test_notify_rules.py` (16) + `test_notify_service.py` (4) + `test_events.py`:

- rapid volume changes within the quiet period are suppressed, allowed after;
- battery warns once, reminds only after `battery_reminder_seconds`,
  charging resets the latch;
- a failing subscriber cannot take down the bus;
- category switches suppress whole categories;
- meter rendering: 50 % → exactly 5 filled of 10 segments; unknown → `--`.

---

## 5. UI — verification

`tests/smoke_ui.py` (now 51 steps, including the new Audio view, engine
provider, and dashboard audio card): **all pass** on the real Tk stack.

---

## 6. TESTS

**136 passed, 0 failed** (`python -m pytest`, app dir), up from 73:
state 14, events 7, notify_rules 16, notify_service 4, audio modules 21,
device monitor 2, plus the previous 72. UI smoke: 51 steps pass.

---

## 7. HARDWARE + AUDIO VERIFICATION (live, this machine)

Isolated topology (no FxSound in the measured path, no feedback loop):
tone → CABLE Input · engine: CABLE loopback → real output · analysis taps
the real output's loopback. Engine ran 225/246 blocks (off/on), 48 kHz
stereo, drops handled by the bounded queue:

| Band | DSP off | DSP on | Δ |
|---|---|---|---|
| 1 kHz | 10.8 | 23.4 | **+3.36 dB** |
| 60 Hz | 100.6 | 3244.6 | **+15.09 dB** |

Verdict: **PASS** — the engine measurably processes real loopback audio.
Deterministic DSP math additionally verified: flat profile transparent
(−0.00 dB); bass 10 + gain 6 dB → exactly +6.00 dB @ 1 kHz; CPU 1.8 % of
real time. `AudioDeviceMonitor` verified live: 9 endpoints enumerated with
friendly names, default flagged, MMDevice push notifications registered and
received. `FxSoundBackend` verified live against installed FxSound 1.2.13.0
(real `status.json`: power, output, preset, effects). Note: on this machine
FxSound's virtual endpoint is the Windows default; the live comparison was
therefore run through the VB-Cable isolation topology above.

---

## 8. BUILD

No new runtime dependencies. `requirements.txt` stays `hidapi>=0.14.0`;
`sounddevice` + `numpy` are optional extras the audio engine degrades
gracefully without (UI shows "Unavailable"); comtypes was removed from the
last hidden import path (dead `_PropertyKey.friendly_name`).

---

## 9. KNOWN LIMITATIONS

1. Rendering to the same endpoint being captured can re-tap our own output;
   the engine documents this and the UI default follows the FxSound model.
2. Notification toasts use `Shell_NotifyIconW` balloons (Win10+ renders them
   as toasts); no WinRT activator, so no action buttons.
3. FxSound control is one-way (CLI); no reading of arbitrary effect state
   beyond `status.json`.
4. Logical odd volume steps (1,3,5,7,9) are UI continuity only; the receiver
   never reports them.
5. The loopback engine binds to the default output at start; device changes
   are picked up via monitor notifications but a running stream restart is
   left to the user (Settings → toggle).

---

## 10. FILES CHANGED

**New:** `ps3hub/state.py`, `ps3hub/events.py`, `ps3hub/notify_rules.py`,
`ps3hub/notify_service.py`, `ps3hub/audio/engine.py`,
`ps3hub/audio/{dsp,profiles,fxsound_backend,loopback_dsp,device_monitor}.py`,
`ps3hub/ui/view_audio.py`,
`tests/{test_state,test_events,test_notify_rules,test_notify_service,test_audio_modules,test_device_monitor}.py`

**Modified:** `ps3hub/device.py` (tracker + bus), `ps3hub/config.py` (v2),
`ps3hub/ui/app.py` (audio view/engine, notification service, tray state),
`ps3hub/ui/view_dashboard.py` (state.py mapping, audio card),
`ps3hub/ui/view_settings.py` (notify toggles), `ps3hub/tray.py` (volume),
`ps3hub/audio/__init__.py` (lazy facade), `ps3hub/audio/dsp.py`
(stereo-aware analysis), `app/README.md`, `TECHNICAL_README.md` (§9),
`poc/ps3_headset_protocol.py` (0x0A → 0x05), `tests/smoke_ui.py`.

**Deleted:** all temp probes (`probe_*_tmp.py`, probe outputs).
