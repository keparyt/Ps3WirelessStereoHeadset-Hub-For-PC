"""Dashboard: what the headset is doing right now."""

from __future__ import annotations

import time
import tkinter as tk
from collections import deque
from typing import Deque

from ..device import EventType, ServiceEvent, ServiceState
from ..inputs import describe_input
from ..mappings import Profile
from ..protocol import TARGET_ADAPTER_MODEL, TARGET_HEADSET_MODEL
from .theme import (
    ABYSS, FAINT, FAULT, ICE, IDLE, LIVE, MUTED, PANEL, PAPER, RIDGE, WARN, fonts,
)
from .widgets import (
    BalanceBar, BatteryGauge, Banner, Card, KeyValue, ScrollFrame, StatusPill,
    VolumeLadder,
)

FEED_LIMIT = 40


class DashboardView(tk.Frame):
    def __init__(self, master, profile_getter) -> None:
        super().__init__(master, bg=ABYSS)
        self._profile_getter = profile_getter
        self._feed: Deque[tuple[float, str, str]] = deque(maxlen=FEED_LIMIT)
        self._feed_dirty = True
        self._volume_level_10 = None
        self._build()

    # --------------------------------------------------------------- build --

    def _build(self) -> None:
        font = fonts()
        scroller = ScrollFrame(self, bg=ABYSS)
        scroller.pack(fill="both", expand=True)
        root = scroller.interior
        root.columnconfigure(0, weight=3, uniform="dash")
        root.columnconfigure(1, weight=2, uniform="dash")

        # -- hero -----------------------------------------------------------
        hero = Card(root, padding=20)
        hero.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        body = hero.body
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)

        left = tk.Frame(body, bg=PANEL)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 24))

        top = tk.Frame(left, bg=PANEL)
        top.pack(fill="x")
        tk.Label(top, text="Headset volume", bg=PANEL, fg=MUTED,
                 font=font.small).pack(side="left")
        self._link_pill = StatusPill(top, "Not connected", IDLE, bg=PANEL, width=170)
        self._link_pill.pack(side="right")

        self._volume = VolumeLadder(left)
        self._volume.pack(fill="x", pady=(6, 0))

        right = tk.Frame(body, bg=PANEL)
        right.grid(row=0, column=1, sticky="nsew")

        self._battery = BatteryGauge(right)
        self._battery.pack(fill="x", pady=(2, 10))

        pills = tk.Frame(right, bg=PANEL)
        pills.pack(fill="x")
        self._vss_pill = StatusPill(pills, "Surround off", IDLE, bg=PANEL, width=160)
        self._vss_pill.pack(anchor="w")
        self._mic_pill = StatusPill(pills, "Mic unknown", IDLE, bg=PANEL, width=160)
        self._mic_pill.pack(anchor="w", pady=(2, 0))

        self._balance = BalanceBar(right)
        self._balance.pack(fill="x", pady=(10, 0))

        # -- receiver -------------------------------------------------------
        receiver = Card(root, "Receiver")
        receiver.grid(row=1, column=0, sticky="nsew", padx=(0, 12), pady=(0, 12))
        self._kv_receiver = KeyValue(receiver.body, "USB receiver", "Not detected")
        self._kv_receiver.pack(fill="x", pady=2)
        self._kv_model = KeyValue(receiver.body, "Headset", "Waiting")
        self._kv_model.pack(fill="x", pady=2)
        self._kv_collection = KeyValue(receiver.body, "Listening on", "--")
        self._kv_collection.pack(fill="x", pady=2)
        self._kv_status = KeyValue(receiver.body, "Status reports", "0")
        self._kv_status.pack(fill="x", pady=2)
        self._kv_age = KeyValue(receiver.body, "Last report", "never")
        self._kv_age.pack(fill="x", pady=2)
        self._kv_raw = KeyValue(receiver.body, "Last bytes", "--", mono=True)
        self._kv_raw.pack(fill="x", pady=2)

        self._notice = Banner(
            receiver.body,
            "This headset only reports when something changes. Silence is normal: "
            "turn the volume wheel or press a button to wake it up.",
        )
        self._notice.pack(fill="x", pady=(10, 0))

        # -- live feed ------------------------------------------------------
        feed_card = Card(root, "Live input", "most recent first")
        feed_card.grid(row=1, column=1, sticky="nsew", pady=(0, 12))
        self._feed_box = tk.Frame(feed_card.body, bg=PANEL)
        self._feed_box.pack(fill="both", expand=True)
        self._feed_empty = tk.Label(
            self._feed_box,
            text="Press a control on the headset and it will appear here.",
            bg=PANEL, fg=FAINT, font=font.small, wraplength=280, justify="left",
        )
        self._feed_empty.pack(anchor="w")
        self._feed_rows: list[tk.Frame] = []

        # -- bindings summary ------------------------------------------------
        bindings = Card(root, "Active bindings")
        bindings.grid(row=2, column=0, columnspan=2, sticky="ew")
        self._bindings_box = tk.Frame(bindings.body, bg=PANEL)
        self._bindings_box.pack(fill="x")
        self._bindings_rows: list[tk.Frame] = []
        self._bindings_signature = ""

    # ------------------------------------------------------------- updating --

    def on_event(self, event: ServiceEvent) -> None:
        if event.type == EventType.STATUS:
            snapshot = event.payload.get("snapshot")
            if snapshot is not None and snapshot.headset_connected:
                # STATUS events are handled one at a time on the Tk thread.
                # Paint each authoritative report immediately so rapid HID
                # changes are not visually collapsed into only the final state.
                if self._volume_level_10 is None and snapshot.volume_level is not None:
                    self._volume_level_10 = min(10, max(0, snapshot.volume_level * 2))
                self._volume.set(
                    self._volume_level_10,
                    self._volume_level_10 * 10 if self._volume_level_10 is not None else None,
                    muted=snapshot.mic_muted,
                )
                self._battery.set(snapshot.battery_percent, snapshot.charging)
                self._balance.set(snapshot.chat_balance)
                self._vss_pill.set(
                    "Surround on" if snapshot.vss else "Surround off",
                    ICE if snapshot.vss else IDLE,
                )
                self._mic_pill.set(
                    "Mic muted" if snapshot.mic_muted else "Mic live",
                    WARN if snapshot.mic_muted else LIVE,
                )
            return

        if event.type != EventType.INPUT or event.input_event is None:
            return
        input_event = event.input_event
        if input_event.input_id == "volume_up":
            self._volume_level_10 = min(10, (self._volume_level_10 if self._volume_level_10 is not None else 0) + 1)
            self._volume.set(self._volume_level_10, self._volume_level_10 * 10)
        elif input_event.input_id == "volume_down":
            self._volume_level_10 = max(0, (self._volume_level_10 if self._volume_level_10 is not None else 0) - 1)
            self._volume.set(self._volume_level_10, self._volume_level_10 * 10)
        profile = self._profile_getter()
        mapping = profile.bound_for(input_event.input_id) if profile else None
        outcome = mapping.action_label if mapping else "not bound"
        label = str(input_event)
        self._feed.appendleft((time.time(), label, outcome))
        self._feed_dirty = True

    def refresh(self, state: ServiceState, profile: Profile) -> None:
        snapshot = state.snapshot
        linked = state.headset_linked and not state.status_stale

        if not state.receiver_present:
            self._link_pill.set("No receiver", IDLE)
        elif state.status_stale and snapshot is None:
            self._link_pill.set("Waiting", WARN)
        elif state.headset_linked:
            self._link_pill.set("Connected", LIVE)
        else:
            self._link_pill.set("Headset off", IDLE)

        if snapshot is not None and state.headset_linked:
            if self._volume_level_10 is None and snapshot.volume_level is not None:
                self._volume_level_10 = min(10, max(0, snapshot.volume_level * 2))
            self._volume.set(
                self._volume_level_10,
                self._volume_level_10 * 10 if self._volume_level_10 is not None else None,
                muted=snapshot.mic_muted,
            )
            self._battery.set(snapshot.battery_percent, snapshot.charging)
            self._balance.set(snapshot.chat_balance)
            self._vss_pill.set(
                "Surround on" if snapshot.vss else "Surround off",
                ICE if snapshot.vss else IDLE,
            )
            self._mic_pill.set(
                "Mic muted" if snapshot.mic_muted else "Mic live",
                WARN if snapshot.mic_muted else LIVE,
            )
        else:
            self._volume_level_10 = None
            self._volume.set(None, None)
            self._battery.set(None, False)
            self._balance.set(None)
            self._vss_pill.set("Surround unknown", IDLE)
            self._mic_pill.set("Mic unknown", IDLE)

        self._kv_receiver.set(
            "Connected" if state.receiver_present else "Not detected",
            LIVE if state.receiver_present else MUTED,
        )
        if snapshot is not None:
            self._kv_model.set(snapshot.model)
        elif state.receiver_present:
            self._kv_model.set(f"{TARGET_HEADSET_MODEL} (expected)", MUTED)
        else:
            self._kv_model.set("Waiting", MUTED)

        self._kv_collection.set(
            f"{state.last_report_collection or '--'}"
            + (f" · {state.readers_active} open" if state.readers_active else "")
        )
        self._kv_status.set(f"{state.status_reports} of {state.total_reports} reports")

        age = state.status_age()
        if age is None:
            self._kv_age.set("never", MUTED)
        elif age < 1:
            self._kv_age.set("just now", LIVE)
        else:
            self._kv_age.set(f"{age:.0f}s ago", MUTED if age < 30 else FAINT)
        self._kv_raw.set(state.last_report_hex or "--")

        if state.last_error:
            self._notice.set(state.last_error, "error")
        elif not state.backend_available:
            self._notice.set(state.backend_message, "warn")
        elif not state.receiver_present:
            self._notice.set(
                f"Plug in the {TARGET_ADAPTER_MODEL} USB receiver. It will be picked "
                "up automatically.", "warn",
            )
        else:
            self._notice.set(
                "This headset only reports when something changes. Silence is normal: "
                "turn the volume wheel or press a button to wake it up.", "info",
            )

        if self._feed_dirty:
            self._render_feed()
            self._feed_dirty = False
        self._render_bindings(profile)

    # -------------------------------------------------------------- render --

    def _render_feed(self) -> None:
        for row in self._feed_rows:
            row.destroy()
        self._feed_rows.clear()

        if not self._feed:
            self._feed_empty.pack(anchor="w")
            return
        self._feed_empty.pack_forget()

        font = fonts()
        for when, label, outcome in list(self._feed)[:9]:
            row = tk.Frame(self._feed_box, bg=PANEL)
            row.pack(fill="x", pady=1)
            tk.Label(row, text=time.strftime("%H:%M:%S", time.localtime(when)),
                     bg=PANEL, fg=FAINT, font=font.code_small).pack(side="left")
            tk.Label(row, text=label, bg=PANEL, fg=PAPER, font=font.strong,
                     anchor="w").pack(side="left", padx=(8, 0))
            tk.Label(row, text=outcome, bg=PANEL,
                     fg=ICE if outcome != "not bound" else FAINT,
                     font=font.small, anchor="e").pack(side="right")
            self._feed_rows.append(row)

    def _render_bindings(self, profile: Profile) -> None:
        active = profile.active if profile else []
        signature = "|".join(f"{m.input_id}:{m.action_label}" for m in active)
        if signature == self._bindings_signature:
            return
        self._bindings_signature = signature

        for row in self._bindings_rows:
            row.destroy()
        self._bindings_rows.clear()

        font = fonts()
        if not active:
            row = tk.Frame(self._bindings_box, bg=PANEL)
            row.pack(fill="x")
            tk.Label(row, text="Nothing is bound yet. Open Mapping to set up controls.",
                     bg=PANEL, fg=FAINT, font=font.small).pack(anchor="w")
            self._bindings_rows.append(row)
            return

        grid = tk.Frame(self._bindings_box, bg=PANEL)
        grid.pack(fill="x")
        self._bindings_rows.append(grid)
        for column in range(2):
            grid.columnconfigure(column, weight=1, uniform="bind")

        for index, mapping in enumerate(active):
            cell = tk.Frame(grid, bg=PANEL)
            cell.grid(row=index // 2, column=index % 2, sticky="ew", pady=3,
                      padx=(0, 18) if index % 2 == 0 else (0, 0))
            tk.Label(cell, text=describe_input(mapping.input_id).label, bg=PANEL,
                     fg=PAPER, font=font.strong, anchor="w", width=20).pack(side="left")
            tk.Label(cell, text="→", bg=PANEL, fg=RIDGE, font=font.base).pack(side="left")
            tk.Label(cell, text=mapping.action_label, bg=PANEL, fg=ICE,
                     font=font.base, anchor="w").pack(side="left", padx=(8, 0))
