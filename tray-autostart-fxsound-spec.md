# Spec — Tray autostart on install + combined FxSound/Hub launch

**Short name:** `tray-autostart-fxsound`
**Status:** Partly implemented. The Audio-page no-access overlay (R15/R16, §7.5) **ships in 1.2.15**. The tray-autostart / combined-launch items (R1–R14, §7.1–§7.4) are still to do.
**Author:** interview with the project owner
**Target release:** next patch of the current line (`1.2.14` → `1.2.15`), tagged `v1.2.15`
**Date:** 2026-10-05

---

## 1. Original request (verbatim)

> Check the source code, i want to make so when its installed, in the installer, you make so it run in background on startup as tray, and make sure, when i launch it and fxsound is not started, it only start fxsound , i have to start it again to finally launch the hub but ye..

Three asks are bundled here:

1. **Installer → background/tray autostart.** The installed Hub must start in the background, in the system tray, when Windows signs in.
2. **One launch must do both.** Today (per the owner's experience) launching the Hub while FxSound is stopped "only starts FxSound", and a *second* launch is needed before the Hub itself appears. A single launch should start FxSound **and** bring the Hub up.
3. **Audio page → no-access overlay.** "cover the whole page, of audio zone that require fxsound, when fxsound is not there, like a no-access overlay, since we cant even interact with fxsound" + "propose to start/install it when this overlay is there" + "when i go look at it and fx is not available just show the page overlay over the audio page, with the buttons that it, no need pop-up prompt".

---

## 2. What the code does today (verified)

| Area | Current behaviour | Where |
|---|---|---|
| Tray mode | `--tray` starts the tray icon, `withdraw()`s the window, hides on window-close, and exits only from the tray menu's **Exit** | `app/ps3hub/__main__.py:20-51`, `app/ps3hub/ui/app.py:185-212`, `app/ps3hub/ui/app.py:757-763` |
| Tray menu | Open / Hide / Reconnect · Refresh / Exit; single or double click on the icon = Open | `app/ps3hub/tray.py:810-840` |
| `start_minimized` setting | Only iconifies when **not** in tray mode (`config.py:45`) | `app/ps3hub/ui/app.py:209-210` |
| Installer autostart | Writes `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` value `PS3PS3HeadsetHub` = `"<install>\PS3HeadsetHub.exe" --tray`, checkbox **"Start with Windows (in the system tray)"**, default on | `app/packaging/installer_gui.py:61-62, 179-186, 421-429` |
| Zip installer autostart | Same entry built as `"PS3$AppSlug"`, `-NoAutostart` opts out | `app/packaging/install.ps1:44, 230-241` |
| Uninstall | Removes the Run value, shortcuts and the program folder; settings in `%APPDATA%\PS3HeadsetHub` kept | `installer_gui.perform_uninstall`, `install.ps1:175-177` (`-Uninstall`) |
| FxSound start | **Only on demand from the Audio page** ("Start FxSound" / `_require_fxsound` → `engine.fxsound_launch()`); nothing starts FxSound at Hub startup | `app/ps3hub/ui/view_audio.py:487-560`, `app/ps3hub/audio/engine.py:235-248` |
| FxSound launch mechanics | `launch()` = hidden `Popen` behind a focus guard; `wait_until_running(timeout=10.0)` polls `--status` | `app/ps3hub/audio/fxsound_backend.py:332-395` |
| FxSound adopt (load, never push) | `adopt_fxsound_status()` / `fxsound_page_opened()` mirror FxSound's live EQ/effects/preset into the profile | `engine.py:288-330` |
| Single instance | **None.** Every launch is a new process | — |
| Post-install launch | Only a **Launch** button (`os.startfile(exe)`), nothing automatic | `installer_gui.py:553-560` |
| Audio page gating | FxSound-dependent equalizer controls are only set to **`state="disabled"`** — no overlay, no scrim, nothing covering them, and they stay visible in place | `app/ps3hub/ui/view_audio.py:468-485` |
| Gated control still reachable | Keyboard focus / a stray click path can still land on the disabled zone, and the EQ is gated even when the Hub's own DSP (not FxSound) is the active processor | `view_audio.py:453-485` |
| Using a gated feature | Raises a yes/no dialog ("Start FxSound now?") instead of a single, coverable surface | `view_audio.py:487-535`, `_start_and_settle` `:536` |

**Consequence:** the *installer* half of the request is already implemented; the parts that are genuinely missing are (a) verification/repair of that entry, (b) starting FxSound as part of a Hub start, (c) single-instance so the "second launch" confusion disappears, and (d) an automatic post-install tray launch.

---

## 3. Requirements

### R1 — Tray autostart on install (verify + repair)
- Both installers keep writing `HKCU\...\Run\PS3PS3HeadsetHub` = `"<install dir>\PS3HeadsetHub.exe" --tray`, default on.
- After writing, the installer **reads the value back** and **repairs** it if it is missing or points at a stale path (re-install / upgrade / moved target dir) — `install.ps1` and the GUI installer stay in sync.
- Uninstall removes it; the value name is identical everywhere.
- Acceptance: after a fresh install, the registry value exists with `--tray`, and after sign-out/sign-in the Hub is running with **no window** and a working tray icon and tooltip.

### R2 — One launch starts FxSound and the Hub
- Launching the Hub (window mode **or** `--tray`) with FxSound **installed but not running** starts FxSound (hidden, focus-guarded, existing `launch()`), waits up to the existing **10 s** for it to answer, and then continues starting the Hub normally.
- **One launch only.** No second launch, no restart.
- When FxSound is already running, nothing is spawned and launching is unchanged.

### R3 — Applies to every Hub start
- The FxSound start runs on **both** paths: sign-in autostart (`--tray`) and a manual launch.
- Gated by a new setting (§5) default **on**, and by a new CLI flag (§6).

### R4 — The Hub appears immediately; the FxSound wait is off the UI thread
- The Hub's window/tray icon appears as soon as it does today; the FxSound start + wait + adopt runs on a **daemon thread**, and results are marshalled onto the Tk thread through the existing `HubApp._ui_requests` queue (`app.py:103, 439-490`).
- No splash screen, no blocking wait, no visible delay.

### R5 — FxSound fails to start → start anyway, warn with a Windows toast
- If FxSound is installed but does not answer within 10 s (`fxsound_backend.wait_until_running`, `:367`), the Hub still starts.
- A **Windows toast** is raised (`ToastCenter.show(...)`, thread-safe, `ui/toast.py:563`), title/short body, e.g. "FxSound did not start" / "The Hub is running without it. Start FxSound from the Audio page."
- The failure is written to the log (`applog`, audio/windows channel).

### R6 — FxSound not installed → normal Hub start
- No launch attempt, no prompt, no toast. The Audio page keeps offering **Install FxSound** as it does today (gate unchanged).

### R7 — Single instance: a second launch focuses the existing Hub
- A second launch must **not** create a second process. It brings the already-running Hub's **window** forward (the tray "Open" behaviour, `_bring_to_front`, `app.py:445-453`) and **also ensures FxSound is running** if it is stopped.
- The second process exits promptly (target: < 1 s after the handoff, excluding its own FxSound wait).
- Works whether the primary is visible, minimised, or withdrawn in the tray.

### R8 — Settings switch (default on), in Settings ▸ Behaviour
- New checkbox **"Start FxSound with the Hub"** in the Behaviour card, next to "Start minimised", with a one-line explanation. Default **on**; controls **every** Hub start (R3).

### R9 — After the auto-start, adopt FxSound's settings (load, never push)
- When FxSound comes up, the Hub **loads** its live equalizer, effect levels and preset into the active profile via the existing `adopt_fxsound_status()` path — never pushing the Hub's saved profile over FxSound's state. This keeps the project's documented load-never-push philosophy (`app/README.md:43-46, 302-311`).
- The Audio page must reflect the adopted state on its next refresh/paint.

### R10 — Quiet success
- On success, no toast. Only a brief **status-line** confirmation in the window ("FxSound is running.") — and nothing at all in tray mode.

### R11 — Escape hatch: `--no-fxsound-start`
- New CLI flag on the Hub (`ps3hub/__main__.py`) that skips the FxSound auto-start for that run. Documented in `--help`.

### R12 — Post-install: launch the Hub in the tray
- On a successful install, the Hub is started **in the tray** by default.
- Offered as a checkbox **"Launch PS3 Headset Hub now (in the tray)"**, default **on**, in the GUI installer; `install.ps1` gets the same default plus a `-NoLaunch` switch to skip.
- Unticking the checkbox (or `-NoLaunch`) produces no launch.

### R13 — Missing autostart entry → offer once to restore
- New persisted intent flag; if the Hub starts and the Run entry is gone (user deleted it, or Task Manager disabled it) while the intent says "enabled", the Hub offers **once** to restore it (status line + toast with a click that opens the Hub / the Settings toggle). It never silently re-writes the registry.
- The offer is only made when running as an **installed/frozen** exe (§9).

### R14 — Scope of supporting work
- Unit tests, installer tests, docs, and a version bump — all four (§10–§12).

### R15 — Whole-page "no-access" overlay on the Audio page
- Whenever the Audio page is **the current view** and FxSound is **not usable** (`missing` or installed-but-`stopped`), a single overlay covers the **entire Audio page** (all four cards: Audio processing, Effect profile, Equalizer, FxSound integration). Nothing on the page is interactable while it is up.
- The overlay is a **proposal, not just a notice**: its context-appropriate primary button is **Start FxSound** (installed, stopped) or **Install FxSound** (missing) — the owner's "propose to start/install it when this overlay is there". No pop-up dialog is ever used for this (§R16).
- Secondary buttons: **Open the download page**, **Check FxSound status**, **Go to Settings** (Settings ▸ Behaviour, where "Start FxSound with the Hub" lives).
- The overlay **clears by itself, both ways**: as soon as FxSound answers it lifts (and its state is adopted/reloaded as the existing page-open sync does), and if FxSound goes away again while the page is open it comes back. It is driven by the page's existing status-file poll, not by a manual press.
- The overlay **never** obeys `start_fxsound_with_hub` or `--no-fxsound-start`: those govern the automatic start at Hub launch only. The Audio page is an explicit user surface, so the overlay always offers the action.
- While displayed, the covered controls are also disabled (`state="disabled"`, keyboard-unreachable) so a scrim gap, a shortcut or Tab order cannot reach them — the overlay is the interaction block, `_gate_ui` is the defence in depth.
- Overlay copy states the reason honestly: FxSound missing vs. installed-but-stopped, plus the current status line from the FxSound card.

### R16 — No pop-up prompts on the Audio page
- The dialog paths that ask "Start FxSound now?" / "Install FxSound now?" (`_require_fxsound(prompt_start=True)`, `view_audio.py:487-535`) are removed: with the whole page covered, a gated control can no longer be reached to trigger them.
- The non-dialog confirmation surfaces stay: the pre-install confirmation for the silent download is replaced by the overlay's **Install FxSound** button being the explicit consent, and the R5 startup-failure **toast** is unchanged (it is a notification, not a prompt).
- Status banners that merely describe the FxSound state remain, but the primary explanation lives on the overlay.

---

## 4. Decisions taken in the interview

| # | Question | Answer |
|---|---|---|
| 1 | What should one launch do? | **One launch does both**: start FxSound, wait, then start the Hub |
| 2 | Change the installer autostart? | Keep it; **verify it actually works** (the owner suspects it previously "only started FxSound") |
| 3 | Start FxSound at sign-in too? | **Both** — every Hub start |
| 4 | Second launch while Hub is running | **Focus the existing Hub** (single instance) |
| 5 | FxSound installed but broken | **Launch anyway + warn** |
| 6 | FxSound not installed | **Start the Hub normally** |
| 7 | Does the Hub window wait for FxSound? | **No — Hub appears immediately** |
| 8 | Second launch also ensures FxSound? | **Focus + ensure FxSound** |
| 9 | Configurable? | **Yes — a Settings switch** |
| 10 | Whose audio settings win after the start? | **Adopt FxSound's settings** (load, never push) |
| 11 | Switch scope | **Every Hub start** (sign-in + manual) |
| 12 | Second-launch UI | **Open the window** |
| 13 | Portable/source runs too? | **Always** — the same behaviour for `python main.py`, `run.bat`, `run_app_tray.bat` and the installed exe |
| 14 | Escape hatch | **Yes, a CLI flag** (`--no-fxsound-start`) |
| 15 | Failure warning channel | **Windows toast** |
| 16 | Patience with a slow FxSound | **Keep 10 s, one attempt** |
| 17 | After install | **Launch in tray** |
| 18 | Ready notice | **Status line only** |
| 19 | Scope | Tests + installer tests + docs + version bump |
| 20 | Switch label/placement | **Settings ▸ Behaviour**, "Start FxSound with the Hub" |
| 21 | Post-install offer | **Checkbox, default on** (GUI + `install.ps1` default) |
| 22 | Autostart entry | **Verify and repair** on install/upgrade |
| 23 | Entry missing at startup | **Offer once to re-add** |
| 24 | Overlay coverage | **The entire Audio page** — all four cards |
| 25 | FxSound installed but stopped | **Same overlay**, with **Start FxSound** as its primary action |
| 26 | Overlay look | Asked for **translucent scrim**; changed during implementation to an **opaque in-window panel** — see 33 |
| 27 | Overlay actions | **Start FxSound**, **Install FxSound**, **Open the download page**, **Check FxSound status**, **Go to Settings** |
| 28 | Overlay lifetime | **Auto, both ways** — lifts when FxSound answers, returns if it goes away |
| 29 | "Propose to start/install" | **A prominent button on the overlay**, no dialog |
| 30 | Prompt rate | **No pop-up prompts at all** — visiting the Audio page simply shows the overlay with its buttons |
| 31 | Respect the auto-start switch/flags? | **No** — the overlay always proposes; those only govern the launch-time auto-start |
| 32 | **O7 resolved (implemented)** | Cover the whole page as asked, and add a **Use the Hub's own audio controls** button that reveals the parts that never needed FxSound (output switching, the Hub's own DSP); the equalizer, effects and presets stay locked |
| 33 | **O8 resolved (implemented)** | One **in-window** overlay, no `-alpha` and no second Toplevel: a borderless always-on-top window would have to track move/resize/DPI/minimise/tray and could strand a floating panel. An opaque panel is deterministic everywhere, identical in the smoke test, and hides with the window for free |
| 34 | Overlay copy source of truth | One pure `overlay_for(found, running)` returning the copy *and* the primary action, so the wording and the choice are unit-tested without a display |

---

## 5. Configuration changes

`app/ps3hub/config.py` → `Settings` (dataclass, `CONFIG_VERSION` stays **2** — the new keys are additive, unknown keys are already filtered and missing keys already fall back to defaults):

```python
#: Start FxSound (when installed and stopped) as part of every Hub launch.
start_fxsound_with_hub: bool = True
#: Whether the Hub should be registered to start in the tray at sign-in.
#: Records intent; the registry entry itself is written by the installers or
#: by the Settings toggle.
start_with_windows: bool = True
#: Whether the one-time "restore start with Windows" offer has been shown.
autostart_offer_shown: bool = False
```

- All three added to `Settings.clamped()` with `bool(...)` coercion.
- `to_dict`/`from_dict` need no surgery (`asdict` + key filtering).
- Reset-everything returns them to defaults; the registry is **not** touched by a reset.
- A config round-trip test must cover the three new keys.

---

## 6. CLI surface

`app/ps3hub/__main__.py`:

```
--tray                 start minimised in the Windows system tray (unchanged)
--no-fxsound-start     do not start FxSound as part of this launch
```

- `--no-fxsound-start` wins over the setting, for one run only.
- No new flags are needed for single-instance (always on).

---

## 7. Technical design

### 7.1 New module: `app/ps3hub/startup.py` (stdlib + ctypes only)

Mirrors the "no extra dependency" stance of `tray.py`. Contains three groups of pure-ish helpers so they are unit-testable without Windows side effects:

**a) Autostart registry (app-side mirror of the installers)**
- `RUN_KEY`, `RUN_VALUE = "PS3PS3HeadsetHub"`, `run_command(exe) -> '"<exe>" --tray'`
- `is_autostart_enabled() -> bool`, `enable_autostart(exe)`, `disable_autostart()`
- `installed_exe() -> Path | None` (frozen exe path, else `None` for source runs)
- A test asserts the constants agree with `packaging/installer_gui.RUN_VALUE`/`RUN_KEY` and with `install.ps1`'s `"PS3$AppSlug"` (the existing sync test `test_the_run_value_stays_in_sync_with_install_ps1`, `app/tests/test_installer.py:55-67`, is the model).

**b) FxSound boot**
- `should_start_fxsound(settings, cli_flag, engine) -> bool` — installed **and** stopped **and** setting on **and** flag absent.
- `ensure_fxsound_running(engine, timeout=10.0) -> FxSoundStatus` — `engine.fxsound_launch(timeout)`; on `running` then `engine.adopt_fxsound_status(status)`; never raises; returns the status for reporting.
- Called from a daemon thread only.

**c) Single instance (ctypes, no dependency)**
- Named mutex (session/per-user scoped, e.g. `PS3HeadsetHub.<user>`), created with `CreateMutexW`; `GetLastError() == ERROR_ALREADY_EXISTS` means a primary exists.
- Named **activation event** (`CreateEventW` / `OpenEventW` / `SetEvent`): the second instance sets it and exits; the primary waits on it with `WaitForSingleObject(..., 0)` inside the already-running 70 ms `_tick` (`app.py:474-490`), which pushes `_bring_to_front` onto `_ui_requests`.
- The primary must **create the event before** signalling readiness, to avoid a race between two simultaneous launches; the loser of the mutex still gets a valid event handle (retry loop with a short timeout).
- Activation path: `_bring_to_front` (deiconify + `lift` + `attributes("-topmost")` toggle) — the same code the tray "Open" action uses, so behaviour is identical from the tray and from a second launch.

### 7.2 Hub launch sequencing

```
main()
 ├─ parse args (--tray, --no-fxsound-start)
 ├─ startup.acquire_single_instance()
 │    ├─ primary?  → create activation event, continue
 │    └─ existing? → (a) ensure_fxsound_running()  [R7]
 │                  (b) signal activate            [R7]
 │                  (c) return 0 (no Tk, no window)
 └─ HubApp(tray_mode=..., fx_boot=enabled) → mainloop()
```

Inside `HubApp.__init__`, **after** `_build()` and `_service.start()` and **after** the tray/window is up (so R4 holds):

```
if settings.start_fxsound_with_hub and not cli_flag:
    threading.Thread(target=self._fxsound_boot, daemon=True).start()
```

`_fxsound_boot()`:
1. engine missing → return.
2. `should_start_fxsound(...)` false → return (already running / not installed).
3. `ensure_fxsound_running(engine)` (10 s, one attempt).
4. Success → `self._ui_requests.put(...)` → status line "FxSound is running." + refresh the Audio view if it is the current view.
5. Failure → `self._notifier.show("FxSound did not start", <hint>, "warn")` (thread-safe; routes to a Windows toast when the window is hidden — exactly the requested channel) + log.

### 7.3 Settings UI

`app/ps3hub/ui/view_settings.py` → Behaviour card, immediately after `_start_minimized`:

- **"Start FxSound with the Hub"** — "Start FxSound when the Hub starts, so the audio features are ready. FxSound opens hidden."
- **"Start with Windows (in the system tray)"** — reflects/updates the registry via `startup.enable_autostart` / `disable_autostart`; **only enabled when running as the installed exe**; for source runs it is shown disabled with a note that the installers own this. (See open question O1.)

### 7.4 Installer changes

- `app/packaging/installer_gui.py` (`add_run_entry` at `:179`)
- `add_run_entry()` → after `SetValueEx`, read the value back; if missing/mismatched, retry once and report the outcome in the install log (R1).
- New `InstallOptions.launch_after_install: bool = True`; checkbox "Launch PS3 Headset Hub now (in the tray)" (default on) in the options frame.
- `perform_install()` end, if `launch_after_install`:  `subprocess.Popen([str(target_exe), "--tray"], ...)`; keep the manual **Launch** button working (`os.startfile` today, `installer_gui.py:553-560`).
- New `--no-launch` CLI switch for `--silent`.

`app/packaging/install.ps1`
- `New-`Run entry`: read back with `Get-ItemProperty` and repair once on mismatch (R1).
- New `-NoLaunch` switch; default is `Start-Process -FilePath $TargetExe -ArgumentList '--tray'` at the end.
- Keep `-NoAutostart`, `-NoFxSound`, `-WithFxSound`, `-Uninstall` behaviour unchanged.

`app/packaging/Install.cmd` header comment updated to mention the tray launch.

### 7.5 Audio page no-access overlay

Lives in `app/ps3hub/ui/view_audio.py`, built once in `_build()` and toggled by `_render_fxsound` / `_gate_ui` (which already run on every status change).

**Where it sits.** The overlay must *cover* content while staying pinned to the visible page area inside the `ScrollFrame` (`view_audio.py:154-158`). Two placements, chosen at runtime:

1. **Preferred — a translucent scrim.** An `overrideredirect(True)` `Toplevel` sized and positioned to the Audio view's viewport rect (`winfo_rootx/rooty` + `winfo_width/height` mapped to screen coordinates), with `-alpha ≈ 0.82` over a near-black fill and a centred **opaque** notice card (glyph, headline, explanation, status line, buttons). It is the only Tk way to genuinely let the covered cards show through, since an in-window `Frame`/`Canvas` cannot be transparent. `transient(parent)` + never taking focus keeps it behaving like an in-window panel.
2. **Fallback — a solid in-window panel.** When alpha/`-transparentcolor` is unavailable (non-Windows dev runs, a Tk build without the attribute), `place()` a solid dark panel over the page viewport with the same notice and buttons. Covers 1 is then a look, not a behaviour: R15's blocking, copy and auto-clear are identical either way.

The overlay is repositioned on the parent's `<Configure>` and hidden when the Audio page is not the current view, so switching to Dashboard/Mapping/Settings never leaves a floating panel behind. In tray mode it is irrelevant (the window is withdrawn); it must be hidden on `withdraw()` and on `_hide_to_tray()`.

**Blocking.** The overlay window swallows mouse events over the page. Underneath, `_gate_ui()` keeps every covered control `state="disabled"` (graph, knobs, band menu, preset box, preset buttons, effect sliders, master gain, power switch, output selector, FxSound buttons) so Tab order and programmatic paths cannot reach them. **No `grab_set()`** — the nav rail and the rest of the window must stay usable; the user has to be able to leave the page. Focus stays in the main window's own Tk focus chain; the overlay's widgets are reachable by click.

**Content, by reason** (one pure mapping function, so it is testable without Tk):

| `reason` | Headline | Primary | Secondary |
|---|---|---|---|
| `missing` | "FxSound is not installed" | **Install FxSound** | Open the download page · Check FxSound status · Go to Settings |
| `stopped` | "FxSound is not running" | **Start FxSound** | Open the download page · Check FxSound status · Go to Settings |

Both variants also carry the existing detail line ("the Hub drives FxSound through its own command line, so it has to be up") and, when relevant, the live install/download progress that the FxSound card narrates today (`_drain_fxsound_install`). Buttons reuse the existing handlers verbatim: `_on_start_fxsound` → `_start_and_settle`, `_on_install_fxsound`, `_on_open_download_page`, `_probe_fxsound`; **Go to Settings** needs a new lightweight callback from `HubApp` (`_ui_requests`-safe) that calls `_select_view("settings")`.

**Auto-clear.** `_poll_status_file()` (`view_audio.py:704-760`) already watches FxSound's `status.json` stamp while the page is open and calls `_load_full_state()`; the overlay visibility is derived from the same status object, so a start from anywhere (the Hub's launch-time boot, the tray, the user starting FxSound by hand) lifts it without any extra polling. A status that stops answering re-raises it.

#### As built (1.2.15)

- The overlay is a `tk.Frame` **child of the `AudioView`**, `place()`d over the whole view (`relwidth=1, relheight=1`) so it covers the visible page area and nothing else; it is packed away with the view on a page switch and hides with the window on minimise/withdraw, which removes every "stray floating panel" failure mode.
- Visibility and copy come from `overlay_for(found, running)`; the **single** call site is `_sync_overlay()`, invoked from `_gate_ui()`, so the overlay and the control gating can never disagree about the lock. An unknown status (nothing probed yet) does *not* cover the page.
- Blocking is belt and braces: the overlay swallows clicks, and `_gate_ui()` disables every control behind it — the page's own controls through `_page_controls()`, the FxSound power switch separately, and the equalizer set as before. Continuing past the overlay restores exactly the pre-1.2.15 gating (own controls usable, FxSound controls locked).
- One detail worth keeping: the output selector is a **read-only combobox**, so restoring it sets `state="readonly"`, never `"normal"` (which would make it editable). A test pins this.
- The escape hatch is `_overlay_continue_without()`; it is re-armed on every page entry (`on_page_open`), never by a status change, so it cannot silently undo itself.
- "Go to Settings" goes through a new optional `on_navigate` callback, which `HubApp` wires to `_select_view` over the existing `_ui_requests` queue (Tk-thread safe).
- All three `messagebox` calls were removed from `view_audio.py`; a test asserts the module no longer contains `askyesno`/`showwarning`.

---

## 8. Edge cases and error handling

| Case | Required behaviour |
|---|---|
| FxSound already running at Hub start | No spawn; adopt only if the Audio page pre-sync would normally do so (i.e. leave it to the page; the boot thread does **not** re-adopt an already-running instance) |
| FxSound starts but is slow (>10 s) | One attempt only; warn with the toast (R5); no retry, no background polling |
| FxSound `launch()` fails (`Popen` error) | Same warning path; log the OSError |
| FxSound uninstalled mid-session | No crash; page keeps its "missing" gate |
| FxSound and Hub raced at sign-in | Single-instance mutex prevents two Hubs; the loser does not start a second tray icon |
| Second launch while primary is still booting (no event yet) | Retry the event open for a short window, then fall back to just exiting (never two Hubs) |
| `--tray` + no FxSound | Hub withdraws to tray as today; nothing appears |
| Two simultaneous double-clicks | Mutex decides; exactly one Hub |
| Audio page overlay vs. the nav rail | No `grab_set`: the user can always navigate away while the overlay is up |
| Regedit value removed by user | One-time offer to restore (R13), never silent re-write |
| Source run (`python main.py`) | Same FxSound boot behaviour (R3); the Start-with-Windows toggle is inactive because the Run entry would have to point at the frozen exe |
| Dev runs in tests / smoke test | FxSound boot must be injectable/disable-able so `tests/smoke_ui.py` stays hermetic |
| Windows focus-stealing rules | Reuse the existing `_bring_to_front` path; the second-instance handoff must not depend on `SetForegroundWindow` alone |
| Config corrupted | Unchanged: quarantine + defaults; new keys simply take their defaults |
| Audio page not the current view | No overlay anywhere else; it is created/positioned on page entry and hidden on page exit |
| Hub window moved, resized, maximised, or DPI changed | Overlay re-positioned on `<Configure>`; on DPI change it re-reads `winfo_*` so it never ends up offset |
| Window minimised / hidden to tray while the overlay is up | Overlay hidden with the window; re-shown on restore if FxSound is still unusable |
| FxSound start pressed on the overlay | Overlay's primary button disables and shows "Starting FxSound..." (existing `_start_and_settle` narration); on success the overlay lifts, on failure it stays with the error line |
| Silent install in progress | Progress is narrated on the overlay; buttons disabled while busy (`_fx_install_busy`) |
| FxSound dies while the page is open | Overlay returns on the next status poll (per the chosen "auto, both ways") |
| User has the Hub's own DSP as the backend and no FxSound | The whole page is still covered (decision 24). Accepted trade-off: output-device switching and native-DSP controls on that page need FxSound started/installed first — see O7 |
| Overlay + keyboard-only user | Covered controls are disabled, so Tab cannot reach them; the overlay's own buttons are focusable and clickable |
| Alpha / transparentcolor unsupported | Falls back to the solid in-window panel; identical behaviour, no error text |

---

## 9. Interaction with existing behaviour (must not regress)

- Tray mode still: hides on window close, exits only from the tray menu, tooltip from `format_tray_status`, battery-coloured icon.
- Headset-connect still brings the window forward in tray mode (`app.py:566-571`) — unrelated, must keep working.
- The Audio page's FxSound **handlers** stay ("Start FxSound" / "Install FxSound" / "Check status" / "Sync from FxSound now"); R15 only replaces the *surface* — the whole page is covered and the buttons move onto the overlay — and the `askyesno` prompts of the old gate go away (R16). R5's launch-time warning stays a toast, never a modal.
- `start_minimized` (window mode) unchanged.
- `packaging/ps3hub.spec` / `build_exe.py` require no new data files (the new module is pure stdlib).
- FxSound's own autostart, install location and CLI are never touched beyond `launch()`/`adopt`.

---

## 10. Test plan

New: `app/tests/test_startup.py` (no real registry, no real FxSound):
1. `RUN_VALUE`/`RUN_KEY`/`run_command` agree with `installer_gui` and with `install.ps1`.
2. `should_start_fxsound`: installed+stopped+setting on+no flag → True; each of (not installed, already running, setting off, flag set) → False.
3. `ensure_fxsound_running` with a fake engine: success → `adopt_fxsound_status` called and **never** `apply_profile`/`set_preset` (load, never push); failure → returns a non-running status, no exception.
4. `--no-fxsound-start` parses and suppresses the boot thread. (`ps3hub/__main__.py` builds its parser inline inside `main()`; extract a `build_parser()` there — the pattern `packaging/installer_gui.build_parser` already uses — so the flags are testable without running Tk.)
5. Single-instance decision: injected probe says "primary exists" → activate + return 0 and **`HubApp` is never constructed**; probe says "none" → proceeds.
6. `Settings` round trip / `clamped()` for the three new keys.
7. One-time autostart offer: fires once, records `autostart_offer_shown`, never silently writes the registry.
8. Overlay reason mapping: `missing` → Install primary; `stopped` → Start primary; both carry the four secondary actions (pure function, no Tk).
9. Overlay visibility: `missing` and `stopped` both require it; `running` does not.
10. Overlay auto-clear both ways: feeding a status sequence (stopped → running → stopped) toggles it off and back on, and a running status is adopted (load, never push) before it lifts.
11. No prompts: interacting with the Audio page while FxSound is unavailable never calls `askyesno`/`showwarning` (assert the prompts are gone and that the overlay's buttons are the only path).
12. Covered controls are all disabled while the overlay is up (assert the widget states, not just the overlay's existence).

Extend:
- `app/tests/test_installer.py` — `--no-launch` parsing, `launch_after_install` default on, the post-install launch command carries `--tray`, and the read-back/repair helper behaviour. Keep `test_the_run_value_stays_in_sync_with_install_ps1` (`:55`) green.
- `app/tests/test_audio_modules.py` — keep `test_fxsound_launch_spawns_the_exe_without_arguments` and the tray/fxsound tests green.
- `app/tests/smoke_ui.py` — construct `HubApp` with the FxSound boot disabled (monkeypatched engine) so the smoke test remains hermetic. Add a pass that opens the Audio page with FxSound **unavailable** and asserts the overlay is up, the covered controls are disabled, and the page still renders; then a pass with FxSound **running** and asserts the overlay is absent.

Manual verification on Windows (the acceptance run):
1. Fresh install with defaults → Run value present and correct; sign out/in → tray-only Hub.
2. FxSound stopped, launch the exe → FxSound comes up hidden, Hub window appears, Audio page shows FxSound running, **one** launch.
3. Launch again while running → no second process, existing window comes forward.
4. `--no-fxsound-start` and the Settings switch off → FxSound untouched.
5. FxSound uninstalled → normal start, no warning.
6. Simulate a hang (FxSound exe replaced by a sleep stub) → Hub still starts, Windows toast appears, log line present.
7. Untick "Launch … now (in the tray)" → install ends without launching.
8. Open the Audio page with FxSound uninstalled → the whole page is covered, primary button **Install FxSound**, no dialog; press it → silent install → the overlay lifts by itself.
9. With FxSound running, kill it while the Audio page is open → the overlay returns within a status poll; start it again → the overlay lifts.

Every non-Windows-only piece must run in CI (`pytest -q`), matching the existing suite's platform-neutral style.

---

## 11. Docs

- `README.md` — release highlights (tray autostart verified/repaired; one launch starts FxSound and the Hub; single instance).
- `app/README.md` — "What is new in 1.2.15" entry; "Install and run" (post-install tray launch, `-NoLaunch`); "Using it"/Settings (the two new switches); a short "How the Hub starts" paragraph (FxSound boot, load-never-push, the toast on failure); and the Audio-page section documents the no-access overlay (whole page covered while FxSound is unavailable, its buttons propose Start/Install, it clears itself both ways, and there is no longer a pop-up prompt).
- `TECHNICAL_README.md` — one architecture line for `ps3hub/startup.py` (tray autostart mirror, FxSound boot, single-instance named objects).
- `ENGINEERING_REPORT.md` — test-count update if the suite count is asserted anywhere.

## 12. Versioning

- `APP_VERSION` `1.2.14` → `1.2.15` in `app/ps3hub/__init__.py`.
- Commit as `chore: release 1.2.15`, tag `v1.2.15` (CI fails if the tag disagrees with `APP_VERSION`).

---

## 13. Non-goals

- The overlay exists **only on the Audio page**; Dashboard, Mapping, Diagnostics and Settings are never covered.

- Bundling, downloading or repairing FxSound beyond the existing offer paths.
- Changing the Audio page's FxSound gating, the DSP, or the `.fac` handling.
- Replacing the tray implementation or adding `pystray`/`pywin32`.
- Auto-updating the Hub, service installation, or admin-level (HKLM) autostart.
- Changing FxSound's own start-with-Windows behaviour.
- A splash screen or any blocking startup wait (explicitly rejected: R4).

---

## 14. Open questions for the next round

- **O1** — For source runs (`run.bat`, `run_app_tray.bat`), should the *Start-with-Windows* Settings toggle write a Run entry pointing at `pythonw main.py --tray` (the `.vbs` pattern), or stay disabled as proposed?
- **O2** — Should the FxSound boot also run when the Hub's audio processing is **disabled** (setting on, backend `native`)? Current spec: yes, the switch is the only gate.
- **O3** — Should the "offer once to restore autostart" surface be the Windows toast with a click, a corner card, or a banner on the Settings page?
- **O4** — Should the post-install tray launch be skipped when FxSound's installation was the last thing to happen (to avoid overlapping with FxSound's first-time setup)?
- **O5** — Named-object scope: per-user (`PS3HeadsetHub.<user>`) vs per-session — any multi-user/RDP requirement?
- **O6** — Should the second-instance handoff also carry a command-line intent (e.g. `--open-audio-page`) so a future "open the Hub on the Audio page" shortcut works?
- ~~**O7** — the Audio processing card is blocked too~~ **Resolved (see 32):** whole-page coverage stays, plus a "Use the Hub's own audio controls" escape hatch that unlocks only the controls that never needed FxSound.
- ~~**O8** — translucent scrim needs `-alpha`~~ **Resolved (see 33):** in-window opaque panel; no `-alpha`, no second window, no platform-dependent look.
- **O9 (new)** — Should the escape hatch be a full "don't ask again" setting, or stay per-visit as implemented?

---

## 15. Acceptance checklist (definition of done)

- [ ] R1–R14 implemented (tray autostart, combined launch, installer work) — **not started**.
- [x] R15/R16 implemented (1.2.15): whole-page overlay, primary Start/Install, no prompts.
- [x] Audio page visited with FxSound **not installed**: the whole page is covered, the primary button is **Install FxSound**, and no dialog appears.
- [x] Audio page visited with FxSound **installed but stopped**: the whole page is covered, the primary button is **Start FxSound**.
- [x] Nothing on the covered page is clickable or keyboard-reachable; the nav rail still works (no `grab_set`).
- [x] Starting/installing from the overlay lifts it automatically once FxSound answers, and it returns if FxSound stops.
- [x] `python -m pytest -q` green (282) and `python tests/smoke_ui.py` passes with three new overlay steps.
- [ ] One launch with FxSound stopped starts FxSound **and** the Hub (manual test, one click).
- [ ] Second launch never creates a second process and brings the existing window forward.
- [ ] Fresh install registers a verified Run entry; sign-in yields a tray-only Hub; uninstall removes it.
- [ ] Post-install tray launch happens by default and can be unticked / `-NoLaunch`'d.
- [ ] New setting + `--no-fxsound-start` both suppress the FxSound boot.
- [ ] `python -m pytest -q` green (including the new `test_startup.py` and the extended installer tests).
- [ ] Docs updated; `APP_VERSION` bumped to `1.2.15`.
