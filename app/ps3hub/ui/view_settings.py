"""Settings.

Detection tuning is exposed because the right values depend on the individual
receiver and on how fast the user turns the volume wheel. Every control says
what it does in plain terms and shows its unit, so nobody has to guess.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable

from .. import APP_NAME, APP_VERSION
from ..config import Settings, config_path
from ..protocol import (
    TARGET_ADAPTER_MODEL, TARGET_HEADSET_MARKING, TARGET_HEADSET_MODEL,
    TARGET_PID, TARGET_VID,
)
from .theme import ABYSS, FAINT, ICE, MUTED, PANEL, PAPER, fonts
from .widgets import Banner, Card, ScrollFrame


class SettingsView(tk.Frame):
    def __init__(
        self,
        master,
        settings_getter: Callable[[], Settings],
        on_changed: Callable[[Settings], None],
        on_reset_all: Callable[[], None],
    ) -> None:
        super().__init__(master, bg=ABYSS)
        self._settings_getter = settings_getter
        self._on_changed = on_changed
        self._on_reset_all = on_reset_all
        self._suspend = False
        self._build()
        self.reload()

    # --------------------------------------------------------------- build --

    def _build(self) -> None:
        font = fonts()
        scroller = ScrollFrame(self, bg=ABYSS)
        scroller.pack(fill="both", expand=True)
        root = scroller.interior
        root.columnconfigure(0, weight=1, uniform="set")
        root.columnconfigure(1, weight=1, uniform="set")

        # -- behaviour --------------------------------------------------------
        behaviour = Card(root, "Behaviour")
        behaviour.grid(row=0, column=0, sticky="nsew", padx=(0, 12), pady=(0, 12))

        self._mappings_enabled = self._checkbox(
            behaviour.body, "Run my bindings",
            "When this is off, inputs are still detected and shown, but no action "
            "is carried out. Useful while you are setting bindings up.",
        )
        self._start_minimized = self._checkbox(
            behaviour.body, "Start minimised",
            "Open the window minimised the next time the app starts.",
        )
        self._show_raw = self._checkbox(
            behaviour.body, "Show raw report bytes",
            "Display the hexadecimal report on the dashboard and in diagnostics.",
        )
        self._verbose = self._checkbox(
            behaviour.body, "Detailed logging",
            "Record every report. Turn this on before reporting a problem, then "
            "copy the log from the Diagnostics page.",
        )

        # -- detection ---------------------------------------------------------
        detection = Card(root, "Input detection")
        detection.grid(row=0, column=1, sticky="nsew", pady=(0, 12))

        Banner(
            detection.body,
            "The headset reports its state, not its buttons. A press is worked out "
            "from the change between two reports, so these values decide how "
            "forgiving that reconstruction is.",
        ).pack(fill="x", pady=(0, 12))

        self._settle = self._number(
            detection.body, "Ignore input after connecting", "ms", 0, 3000, 50,
            "The receiver repeats its state when the headset links. Anything "
            "inside this window is treated as an echo, not a press.",
        )
        self._debounce = self._number(
            detection.body, "Minimum gap between repeats", "ms", 0, 1000, 5,
            "Two identical inputs closer together than this count as one.",
        )
        self._resync = self._number(
            detection.body, "Largest believable jump", "steps", 1, 10, 1,
            "A change bigger than this means reports were dropped, so no action "
            "is run. Raise it if fast wheel turns are being missed.",
        )
        self._battery = self._number(
            detection.body, "Low battery warning at", "%", 5, 50, 5,
            "When the battery falls to this level, the Battery low input fires.",
        )

        self._read_all = self._checkbox(
            detection.body, "Open every HID collection",
            "Normally only the status collection is read. Turn this on if inputs "
            "are not being detected at all; it opens the other collections too.",
        )

        # -- device -----------------------------------------------------------
        device = Card(root, "Device")
        device.grid(row=1, column=0, sticky="nsew", padx=(0, 12), pady=(0, 12))
        for label, value in (
            ("Headset", f"{TARGET_HEADSET_MODEL} {TARGET_HEADSET_MARKING}"),
            ("Wireless adapter", TARGET_ADAPTER_MODEL),
            ("USB identity", f"VID 0x{TARGET_VID:04X} / PID 0x{TARGET_PID:04X}"),
            ("Volume steps", "10 discrete hardware levels, shown as a percentage"),
        ):
            row = tk.Frame(device.body, bg=PANEL)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=label, bg=PANEL, fg=MUTED, font=font.small,
                     anchor="w", width=18).pack(side="left")
            tk.Label(row, text=value, bg=PANEL, fg=PAPER, font=font.small,
                     anchor="w", justify="left", wraplength=280).pack(
                side="left", fill="x", expand=True)

        # -- configuration ------------------------------------------------------
        config_card = Card(root, "Configuration")
        config_card.grid(row=1, column=1, sticky="nsew", pady=(0, 12))
        tk.Label(config_card.body, text="Settings and bindings are saved to",
                 bg=PANEL, fg=MUTED, font=font.small, anchor="w").pack(fill="x")
        path_label = tk.Label(
            config_card.body, text=str(config_path()), bg=PANEL, fg=ICE,
            font=font.code_small, anchor="w", justify="left", wraplength=320,
        )
        path_label.pack(fill="x", pady=(3, 12))
        tk.Label(
            config_card.body,
            text="Saving happens automatically when you change a binding.",
            bg=PANEL, fg=FAINT, font=font.tiny, anchor="w",
        ).pack(fill="x")

        buttons = tk.Frame(config_card.body, bg=PANEL)
        buttons.pack(fill="x", pady=(14, 0))
        ttk.Button(buttons, text="Reset everything", style="Danger.TButton",
                   command=self._on_reset_all).pack(side="left")

        tk.Label(
            root, text=f"{APP_NAME} · version {APP_VERSION}",
            bg=ABYSS, fg=FAINT, font=font.tiny,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))

    # -------------------------------------------------------------- helpers --

    def _checkbox(self, parent: tk.Widget, title: str, help_text: str) -> tk.BooleanVar:
        font = fonts()
        var = tk.BooleanVar(value=False)
        frame = tk.Frame(parent, bg=PANEL)
        frame.pack(fill="x", pady=(0, 12))
        ttk.Checkbutton(frame, text=title, variable=var,
                        command=self._emit).pack(anchor="w")
        tk.Label(frame, text=help_text, bg=PANEL, fg=FAINT, font=font.tiny,
                 anchor="w", justify="left", wraplength=320).pack(
            fill="x", padx=(22, 0), pady=(2, 0))
        return var

    def _number(self, parent: tk.Widget, title: str, unit: str, low: int,
                high: int, step: int, help_text: str) -> tk.StringVar:
        font = fonts()
        var = tk.StringVar(value=str(low))
        frame = tk.Frame(parent, bg=PANEL)
        frame.pack(fill="x", pady=(0, 12))

        head = tk.Frame(frame, bg=PANEL)
        head.pack(fill="x")
        tk.Label(head, text=title, bg=PANEL, fg=PAPER, font=font.base,
                 anchor="w").pack(side="left")
        spin = ttk.Spinbox(head, from_=low, to=high, increment=step, width=7,
                           textvariable=var, command=self._emit)
        spin.pack(side="right")
        tk.Label(head, text=unit, bg=PANEL, fg=MUTED, font=font.small).pack(
            side="right", padx=(0, 6))
        var.trace_add("write", lambda *_: self._emit())

        tk.Label(frame, text=help_text, bg=PANEL, fg=FAINT, font=font.tiny,
                 anchor="w", justify="left", wraplength=320).pack(fill="x",
                                                                   pady=(2, 0))
        return var

    @staticmethod
    def _to_int(var: tk.StringVar, fallback: int) -> int:
        try:
            return int(float(var.get()))
        except (TypeError, ValueError):
            return fallback

    # --------------------------------------------------------------- state --

    def reload(self) -> None:
        settings = self._settings_getter()
        self._suspend = True
        self._mappings_enabled.set(settings.mappings_enabled)
        self._start_minimized.set(settings.start_minimized)
        self._show_raw.set(settings.show_raw_reports)
        self._verbose.set(settings.verbose_logging)
        self._read_all.set(settings.read_all_collections)
        self._settle.set(str(int(settings.settle_seconds * 1000)))
        self._debounce.set(str(int(settings.debounce_seconds * 1000)))
        self._resync.set(str(settings.resync_threshold))
        self._battery.set(str(settings.low_battery_threshold))
        self._suspend = False

    def _emit(self) -> None:
        if self._suspend:
            return
        current = self._settings_getter()
        updated = Settings(
            mappings_enabled=self._mappings_enabled.get(),
            start_minimized=self._start_minimized.get(),
            read_all_collections=self._read_all.get(),
            low_battery_threshold=self._to_int(self._battery,
                                               current.low_battery_threshold),
            settle_seconds=self._to_int(
                self._settle, int(current.settle_seconds * 1000)) / 1000,
            debounce_seconds=self._to_int(
                self._debounce, int(current.debounce_seconds * 1000)) / 1000,
            resync_threshold=self._to_int(self._resync, current.resync_threshold),
            verbose_logging=self._verbose.get(),
            show_raw_reports=self._show_raw.get(),
            window_geometry=current.window_geometry,
        ).clamped()
        self._on_changed(updated)
