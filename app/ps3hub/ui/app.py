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
from ..updates import UpdateChecker
from ..config import AppConfig, ConfigStore, Settings
from ..device import EventType, HeadsetService, ServiceEvent
from ..inputs import InputEvent, InputId
from ..mappings import Profile
from ..tray import (
    TrayManager, format_tray_status, tray_icon_kind,
)
from ..notify_service import NotificationService
from .theme import (
    ABYSS, DECK, FAINT, FAULT, GOLD, IDLE, LIVE, MUTED, PANEL, PAPER, RIDGE,
    WARN, apply, fonts,
)
from .toast import ToastCenter
from .view_audio import AudioView
from .view_dashboard import DashboardView
from .view_diagnostics import DiagnosticsView
from .view_mapping import MappingView
from .view_settings import SettingsView
from .widgets import NavButton, StatusPill

#: Application-wide event bus. Tests may monkeypatch this import.
try:
    from ..events import bus as app_event_bus
except Exception:  # pragma: no cover
    app_event_bus = None

log = get_logger("ui")


class _TrayStateView:
    """Adapter that pairs the service snapshot with the logical state.

    ``format_tray_status`` reads ``headset_state`` for the logical volume and
    every other attribute from the plain service snapshot.
    """

    def __init__(self, service_state: Any, headset_state: Any) -> None:
        self._service_state = service_state
        self.headset_state = headset_state

    def __getattr__(self, name: str) -> Any:
        return getattr(self._service_state, name)


TICK_MS = 70
REFRESH_MS = 160
SAVE_DELAY_MS = 900
STATUS_CLEAR_MS = 6000

MIN_WIDTH = 1040
MIN_HEIGHT = 680


def _load_audio_engine():
    """Create the AudioEngine, or None when audio deps are missing."""
    try:
        from ..audio import AudioEngine
        return AudioEngine()
    except Exception as exc:
        log.warning("Audio engine unavailable: %s", exc)
        return None


class HubApp(tk.Tk):
    def __init__(
        self,
        store: ConfigStore | None = None,
        tray_mode: bool = False,
    ) -> None:
        super().__init__()
        self._store = store or ConfigStore()
        self._tray_mode = bool(tray_mode)
        self._tray: TrayManager | None = None
        self._shutting_down = False
        self._config: AppConfig = self._store.load()
        self._profile_lock = threading.RLock()
        self._ui_requests: "queue.Queue[Callable[[], None]]" = queue.Queue()
        self._save_job: str | None = None
        self._status_job: str | None = None
        self._refresh_accumulator = 0
        self._previous_headset_linked = False

        self.title(f"{APP_NAME}")
        self.minsize(MIN_WIDTH, MIN_HEIGHT)
        self._restore_geometry()
        self._apply_icon()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Unmap>", self._on_unmap, add="+")

        apply(self)
        self._fonts = fonts()
        log.debug(
            "UI constructed: %s %s | python %s | display %sx%s | settings: "
            "mappings=%s settle=%sms debounce=%sms resync=%s low_battery=%s%% "
            "force_windows_toasts=%s",
            APP_NAME, APP_VERSION, sys.version.split()[0],
            self.winfo_screenwidth(), self.winfo_screenheight(),
            self._config.settings.mappings_enabled,
            self._config.settings.settle_seconds,
            self._config.settings.debounce_seconds,
            self._config.settings.resync_threshold,
            self._config.settings.low_battery_threshold,
            getattr(self._config.settings, "force_windows_toasts", False),
        )

        self._service = HeadsetService(
            input_handler=self._on_input,
            read_all_collections=self._config.settings.read_all_collections,
            settle_seconds=self._config.settings.settle_seconds,
            debounce_seconds=self._config.settings.debounce_seconds,
            resync_threshold=self._config.settings.resync_threshold,
            low_battery_threshold=self._config.settings.low_battery_threshold,
        )

        # Toasts: one center for every user-facing notification. The
        # in-app overlay serves the news while the window is visible; a
        # Windows toast takes over when it is hidden or minimised - or
        # always, when Settings forces it.
        self._notifier = ToastCenter(self, APP_NAME)
        self._notifier.set_force_windows(
            bool(getattr(self._config.settings, "force_windows_toasts", False)))

        # Event-driven notifications: the dispatch thread publishes logical
        # state changes on the bus; the service decides what deserves a toast.
        # Clicking a connection or battery toast opens the Hub.
        self._notify_service = NotificationService(
            self._notifier, on_click=self._bring_to_front)
        self._notify_service.attach(app_event_bus)
        self._notify_service.apply_settings(
            connection=self._config.settings.notify_connection,
            volume=self._config.settings.notify_volume,
            audio=self._config.settings.notify_audio,
        )

        # Audio subsystem: loopback DSP + device monitor + optional FxSound.
        self._audio = _load_audio_engine()
        if self._audio is not None:
            self._audio.import_state(self._config.audio)
            self._audio.start()
            if self._config.audio.get("enabled"):
                self._audio.set_enabled(True, self._config.audio.get("backend", "native"))

        # Update check: throttled to one request a day on a daemon thread;
        # the toast only fires when a genuinely newer release exists.
        self._updates = UpdateChecker()
        self._updates.check(on_done=self._on_update_result)

        self._runner = ActionRunner(ActionContext(
            keys=KeySender(),
            notify=self._notify_threadsafe,
            show_window=self._show_window_threadsafe,
        ))

        self._build()
        self._service.start()
        self._select_view("dashboard")
        self.after(TICK_MS, self._tick)

        if self._tray_mode:
            self._tray = TrayManager(
                status_provider=lambda: format_tray_status(
                    _TrayStateView(self._service.snapshot(),
                                   self._service.headset_state())
                ),
                icon_kind_provider=lambda: tray_icon_kind(
                    self._service.headset_state().linked,
                    self._service.headset_state().battery_percent,
                    self._service.headset_state().charging,
                ),
                on_open=lambda: self._ui_requests.put(self._bring_to_front),
                on_hide=lambda: self._ui_requests.put(self._hide_to_tray),
                on_refresh=self._service.request_scan,
                on_exit=lambda: self._ui_requests.put(self._shutdown),
                app_name=APP_NAME,
            )
            if self._tray.start():
                self.withdraw()
            else:
                log.error("Tray mode was requested but the system tray could not be started")
                self._tray = None
                self._tray_mode = False

        if not self._tray_mode and self._config.settings.start_minimized:
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
            ("audio", "Audio", "♪"),
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
        self._dashboard = DashboardView(
            self._content, self._get_profile,
            audio_provider=(lambda: self._audio.as_dict()
                            if self._audio is not None else None),
        )
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
        self._audio_view = AudioView(
            self._content,
            engine_provider=lambda: self._audio,
            on_changed=self._schedule_save,
            # The Audio page's no-access overlay can send the user to
            # Settings; the switch happens on the Tk thread via the queue.
            on_navigate=lambda key: self._ui_requests.put(
                lambda k=key: self._select_view(k)),
        )
        self._settings = SettingsView(
            self._content,
            settings_getter=lambda: self._config.settings,
            on_changed=self._settings_changed,
            on_reset_all=self._reset_everything,
        )
        self._views = {
            "dashboard": self._dashboard,
            "audio": self._audio_view,
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

    def _on_update_result(self, info) -> None:
        """Worker-thread callback: surface a new release as a toast."""
        if info is None:
            return
        self.after(0, lambda: self._notifier.show(
            "Update available",
            f"{info.label} - click to open the download page.",
            "info",
            on_click=lambda: self._open_url(info.url),
        ))

    def _open_url(self, url: str) -> None:
        import webbrowser
        try:
            webbrowser.open(url)
        except Exception:
            log.debug("Could not open %s", url)

    def _select_view(self, key: str) -> None:
        log.debug("View switched: %s -> %s", getattr(self, "_current", "?"), key)
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
            "audio": "Audio",
            "mapping": "Mapping",
            "diagnostics": "Diagnostics",
            "settings": "Settings",
        }[key])
        self._subheading.configure(text={
            "dashboard": "live headset state",
            "audio": "Windows audio processing",
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
            action_label = mapping.action_label
        ok = self._runner.run(action_id, params, repeat=event.repeat)
        if ok and self._config.settings.action_toast:
            # ToastCenter queues the work, so this stays cheap here.
            self._notifier.show(
                "Shortcut executed",
                f"{event} → {action_label}",
                "info",
                on_click=self._bring_to_front,
            )

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
        """Raise the window. Safe as a toast click callback from any thread."""
        try:
            self.deiconify()
            self.state("normal")
            self.lift()
            self.focus_force()
        except Exception:
            pass

    def _hide_to_tray(self) -> None:
        if not self._tray_mode or self._shutting_down:
            return
        try:
            self.withdraw()
        except Exception:
            pass

    def _on_unmap(self, _event=None) -> None:
        if not self._tray_mode or self._shutting_down:
            return
        try:
            if self.state() == "iconic":
                self.after_idle(self._hide_to_tray)
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
            if (
                event.input_event.input_id == InputId.BATTERY_LOW
                and self._config.settings.low_battery_toast
            ):
                percent = event.input_event.value
                self._notifier.show(
                    "Headset battery low",
                    (
                        f"{percent}% remaining. Put the headset on charge."
                        if percent is not None
                        else "The battery has reached the warning level. "
                        "Put the headset on charge."
                    ),
                    "warn",
                    on_click=self._bring_to_front,
                )

    def _refresh(self) -> None:
        """The 160 ms UI tick. The Audio page pre-syncs from FxSound once
        per visit, on its first tick after being opened (load-only: the
        application's settings are read into the Hub, never pushed back)."""
        state = self._service.snapshot()

        if self._current == "audio":
            try:
                self._audio_view.on_page_open()
            except Exception:
                log.exception("Audio page pre-sync failed")
        else:
            # Every visit to the Audio page pre-syncs from FxSound, so
            # leaving the page re-arms the next visit's sync.
            self._audio_view.on_page_closed()

        if self._tray_mode:
            headset_linked = state.headset_linked
            if headset_linked and not self._previous_headset_linked:
                log.info("Headset connected; showing the interface")
                self._bring_to_front()
            self._previous_headset_linked = headset_linked

        if not state.backend_available:
            self._rail_pill.set("Unavailable", FAULT)
        elif not state.receiver_present:
            self._rail_pill.set("No receiver", IDLE)
        elif state.headset_linked and not state.status_stale:
            self._rail_pill.set("Connected", GOLD)
        elif state.headset_linked:
            self._rail_pill.set("Idle", WARN)
        elif state.snapshot is None:
            self._rail_pill.set("Waiting for headset", IDLE)
        else:
            self._rail_pill.set("Headset off", IDLE)

        current = self._current
        if current == "dashboard":
            self._dashboard.refresh(state, self._get_profile())
        elif current == "audio" and self._audio is not None:
            self._audio_view.refresh()
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

        self._notify_service.apply_settings(
            connection=settings.notify_connection,
            volume=settings.notify_volume,
            audio=settings.notify_audio,
        )
        # Windows toasts can be forced even while the window is visible.
        if hasattr(self._notifier, "set_force_windows"):
            self._notifier.set_force_windows(settings.force_windows_toasts)
        self._service.configure_detection(
            settings.settle_seconds, settings.debounce_seconds,
            settings.resync_threshold, settings.low_battery_threshold,
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
        if self._audio is not None:
            self._config.audio = self._audio.export_state()
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
            self._config.settings.low_battery_threshold,
        )
        self._service.read_all_collections = self._config.settings.read_all_collections
        if self._audio is not None:
            self._audio.import_state(self._config.audio)
            self._audio_view.refresh()
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

    def _save_geometry(self) -> None:
        try:
            self._config.settings.window_geometry = self.geometry()
            with self._profile_lock:
                self._store.save(self._config)
        except Exception:
            log.exception("Could not save the configuration")

    def _on_close(self) -> None:
        if self._tray_mode and not self._shutting_down:
            log.info("Window close requested; hiding to tray")
            self._save_geometry()
            self._hide_to_tray()
            return
        self._shutdown()

    def _shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        log.info("Shutting down (clean exit: saving geometry, stopping audio, "
                 "device service, notifier, tray)")
        self._save_geometry()
        try:
            if self._audio is not None:
                self._audio.shutdown()
        except Exception:
            log.exception("Could not stop the audio engine cleanly")
        try:
            self._service.stop()
        except Exception:
            log.exception("Could not stop the device service cleanly")
        try:
            self._notifier.shutdown()
        except Exception:
            log.exception("Could not stop the notifier cleanly")
        try:
            if self._tray is not None:
                self._tray.stop()
                self._tray = None
        except Exception:
            log.exception("Could not stop the system tray cleanly")
        log.info("Shutdown complete; all subsystems stopped")
        self.destroy()
