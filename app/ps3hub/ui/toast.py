"""Toasts: the single route for every user-facing notification.

This app reports three kinds of news - the headset's link, its battery and
executed actions - and each kind used to have its own delivery path. Now
every toast goes through :class:`ToastCenter`, which picks one of two
surfaces:

* **The screen corner.** A stacked overlay anchored to the bottom-right of
  the *screen* (not the window: the window may be minimised or hidden in the
  tray while events arrive). Rounded cards in the theme's black-gold look,
  hover to pause, a click callback, deduplication of identical consecutive
  toasts, and a fade-out.
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
from .theme import ABYSS, FAULT, GOLD, MUTED, PANEL, PAPER, RIDGE, WARN, fonts

log = get_logger("ui.toast")

IS_WINDOWS = os.name == "nt"

#: How long one in-app toast stays fully visible.
TOAST_LIFETIME_MS = 3800
#: How often the overlay animates (the fade runs on the same clock).
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


def _level_colour(level: str) -> str:
    return {
        "ok": GOLD, "info": MUTED, "warn": WARN, "error": FAULT,
    }.get(str(level or "info").lower(), MUTED)


# ---------------------------------------------------------------------------
# The in-app overlay


class ToastOverlay:
    """A stack of toasts anchored to the bottom-right of the screen.

    Owns one borderless ``Toplevel`` per visible toast. Hovering pauses the
    countdown; clicking invokes the toast's callback and dismisses it.
    """

    WIDTH = 300
    PADDING = 12
    GAP = 8
    CORNER = 10
    ACCENT_WIDTH = 3

    def __init__(self, root) -> None:
        self._root = root
        self._toasts: list[_ToastCard] = []
        self._job: str | None = None

    # ------------------------------------------------------------- public --

    def show(self, title: str, message: str, level: str = "info",
             on_click: Callable[[], None] | None = None) -> bool:
        """Show one toast. Never raises, never blocks."""
        try:
            card = _ToastCard(self, str(title or ""), str(message or ""),
                              level, on_click)
        except Exception:
            log.exception("In-app toast failed")
            return False
        # An identical toast still on screen restarts instead of stacking.
        for existing in self._toasts:
            if existing.matches(title, message):
                existing.refresh()
                card.close(instant=True)
                return True
        self._toasts.append(card)
        while len(self._toasts) > MAX_STACK:
            self._toasts.pop(0).close(instant=True)
        self._reposition()
        self._ensure_loop()
        return True

    def dismiss_all(self) -> None:
        for card in list(self._toasts):
            card.close(instant=True)

    # ------------------------------------------------------------ internals --

    def _reposition(self) -> None:
        """Stack the cards upward from the bottom-right of the screen."""
        import tkinter as tk
        try:
            right = self._root.winfo_screenwidth() - 14
            bottom = self._root.winfo_screenheight() - 48
        except tk.TclError:
            return
        for card in reversed(self._toasts):
            card.place_at(right, bottom)
            bottom -= card.height + self.GAP

    def _ensure_loop(self) -> None:
        if self._job is not None:
            return
        self._job = self._root.after(TOAST_TICK_MS, self._step)

    def _step(self) -> None:
        self._job = None
        now = time.monotonic()
        for card in list(self._toasts):
            card.tick(now)
        before = len(self._toasts)
        self._toasts = [c for c in self._toasts if c.alive]
        if len(self._toasts) != before:
            self._reposition()
        if self._toasts:
            self._job = self._root.after(TOAST_TICK_MS, self._step)

    def _card_closed(self, card: "_ToastCard") -> None:
        if card in self._toasts:
            self._toasts.remove(card)
            self._reposition()


class _ToastCard:
    """One toast: a borderless Toplevel with rounded corners and a fade."""

    def __init__(self, owner: ToastOverlay, title: str, message: str,
                 level: str, on_click: Callable[[], None] | None) -> None:
        import tkinter as tk

        self._owner = owner
        self._title = title
        self._message = message
        self._on_click = on_click
        self._alive = True
        self._hover = False
        self._shown_at = time.monotonic()
        self._elapsed = 0.0

        top = tk.Toplevel(master=owner._root)
        top.withdraw()
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        try:
            # The ABYSS margins around the card become transparent, which is
            # what gives the rounded corners real shape on Windows.
            top.attributes("-transparentcolor", ABYSS)
        except tk.TclError:
            pass
        top.configure(bg=ABYSS)
        self._top = top

        width = ToastOverlay.WIDTH
        pad = ToastOverlay.PADDING
        text_width = width - pad * 2 - ToastOverlay.ACCENT_WIDTH - 8

        font = fonts()
        rows: list[tuple[str, object, str]] = []
        if title:
            rows.append((title, font.strong, PAPER))
        for line in ((message or "").splitlines() or [""]):
            rows.append((line, font.small, MUTED))

        # Measure first (on a throwaway canvas) so the window is born the
        # right size instead of visibly resizing into place.
        measure = tk.Canvas(top, bg=ABYSS, highlightthickness=0)
        y = float(pad)
        for text, fnt, _colour in rows:
            tid = measure.create_text(0, 0, text=text, font=fnt,
                                      width=text_width, anchor="nw")
            bbox = measure.bbox(tid)
            y = (bbox[3] if bbox else y + 12) + 6
        measure.destroy()
        content_h = int(max(y + pad - 4, 44))
        self._height = content_h + 2

        canvas = tk.Canvas(top, bg=ABYSS, highlightthickness=0, bd=0,
                           width=width - 2, height=self._height)
        canvas.pack(fill="both", expand=True)
        self._canvas = canvas
        canvas.bind("<Button-1>", self._clicked)
        canvas.bind("<Enter>", self._enter)
        canvas.bind("<Leave>", self._leave)

        from .theme import round_rect
        round_rect(canvas, 1, 1, width - 3, content_h, ToastOverlay.CORNER,
                   fill=PANEL, outline=RIDGE)
        accent = _level_colour(level)
        canvas.create_rectangle(
            2, 8, 2 + ToastOverlay.ACCENT_WIDTH, content_h - 8,
            fill=accent, outline="")
        y = float(pad)
        for text, fnt, colour in rows:
            tid = canvas.create_text(pad + ToastOverlay.ACCENT_WIDTH + 8, y,
                                     text=text, anchor="nw", fill=colour,
                                     font=fnt, width=text_width)
            bbox = canvas.bbox(tid)
            y = (bbox[3] if bbox else y + 12) + 6

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
        """A duplicate arrived: restart the countdown instead of stacking."""
        self._shown_at = time.monotonic()
        self._elapsed = 0.0

    def place_at(self, right: int, bottom: int) -> None:
        import tkinter as tk
        try:
            width = ToastOverlay.WIDTH
            self._top.geometry(
                f"{width}x{self._height}+{right - width}+{bottom - self._height}"
            )
            if not self._top.winfo_ismapped():
                self._top.deiconify()
        except tk.TclError:
            pass

    def tick(self, now: float) -> None:
        """Advance the countdown; close the card when it has expired."""
        if not self._hover:
            self._elapsed = now - self._shown_at
            if self._elapsed * 1000 >= TOAST_LIFETIME_MS:
                self.close()

    def close(self, instant: bool = False) -> None:
        if not self._alive:
            return
        self._alive = False
        if instant:
            self._destroy()
            self._owner._card_closed(self)
            return
        self._fade(1.0)

    # ------------------------------------------------------------ internals --

    def _fade(self, alpha: float) -> None:
        import tkinter as tk
        if not self._alive:
            return
        try:
            self._top.attributes("-alpha", alpha)
        except tk.TclError:
            pass
        if alpha <= 0.1:
            self._destroy()
            self._owner._card_closed(self)
            return
        try:
            self._top.after(30, lambda: self._fade(alpha - 0.25))
        except tk.TclError:
            pass

    def _destroy(self) -> None:
        try:
            self._top.destroy()
        except tk.TclError:
            pass

    # ------------------------------------------------------------ handlers --

    def _enter(self, _e=None) -> None:
        """Hover: freeze the countdown at its current progress."""
        self._hover = True
        self._elapsed = time.monotonic() - self._shown_at

    def _leave(self, _e=None) -> None:
        """Unhover: resume the countdown from where it was frozen."""
        self._hover = False
        self._shown_at = time.monotonic() - self._elapsed

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
