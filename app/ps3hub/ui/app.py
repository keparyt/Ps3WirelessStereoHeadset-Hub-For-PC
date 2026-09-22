"""The main window.

Threading rules observed here, because Tk is not thread safe:

* background threads never call a Tk method; they put work on a queue;
* :meth:`_tick` runs on the Tk thread and is the only place the interface is
  touched;
* the binding profile is read from the dispatch thread when an input arrives
  and written from the Tk thread when the user edits it, so every access goes
  through ``_profile_lock``.
"""

from __future__ import annotations

import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Callable

from .. import APP_NAME, APP_VERSION
from ..actions import ActionContext, ActionRunner, KeySender
from ..applog import get_logger
from ..config import AppConfig, ConfigStore, Settings
from ..device import EventType, HeadsetService, ServiceEvent
from ..inputs import InputEvent
from ..mappings import Profile
from .theme import (
    ABYSS, DECK, FAINT, FAULT, ICE, IDLE, LIVE, MUTED, PANEL, PAPER, RIDGE,
    WARN, apply, fonts,
)
from .view_dashboard import DashboardView
from .view_diagnostics import DiagnosticsView
from .view_mapping import MappingView
from .view_settings import SettingsView
from .widgets import NavButton, StatusPill

log = get_logger("ui")

TICK_MS = 70
REFRESH_MS = 160
SAVE_DELAY_MS = 900
STATUS_CLEAR_MS = 6000

MIN_WIDTH = 1040
MIN_HEIGHT = 680


class HubApp(tk.Tk):
    def __init__(self, store: ConfigStore | None = None) -> None:
        super().__init__()
        self._store = store or ConfigStore()
        self._config: AppConfig = self._store.load()
        self._profile_lock = threading.RLock()
        self._ui_requests: "queue.Queue[Callable[[], None]]" = queue.Queue()
        self._save_job: str | None = None
        self._status_job: str | None = None
        self._refresh_accumulator = 0

        self.title(f"{APP_NAME}")
        self.minsize(MIN_WIDTH, MIN_HEIGHT)
        self._restore_geometry()
        self._apply_icon()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        apply(self)
        self._fonts = fonts()

        self._service = HeadsetService(
            input_handler=self._on_input,
            read_all_collections=self._config.settings.read_all_collections,
            settle_seconds=self._config.settings.settle_seconds,
            debounce_seconds=self._config.settings.debounce_seconds,
            resync_threshold=self._config.settings.resync_threshold,
        )

        self._runner = ActionRunner(ActionContext(
            keys=KeySender(),
            notify=self._notify_threadsafe,
            show_window=self._show_window_threadsafe,
        ))

        self._build()
        self._service.start()
        self._select_view("dashboard")
        self.after(TICK_MS, self._tick)

        if self._config.settings.start_minimized:
            self.iconify()
        if self._store.last_error:
            self._set_status(
                "The saved configuration could not be read, so defaults were "
                "loaded. The old file was kept alongside it.", "error",
            )

    # ---------------------------------------------------------------- build --

    def _build(self) -> None:
        font = self._fonts
        self.configure(bg=ABYSS)

        rail = tk.Frame(self, bg=DECK, width=196)
        rail.pack(side="left", fill="y")
        rail.pack_propagate(False)

        brand = tk.Frame(rail, bg=DECK)
        brand.pack(fill="x", pady=(20, 22), padx=16)
        tk.Label(brand, text="Headset Hub", bg=DECK, fg=PAPER,
                 font=(font.light, 17), anchor="w").pack(fill="x")
        tk.Label(brand, text="PlayStation wireless stereo", bg=DECK, fg=FAINT,
                 font=font.tiny, anchor="w").pack(fill="x")

        self._nav: dict[str, NavButton] = {}
        for key, label, glyph in (
            ("dashboard", "Dashboard", "◉"),
            ("mapping", "Mapping", "⌘"),
            ("diagnostics", "Diagnostics", "≡"),
            ("settings", "Settings", "⚙"),
        ):
            button = NavButton(rail, label, glyph,
                               lambda k=key: self._select_view(k))
            button.pack(fill="x")
            self._nav[key] = button

        rail_footer = tk.Frame(rail, bg=DECK)
        rail_footer.pack(side="bottom", fill="x", padx=16, pady=16)
        self._rail_pill = StatusPill(rail_footer, "Starting", IDLE, bg=DECK, width=160)
        self._rail_pill.pack(fill="x")
        tk.Label(rail_footer, text=f"Version {APP_VERSION}", bg=DECK, fg=FAINT,
                 font=font.tiny, anchor="w").pack(fill="x", pady=(8, 0))

        # -- main column -------------------------------------------------------
        main = tk.Frame(self, bg=ABYSS)
        main.pack(side="left", fill="both", expand=True)

        header = tk.Frame(main, bg=ABYSS)
        header.pack(fill="x", padx=22, pady=(20, 14))
        self._heading = tk.Label(header, text="Dashboard", bg=ABYSS, fg=PAPER,
                                 font=font.headline, anchor="w")
        self._heading.pack(side="left")
        self._subheading = tk.Label(header, text="", bg=ABYSS, fg=MUTED,
                                    font=font.small, anchor="w")
        self._subheading.pack(side="left", padx=(12, 0), pady=(8, 0))

        self._master_var = tk.BooleanVar(value=self._config.settings.mappings_enabled)
        toggle = ttk.Checkbutton(
            header, text="Bindings active", variable=self._master_var,
            command=self._toggle_master, style="TCheckbutton",
        )
        toggle.pack(side="right")

        self._content = tk.Frame(main, bg=ABYSS)
        self._content.pack(fill="both", expand=True, padx=22)

        # -- status bar --------------------------------------------------------
        status = tk.Frame(main, bg=DECK, height=32)
        status.pack(fill="x", side="bottom")
        status.pack_propagate(False)
        self._status_label = tk.Label(status, text="Ready", bg=DECK, fg=MUTED,
                                      font=font.small, anchor="w")
        self._status_label.pack(side="left", padx=16)
        self._counter_label = tk.Label(status, text="", bg=DECK, fg=FAINT,
                                       font=font.tiny, anchor="e")
        self._counter_label.pack(side="right", padx=16)

        # -- views -------------------------------------------------------------
        self._views: dict[str, tk.Frame] = {}
        self._dashboard = DashboardView(self._content, self._get_profile)
        self._mapping = MappingView(
            self._content,
            profile_getter=self._get_profile,
            on_changed=self._profile_changed,
            on_save=self._save_now,
            on_reset=self._reset_bindings,
            on_import=self._import_profile,
            on_export=self._export_profile,
        )
        self._mapping.set_test_callback(self._test_action)
        self._diagnostics = DiagnosticsView(self._content, self._service)
        self._settings = SettingsView(
            self._content,
            settings_getter=lambda: self._config.settings,
            on_changed=self._settings_changed,
            on_reset_all=self._reset_everything,
        )
        self._views = {
            "dashboard": self._dashboard,
            "mapping": self._mapping,
            "diagnostics": self._diagnostics,
            "settings": self._settings,
        }

    def _apply_icon(self) -> None:
        """Set the window icon, from source tree or a PyInstaller bundle."""
        candidates = []
        bundled = getattr(sys, "_MEIPASS", None)
        if bundled:
            candidates.append(Path(bundled) / "ps3hub.ico")
        candidates.append(
            Path(__file__).resolve().parents[2] / "packaging" / "ps3hub.ico"
        )
        for candidate in candidates:
            try:
                if candidate.exists():
                    self.iconbitmap(default=str(candidate))
                    return
            except Exception:
                # Tk only supports .ico on Windows; elsewhere this is expected.
                log.debug("Window icon not applied from %s", candidate)
                return

    def _restore_geometry(self) -> None:
        geometry = self._config.settings.window_geometry
        if geometry:
            try:
                self.geometry(geometry)
                return
            except Exception:
                log.debug("Saved window geometry was rejected: %r", geometry)
        self.geometry(f"{MIN_WIDTH}x{MIN_HEIGHT}")

    # ----------------------------------------------------------- navigation --

    def _select_view(self, key: str) -> None:
        for name, view in self._views.items():
            if name == key:
                view.pack(fill="both", expand=True, pady=(0, 14))
            else:
                view.pack_forget()
        for name, button in self._nav.items():
            button.set_selected(name == key)
        self._current = key
        self._heading.configure(text={
            "dashboard": "Dashboard",
            "mapping": "Mapping",
            "diagnostics": "Diagnostics",
            "settings": "Settings",
        }[key])
        self._subheading.configure(text={
            "dashboard": "live headset state",
            "mapping": "bind headset controls to actions",
            "diagnostics": "reports, collections and logs",
            "settings": "preferences and detection tuning",
        }[key])
        self._refresh_accumulator = REFRESH_MS  # force an immediate refresh

    # ---------------------------------------------------------------- profile --

    def _get_profile(self) -> Profile:
        with self._profile_lock:
            return self._config.profile

    def _profile_changed(self) -> None:
        self._schedule_save()

    # ------------------------------------------------------------ input path --

    def _on_input(self, event: InputEvent) -> None:
        """Runs on the dispatch thread. Keeps the media-key latency low."""
        if not self._config.settings.mappings_enabled:
            return
        with self._profile_lock:
            mapping = self._config.profile.bound_for(event.input_id)
            if mapping is None:
                return
            action_id = mapping.action_id
            params = dict(mapping.params)
        self._runner.run(action_id, params, repeat=event.repeat)

    def _test_action(self, action_id: str, params: dict[str, Any]) -> None:
        ok = self._runner.run(action_id, params, repeat=1)
        if ok:
            self._set_status("Action sent.", "ok")

    # ------------------------------------------------------- cross-thread UI --

    def _notify_threadsafe(self, level: str, message: str) -> None:
        self._ui_requests.put(lambda: self._set_status(message, level))

    def _show_window_threadsafe(self) -> None:
        self._ui_requests.put(self._bring_to_front)

    def _bring_to_front(self) -> None:
        try:
            self.deiconify()
            self.lift()
            self.focus_force()
        except Exception:
            pass

    # ------------------------------------------------------------------ tick --

    def _tick(self) -> None:
        try:
            self._drain_ui_requests()
            self._drain_service_events()
            self._refresh_accumulator += TICK_MS
            if self._refresh_accumulator >= REFRESH_MS:
                self._refresh_accumulator = 0
                self._refresh()
        except Exception:
            log.exception("Interface update failed")
        finally:
            self.after(TICK_MS, self._tick)

    def _drain_ui_requests(self) -> None:
        while True:
            try:
                callback = self._ui_requests.get_nowait()
            except queue.Empty:
                return
            try:
                callback()
            except Exception:
                log.exception("Queued interface callback failed")

    def _drain_service_events(self) -> None:
        saw_status = False
        for event in self._service.poll_events():
            self._dashboard.on_event(event)
            self._mapping.on_event(event)
            self._handle_service_event(event)
            if event.type == EventType.STATUS:
                saw_status = True

        # A status report is the authoritative headset state. Do not wait for
        # the normal 160 ms UI refresh interval after a real HID report arrives.
        # The next Tk tick will repaint from the newest headset snapshot.
        if saw_status:
            self._refresh_accumulator = REFRESH_MS

    def _handle_service_event(self, event: ServiceEvent) -> None:
        if event.type == EventType.RECEIVER_ATTACHED:
            self._set_status("USB receiver connected.", "ok")
        elif event.type == EventType.RECEIVER_DETACHED:
            self._set_status("USB receiver disconnected.", "warn")
        elif event.type == EventType.ERROR:
            self._set_status(event.message, "error")
        elif event.type == EventType.UNKNOWN_REPORT:
            self._set_status(
                "A report shape that is not in the protocol arrived. It is listed "
                "under Diagnostics.", "warn",
            )
        elif event.type == EventType.INPUT and event.input_event is not None:
            mapping = self._get_profile().bound_for(event.input_event.input_id)
            if mapping and self._config.settings.mappings_enabled:
                self._set_status(
                    f"{event.input_event} → {mapping.action_label}", "info"
                )
            else:
                self._set_status(f"{event.input_event} detected.", "info")

    def _refresh(self) -> None:
        state = self._service.snapshot()

        if not state.backend_available:
            self._rail_pill.set("Unavailable", FAULT)
        elif not state.receiver_present:
            self._rail_pill.set("No receiver", IDLE)
        elif state.headset_linked and not state.status_stale:
            self._rail_pill.set("Connected", LIVE)
        elif state.headset_linked:
            self._rail_pill.set("Idle", WARN)
        else:
            self._rail_pill.set("Headset off", IDLE)

        current = self._current
        if current == "dashboard":
            self._dashboard.refresh(state, self._get_profile())
        elif current == "diagnostics":
            self._diagnostics.refresh(state)

        self._counter_label.configure(
            text=(
                f"{state.status_reports} status · {state.inputs_detected} inputs · "
                f"{self._runner.executed} actions"
            )
        )

    # ---------------------------------------------------------------- status --

    def _set_status(self, message: str, tone: str = "info") -> None:
        colour = {"ok": LIVE, "warn": WARN, "error": FAULT,
                  "info": MUTED}.get(tone, MUTED)
        self._status_label.configure(text=message, fg=colour)
        if self._status_job is not None:
            try:
                self.after_cancel(self._status_job)
            except Exception:
                pass
        self._status_job = self.after(
            STATUS_CLEAR_MS, lambda: self._status_label.configure(text="Ready",
                                                                  fg=MUTED)
        )

    # --------------------------------------------------------------- settings --

    def _toggle_master(self) -> None:
        self._config.settings.mappings_enabled = self._master_var.get()
        self._settings.reload()
        self._schedule_save()
        self._set_status(
            "Bindings are active." if self._master_var.get()
            else "Bindings are paused. Inputs are still detected.",
            "ok" if self._master_var.get() else "warn",
        )

    def _settings_changed(self, settings: Settings) -> None:
        previous = self._config.settings
        self._config.settings = settings
        self._master_var.set(settings.mappings_enabled)

        self._service.configure_detection(
            settings.settle_seconds, settings.debounce_seconds,
            settings.resync_threshold,
        )
        if settings.read_all_collections != previous.read_all_collections:
            self._service.read_all_collections = settings.read_all_collections
            self._set_status(
                "Collection selection changed. It takes effect within a second.",
                "info",
            )
        if settings.verbose_logging != previous.verbose_logging:
            import logging
            logging.getLogger().setLevel(
                logging.DEBUG if settings.verbose_logging else logging.INFO
            )
        self._schedule_save()

    # ------------------------------------------------------------- persistence --

    def _schedule_save(self) -> None:
        if self._save_job is not None:
            try:
                self.after_cancel(self._save_job)
            except Exception:
                pass
        self._save_job = self.after(SAVE_DELAY_MS, self._save_now)

    def _save_now(self) -> None:
        self._save_job = None
        try:
            self._config.settings.window_geometry = self.geometry()
        except Exception:
            pass
        with self._profile_lock:
            ok = self._store.save(self._config)
        if ok:
            self._set_status("Saved.", "ok")
        else:
            self._set_status(
                f"Could not save the configuration: {self._store.last_error}", "error"
            )

    def _reset_bindings(self) -> None:
        if not messagebox.askyesno(
            "Reset bindings",
            "Replace every binding with the defaults?\n\n"
            "Chat mix up and down become next and previous track, the VSS button "
            "becomes play/pause, and the volume wheel changes the Windows volume.",
            parent=self,
        ):
            return
        with self._profile_lock:
            self._config.profile = Profile.default()
        self._mapping.reload()
        self._save_now()
        self._set_status("Bindings reset to defaults.", "ok")

    def _reset_everything(self) -> None:
        if not messagebox.askyesno(
            "Reset everything",
            "Restore the default settings and bindings?\n\n"
            "This cannot be undone.",
            parent=self,
        ):
            return
        with self._profile_lock:
            self._config = AppConfig()
        self._master_var.set(self._config.settings.mappings_enabled)
        self._service.configure_detection(
            self._config.settings.settle_seconds,
            self._config.settings.debounce_seconds,
            self._config.settings.resync_threshold,
        )
        self._service.read_all_collections = self._config.settings.read_all_collections
        self._settings.reload()
        self._mapping.reload()
        self._save_now()
        self._set_status("Everything reset.", "ok")

    def _import_profile(self, path: str) -> None:
        imported = self._store.import_profile(Path(path))
        if imported is None:
            self._set_status(
                f"Could not import that file: {self._store.last_error}", "error"
            )
            return
        with self._profile_lock:
            self._config.profile = imported
        self._mapping.reload()
        self._save_now()
        self._set_status(f"Imported {len(imported)} binding(s).", "ok")

    def _export_profile(self, path: str) -> None:
        if self._store.export_profile(self._get_profile(), Path(path)):
            self._set_status(f"Exported to {Path(path).name}.", "ok")
        else:
            self._set_status(
                f"Could not export: {self._store.last_error}", "error"
            )

    # ------------------------------------------------------------------ close --

    def _on_close(self) -> None:
        log.info("Shutting down")
        try:
            self._config.settings.window_geometry = self.geometry()
            with self._profile_lock:
                self._store.save(self._config)
        except Exception:
            log.exception("Could not save on exit")
        try:
            self._service.stop()
        except Exception:
            log.exception("Could not stop the device service cleanly")
        self.destroy()
