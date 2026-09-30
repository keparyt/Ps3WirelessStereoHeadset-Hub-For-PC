"""Toasts: the single route for every user-facing notification.

This app reports three kinds of news - the headset's link, its battery and
executed actions - and each kind used to have its own delivery path. Now
every toast goes through :class:`ToastCenter`, which picks one of two
surfaces:

* **The screen corner.** Stacked toasts anchored to the bottom-right of
  the *screen* (not the window: the window may be minimised or hidden in
  the tray while events arrive), rendered by ``ttkbootstrap``'s
  ``ToastNotification`` - it owns the card look, the fade-out and the
  stacking/reflow of concurrent toasts. This module keeps the routing-level
  behaviour on top: dedup, the cap on concurrent cards, and the click
  callback.
* **A Windows toast.** When the main window is hidden or minimised, an
  in-app overlay would be invisible, so the toast goes to Windows instead,
  delivered with ``Shell_NotifyIconW`` balloon tips (rendered as proper
  toasts on Windows 10/11). The tray icon used as the balloon source is
  created once and kept for the sender's lifetime; creating and deleting it
  per burst was visible as taskbar flicker and dropped toasts.

Threading: :meth:`ToastCenter.show` is safe to call from any thread. Toasts
are marshalled onto the Tk thread through an internal queue - the same
pattern the rest of the app uses for cross-thread UI work - because Tk calls
from foreign threads are not supported.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from typing import Callable

from ..applog import get_logger

log = get_logger("ui.toast")

IS_WINDOWS = os.name == "nt"

#: How long one in-app toast stays fully visible.
TOAST_LIFETIME_MS = 3800
#: How often queued toasts are drained onto the Tk thread, and how often
#: the overlay notices toasts the library has auto-dismissed.
TOAST_TICK_MS = 33
#: Identical consecutive toasts within this window are swallowed.
DEDUP_SECONDS = 3.0
#: Upper bound on the stack; the oldest toast drops off when exceeded.
MAX_STACK = 4

#: Win32 balloon buffers need room for the terminator.
TITLE_LIMIT = 63
MESSAGE_LIMIT = 255

_LEVEL_FLAGS = {
    "info": 0x1, "ok": 0x1, "notice": 0x1,
    "warn": 0x2, "warning": 0x2,
    "error": 0x3, "fault": 0x3,
}


def _clip(text: str, limit: int) -> str:
    return (text or "").strip()[:limit]


def _level_flag(level: str) -> int:
    return _LEVEL_FLAGS.get(str(level or "info").lower(), 0x1)


# ---------------------------------------------------------------------------
# The in-app overlay (ttkbootstrap-backed)


#: ttkbootstrap bootstyle per app toast level.
_LEVEL_BOOTSTYLE = {
    "ok": "success", "info": "info", "notice": "info",
    "warn": "warning", "error": "danger",
}
#: Bootstrap-Icons glyph per level (rendered from the built-in icon font).
_LEVEL_ICON = {
    "ok": "headphones", "info": "info-circle-fill",
    "warn": "exclamation-triangle-fill", "error": "x-octagon-fill",
}


class ToastOverlay:
    """Toasts anchored to the bottom-right of the screen, via ttkbootstrap.

    Delegates drawing, stacking, reflow, timing and dismissal to
    ``ttkbootstrap.widgets.toast.ToastNotification`` - concurrent toasts at
    the same corner stack and reflow automatically - and keeps only the
    routing-level bookkeeping: dedup, the cap on concurrent cards, and the
    click callback.
    """

    def __init__(self, root) -> None:
        self._root = root
        self._toasts: list[_ToastCard] = []

    # ------------------------------------------------------------- public --

    def show(self, title: str, message: str, level: str = "info",
             on_click: Callable[[], None] | None = None) -> bool:
        """Show one toast. Never raises, never blocks."""
        # An identical toast still on screen restarts instead of stacking.
        for existing in self._toasts:
            if existing.matches(title, message):
                existing.refresh()
                return True
        try:
            card = _ToastCard(self, str(title or ""), str(message or ""),
                              level, on_click)
        except Exception:
            log.exception("In-app toast failed")
            return False
        self._toasts.append(card)
        while len(self._toasts) > MAX_STACK:
            self._toasts.pop(0).close(instant=True)
        return True

    def dismiss_all(self) -> None:
        for card in list(self._toasts):
            card.close(instant=True)

    # ------------------------------------------------------------ internals --

    def sweep(self) -> None:
        """Drop cards the library has already dismissed and destroyed.

        ``ToastNotification`` auto-closes on its own duration without telling
        anyone, so the pump calls this every tick to notice cards whose
        window is gone and keep the bookkeeping (dedup, the cap) truthful.
        """
        for card in list(self._toasts):
            card.tick(time.monotonic())

    def _card_closed(self, card: "_ToastCard") -> None:
        if card in self._toasts:
            self._toasts.remove(card)


class _ToastCard:
    """One toast, rendered by ttkbootstrap's ``ToastNotification``.

    Stacking, reflow, theming, the icon and the duration countdown are the
    library's job; this wrapper adds the click callback, dedup identity and
    the lifecycle bookkeeping the overlay tracks. The window is created and
    shown right here: the library's constructor only validates, and
    ``show_toast()`` is what puts a card on screen.
    """

    def __init__(self, owner: ToastOverlay, title: str, message: str,
                 level: str, on_click: Callable[[], None] | None) -> None:
        self._owner = owner
        self._title = title
        self._message = message
        self._on_click = on_click
        self._alive = True

        from ttkbootstrap.widgets.toast import ToastNotification

        bootstyle = _LEVEL_BOOTSTYLE.get(str(level or "info").lower(), "info")
        icon = _LEVEL_ICON.get(str(level or "info").lower(), "info-circle-fill")
        # (x, y, anchor): 20 px in from the right, 40 px up from the bottom.
        self._toast = ToastNotification(
            title=title,
            message=message,
            duration=TOAST_LIFETIME_MS,
            bootstyle=bootstyle,
            icon=icon,
            position=(20, 40, "se"),
            master=owner._root,
        ).show_toast()
        # The library measured the real height on show; the floor is its
        # minimum card height, used when the value is somehow missing.
        self._height = int(getattr(self._toast, "_height", 0) or 0) or 75
        # The library's own button press only hides the toast; add our
        # callback alongside it ("+" keeps both bindings alive).
        toplevel = getattr(self._toast, "toplevel", None)
        if toplevel is not None:
            toplevel.bind("<ButtonPress>", self._clicked, add="+")

    # ------------------------------------------------------------- public --

    @property
    def alive(self) -> bool:
        return self._alive

    @property
    def height(self) -> int:
        return self._height

    def matches(self, title: str, message: str) -> bool:
        return self._title == title and self._message == message

    def refresh(self) -> None:
        """A duplicate arrived: restart the countdown instead of stacking.

        Done with a *fresh* popup: the library's fade-out reads its current
        ``toplevel`` attribute on every step, so hiding and re-showing the
        same object would point the old window's fade at the new one and
        destroy it. Separate objects fade and show independently.
        """
        if not self._alive:
            return
        try:
            self._toast.hide()
        except Exception:
            log.debug("Toast refresh failed", exc_info=True)
        self._alive = False
        self._owner._card_closed(self)
        self._owner.show(self._title, self._message, self._level_name(),
                         self._on_click)

    def tick(self, now: float) -> None:
        """Notice auto-dismissal: the library hides itself on its duration
        and never announces it, so the pump polls for the dead window."""
        if not self._alive:
            return
        if getattr(self._toast, "toplevel", None) is None:
            # Fully faded out and destroyed by the library's own timer.
            self._alive = False
            self._owner._card_closed(self)

    def close(self, instant: bool = False) -> None:
        if not self._alive:
            return
        self._alive = False
        try:
            self._toast.hide()  # idempotent in the library, any state
        except Exception:
            log.debug("Toast close failed", exc_info=True)
        self._owner._card_closed(self)

    # ------------------------------------------------------------ handlers --

    def _level_name(self) -> str:
        for name, style in _LEVEL_BOOTSTYLE.items():
            if style == self._toast.bootstyle:
                return name
        return "info"

    def _clicked(self, _e=None) -> None:
        callback = self._on_click
        self.close()
        if callback is not None:
            try:
                callback()
            except Exception:
                log.exception("Toast click handler failed")


# ---------------------------------------------------------------------------
# The Windows toast sender


if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    _user32 = ctypes.WinDLL("user32", use_last_error=True)

    _NIM_ADD, _NIM_MODIFY, _NIM_DELETE = 0, 1, 2
    _NIF_MESSAGE, _NIF_ICON, _NIF_TIP, _NIF_INFO = 0x1, 0x2, 0x4, 0x10

    #: Windows delivers tray events to this message; we never act on them.
    _WM_TRAY = 0x8000 + 1  # WM_APP + 1
    #: A message-only window: never shown, never in the task list.
    _HWND_MESSAGE = wintypes.HWND(-3)
    _HOST_WINDOW_NAME = "PS3HeadsetHubToastHost"
    _TRAY_UID = 1

    _user32.CreateWindowExW.restype = wintypes.HWND

    class NOTIFYICONDATAW(ctypes.Structure):
        """The Shell_NotifyIconW payload, without the GUID tail union."""

        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HICON),
            ("szTip", wintypes.WCHAR * 128),
            ("szInfo", wintypes.WCHAR * 256),
            ("uTimeout", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD),
            ("guidItem", ctypes.c_ubyte * 16),
        ]

    def _create_host_window() -> int | None:
        """A hidden owner window for the balloon's tray icon."""
        try:
            handle = _user32.CreateWindowExW(
                0, "Static", _HOST_WINDOW_NAME, 0,
                0, 0, 0, 0, _HWND_MESSAGE, None, None, None,
            )
        except Exception:
            log.debug("Could not create the toast host window", exc_info=True)
            return None
        return int(getattr(handle, "value", handle) or 0) or None

    def _base_data(window: int, uid: int) -> NOTIFYICONDATAW:
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = wintypes.HWND(window)
        data.uID = uid
        data.uCallbackMessage = _WM_TRAY
        return data

    def _add_icon(window: int, uid: int) -> bool:
        """Register an invisible tray icon (message-only, no icon, no tip)."""
        data = _base_data(window, uid)
        data.uFlags = _NIF_MESSAGE
        return bool(_shell32.Shell_NotifyIconW(_NIM_ADD, ctypes.byref(data)))

    def _show_balloon(window: int, uid: int, title: str, message: str,
                      level: str) -> bool:
        """Show or replace the balloon; Windows 10+ renders it as a toast."""
        data = _base_data(window, uid)
        data.uFlags = _NIF_INFO
        data.szInfo = _clip(message, MESSAGE_LIMIT)
        data.szInfoTitle = _clip(title, TITLE_LIMIT)
        data.dwInfoFlags = _level_flag(level)
        return bool(_shell32.Shell_NotifyIconW(_NIM_MODIFY, ctypes.byref(data)))

    def _remove_icon(window: int, uid: int) -> None:
        _shell32.Shell_NotifyIconW(_NIM_DELETE, ctypes.byref(_base_data(window, uid)))

else:

    def _create_host_window() -> None:
        return None

    def _add_icon(window: None, uid: int) -> bool:
        return False

    def _show_balloon(window: None, uid: int, title: str, message: str,
                      level: str) -> bool:
        return False

    def _remove_icon(window: None, uid: int) -> None:
        return None


class _TrayIconHost:
    """One hidden window and tray icon, kept for the worker's lifetime."""

    def __init__(self) -> None:
        self._window = _create_host_window()
        self._added = self._window is not None and _add_icon(self._window, _TRAY_UID)

    def show_toast(self, title: str, message: str, level: str) -> bool:
        if not self._added:
            return False
        return _show_balloon(self._window, _TRAY_UID, title, message, level)

    def close(self) -> None:
        if self._window is not None:
            try:
                if self._added:
                    _remove_icon(self._window, _TRAY_UID)
            except Exception:
                log.debug("Toast host cleanup failed", exc_info=True)


class WindowsToastSender:
    """Delivers toasts through Win32 from a dedicated worker thread."""

    def __init__(self, app_name: str, enabled: bool = True) -> None:
        self.app_name = app_name
        self.enabled = enabled and IS_WINDOWS
        self.sent = 0
        self.skipped = 0
        self._queue: "queue.Queue[tuple[str, str, str] | None]" = queue.Queue()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._start_lock = threading.Lock()

    def show(self, title: str, message: str, level: str = "info") -> bool:
        if not self.enabled:
            self.skipped += 1
            log.info("Windows toasts unavailable here; would show %s: %s",
                     title, message)
            return False
        try:
            self._queue.put((
                _clip(title or self.app_name, TITLE_LIMIT),
                _clip(message, MESSAGE_LIMIT),
                level,
            ))
            self._start_worker()
            self.sent += 1
        except Exception:
            log.exception("Could not queue a Windows toast")
            return False
        return True

    def shutdown(self) -> None:
        self._stop.set()
        try:
            self._queue.put(None)
        except Exception:
            pass
        if self._worker is not None:
            self._worker.join(timeout=2.0)
            self._worker = None

    def _start_worker(self) -> None:
        with self._start_lock:
            if self._worker is None or not self._worker.is_alive():
                self._stop.clear()
                self._worker = threading.Thread(
                    target=self._loop, name="windows-toast", daemon=True)
                self._worker.start()

    def _loop(self) -> None:
        host = _TrayIconHost()
        try:
            while not self._stop.is_set():
                try:
                    item = self._queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                if item is None:
                    return
                title, message, level = item
                try:
                    host.show_toast(title, message, level)
                except Exception:
                    log.exception("Windows toast delivery failed")
        finally:
            host.close()


# ---------------------------------------------------------------------------
# The router


class ToastCenter:
    """Chooses between the in-app overlay and a Windows toast.

    The rule is visibility: when the main window is shown normally, the
    in-app overlay delivers the news right where the user is looking; when
    it is withdrawn (tray) or minimised, the news would land on a hidden
    overlay, so it goes to Windows instead. During a show/hide transition
    the window manager may briefly report the old state; a toast that races
    it is delivered through Windows, which notifies rather than loses it.

    All methods are safe to call from any thread. Delivery happens on the
    Tk thread through an internal queue, drained by one repeating ``after``
    job - the app's established pattern for cross-thread UI work.
    """

    _Request = tuple[str, str, str, Callable[[], None] | None]

    def __init__(self, root, app_name: str, enable_windows: bool = True) -> None:
        self._root = root
        self._overlay = ToastOverlay(root)
        self._windows = WindowsToastSender(
            app_name, enabled=enable_windows and IS_WINDOWS)
        self._pending: "queue.Queue[ToastCenter._Request]" = queue.Queue()
        self.in_app_shown = 0
        self.windows_shown = 0
        #: When set, every toast goes to Windows even while the window is
        #: visible (the Settings page exposes the switch).
        self.force_windows = False
        self._dedup: tuple[str, str] | None = None
        self._dedup_at = 0.0
        self._job: str | None = None
        self._pump()

    # ------------------------------------------------------------- public --

    def show(self, title: str, message: str, level: str = "info",
             on_click: Callable[[], None] | None = None) -> bool:
        """Queue one toast. Never blocks, never raises, any thread."""
        key = (str(title or ""), str(message or ""))
        now = time.monotonic()
        if key == self._dedup and (now - self._dedup_at) < DEDUP_SECONDS:
            return True  # a duplicate: silence, not failure
        self._dedup, self._dedup_at = key, now
        try:
            self._pending.put(
                (str(title or ""), str(message or ""), str(level or "info"),
                 on_click))
        except Exception:
            log.exception("Could not queue a toast")
            return False
        return True

    def set_force_windows(self, forced: bool) -> None:
        """Send every toast to Windows, even when the window is visible."""
        self.force_windows = bool(forced)

    def shutdown(self) -> None:
        self._windows.shutdown()
        try:
            self._overlay.dismiss_all()
        except Exception:
            pass

    # ------------------------------------------------------------ internals --

    def _pump(self) -> None:
        """Drain queued toasts onto the Tk thread. One repeating job."""
        while True:
            try:
                title, message, level, on_click = self._pending.get_nowait()
            except queue.Empty:
                break
            try:
                self._deliver(title, message, level, on_click)
            except Exception:
                log.exception("Toast delivery failed")
        try:
            self._overlay.sweep()
        except Exception:
            log.debug("Toast sweep failed", exc_info=True)
        try:
            self._job = self._root.after(TOAST_TICK_MS, self._pump)
        except Exception:
            self._job = None  # the application is going away

    def _deliver(self, title: str, message: str, level: str,
                 on_click: Callable[[], None] | None) -> None:
        if not self.force_windows:
            try:
                state = self._root.state()
            except Exception:
                return
        else:
            state = "withdrawn"  # force the Windows route
        if state in ("normal", "zoomed"):
            if self._overlay.show(title, message, level, on_click):
                self.in_app_shown += 1
                return
        if self._windows.show(title, message, level):
            self.windows_shown += 1
