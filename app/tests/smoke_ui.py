"""Headless smoke test.

Builds the real window, walks every view, pushes synthetic device events
through the pump and exercises the editing paths. Catches Tk API mistakes that
compilation cannot: bad option names, geometry-manager conflicts, missing
attributes on a rarely-visited branch.

Run with:  xvfb-run -a python3 tests/smoke_ui.py
"""

from __future__ import annotations

import logging
import sys
import tempfile
import traceback
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ps3hub.applog import configure
from ps3hub.config import ConfigStore
from ps3hub.device import EventType, ServiceEvent
from ps3hub.inputs import InputEvent, InputId
from ps3hub.protocol import parse_status

FAILURES: list[str] = []


def step(name: str, fn) -> None:
    try:
        fn()
        print(f"  ok   {name}")
    except Exception as exc:
        FAILURES.append(f"{name}: {exc}")
        print(f"  FAIL {name}: {exc}")
        traceback.print_exc()


def main() -> int:
    configure(logging.WARNING, to_console=False)
    from ps3hub.ui.app import HubApp

    tmp = Path(tempfile.mkdtemp())
    app = HubApp(store=ConfigStore(tmp / "config.json"))
    app.geometry("1200x800")
    app.update_idletasks()
    app.update()

    def pump(count: int = 3) -> None:
        for _ in range(count):
            app.update_idletasks()
            app.update()

    # -- navigation --------------------------------------------------------
    for view in ("dashboard", "audio", "mapping", "diagnostics", "settings",
                 "dashboard"):
        step(f"open {view}", lambda v=view: (app._select_view(v), pump())[0])

    # -- audio view ----------------------------------------------------------
    step("audio view renders engine state", lambda: (app._audio_view.refresh(),
                                                     pump())[0])
    step("audio view engine provider returns data", lambda: (
        None if app._audio is None or app._audio.as_dict() else
        (_ for _ in ()).throw(AssertionError("empty engine dict"))))
    step("dashboard audio card renders", lambda: (
        app._dashboard._render_audio(), pump())[0])

    # -- equalizer -----------------------------------------------------------
    def eq_drag() -> None:
        view = app._audio_view
        assert view._eq_graph is not None, "no EQ graph"
        view._eq_graph.set_curve([100.0, 1000.0, 10000.0], [0.0, 0.0, 0.0])
        pump()
        # A press on a point, a drag, and a release, as a user would.
        class _Event:
            def __init__(self, x, y):
                self.x, self.y = x, y
        x, y = view._eq_graph._points[1]
        view._eq_graph._press(_Event(x, y))
        view._eq_graph._drag(_Event(x, y - 40))
        view._eq_graph._release(_Event(x, y - 40))
        assert view._eq_graph.gains[1] > 0, "drag did not raise the band"
        pump()

    step("equalizer graph drags a band", eq_drag)

    def eq_band_count() -> None:
        view = app._audio_view
        view._band_var.set("15 Bands")
        view._on_band_count()
        pump()
        assert len(view._eq_graph.gains) == 15, view._eq_graph.gains

    step("equalizer switches to 15 bands", eq_band_count)

    def eq_reset() -> None:
        view = app._audio_view
        view._on_reset_eq()
        pump()
        assert all(abs(g) < 0.001 for g in view._eq_graph.gains), \
            view._eq_graph.gains

    step("equalizer resets to flat", eq_reset)

    step("equalizer outputs are listed", lambda: (
        app._audio_view.refresh_outputs(), pump())[0])

    # -- audio page no-access overlay -----------------------------------------
    def overlay_covers_the_page() -> None:
        view = app._audio_view
        view._page_open = True
        view._overlay_dismissed = False
        view._render_fxsound(SimpleNamespace(found=False, running=False,
                                             power=False))
        pump()
        assert view._overlay_shown, "the page is not covered"
        assert view._overlay.winfo_manager() == "place"
        assert not view._fx_ui_enabled, "FxSound features must stay locked"
        assert str(view._overlay_primary.cget("text")) == "Install FxSound"
        assert str(view._gain_scale.cget("state")) == "disabled"

    step("audio page is covered while FxSound is unavailable",
         overlay_covers_the_page)

    def overlay_proposes_starting_when_installed() -> None:
        view = app._audio_view
        view._render_fxsound(SimpleNamespace(found=True, running=False,
                                             power=False))
        pump()
        assert view._overlay_shown, "the page is not covered"
        assert str(view._overlay_primary.cget("text")) == "Start FxSound"

    step("overlay proposes starting FxSound when it is installed",
         overlay_proposes_starting_when_installed)

    def overlay_clears_when_running() -> None:
        view = app._audio_view
        view._render_fxsound(SimpleNamespace(
            found=True, running=True, power=True, selected_output="",
            selected_preset="", effects={}))
        pump()
        assert not view._overlay_shown, "the overlay did not clear"
        assert view._overlay.winfo_manager() == ""

    step("audio page overlay clears when FxSound is running",
         overlay_clears_when_running)

    def overlay_sends_the_user_to_settings() -> None:
        view = app._audio_view
        view._overlay_go_settings()
        app._drain_ui_requests()
        assert app._current == "settings", app._current
        app._select_view("audio")
        pump()

    step("overlay sends the user to Settings", overlay_sends_the_user_to_settings)

    # -- synthetic device traffic -------------------------------------------
    def push_status(hexstr: str) -> None:
        snapshot = parse_status(bytes.fromhex(hexstr))
        assert snapshot is not None, hexstr
        app._service._state.snapshot = snapshot
        app._service._state.receiver_present = True
        app._service._state.status_reports += 1
        app._service._state.last_status_time = __import__("time").monotonic()
        app._service._state.last_report_hex = snapshot.raw_hex
        app._service._state.last_report_collection = "Status (FF01:0020)"
        app._refresh()
        pump()

    step("render a connected headset", lambda: push_status("B0 07 40 55 49 2A 11 00"))
    step("render muted + surround", lambda: push_status("B0 0A 64 12 4B 2A 11 00"))
    step("render charging", lambda: push_status("B0 00 00 80 48 00 11 00"))
    step("render volume zero", lambda: push_status("B0 00 00 00 48 00 11 00"))

    # -- events through the real pump ----------------------------------------
    def emit(event: ServiceEvent) -> None:
        app._service._emit(event)
        app._drain_service_events()
        pump()

    for input_id in (InputId.VOLUME_UP, InputId.CHATMIX_UP, InputId.VSS_BUTTON,
                     InputId.MIC_BUTTON, InputId.BATTERY_LOW,
                     InputId.HEADSET_UNLINKED, InputId.HEADSET_LINKED):
        step(f"input event {input_id}", lambda i=input_id: emit(ServiceEvent(
            EventType.INPUT, i, input_event=InputEvent(i, repeat=2)
        )))

    for kind in (EventType.RECEIVER_ATTACHED, EventType.RECEIVER_DETACHED,
                 EventType.ERROR, EventType.UNKNOWN_REPORT, EventType.NOTICE):
        step(f"service event {kind}",
             lambda k=kind: emit(ServiceEvent(k, "synthetic message")))

    # -- mapping interactions -------------------------------------------------
    app._select_view("mapping")
    pump()
    step("select an input row", lambda: (app._mapping._select(InputId.VOLUME_UP),
                                         pump())[0])
    step("listen mode on", lambda: (app._mapping._toggle_listen(), pump())[0])
    step("learn from an event", lambda: (app._mapping.on_event(ServiceEvent(
        EventType.INPUT, "x", input_event=InputEvent(InputId.MIC_BUTTON))), pump())[0])

    def choose(action_label: str) -> None:
        app._mapping._action_combo.set(action_label)
        app._mapping._apply_action()
        pump()

    labels = app._mapping._action_labels
    step("bind a shortcut", lambda: choose(labels["keyboard.combo"]))
    step("bind a launcher", lambda: choose(labels["app.launch"]))
    step("bind a volume step", lambda: choose(labels["system.volume_up"]))
    step("bind a notify", lambda: choose(labels["hub.notify"]))
    step("bind play/pause", lambda: choose(labels["media.play_pause"]))
    step("unbind", lambda: choose(labels["none"]))

    step("toggle enabled", lambda: (
        app._mapping._select(InputId.VSS_BUTTON),
        app._mapping._enabled_var.set(False),
        app._mapping._apply_enabled(), pump())[0])
    step("remove binding", lambda: (app._mapping._clear(), pump())[0])
    step("test action", lambda: (
        app._mapping._select(InputId.VOLUME_DOWN),
        app._mapping._test(), pump())[0])
    step("reload mapping view", lambda: (app._mapping.reload(), pump())[0])

    # -- settings --------------------------------------------------------------
    app._select_view("settings")
    pump()
    step("flip a checkbox", lambda: (
        app._settings._verbose.set(True), app._settings._emit(), pump())[0])
    step("change a number", lambda: (
        app._settings._settle.set("900"), app._settings._emit(), pump())[0])
    step("bad number is tolerated", lambda: (
        app._settings._debounce.set(""), app._settings._emit(), pump())[0])
    step("read-all collections", lambda: (
        app._settings._read_all.set(True), app._settings._emit(), pump())[0])
    step("master toggle", lambda: (
        app._master_var.set(False), app._toggle_master(), pump())[0])

    # -- diagnostics with unknown traffic ---------------------------------------
    app._select_view("diagnostics")
    pump()
    step("unknown report fingerprint", lambda: (
        app._service._collections.__setitem__("p", __import__(
            "ps3hub.device", fromlist=["CollectionInfo"]
        ).CollectionInfo("p", 0x000C, 0x0001, 0, "Unknown", "Sony")),
        app._service._record_unknown("p", bytes.fromhex("01 02 03 04"), "Consumer"),
        app._diagnostics.refresh(app._service.snapshot()), pump())[0])
    step("diagnostics refresh", lambda: (
        app._diagnostics.refresh(app._service.snapshot()), pump())[0])
    step("copy log", lambda: (app._diagnostics._copy(), pump())[0])

    # -- persistence -------------------------------------------------------------
    step("save", lambda: (app._save_now(), pump())[0])
    step("export", lambda: app._export_profile(str(tmp / "out.json")))
    step("import", lambda: app._import_profile(str(tmp / "out.json")))
    step("import a bad file", lambda: app._import_profile(str(tmp / "missing.json")))

    # -- shutdown -------------------------------------------------------------
    step("close", app._on_close)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for failure in FAILURES:
            print("  -", failure)
        return 1
    print("All smoke steps passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
