"""Audio view: Windows audio processing controls.

Lets the user enable the Hub's loopback DSP, tune the effect profile for the
default output, and see at a glance what the engine is actually doing -
including the optional FxSound integration when that application is
installed.

The FxSound features are honest about their dependency: the equalizer card
and the preset controls only work when a FxSound instance is up and
answering. When it is installed but stopped the view offers to start it; when
it is not installed at all the view says so and points at the download page.
Before any FxSound feature is used, the view loads **all** of the
application's presets and mirrors its current settings into the active
profile, so the Hub shows the same exact configuration FxSound is running.

Everything here reads from the :class:`~ps3hub.audio.engine.AudioEngine`
facade; no COM, no HID and no thread ownership leaks into the view.
"""

from __future__ import annotations

import time
import tkinter as tk
from dataclasses import replace as dataclass_replace
from tkinter import messagebox as tkmessagebox
from tkinter import ttk
from typing import Any, Callable

from ..applog import get_logger
from .theme import ABYSS, FAINT, FAULT, GOLD, IDLE, LIVE, MUTED, PANEL, PAPER, WARN, fonts
from .widget_eq import BAND_COUNTS, EQGraph
from .widgets import Banner, Card, KeyValue, ScrollFrame, StatusPill

log = get_logger("ui.audio")


class _ProfileEcho:
    """Presents a profile as enough of a status for ``AudioView._fx_signature``.

    Used to fingerprint what the Hub just pushed to FxSound, so a rewrite of
    status.json that merely echoes the push is recognised and never mirrored
    back over the curve being edited.
    """

    def __init__(self, profile) -> None:
        from ..audio.fac import default_band_frequencies
        count = int(getattr(profile, "eq_bands", 10) or 10)
        bands = [dict(frequency=float(f), gain=float(g))
                 for f, g in (getattr(profile, "eq", []) or [])][:count]
        if not bands:
            bands = [dict(frequency=float(f), gain=0.0)
                     for f in default_band_frequencies(count)]
        self.effects = {
            "bass": float(getattr(profile, "bass", 0.0)),
            "clarity": float(getattr(profile, "clarity", 0.0)),
            "ambience": float(getattr(profile, "ambience", 0.0)),
            "surround": float(getattr(profile, "surround", 0.0)),
            "dynamic_boost": float(getattr(profile, "dynamic_boost", 0.0)),
        }
        self.equalizer = {
            "num_bands": count,
            "master_gain": float(getattr(profile, "master_gain_db", 0.0)),
            "volume_leveling": float(getattr(profile, "volume_leveling_db", 0.0)),
            "filter_q": float(getattr(profile, "filter_q", 1.0)),
            "balance": float(getattr(profile, "balance_db", 0.0)),
            "bands": bands,
        }
        self.selected_preset = getattr(profile, "preset_name", "") or ""


class AudioView(tk.Frame):
    """The Audio page."""

    def __init__(
        self,
        master,
        engine_provider: Callable[[], Any],
        on_changed: Callable[[], None],
    ) -> None:
        super().__init__(master, bg=ABYSS)
        self._engine_provider = engine_provider
        self._on_changed = on_changed
        self._suspend = False
        self._eq_loaded = False
        # The most recent FxSound status the view knows about, plus whether
        # the FxSound features are currently unlocked.
        self._last_fx_status: Any = None
        self._fx_ui_enabled = False
        # Live-mirroring bookkeeping: the status.json write we last saw, and
        # a fingerprint of the status we last applied, so the poll can tell
        # "FxSound changed something on its own" apart from "this is the
        # echo of a push we just made".
        self._mirror_stamp: tuple[int, int] | None = None
        self._fxsig: tuple | None = None
        # Push bookkeeping. Everything FxSound writes while (or just after)
        # the Hub pushes a curve is an echo, not an independent change; the
        # reported symptom of the equalizer "snapping back to zero" was the
        # mirror adopting the application's mid-apply snapshot over the
        # user's edits. These fields make the poll ignore that window.
        self._user_edit_until = 0.0
        self._pushed_fxsig: tuple | None = None
        self._pushed_not_flat = False
        self._build()
        self.refresh()

    # --------------------------------------------------------------- build --

    def _build(self) -> None:
        font = fonts()
        scroller = ScrollFrame(self, bg=ABYSS)
        scroller.pack(fill="both", expand=True)
        root = scroller.interior
        root.columnconfigure(0, weight=1, uniform="audio")
        root.columnconfigure(1, weight=1, uniform="audio")

        # -- processing -------------------------------------------------------
        processing = Card(root, "Audio processing", "Windows loopback DSP")
        processing.grid(row=0, column=0, sticky="nsew", padx=(0, 12), pady=(0, 12))

        self._enabled_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            processing.body, text="Process the default output's audio",
            variable=self._enabled_var, command=self._on_toggle,
        ).pack(anchor="w")

        tk.Label(
            processing.body,
            text=(
                "The Hub captures what Windows mixes for the default output, "
                "applies the effect profile below and plays the result. Effects "
                "at 0 are fully transparent."
            ),
            bg=PANEL, fg=FAINT, font=font.tiny, anchor="w", justify="left",
            wraplength=380,
        ).pack(fill="x", pady=(2, 10))

        self._status_pill = StatusPill(processing.body, "Off", IDLE, bg=PANEL, width=160)
        self._status_pill.pack(anchor="w", pady=(0, 6))
        self._kv_device = KeyValue(processing.body, "Device", "--")
        self._kv_device.pack(fill="x", pady=2)
        self._kv_format = KeyValue(processing.body, "Format", "--")
        self._kv_format.pack(fill="x", pady=2)
        self._kv_blocks = KeyValue(processing.body, "Processed", "--")
        self._kv_blocks.pack(fill="x", pady=2)

        self._audio_banner = Banner(
            processing.body,
            "Processing follows the Windows default output. Switch devices in "
            "Windows and the Hub follows.",
        )
        self._audio_banner.pack(fill="x", pady=(8, 0))

        # Sending the headset's own endpoint to the front.
        output = tk.Frame(processing.body, bg=PANEL)
        output.pack(fill="x", pady=(10, 0))
        self._output_var = tk.StringVar(value="")
        self._output_box = ttk.Combobox(
            output, textvariable=self._output_var, state="readonly", width=34)
        self._output_box.pack(side="left")
        ttk.Button(output, text="Use this output",
                   command=self._on_set_output).pack(side="left", padx=(8, 0))
        self._auto_output_var = tk.BooleanVar(value=False)
        self._auto_output_check = ttk.Checkbutton(
            output, text="Switch to it when the headset connects",
            variable=self._auto_output_var, command=self._on_auto_output,
        )
        self._auto_output_check.pack(anchor="w", pady=(6, 0))

        # -- effects ------------------------------------------------------------
        effects = Card(root, "Effect profile", "for the default output")
        effects.grid(row=0, column=1, sticky="nsew", pady=(0, 12))

        grid = tk.Frame(effects.body, bg=PANEL)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)
        self._effect_vars: dict[str, tk.DoubleVar] = {}
        for row, (key, label) in enumerate((
            ("bass", "Bass"),
            ("clarity", "Clarity"),
            ("ambience", "Ambience"),
            ("surround", "Surround"),
            ("dynamic_boost", "Dynamic boost"),
        )):
            tk.Label(grid, text=label, bg=PANEL, fg=PAPER, font=font.base,
                     anchor="w").grid(row=row, column=0, sticky="w", pady=3)
            var = tk.DoubleVar(value=0.0)
            scale = ttk.Scale(grid, from_=0.0, to=10.0, variable=var,
                              command=lambda _v: self._on_effect())
            scale.grid(row=row, column=1, sticky="ew", padx=(12, 8))
            value_label = tk.Label(grid, text="0.0", bg=PANEL, fg=MUTED,
                                   font=font.code_small, width=5)
            value_label.grid(row=row, column=2, sticky="e")
            var.trace_add("write", lambda *_a, lab=value_label, v=var:
                          lab.configure(text=f"{v.get():.1f}"))
            self._effect_vars[key] = var

        gain_row = tk.Frame(effects.body, bg=PANEL)
        gain_row.pack(fill="x", pady=(12, 0))
        tk.Label(gain_row, text="Master gain", bg=PANEL, fg=PAPER,
                 font=font.base, anchor="w").pack(side="left")
        self._gain_var = tk.DoubleVar(value=0.0)
        ttk.Scale(gain_row, from_=-12.0, to=12.0, variable=self._gain_var,
                  command=lambda _v: self._on_effect()).pack(
            side="left", fill="x", expand=True, padx=12)
        self._gain_label = tk.Label(gain_row, text="0.0 dB", bg=PANEL, fg=MUTED,
                                    font=font.code_small, width=8)
        self._gain_label.pack(side="right")
        self._gain_var.trace_add(
            "write", lambda *_: self._gain_label.configure(
                text=f"{self._gain_var.get():+.1f} dB")
        )

        ttk.Button(effects.body, text="Reset profile",
                   command=self._on_reset_profile).pack(anchor="w", pady=(12, 0))

        # -- equalizer ------------------------------------------------------------
        equalizer = Card(root, "Equalizer", "drag a point to shape the curve")
        equalizer.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(0, 12))
        self._build_equalizer(equalizer.body)

        # -- fxsound --------------------------------------------------------------
        fxsound = Card(root, "FxSound integration", "optional, via its command line")
        fxsound.grid(row=2, column=0, columnspan=2, sticky="nsew")
        self._fxsound_pill = StatusPill(fxsound.body, "Unknown", IDLE, bg=PANEL, width=170)
        self._fxsound_pill.pack(anchor="w", pady=(0, 6))
        self._fxsound_detail = tk.Label(
            fxsound.body, text="Checking for FxSound...",
            bg=PANEL, fg=FAINT, font=font.small, anchor="w", justify="left",
            wraplength=620,
        )
        self._fxsound_detail.pack(fill="x")

        buttons = tk.Frame(fxsound.body, bg=PANEL)
        buttons.pack(fill="x", pady=(10, 0))
        self._fx_start_button = ttk.Button(
            buttons, text="Start FxSound", command=self._on_start_fxsound)
        self._fx_start_button.pack(side="left")
        self._fx_install_button = ttk.Button(
            buttons, text="Open the FxSound download page",
            command=self._on_open_download_page)
        self._fx_install_button.pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Check FxSound status",
                   command=self._probe_fxsound).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Sync from FxSound now",
                   command=self._on_sync_from_fxsound).pack(side="left", padx=(8, 0))

    # ----------------------------------------------------------- equalizer --

    def _build_equalizer(self, body) -> None:
        """The EQ card: band count, curve, and the four FxSound controls."""
        font = fonts()
        top = tk.Frame(body, bg=PANEL)
        top.pack(fill="x")

        tk.Label(top, text="Bands", bg=PANEL, fg=PAPER, font=font.base
                 ).pack(side="left")
        self._band_var = tk.StringVar(value="10 Bands")
        self._band_menu = ttk.Combobox(
            top, textvariable=self._band_var, state="readonly", width=12,
            values=[f"{n} Bands" for n in BAND_COUNTS],
        )
        self._band_menu.pack(side="left", padx=(10, 0))
        self._band_menu.bind("<<ComboboxSelected>>", self._on_band_count)

        self._eq_pill = StatusPill(top, "Flat", IDLE, bg=PANEL, width=150)
        self._eq_pill.pack(side="right")

        ttk.Button(top, text="Reset EQ", command=self._on_reset_eq
                   ).pack(side="right", padx=(0, 8))

        self._eq_graph = EQGraph(body, self._on_band_dragged)
        self._eq_graph.pack(fill="x", pady=(10, 0))

        # The four controls FxSound puts beside its curve.
        grid = tk.Frame(body, bg=PANEL)
        grid.pack(fill="x", pady=(12, 0))
        grid.columnconfigure(1, weight=1)
        self._eq_vars: dict[str, tk.DoubleVar] = {}
        eq_controls = (
            ("master_gain_db", "Master gain", -20.0, 20.0, "dB"),
            ("volume_leveling_db", "Volume leveling", 0.0, 4.0, "dB"),
            ("filter_q", "Filter Q", 1.0, 3.0, "x"),
            ("balance_db", "Balance", -20.0, 20.0, "dB"),
        )
        for row, (key, label, low, high, unit) in enumerate(eq_controls):
            tk.Label(grid, text=label, bg=PANEL, fg=PAPER, font=font.base,
                     anchor="w").grid(row=row, column=0, sticky="w", pady=3)
            var = tk.DoubleVar(value=0.0)
            ttk.Scale(grid, from_=low, to=high, variable=var,
                      command=lambda _v, k=key: self._on_eq_control(k)
                      ).grid(row=row, column=1, sticky="ew", padx=(12, 8))
            readout = tk.Label(grid, text=f"0.0 {unit}", bg=PANEL, fg=MUTED,
                               font=font.code_small, width=9)
            readout.grid(row=row, column=2, sticky="e")
            var.trace_add(
                "write",
                lambda *_a, v=var, readout=readout, unit=unit: readout.configure(
                    text=f"{v.get():+.1f} {unit}" if unit == "dB"
                    else f"{v.get():.1f} {unit}"),
            )
            self._eq_vars[key] = var

        # Presets: load, save, and file import/export.
        files = tk.Frame(body, bg=PANEL)
        files.pack(fill="x", pady=(12, 0))
        self._preset_var = tk.StringVar(value="")
        self._preset_box = ttk.Combobox(
            files, textvariable=self._preset_var, state="disabled", width=24)
        self._preset_box.pack(side="left")
        self._preset_box.bind("<<ComboboxSelected>>", self._on_pick_preset)
        self._preset_load = ttk.Button(files, text="Load",
                                       command=self._on_load_preset, state="disabled")
        self._preset_load.pack(side="left", padx=(8, 0))
        self._preset_save = ttk.Button(files, text="Save as...",
                                       command=self._on_save_preset, state="disabled")
        self._preset_save.pack(side="left", padx=(6, 0))
        self._preset_import = ttk.Button(files, text="Import .fac...",
                                         command=self._on_import_fac, state="disabled")
        self._preset_import.pack(side="left", padx=(6, 0))
        self._preset_export = ttk.Button(files, text="Export .fac...",
                                         command=self._on_export_fac, state="disabled")
        self._preset_export.pack(side="left", padx=(6, 0))

        self._eq_banner = Banner(
            body,
            "The equalizer is applied by FxSound when it is the active "
            "processor, and by the Hub's own DSP otherwise.",
        )
        self._eq_banner.pack(fill="x", pady=(10, 0))

    # ------------------------------------------------------ fxsound gating --

    def _fxsound_ready(self) -> tuple[bool, str]:
        """Whether FxSound features may be used, and why not if they may not.

        ``reason`` is ``""`` when ready, ``"missing"`` when the application is
        not installed and ``"stopped"`` when it is installed but not running.
        """
        status = self._last_fx_status
        if status is None or not getattr(status, "found", False):
            return False, "missing"
        if not getattr(status, "running", False):
            return False, "stopped"
        return True, ""

    def _gate_ui(self) -> None:
        """Enable the FxSound-dependent controls only when FxSound is up."""
        ready, _reason = self._fxsound_ready()
        self._fx_ui_enabled = ready
        state = "normal" if ready else "disabled"
        self._eq_graph.set_enabled(ready)
        self._band_menu.configure(state=state)
        self._preset_box.configure(state=state)
        for button in (self._preset_load, self._preset_save,
                       self._preset_import, self._preset_export):
            button.configure(state=state)
        found = bool(getattr(self._last_fx_status or (), "found", False))
        self._fx_start_button.configure(
            state="normal" if found and not ready else "disabled")
        self._fx_install_button.configure(
            state="normal" if not found else "disabled")

    def _require_fxsound(self, prompt_start: bool = True) -> bool:
        """Make FxSound usable before a feature runs, or explain why not.

        Installed but stopped: ask the user, then start it and wait.
        Not installed: point at the download page. Returns whether the caller
        may proceed.
        """
        engine = self._engine_provider()
        if engine is None:
            return False
        self._probe_fxsound(quiet=True)
        ready, reason = self._fxsound_ready()
        if ready:
            return True
        if reason == "stopped" and prompt_start:
            if tkmessagebox.askyesno(
                    "Start FxSound",
                    "FxSound is installed but is not running.\n\n"
                    "The Hub drives FxSound through its own command line, so "
                    "the application has to be up for this feature.\n\n"
                    "Start FxSound now?",
                    parent=self):
                return self._start_and_settle()
            self._eq_banner.set(
                "FxSound is not running, so that feature stayed locked. "
                "Start FxSound (or press “Start FxSound”) and try again.",
                "warn")
            return False
        if reason == "missing":
            url = ""
            try:
                url = engine.fxsound_download_url()
            except AttributeError:
                url = ""
            if tkmessagebox.showwarning(
                    "FxSound is not installed",
                    "FxSound is not installed on this PC, so the Hub cannot "
                    "drive it.\n\n"
                    "FxSound is free and open source. Install it from:\n" + url,
                    parent=self):
                self._on_open_download_page()
            else:
                self._eq_banner.set(
                    "FxSound is not installed. Install it, then press "
                    "“Check FxSound status”.", "warn")
        return False

    def _start_and_settle(self) -> bool:
        """Start FxSound, wait for it to answer, and reload everything."""
        engine = self._engine_provider()
        if engine is None:
            return False
        self._fxsound_detail.configure(text="Starting FxSound...", fg=MUTED)
        self.update_idletasks()
        status = engine.fxsound_launch()
        self._last_fx_status = status
        if not status.running:
            self._eq_banner.set(
                f"FxSound did not come up: {status.error or 'no response'}. "
                "Check the FxSound installation.", "error")
            self._render_fxsound(status)
            return False
        self._eq_banner.set("FxSound is running. Loading its presets and "
                            "current settings.", "info")
        self._load_full_state(status, force=True)
        self._render_fxsound(status)
        self._on_changed()
        return True

    def _on_start_fxsound(self) -> None:
        self._start_and_settle()

    def _on_open_download_page(self) -> None:
        """Open the FxSound download page in the default browser."""
        import webbrowser
        engine = self._engine_provider()
        url = ""
        if engine is not None:
            try:
                url = engine.fxsound_download_url()
            except AttributeError:
                url = ""
        if url:
            webbrowser.open(url)

    # ------------------------------------------- load FxSound's full state --

    @staticmethod
    def _fx_signature(status) -> tuple:
        """Fingerprint of the status fields the mirror adopts.

        Two statuses with equal fingerprints produce equal UI state, so a
        file rewrite that carries nothing new is skipped rather than
        re-drawn over the user's in-progress edits.
        """
        eq = getattr(status, "equalizer", {}) or {}
        bands = tuple(
            (float(b.get("frequency", 0.0)), float(b.get("gain", 0.0)))
            for b in (eq.get("bands") or [])
        )
        effects = getattr(status, "effects", {}) or {}
        return (
            tuple(sorted((k, round(float(v), 2)) for k, v in effects.items())),
            int(eq.get("num_bands", 0) or 0),
            round(float(eq.get("master_gain", 0.0) or 0.0), 2),
            round(float(eq.get("volume_leveling", 0.0) or 0.0), 2),
            round(float(eq.get("filter_q", 0.0) or 0.0), 2),
            round(float(eq.get("balance", 0.0) or 0.0), 2),
            bands,
            str(getattr(status, "selected_preset", "") or ""),
        )

    def _safe_stamp(self, engine) -> tuple[int, int] | None:
        try:
            return engine.fxsound_status_stamp()
        except AttributeError:
            return None

    def _poll_status_file(self) -> None:
        """Re-mirror when FxSound rewrites status.json on its own.

        Called from ``refresh`` on every tick while the Audio page is open.
        Cheap guards run before anything heavier happens: a band drag must
        not be in progress (a mirror would yank the point out of the user's
        hand), the page must be unlocked (FxSound up), the file stamp must
        actually have changed (one stat call), and the new content must
        differ from what was already applied (the echo of our own pushes
        carries no news). A stamp is only consumed once its file parsed
        cleanly, so a read that landed mid-write is retried next tick.
        """
        if not self._fx_ui_enabled:
            return
        engine = self._engine_provider()
        if engine is None:
            return
        if getattr(self._eq_graph, "_drag_index", None) is not None:
            return
        stamp = self._safe_stamp(engine)
        if stamp is None or stamp == self._mirror_stamp:
            return
        # The file was just written, so it is fresh: reading it back does
        # not need a --status round trip to the application.
        status = engine.fxsound_status(force=False)
        if getattr(status, "error", "") and not getattr(status, "running", False):
            return  # likely read mid-write; the stamp stays pending
        self._mirror_stamp = stamp
        self._last_fx_status = status
        if not getattr(status, "running", False):
            self._render_fxsound(status)
            return
        if time.monotonic() < self._user_edit_until:
            # Our own push (or the user's edit) is still settling. Anything
            # the application writes in this window is our echo or a
            # mid-apply snapshot - never an independent change.
            return
        signature = self._fx_signature(status)
        if signature == self._pushed_fxsig or signature == self._fxsig:
            # The settled echo of what we pushed, or state already mirrored.
            self._fxsig = signature
            return
        bands = (getattr(status, "equalizer", {}) or {}).get("bands") or []
        gains = [abs(float(b.get("gain", 0.0))) for b in bands]
        if self._pushed_not_flat and gains and all(g < 0.05 for g in gains):
            # A flat snapshot right after we pushed a shaped curve is the
            # application's transient state, not a user action. Real
            # flattening done inside FxSound is picked up by "Sync from
            # FxSound now", which bypasses this guard deliberately.
            return
        # FxSound changed its own state (a preset, the app's sliders, the
        # output device): pull the whole thing in again.
        self._load_full_state(status)

    def _load_full_state(self, status=None, force: bool = False) -> Any:
        """Pull everything FxSound knows into the view and the active profile.

        All presets (built-in and user) fill the dropdown, and the
        application's live equalizer, effect levels and selected preset are
        adopted as the active profile. Called before FxSound features are
        used and when the view first sees a running instance, so the Hub
        always starts from the same exact configuration as FxSound.
        """
        engine = self._engine_provider()
        if engine is None:
            return None
        if status is None:
            status = engine.fxsound_status(force=force)
        self._last_fx_status = status
        self._mirror_stamp = self._safe_stamp(engine)
        if not getattr(status, "running", False):
            return status
        # Adopt the live settings into the active profile, then draw them.
        try:
            engine.adopt_fxsound_status(status)
        except AttributeError:
            pass
        self._adopt_equalizer_from_status(status)
        self._sync_effect_vars_from_status(status)
        self._refresh_preset_list(status)
        self._gate_ui()
        # Fingerprint what was applied, so the file poll recognises the echo
        # of this very state and does not mirror it back as a user edit.
        self._fxsig = self._fx_signature(status)
        return status

    def _sync_effect_vars_from_status(self, status) -> None:
        """Mirror FxSound's five effect levels into the sliders."""
        effects = getattr(status, "effects", {}) or {}
        if not effects:
            return
        self._suspend = True
        try:
            for key in ("bass", "clarity", "ambience", "surround",
                        "dynamic_boost"):
                if key in effects and key in self._effect_vars:
                    self._effect_vars[key].set(float(effects[key]))
            if getattr(status, "master_gain", 0.0):
                self._gain_var.set(float(status.master_gain))
        finally:
            self._suspend = False

    def _on_sync_from_fxsound(self) -> None:
        if not self._require_fxsound(prompt_start=False):
            return
        status = self._load_full_state(force=True)
        if status is not None and getattr(status, "running", False):
            preset = getattr(status, "selected_preset", "") or "no preset"
            self._eq_banner.set(
                f"Loaded every preset and FxSound's current settings "
                f"(preset: {preset}).", "info")

    # --------------------------------------------------------------- events --

    def _on_toggle(self) -> None:
        if self._suspend:
            return
        engine = self._engine_provider()
        if engine is None:
            return
        engine.set_enabled(self._enabled_var.get(), "native")
        self._on_changed()
        self.refresh()

    def _on_effect(self) -> None:
        if self._suspend:
            return
        engine = self._engine_provider()
        if engine is None:
            return
        endpoint = engine.default_endpoint()
        if endpoint is None:
            return
        profile = engine.profile_for(endpoint.device_id, endpoint.name)
        from ..audio.profiles import AudioProfile
        profile = dataclass_replace(
            profile,
            bass=self._effect_vars["bass"].get(),
            clarity=self._effect_vars["clarity"].get(),
            ambience=self._effect_vars["ambience"].get(),
            surround=self._effect_vars["surround"].get(),
            dynamic_boost=self._effect_vars["dynamic_boost"].get(),
            master_gain_db=self._gain_var.get(),
        )
        engine.set_profile(profile)
        self._on_changed()
        # The five effect sliders go out on the same live path as the EQ.
        self._request_live_push()

    def _on_reset_profile(self) -> None:
        engine = self._engine_provider()
        if engine is None:
            return
        endpoint = engine.default_endpoint()
        if endpoint is None:
            return
        from ..audio.profiles import AudioProfile
        engine.set_profile(AudioProfile(device_id=endpoint.device_id,
                                        device_name=endpoint.name))
        self._on_changed()
        self.refresh()

    # ----------------------------------------------------- equalizer events --

    def _active_profile(self):
        """The profile for the default output, or None if there is no engine."""
        engine = self._engine_provider()
        if engine is None:
            return None, None
        endpoint = engine.default_endpoint()
        if endpoint is None:
            return engine, None
        return engine, engine.profile_for(endpoint.device_id, endpoint.name)

    def _commit_eq(self, **changes) -> None:
        """Write EQ changes to the profile and push them to the audio path."""
        if self._suspend:
            return
        engine, profile = self._active_profile()
        if engine is None or profile is None:
            return
        updated = dataclass_replace(profile, **changes)
        engine.set_profile(updated)
        self._on_changed()

    def _on_band_dragged(self, index: int, gain_db: float) -> None:
        engine, profile = self._active_profile()
        if engine is None or profile is None:
            return
        from ..audio.fac import default_band_frequencies
        count = profile.eq_bands or 10
        frequencies = default_band_frequencies(count)
        # The graph always works on a full set of bands; a profile with no
        # stored curve still needs one to drag against.
        gains = [g for _f, g in profile.eq] or [0.0] * count
        while len(gains) < count:
            gains.append(0.0)
        gains[index] = gain_db
        bands = list(zip(frequencies[:count], gains[:count]))
        self._commit_eq(eq=bands)
        self._refresh_eq_summary(bands)
        # Hear it while dragging: the curve goes to FxSound live.
        self._request_live_push()

    def _on_band_count(self, _event=None) -> None:
        try:
            count = int(self._band_var.get().split()[0])
        except (ValueError, IndexError):
            return
        from ..audio.fac import default_band_frequencies
        frequencies = default_band_frequencies(count)
        self._commit_eq(eq_bands=count,
                        eq=[(freq, 0.0) for freq in frequencies])
        self._eq_graph.set_curve(frequencies, [0.0] * count)
        self._refresh_eq_summary([(f, 0.0) for f in frequencies])
        self._request_live_push()

    def _on_eq_control(self, key: str) -> None:
        self._commit_eq(**{key: self._eq_vars[key].get()})
        self._request_live_push()

    def _request_live_push(self) -> None:
        """Push the current curve to FxSound as the user moves a control.

        The backend coalesces the stream into rate-bounded single-invocation
        pushes on its own thread, and its focus guard keeps FxSound from
        raising itself over the user's window while dragging.
        """
        if not getattr(self._last_fx_status, "running", False):
            return
        engine, profile = self._active_profile()
        if engine is None or profile is None:
            return
        push = getattr(engine, "fxsound_request_live_push", None)
        if callable(push):
            push(profile)
            # Open the echo-suppression window around this push. The echo
            # fingerprint carries the effect levels FxSound last reported:
            # the push does not touch them, so the settled status.json will
            # still hold the application's own values, and matching on them
            # keeps the settled echo from reading as an external change.
            echo = _ProfileEcho(profile)
            live_effects = getattr(self._last_fx_status, "effects", None)
            if live_effects:
                echo.effects = dict(live_effects)
            self._user_edit_until = time.monotonic() + 3.0
            self._pushed_fxsig = self._fx_signature(echo)
            gains = [g for _f, g in (profile.eq or [])]
            self._pushed_not_flat = any(abs(g) >= 0.05 for g in gains)

    def _on_reset_eq(self) -> None:
        _, profile = self._active_profile()
        if profile is None:
            return
        from ..audio.fac import default_band_frequencies
        count = profile.eq_bands or 10
        frequencies = default_band_frequencies(count)
        self._commit_eq(eq=[(freq, 0.0) for freq in frequencies],
                        master_gain_db=0.0, volume_leveling_db=0.0,
                        filter_q=1.0, balance_db=0.0)
        self._eq_graph.set_curve(frequencies, [0.0] * count)
        for key in self._eq_vars:
            self._eq_vars[key].set(0.0)
        self._refresh_eq_summary([(freq, 0.0) for freq in frequencies])
        self._request_live_push()

    def _refresh_eq_summary(self, bands) -> None:
        touched = [g for _f, g in bands if abs(g) >= 0.05]
        if not touched:
            self._eq_pill.set("Flat", IDLE)
            return
        peak = max(touched, key=abs)
        word = "Boost" if peak > 0 else "Cut"
        self._eq_pill.set(f"{len(touched)} bands · {word} {abs(peak):.1f} dB", LIVE)

    # -------------------------------------------------------- preset files --

    def _refresh_preset_list(self, status=None) -> None:
        """Fill the preset dropdown with every preset FxSound reports."""
        engine = self._engine_provider()
        if engine is None:
            return
        if status is None:
            status = self._last_fx_status or engine.fxsound_status()
            self._last_fx_status = status
        names = list(getattr(status, "built_in_presets", []) or [])
        names += [n for n in (getattr(status, "user_presets", []) or [])
                  if n not in names]
        selected = getattr(status, "selected_preset", "") or ""
        if selected and selected not in names:
            names.insert(0, selected)
        self._preset_box.configure(values=names)
        if selected:
            self._preset_var.set(selected)
        elif names and not self._preset_var.get():
            self._preset_var.set(names[0])

    def _on_pick_preset(self, _event=None) -> None:
        name = self._preset_var.get()
        engine = self._engine_provider()
        if engine is None or not name:
            return
        if not self._require_fxsound():
            return
        _, profile = self._active_profile()
        if profile is None:
            return
        status = engine.apply_profile_via_fxsound(
            dataclass_replace(profile, preset_name=name), "")
        if status.error:
            self._eq_banner.set(f"FxSound refused that preset: {status.error}",
                                "error")
            return
        # Read the preset back out of FxSound so the graph shows what it
        # actually applied, rather than what we asked for.
        self._load_full_state(status)

    def _adopt_equalizer_from_status(self, status) -> None:
        eq = getattr(status, "equalizer", {}) or {}
        bands = eq.get("bands") or []
        if not bands:
            return
        frequencies = [float(b.get("frequency", 0.0)) for b in bands]
        gains = [float(b.get("gain", 0.0)) for b in bands]
        self._suspend = True
        try:
            self._band_var.set(f"{int(eq.get('num_bands', len(bands)))} Bands")
            self._eq_graph.set_curve(frequencies, gains,
                                     enabled=self._fx_ui_enabled)
            for key in ("master_gain_db", "volume_leveling_db",
                        "filter_q", "balance_db"):
                if key in self._eq_vars and key in eq:
                    self._eq_vars[key].set(float(eq[key]))
        finally:
            self._suspend = False
        self._refresh_eq_summary(list(zip(frequencies, gains)))

    def _on_load_preset(self) -> None:
        """Apply the preset chosen in the dropdown to the running audio path."""
        self._on_pick_preset()

    def _on_save_preset(self) -> None:
        engine = self._engine_provider()
        if engine is None:
            return
        if not self._require_fxsound():
            return
        _, profile = self._active_profile()
        if profile is None:
            return
        from tkinter import simpledialog
        name = simpledialog.askstring("Save preset", "Preset name:",
                                      parent=self)
        if not name:
            return
        # FxSound saves the *running* instance's settings, so the profile has
        # to be applied first or the saved preset would be the old curve.
        engine.apply_profile_via_fxsound(
            dataclass_replace(profile, preset_name=""), "")
        saved = self._save_via_backend(name)
        if saved:
            self._eq_banner.set(f"Saved the preset “{name}”.", "info")
            # Read the preset lists back so the new name is in the dropdown.
            self._load_full_state(force=True)
        else:
            self._eq_banner.set(
                "FxSound did not save the preset. It refuses when the current "
                "preset has no unsaved changes, or when the user preset limit "
                "is reached.", "warn")

    def _save_via_backend(self, name: str) -> bool:
        engine = self._engine_provider()
        backend = getattr(engine, "_fxsound", None)
        if backend is None:
            return False
        return bool(backend.save_preset(name))

    def _on_import_fac(self) -> None:
        from tkinter import filedialog
        from ..audio.profiles import AudioProfile
        path = filedialog.askopenfilename(
            parent=self, title="Open an FxSound preset",
            filetypes=[("FxSound preset", "*.fac"), ("All files", "*.*")])
        if not path:
            return
        engine, profile = self._active_profile()
        if engine is None or profile is None:
            return
        if not self._require_fxsound():
            return
        imported, error = AudioProfile.from_fac(
            path, profile.device_id, profile.device_name)
        if error and not imported.eq:
            self._eq_banner.set(f"Could not read that preset: {error}", "error")
            return
        # Hand the imported curve to the running FxSound instance - the
        # curve only, never a preset selection (selecting the file's named
        # preset would reload the application's stored copy over the curve
        # we just pushed). Then read back what actually applied.
        status = engine.apply_curve_via_fxsound(imported)
        if status.error:
            engine.set_profile(imported)
        self._on_changed()
        self._show_profile_eq(imported)
        if error:
            self._eq_banner.set(error, "warn")
        elif status.error:
            self._eq_banner.set(
                f"Loaded “{imported.preset_name or imported.name}”, but "
                f"FxSound did not accept it: {status.error}", "warn")
        else:
            # Keep the file's name as metadata, but adopt the *applied*
            # curve FxSound reports so the view shows reality.
            self._last_fx_status = status
            self._mirror_stamp = self._safe_stamp(engine)
            self._fxsig = self._fx_signature(status)
            self._refresh_preset_list(status)
            self._eq_banner.set(
                f"Loaded “{imported.preset_name or imported.name}” into "
                "FxSound.", "info")

    def _on_export_fac(self) -> None:
        from tkinter import filedialog
        # The exported file should be the configuration FxSound is actually
        # running, so mirror its live settings into the profile first.
        if self._last_fx_status is not None and \
                getattr(self._last_fx_status, "running", False):
            self._load_full_state(self._last_fx_status)
        _, profile = self._active_profile()
        if profile is None:
            return
        suggested = f"{profile.preset_name or profile.name or 'preset'}.fac"
        path = filedialog.asksaveasfilename(
            parent=self, title="Save an FxSound preset",
            initialfile=suggested, defaultextension=".fac",
            filetypes=[("FxSound preset", "*.fac")])
        if not path:
            return
        ok, message = profile.write_fac(path)
        if ok:
            self._eq_banner.set(f"Saved the preset file to {path}.", "info")
        else:
            self._eq_banner.set(message, "error")

    def _show_profile_eq(self, profile) -> None:
        from ..audio.fac import default_band_frequencies
        count = profile.eq_bands or 10
        if profile.eq:
            frequencies = [f for f, _g in profile.eq]
            gains = [g for _f, g in profile.eq]
        else:
            frequencies = default_band_frequencies(count)
            gains = [0.0] * count
        self._suspend = True
        try:
            self._band_var.set(f"{count} Bands")
            self._eq_graph.set_curve(frequencies, gains,
                                     enabled=self._fx_ui_enabled)
            for key in self._eq_vars:
                self._eq_vars[key].set(float(getattr(profile, key)))
        finally:
            self._suspend = False
        self._refresh_eq_summary(list(zip(frequencies, gains)))

    # -------------------------------------------------------- output device --

    def refresh_outputs(self) -> None:
        """Repopulate the output list. Called when endpoints change."""
        engine = self._engine_provider()
        if engine is None:
            return
        try:
            endpoints = engine.endpoints()
        except Exception:
            return
        self._endpoints_by_name = {e.name: e.device_id for e in endpoints}
        self._output_box.configure(values=list(self._endpoints_by_name))
        current = engine.default_endpoint()
        if current is not None and current.name in self._endpoints_by_name:
            self._output_var.set(current.name)

    def _on_set_output(self) -> None:
        """Point Windows (and FxSound) at the chosen output device."""
        engine = self._engine_provider()
        name = self._output_var.get()
        device_id = getattr(self, "_endpoints_by_name", {}).get(name, "")
        if engine is None:
            return
        if not device_id:
            self._audio_banner.set("Choose an output device first.", "warn")
            return
        # Routed through the engine so the rate limit applies to the automatic
        # switch as well as the manual one.
        ok, message = engine.set_output_device(device_id, name)
        if ok:
            self._audio_banner.set(f"Output is now {name}.", "info")
        else:
            self._audio_banner.set(message, "warn")
        self._on_changed()

    def _on_auto_output(self) -> None:
        engine = self._engine_provider()
        if engine is None:
            return
        engine.set_auto_output(bool(self._auto_output_var.get()))
        self._on_changed()

    def maybe_auto_output(self) -> None:
        """Switch to the headset if the user asked us to.

        Called when a device-arrival notification arrives. Three guards keep
        this from misbehaving, because it runs unattended:

        * it is a no-op unless the toggle is on;
        * it does nothing if the chosen device is already the default;
        * the engine rate-limits the change itself, so a device that keeps
          arriving and leaving cannot turn this into a loop of disruptive
          endpoint reassignments.
        """
        engine = self._engine_provider()
        if engine is None or not engine.auto_output:
            return
        self.refresh_outputs()
        name = self._output_var.get()
        if not name:
            return
        current = engine.default_endpoint()
        if current is not None and current.name == name:
            return
        device_id = getattr(self, "_endpoints_by_name", {}).get(name, "")
        if not device_id:
            return
        ok, message = engine.set_output_device(device_id, name)
        if ok:
            self._audio_banner.set(f"Switched to {name} on connect.", "info")
            self._on_changed()

    def _probe_fxsound(self, quiet: bool = False) -> None:
        engine = self._engine_provider()
        if engine is None:
            return
        try:
            status = engine.fxsound_status(force=True)
        except AttributeError:
            return
        self._last_fx_status = status
        self._render_fxsound(status)
        if getattr(status, "running", False):
            self._load_full_state(status)
        if not quiet and not getattr(status, "running", False):
            ready, reason = self._fxsound_ready()
            if reason == "missing":
                self._eq_banner.set(
                    "FxSound is not installed. Install it from the download "
                    "page to use the equalizer and presets here.", "warn")
            elif reason == "stopped":
                self._eq_banner.set(
                    "FxSound is installed but not running. Press “Start "
                    "FxSound” to use the equalizer and presets.", "warn")

    # -------------------------------------------------------------- refresh --

    def refresh(self) -> None:
        engine = self._engine_provider()
        self._suspend = True
        try:
            if engine is None:
                self._status_pill.set("Unavailable", FAULT)
                self._audio_banner.set(
                    "The audio engine could not be loaded. Install sounddevice "
                    "and numpy, then restart the Hub.", "warn")
                self._suspend = False
                return

            state = engine.processing_state
            enabled = engine.enabled
            self._enabled_var.set(enabled)

            if enabled and state.active:
                self._status_pill.set("Processing", LIVE)
            elif enabled:
                self._status_pill.set("Starting", WARN)
            else:
                self._status_pill.set("Off", IDLE)

            endpoint = engine.default_endpoint()
            device_text = state.device_name or (endpoint.name if endpoint else "--")
            self._kv_device.set(device_text)
            if state.active and state.sample_rate:
                self._kv_format.set(
                    f"{state.sample_rate / 1000:.1f} kHz · {state.channels} ch"
                )
                self._kv_blocks.set(
                    f"{state.blocks_processed} blocks · {state.drops} dropped"
                )
            else:
                self._kv_format.set("--")
                self._kv_blocks.set("--")

            if endpoint is not None and (state.active or enabled):
                profile = engine.profile_for(endpoint.device_id, endpoint.name)
                self._effect_vars["bass"].set(profile.bass)
                self._effect_vars["clarity"].set(profile.clarity)
                self._effect_vars["ambience"].set(profile.ambience)
                self._effect_vars["surround"].set(profile.surround)
                self._effect_vars["dynamic_boost"].set(profile.dynamic_boost)
                self._gain_var.set(profile.master_gain_db)
                if not self._eq_loaded:
                    # Only seed the graph once: refreshing on every tick would
                    # wipe out a curve the user is in the middle of dragging.
                    self._eq_loaded = True
                    self._show_profile_eq(profile)
                    self._refresh_preset_list()

            if state.active and state.error:
                self._audio_banner.set(state.error, "error")
            elif enabled and not state.active:
                self._audio_banner.set(
                    state.error or "Processing is on but the stream has not started.",
                    "warn",
                )
            else:
                self._audio_banner.set(
                    "Processing follows the Windows default output. Switch "
                    "devices in Windows and the Hub follows.", "info",
                )

            if not hasattr(self, "_fxsound_checked"):
                self._fxsound_checked = True
                # First look at FxSound: if it is already running, load every
                # preset and mirror its live settings before anything else
                # can use the feature.
                self._load_full_state(force=True)
                self.refresh_outputs()
                self._auto_output_var.set(bool(getattr(engine, "auto_output", False)))
            else:
                # While the page is open, follow FxSound: when the
                # application rewrote its status file on its own (preset
                # change, its own sliders, output switch), re-mirror it.
                self._poll_status_file()
                # Re-render from the cached status so the gate follows
                # without probing on every tick.
                self._render_fxsound(self._last_fx_status)
        finally:
            self._suspend = False

    def _render_fxsound(self, status: Any) -> None:
        found = bool(getattr(status, "found", False))
        running = bool(getattr(status, "running", False))
        if not found:
            self._fxsound_pill.set("Not installed", IDLE)
            self._fxsound_detail.configure(
                text=(
                    "FxSound (free, open source) is not installed. Install it "
                    "to drive the equalizer and its presets from the Hub; the "
                    "Hub's own DSP works without it."
                ),
                fg=FAINT,
            )
            self._gate_ui()
            return
        if not running:
            self._fxsound_pill.set("Installed · not running", WARN)
            self._fxsound_detail.configure(
                text=(
                    "FxSound is installed but is not running. Its equalizer "
                    "and presets are locked here until the application is up; "
                    "press “Start FxSound” to launch it."
                ),
                fg=MUTED,
            )
            self._gate_ui()
            return
        if getattr(status, "power", False):
            self._fxsound_pill.set("Active", LIVE)
        else:
            self._fxsound_pill.set("Running · standby", IDLE)
        parts = []
        if status.selected_output:
            parts.append(f"Output: {status.selected_output}")
        if status.selected_preset:
            parts.append(f"Preset: {status.selected_preset}")
        effects = getattr(status, "effects", {}) or {}
        if effects:
            pretty = ", ".join(
                f"{name.title()} {value:.1f}" for name, value in sorted(effects.items())
            )
            parts.append(f"Effects: {pretty}")
        self._fxsound_detail.configure(
            text=" · ".join(parts) if parts else "FxSound is running.",
            fg=PAPER if status.power else MUTED,
        )
        self._gate_ui()
