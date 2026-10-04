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

import queue
import sys
import threading
import time
import tkinter as tk
from dataclasses import replace as dataclass_replace
from pathlib import Path
from tkinter import messagebox as tkmessagebox
from tkinter import ttk
from typing import Any, Callable

from ..applog import get_logger
from .theme import ABYSS, FAINT, FAULT, GOLD, IDLE, LIVE, MUTED, PANEL, PAPER, WARN, fonts
from .widget_eq import BAND_COUNTS, EQGraph, EQKnobRow
from ..audio.fac import default_band_frequencies
from .widgets import Banner, Card, KeyValue, ScrollFrame, StatusPill

log = get_logger("ui.audio")


def _example_presets() -> list[tuple[str, str, Any]]:
    """Bundled example presets: ``(display name, description, path)``.

    They ship next to the package in ``EQExamples/``: one ``.fac`` curve per
    example plus a ``.txt`` carrying its one-line description. Found beside
    the sources in development and inside a PyInstaller bundle alike.
    """
    directories: list[Any] = []
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        directories.append(Path(bundled) / "EQExamples")
    directories.append(Path(__file__).resolve().parents[2] / "EQExamples")

    for directory in directories:
        if not directory.is_dir():
            continue
        examples: list[tuple[str, str, Any]] = []
        for fac in sorted(directory.glob("*.fac")):
            txt = fac.with_suffix(".txt")
            try:
                description = txt.read_text(encoding="utf-8").strip() if txt.exists() else ""
            except OSError:
                description = ""
            examples.append((fac.stem, description, fac))
        return examples
    return []


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
        # Until this monotonic time, ignore FxSound power echoes: the status
        # written right after our own --power command still reports the old
        # state, and mirroring it would flip the toggle straight back.
        self._power_gate_until = 0.0
        # The most recent FxSound status the view knows about, plus whether
        # the FxSound features are currently unlocked.
        self._last_fx_status: Any = None
        self._fx_ui_enabled = False
        # The page-open pre-sync runs once per visit; refresh() resets the
        # flag whenever the page is closed elsewhere.
        self._fx_loaded_for_page = False
        # The automatic FxSound install (download + silent setup) runs in a
        # worker thread and talks to the UI through this queue.
        self._fx_install_busy = False
        self._fx_install_log: queue.Queue = queue.Queue()
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

        # FxSound's own power switch: the DSP keeps every setting while
        # bypassed, so this is a mute of the processing, not a reset.
        power_row = tk.Frame(processing.body, bg=PANEL)
        power_row.pack(fill="x", pady=(8, 0))
        self._fx_power_var = tk.BooleanVar(value=True)
        self._fx_power_check = ttk.Checkbutton(
            power_row, text="Enable FxSound processing (bypass without losing "
            "your settings)", variable=self._fx_power_var,
            command=self._on_fx_power,
        )
        self._fx_power_check.pack(anchor="w")

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

        # One set of EQ-control variables shared by both cards: the Master
        # gain slider used to exist twice (here and beside the equalizer)
        # with independent values that silently overwrote each other, so the
        # two widgets now bind one variable and move together.
        self._eq_vars: dict[str, tk.DoubleVar] = {}

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
        # The full range the profile and the FxSound CLI accept (-20..+20);
        # the old -12..+12 slider could not reach the values FxSound itself
        # reports, so adopting its state shoved the slider off-scale.
        self._gain_var = self._eq_vars["master_gain_db"] = tk.DoubleVar(value=0.0)
        ttk.Scale(gain_row, from_=-20.0, to=20.0, variable=self._gain_var,
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
            buttons, text="Install FxSound",
            command=self._on_install_fxsound)
        self._fx_install_button.pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Open the download page",
                   command=self._on_open_download_page).pack(
            side="left", padx=(8, 0))
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

        # The fine control under the graph: one knob per band, mirroring the
        # curve above. Knob drags feed the same handler as graph drags. The
        # row takes the graph's log-axis geometry, so every knob sits
        # exactly under its band's point; when the graph re-lays out (a
        # window resize), the columns are re-cut from it.
        self._eq_knobs = EQKnobRow(body, self._on_band_dragged)
        self._eq_knobs.pack(fill="x", pady=(10, 0))
        self._eq_knobs.bind_graph(self._eq_graph)
        self._eq_graph.bind("<Configure>",
                            lambda _e: self._eq_knobs._relayout())

        # The four controls FxSound puts beside its curve. Master gain is
        # deliberately absent: its variable lives on the Effect profile card
        # (both sliders bind the same tk variable and stay in lock-step), so
        # there is exactly one value behind the two widgets.
        grid = tk.Frame(body, bg=PANEL)
        grid.pack(fill="x", pady=(12, 0))
        grid.columnconfigure(1, weight=1)
        eq_controls = (
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

        # The master gain row mirrors the Effect profile card's slider: one
        # shared variable, its own readout.
        gain_row = tk.Frame(grid, bg=PANEL)
        gain_row.grid(row=len(eq_controls), column=0, columnspan=3,
                      sticky="ew", pady=(3, 0))
        tk.Label(gain_row, text="Master gain", bg=PANEL, fg=PAPER,
                 font=font.base, anchor="w").pack(side="left")
        ttk.Scale(gain_row, from_=-20.0, to=20.0,
                  variable=self._eq_vars["master_gain_db"],
                  command=lambda _v: self._on_eq_control("master_gain_db")
                  ).pack(side="left", fill="x", expand=True, padx=(12, 8))
        self._eq_gain_label = tk.Label(gain_row, text="0.0 dB", bg=PANEL,
                                       fg=MUTED, font=font.code_small, width=9)
        self._eq_gain_label.pack(side="right")
        self._eq_vars["master_gain_db"].trace_add(
            "write", lambda *_: self._eq_gain_label.configure(
                text=f"{self._eq_vars['master_gain_db'].get():+.1f} dB")
        )

        # Bundled example presets with their one-line descriptions.
        self._examples = _example_presets()
        if self._examples:
            examples_row = tk.Frame(body, bg=PANEL)
            examples_row.pack(fill="x", pady=(12, 0))
            tk.Label(examples_row, text="Examples", bg=PANEL, fg=PAPER,
                     font=font.base, anchor="w").pack(side="left")
            self._example_var = tk.StringVar(value=self._examples[0][0])
            self._example_box = ttk.Combobox(
                examples_row, textvariable=self._example_var, state="readonly",
                width=16, values=[name for name, _d, _p in self._examples])
            self._example_box.pack(side="left", padx=(10, 0))
            self._example_box.bind("<<ComboboxSelected>>",
                                   self._on_example_description)
            ttk.Button(examples_row, text="Load example",
                       command=self._on_load_example).pack(side="left", padx=(8, 0))
            self._example_description = tk.Label(
                body, text="", bg=PANEL, fg=MUTED, font=font.small,
                anchor="w", justify="left", wraplength=560,
            )
            self._example_description.pack(fill="x", pady=(4, 0))
            self._on_example_description()

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
        self._eq_knobs.set_enabled(ready)
        self._band_menu.configure(state=state)
        self._preset_box.configure(state=state)
        for button in (self._preset_load, self._preset_save,
                       self._preset_import, self._preset_export):
            button.configure(state=state)
        found = bool(getattr(self._last_fx_status or (), "found", False))
        self._fx_start_button.configure(
            state="normal" if found and not ready else "disabled")
        self._fx_install_button.configure(
            state="normal" if not found and not self._fx_install_busy
            else "disabled")

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
                    "FxSound is free and open source. Press \u201cInstall "
                    "FxSound\u201d on this page to install it automatically, "
                    "or get it from:\n" + url,
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

    def _on_install_fxsound(self) -> None:
        """Download and silently install FxSound, then start and load it.

        The setup installs per-machine into Program Files without any
        wizard; the only click it asks for is the UAC confirmation its own
        manifest triggers. The download runs in a worker thread so the page
        stays responsive, narrating into the FxSound detail line.
        """
        engine = self._engine_provider()
        if engine is None or self._fx_install_busy:
            return
        source = ""
        try:
            source = engine.fxsound_setup_url()
        except AttributeError:
            source = ""
        if not tkmessagebox.askyesno(
                "Install FxSound",
                "Download and install FxSound now?\n\n"
                "The official setup runs silently and installs into Program "
                "Files; the only thing it asks for is the standard UAC "
                "confirmation. When it is done the Hub starts it and loads "
                "its settings.\n\n"
                + (f"Source: {source}" if source else ""),
                parent=self):
            return
        self._fx_install_busy = True
        self._gate_ui()
        self._fxsound_detail.configure(text="Downloading FxSound...",
                                       fg=MUTED)
        threading.Thread(target=self._fxsound_install_worker,
                         daemon=True).start()
        self.after(120, self._drain_fxsound_install)

    def _fxsound_install_worker(self) -> None:
        """Worker side of the automatic install; UI calls only via the queue."""
        engine = self._engine_provider()
        if engine is None:
            self._fx_install_log.put(("done", False))
            return

        def report(text: str) -> None:
            self._fx_install_log.put(("log", text))

        def progress(done: int, total: int) -> None:
            self._fx_install_log.put(("progress", done, total))

        try:
            ok = engine.install_fxsound_silently(report=report,
                                                 progress=progress)
        except Exception as error:  # defensive: never take the page down
            log(f"fxsound auto-install failed: {error}")
            self._fx_install_log.put(("done", False))
            return
        self._fx_install_log.put(("done", ok))

    def _drain_fxsound_install(self) -> None:
        """Paint worker messages; re-arms itself until the install finishes."""
        try:
            while True:
                message = self._fx_install_log.get_nowait()
                kind = message[0]
                if kind == "log":
                    self._fxsound_detail.configure(text=str(message[1]),
                                                   fg=MUTED)
                elif kind == "progress":
                    done, total = message[1], message[2]
                    if total:
                        self._fxsound_detail.configure(
                            text=f"Downloading FxSound... "
                                 f"{100 * done / total:.0f}%", fg=MUTED)
                elif kind == "done":
                    self._fxsound_install_done(bool(message[1]))
                    return  # finished: stop polling
        except queue.Empty:
            pass
        self.after(120, self._drain_fxsound_install)

    def _fxsound_install_done(self, ok: bool) -> None:
        self._fx_install_busy = False
        if ok:
            self._eq_banner.set("FxSound installed. Starting it and loading "
                                "its settings...", "info")
            # Starts FxSound if it is not up yet, then loads its live state
            # and unlocks the equalizer - the same path as "Start FxSound".
            self._start_and_settle()
        else:
            self._fxsound_detail.configure(
                text="FxSound was not installed. You can retry, or use “Open "
                     "the download page” to install it by hand.",
                fg=FAINT,
            )
            self._eq_banner.set("FxSound was not installed. Retry the "
                                "automatic install, or get it from the "
                                "download page.", "warn")
            self._gate_ui()

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
        if getattr(self._eq_knobs, "_drag_index", None) is not None:
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
        # Load-only by definition: this button re-reads the application's
        # settings and mirrors them here, it never pushes anything back.
        status = self._load_full_state(force=True)
        if status is not None and getattr(status, "running", False):
            preset = getattr(status, "selected_preset", "") or "no preset"
            self._eq_banner.set(
                f"Loaded every preset and FxSound's current settings "
                f"(preset: {preset}).", "info")

    # --------------------------------------------------------------- events --

    def _on_fx_power(self) -> None:
        """Drive FxSound's power switch from the checkbox.

        The status read back right after the command can still carry the old
        state (the application answers asynchronously), so re-mirroring is
        gated briefly - otherwise the poll would see the stale ``power`` and
        snap the checkbox back.
        """
        if self._suspend:
            return
        engine = self._engine_provider()
        if engine is None or not getattr(self._last_fx_status, "found", False):
            # Nowhere to send it: snap the checkbox back to the real state.
            self._suspend = True
            self._fx_power_var.set(bool(getattr(self._last_fx_status, "power", False)))
            self._suspend = False
            return
        wanted = bool(self._fx_power_var.get())
        try:
            engine.fxsound_set_power(wanted)
        except AttributeError:
            pass
        self._power_gate_until = time.monotonic() + 2.5
        if getattr(self._last_fx_status, "running", False):
            status = engine.fxsound_status(force=True)
            if getattr(status, "running", False):
                self._last_fx_status = status
                self._render_fxsound(status)

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
        # The knob row and the graph share one source of truth: a knob drag
        # already updated the graph via _on_band_dragged, so read the gains
        # back from the graph rather than from the profile, which a running
        # mirror may have changed under us mid-edit.
        knob_gains = self._eq_knobs.gains
        eq = list(profile.eq)
        if knob_gains:
            frequencies = [f for f, _g in eq] or list(
                default_band_frequencies(len(knob_gains)))
            eq = list(zip(frequencies[:len(knob_gains)], knob_gains))
        profile = dataclass_replace(
            profile,
            bass=self._effect_vars["bass"].get(),
            clarity=self._effect_vars["clarity"].get(),
            ambience=self._effect_vars["ambience"].get(),
            surround=self._effect_vars["surround"].get(),
            dynamic_boost=self._effect_vars["dynamic_boost"].get(),
            master_gain_db=self._gain_var.get(),
            eq=eq,
        )
        engine.set_profile(profile)
        self._on_changed()
        # The five effect sliders go out on the same live path as the EQ.
        self._request_live_push()

    def _on_reset_profile(self) -> None:
        """Restore the factory profile and make every surface agree.

        The new profile is written first, then the sliders, the gain, the
        graph and the knob row are re-drawn from it with the change handlers
        suspended - each ``var.set()`` fires its scale's command, and an
        unsuspended set would immediately re-commit the old value over the
        reset. Finally the curve goes out to FxSound, so its own sliders and
        per-band knobs follow instead of silently keeping the previous
        settings (observed live: the Hub reset while FxSound did not).
        """
        engine, profile = self._active_profile()
        if engine is None or profile is None:
            return
        from ..audio.profiles import AudioProfile
        fresh = AudioProfile(device_id=profile.device_id,
                             device_name=profile.device_name)
        engine.set_profile(fresh)
        count = fresh.eq_bands
        frequencies = list(default_band_frequencies(count))
        flat = [0.0] * count
        self._suspend = True
        try:
            self._effect_vars["bass"].set(fresh.bass)
            self._effect_vars["clarity"].set(fresh.clarity)
            self._effect_vars["ambience"].set(fresh.ambience)
            self._effect_vars["surround"].set(fresh.surround)
            self._effect_vars["dynamic_boost"].set(fresh.dynamic_boost)
            self._gain_var.set(fresh.master_gain_db)
            for key, value in (("volume_leveling_db", 0.0),
                               ("filter_q", 1.0), ("balance_db", 0.0)):
                self._eq_vars[key].set(value)
            self._band_var.set(f"{count} Bands")
        finally:
            self._suspend = False
        self._eq_graph.set_curve(frequencies, flat, enabled=self._fx_ui_enabled)
        self._eq_knobs.set_bands(frequencies, flat)
        self._refresh_eq_summary(list(zip(frequencies, flat)))
        self._request_live_push()
        self._on_changed()
        self._eq_banner.set("Profile restored to the factory defaults.", "info")

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
        # Keep the stored frequencies - FxSound's own or a preset's custom
        # centres - rather than snapping the curve back onto the default
        # table on every drag (observed live: FxSound's 20-band centres were
        # silently replaced, so the Hub then pushed the wrong curve).
        stored = list(profile.eq or [])
        frequencies = [f for f, _g in stored[:count]]
        if len(frequencies) < count:
            frequencies += list(
                default_band_frequencies(count)[len(frequencies):])
        # The graph always works on a full set of bands; a profile with no
        # stored curve still needs one to drag against.
        gains = [g for _f, g in stored] or [0.0] * count
        while len(gains) < count:
            gains.append(0.0)
        gains[index] = gain_db
        bands = list(zip(frequencies[:count], gains[:count]))
        self._commit_eq(eq=bands)
        # Keep the knob row on the curve the graph now shows.
        self._eq_knobs.set_bands(frequencies[:count], gains[:count])
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
        # Carry the current curve onto the new band count (fxsound keeps the
        # gains it can when its own band count changes), rather than wiping
        # the user's tuning. Extra bands start flat.
        current = list(self._eq_graph.gains)
        gains = (current + [0.0] * count)[:count]
        self._commit_eq(eq_bands=count,
                        eq=list(zip(frequencies, gains)))
        self._eq_graph.set_curve(frequencies, gains)
        self._eq_knobs.set_bands(frequencies, gains)
        self._refresh_eq_summary(list(zip(frequencies, gains)))
        self._request_live_push()

    def _on_eq_control(self, key: str) -> None:
        if key == "master_gain_db":
            # One shared variable drives both cards; the effect handler owns
            # the master gain commit and the live push for it.
            self._on_effect()
            return
        self._commit_eq(**{key: self._eq_vars[key].get()})
        self._request_live_push()

    def _on_knob_dragged(self, index: int, gain_db: float) -> None:
        """A knob turned: route it through the graph's drag handler.

        The graph owns the curve - redrawing it here keeps the two in lock-
        step without either widget knowing about the other.
        """
        self._on_band_dragged(index, gain_db)

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
            log.debug(
                "Live push queued: bands=%s master=%+.1f q=%.1f leveling=%.1f "
                "balance=%+.1f | effects bass=%.1f clarity=%.1f ambience=%.1f "
                "surround=%.1f boost=%.1f",
                len(profile.eq), profile.master_gain_db, profile.filter_q,
                profile.volume_leveling_db, profile.balance_db,
                profile.bass, profile.clarity, profile.ambience,
                profile.surround, profile.dynamic_boost,
            )
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
        """Flatten the curve and the four EQ controls - the full block.

        The slider vars are set with the change handlers suspended: each set
        fires its scale's command, and filter_q's would otherwise re-commit
        the pre-reset value over the reset (leaving the "reset" Q at 0.0,
        which FxSound then refuses). The flat curve goes out to FxSound, so
        its own per-band knobs clear too.
        """
        _, profile = self._active_profile()
        if profile is None:
            return
        count = profile.eq_bands or 10
        frequencies = list(default_band_frequencies(count))
        flat = [0.0] * count
        self._commit_eq(eq=[(freq, 0.0) for freq in frequencies],
                        master_gain_db=0.0, volume_leveling_db=0.0,
                        filter_q=1.0, balance_db=0.0)
        self._suspend = True
        try:
            # The shared master gain variable serves both cards' sliders.
            self._gain_var.set(0.0)
            self._eq_vars["volume_leveling_db"].set(0.0)
            self._eq_vars["filter_q"].set(1.0)
            self._eq_vars["balance_db"].set(0.0)
        finally:
            self._suspend = False
        self._eq_graph.set_curve(frequencies, flat, enabled=self._fx_ui_enabled)
        self._eq_knobs.set_bands(frequencies, flat)
        self._refresh_eq_summary(list(zip(frequencies, flat)))
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

    def _on_example_description(self, _event=None) -> None:
        """Show the selected example's one-line description."""
        if self._example_var.get() not in [name for name, _d, _p in self._examples]:
            return
        label = self._example_var.get()
        for name, description, _path in self._examples:
            if name == label:
                self._example_description.configure(
                    text=description or f"{name} — a bundled example curve.")
                return

    def _on_load_example(self) -> None:
        """Load the selected example curve into the audio path.

        With FxSound running the curve goes to it directly (no preset
        selection, the same route as a file import); otherwise the Hub's own
        DSP takes it. The example's name is kept as profile metadata.
        """
        label = self._example_var.get()
        example = next(((n, d, p) for n, d, p in self._examples if n == label),
                       None)
        if example is None:
            return
        _name, _description, path = example
        engine, profile = self._active_profile()
        if engine is None or profile is None:
            return
        from ..audio.profiles import AudioProfile
        imported, error = AudioProfile.from_fac(
            path, profile.device_id, profile.device_name)
        if error and not imported.eq:
            self._eq_banner.set(f"Could not read that example: {error}", "error")
            return
        imported.preset_name = label
        if getattr(self._last_fx_status, "running", False):
            status = engine.apply_curve_via_fxsound(imported)
            if status.error:
                engine.set_profile(imported)
                self._eq_banner.set(
                    f"Loaded “{label}” on the Hub's DSP, but FxSound did not "
                    f"accept it: {status.error}", "warn")
            else:
                # Store the imported profile so the Hub matches what was
                # just pushed, and arm the echo window through the normal
                # live-push path (the status read back right after a push
                # can still carry the previous curve).
                engine.set_profile(imported)
                self._request_live_push()
                self._refresh_preset_list()
                self._eq_banner.set(
                    f"Loaded “{label}” into FxSound.", "info")
        else:
            engine.set_profile(imported)
            self._eq_banner.set(
                f"Loaded “{label}” on the Hub's own DSP.", "info")
        self._on_changed()
        self._show_profile_eq(imported)

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
        """Draw exactly what the running application reports.

        status.json lists every band the application's engine holds, but the
        active curve is ``num_bands`` long: a 20-band selection still
        carries 31 entries, and drawing all of them put 31 knobs under a
        "20 Bands" selector (and pushed 31 bands back to the application).
        The list is therefore cut to the declared count, and each slider is
        only touched for values the status actually reports.
        """
        eq = getattr(status, "equalizer", {}) or {}
        bands = eq.get("bands") or []
        if not bands:
            return
        try:
            count = int(eq.get("num_bands") or len(bands))
        except (TypeError, ValueError):
            count = len(bands)
        count = max(1, min(count, len(bands)))
        frequencies = [float(b.get("frequency", 0.0)) for b in bands[:count]]
        gains = [float(b.get("gain", 0.0)) for b in bands[:count]]
        self._suspend = True
        try:
            self._band_var.set(f"{count} Bands")
            # The newly adopted state is drawable regardless of how the
            # gate last rendered: set_enabled was decided for the *previous*
            # status, and leaving the curve disabled makes the adopted
            # graph look dead until the next gate pass.
            self._fx_ui_enabled = True
            self._eq_graph.set_curve(frequencies, gains, enabled=True)
            self._eq_knobs.set_bands(frequencies, gains)
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
        # we just pushed).
        status = engine.apply_curve_via_fxsound(imported)
        if status.error:
            engine.set_profile(imported)
        else:
            # Store the imported profile so the Hub matches what was pushed,
            # and arm the echo window via the normal live-push path.
            engine.set_profile(imported)
            self._request_live_push()
            self._refresh_preset_list()
        self._on_changed()
        self._show_profile_eq(imported)
        if error:
            self._eq_banner.set(error, "warn")
        elif status.error:
            self._eq_banner.set(
                f"Loaded “{imported.preset_name or imported.name}”, but "
                f"FxSound did not accept it: {status.error}", "warn")
        else:
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
            frequencies = [f for f, _g in profile.eq][:count]
            gains = [g for _f, g in profile.eq][:count]
        else:
            frequencies = default_band_frequencies(count)
            gains = [0.0] * count
        # Pad a short stored curve up to the band count so the knob row and
        # the graph agree on the number of visible bands.
        while len(frequencies) < count:
            index = len(frequencies)
            frequencies.append(default_band_frequencies(count)[index])
            gains.append(0.0)
        self._suspend = True
        try:
            self._band_var.set(f"{count} Bands")
            self._eq_graph.set_curve(frequencies, gains,
                                     enabled=self._fx_ui_enabled)
            self._eq_knobs.set_bands(frequencies, gains)
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
                    "FxSound is not installed. Press \u201cInstall FxSound\u201d "
                    "in the FxSound card to set it up automatically.", "warn")
            elif reason == "stopped":
                self._eq_banner.set(
                    "FxSound is installed but not running. Press “Start "
                    "FxSound” to use the equalizer and presets.", "warn")

    # -------------------------------------------------------------- refresh --

    def on_page_open(self) -> None:
        """Pre-sync from FxSound when the user enters the Audio page.

        Runs once per page visit, before the first refresh tick can offer
        any edit: the application's live equalizer, effect levels and
        selected preset are **loaded** into the Hub (never pushed back), so
        the very first slider the user touches continues from the exact
        configuration FxSound is running instead of overwriting it with a
        stale profile.
        """
        if self._fx_loaded_for_page:
            return
        self._fx_loaded_for_page = True
        engine = self._engine_provider()
        if engine is None:
            return
        opener = getattr(engine, "fxsound_page_opened", None)
        if not callable(opener):
            return
        try:
            status = opener()
        except Exception:
            log.exception("FxSound pre-sync on page open failed")
            return
        if status is None:
            return
        self._last_fx_status = status
        if getattr(status, "running", False):
            self._load_full_state(status)
            self._eq_banner.set(
                "Synced with FxSound: its current equalizer, effects and "
                "preset are loaded here.", "info")
        self._render_fxsound(status)

    def on_page_closed(self) -> None:
        """Re-arm the page-open pre-sync for the next visit.

        Every entry into the Audio page syncs from FxSound again, so edits
        made inside the application while the user was on another page are
        picked up before the next edit starts.
        """
        self._fx_loaded_for_page = False

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
                for key in ("volume_leveling_db", "filter_q", "balance_db"):
                    if key in self._eq_vars:
                        self._eq_vars[key].set(float(getattr(profile, key)))
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
                self._fx_loaded_for_page = True
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
        # The checkbox mirrors the application's power unless our own toggle
        # is still settling (the echo of the command can carry the old state).
        if time.monotonic() >= self._power_gate_until:
            was_suspended = self._suspend
            self._suspend = True
            self._fx_power_var.set(bool(getattr(status, "power", False)) if found else False)
            self._suspend = was_suspended
        self._fx_power_check.configure(
            state="normal" if found else "disabled")
        if not found:
            self._fxsound_pill.set("Not installed", IDLE)
            self._fxsound_detail.configure(
                text=(
                    "FxSound (free, open source) is not installed. Press "
                    "\u201cInstall FxSound\u201d to set it up automatically; "
                    "the Hub's own DSP works without it."
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
