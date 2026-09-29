"""Audio view: Windows audio processing controls.

Lets the user enable the Hub's loopback DSP, tune the effect profile for the
default output, and see at a glance what the engine is actually doing -
including the optional FxSound integration when that application is
installed.

Everything here reads from the :class:`~ps3hub.audio.engine.AudioEngine`
facade; no COM, no HID and no thread ownership leaks into the view.
"""

from __future__ import annotations

import tkinter as tk
from dataclasses import replace as dataclass_replace
from tkinter import ttk
from typing import Any, Callable

from ..applog import get_logger
from .theme import ABYSS, FAINT, FAULT, ICE, IDLE, LIVE, MUTED, PANEL, PAPER, WARN, fonts
from .widget_eq import BAND_COUNTS, EQGraph
from .widgets import Banner, Card, KeyValue, ScrollFrame, StatusPill

log = get_logger("ui.audio")


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
        ttk.Button(fxsound.body, text="Check FxSound status",
                   command=self._probe_fxsound).pack(anchor="w", pady=(10, 0))

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
            files, textvariable=self._preset_var, state="readonly", width=24)
        self._preset_box.pack(side="left")
        self._preset_box.bind("<<ComboboxSelected>>", self._on_pick_preset)
        ttk.Button(files, text="Load", command=self._on_load_preset
                   ).pack(side="left", padx=(8, 0))
        ttk.Button(files, text="Save as...", command=self._on_save_preset
                   ).pack(side="left", padx=(6, 0))
        ttk.Button(files, text="Import .fac...", command=self._on_import_fac
                   ).pack(side="left", padx=(6, 0))
        ttk.Button(files, text="Export .fac...", command=self._on_export_fac
                   ).pack(side="left", padx=(6, 0))

        self._eq_banner = Banner(
            body,
            "The equalizer is applied by FxSound when it is the active "
            "processor, and by the Hub's own DSP otherwise.",
        )
        self._eq_banner.pack(fill="x", pady=(10, 0))

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

    def _on_eq_control(self, key: str) -> None:
        self._commit_eq(**{key: self._eq_vars[key].get()})

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

    def _refresh_eq_summary(self, bands) -> None:
        touched = [g for _f, g in bands if abs(g) >= 0.05]
        if not touched:
            self._eq_pill.set("Flat", IDLE)
            return
        peak = max(touched, key=abs)
        word = "Boost" if peak > 0 else "Cut"
        self._eq_pill.set(f"{len(touched)} bands · {word} {abs(peak):.1f} dB", LIVE)

    # -------------------------------------------------------- preset files --

    def _refresh_preset_list(self) -> None:
        engine = self._engine_provider()
        if engine is None:
            return
        status = engine.fxsound_status()
        names = list(status.user_presets)
        if status.selected_preset and status.selected_preset not in names:
            names.insert(0, status.selected_preset)
        self._preset_box.configure(values=names)
        if status.selected_preset:
            self._preset_var.set(status.selected_preset)

    def _on_pick_preset(self, _event=None) -> None:
        name = self._preset_var.get()
        engine = self._engine_provider()
        if engine is None or not name:
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
        self._adopt_equalizer_from_status(status)

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
            self._eq_graph.set_curve(frequencies, gains)
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
        from tkinter import simpledialog
        engine = self._engine_provider()
        _, profile = self._active_profile()
        if engine is None or profile is None:
            return
        name = simpledialog.askstring("Save preset", "Preset name:",
                                      parent=self)
        if not name:
            return
        # FxSound saves the *running* instance's settings, so the profile has
        # to be applied first or the saved preset would be the old curve.
        engine.apply_profile_via_fxsound(
            dataclass_replace(profile, preset_name=""), "")
        status = engine.fxsound_status()
        backend_status = self._save_via_backend(name)
        if backend_status:
            self._eq_banner.set(f"Saved the preset “{name}”.", "info")
            self._refresh_preset_list()
        else:
            self._eq_banner.set(
                "FxSound did not save the preset. It refuses when the current "
                "preset has no unsaved changes, or when the user preset limit "
                "is reached.", "warn")
        del status

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
        imported, error = AudioProfile.from_fac(
            path, profile.device_id, profile.device_name)
        if error and not imported.eq:
            self._eq_banner.set(f"Could not read that preset: {error}", "error")
            return
        engine.set_profile(imported)
        self._on_changed()
        self._show_profile_eq(imported)
        if error:
            self._eq_banner.set(error, "warn")
        else:
            self._eq_banner.set(
                f"Loaded “{imported.preset_name or imported.name}” from the "
                "preset file.", "info")

    def _on_export_fac(self) -> None:
        from tkinter import filedialog
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
            self._eq_graph.set_curve(frequencies, gains)
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
        from ..audio.device_monitor import set_default_render_endpoint
        name = self._output_var.get()
        device_id = getattr(self, "_endpoints_by_name", {}).get(name, "")
        if not device_id:
            self._audio_banner.set("Choose an output device first.", "warn")
            return
        ok, message = set_default_render_endpoint(device_id)
        if ok:
            # FxSound holds its own output selection, so it has to be told
            # separately or it keeps playing to the previous device.
            engine = self._engine_provider()
            if engine is not None:
                engine.fxsound_set_output(name)
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

        Called when a device-arrival notification arrives. It is a no-op
        unless the toggle is on, so the common case costs nothing.
        """
        engine = self._engine_provider()
        if engine is None or not engine.auto_output:
            return
        self.refresh_outputs()
        current = engine.default_endpoint()
        if current is not None and current.name == self._output_var.get():
            return
        name = self._output_var.get()
        if not name:
            return
        self._output_var.set(name)
        self._on_set_output()

    def _probe_fxsound(self) -> None:
        engine = self._engine_provider()
        if engine is None:
            return
        status = engine.fxsound_status(force=True)
        self._render_fxsound(status)

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
                self._render_fxsound(engine.fxsound_status())
                self.refresh_outputs()
                self._auto_output_var.set(bool(getattr(engine, "auto_output", False)))
        finally:
            self._suspend = False

    def _render_fxsound(self, status: Any) -> None:
        if not getattr(status, "found", False):
            self._fxsound_pill.set("Not installed", IDLE)
            self._fxsound_detail.configure(
                text=(
                    "FxSound (free, open source) is not installed. The Hub's "
                    "own DSP works without it; this integration only drives an "
                    "existing installation through its documented command line."
                ),
                fg=FAINT,
            )
            return
        if not getattr(status, "running", False):
            self._fxsound_pill.set("Installed", IDLE)
            self._fxsound_detail.configure(
                text="FxSound is installed but is not running, so its status is unknown.",
                fg=MUTED,
            )
            return
        if getattr(status, "power", False):
            self._fxsound_pill.set("Active", LIVE)
        else:
            self._fxsound_pill.set("Standby", IDLE)
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
