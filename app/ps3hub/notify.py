"""Windows notifications, with no new dependencies.

Two things in this app are worth interrupting the user for: the headset's
battery running low, and a binding firing. Both surface as a Windows toast.

The usual routes for toasts on Windows are:

* a WinRT toast activator -- correct, but needs an AUMID, a Start Menu
  shortcut and a pile of interface definitions;
* a third-party package -- breaks the "exactly one dependency (hidapi)" rule
  the rest of the code base keeps;
* ``Shell_NotifyIconW`` balloon tips -- the old API, which Windows 10 and 11
  render as a normal toast in the corner.

This module takes the third route. A tray icon exists only for as long as a
notification is on screen and is removed afterwards, so nothing is left
parked in the tray. A burst of notifications replaces the balloon's contents
rather than stacking icons.

Threading: the UI calls this from the Tk thread and the dispatch thread calls
it from the HID thread. A dedicated worker thread owns the tray window, so
callers only ever put a tuple on a queue and never block.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from typing import Any

from .applog import get_logger

log = get_logger("notify")

IS_WINDOWS = os.name == "nt"

#: How long the tray icon stays alive after the last balloon, in seconds.
HOLD_SECONDS = 6.0
#: Win32 balloon icon flavours (defined off Windows too, so tests can read them).
NIIF_INFO = 0x1
NIIF_WARNING = 0x2
NIIF_ERROR = 0x3

_LEVEL_FLAGS = {
    "info": NIIF_INFO,
    "ok": NIIF_INFO,
    "notice": NIIF_INFO,
    "warn": NIIF_WARNING,
    "warning": NIIF_WARNING,
    "error": NIIF_ERROR,
    "fault": NIIF_ERROR,
}

#: The balloon title is a 64-character buffer, the body a 256-character one.
TITLE_LIMIT = 63
MESSAGE_LIMIT = 255


def _clip(text: str, limit: int) -> str:
    """Trim to a Win32 fixed buffer, which needs room for the terminator."""
    return (text or "").strip()[:limit]

_TRAY_WINDOW_NAME = "PS3HeadsetHubNotifications"
_TRAY_ID = 1


def _level_flag(level: str) -> int:
    """Map an app tone ("info", "warn", "error") onto a Win32 balloon flag."""
    return _LEVEL_FLAGS.get(str(level or "info").lower(), NIIF_INFO)


if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    _user32 = ctypes.WinDLL("user32", use_last_error=True)

    NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
    NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10

    #: Windows delivers tray events to this message; we never act on them.
    _WM_TRAY = 0x8000 + 1  # WM_APP + 1
    #: A message-only window: never shown, never in the task list.
    _HWND_MESSAGE = wintypes.HWND(-3)

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
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256),
            ("uTimeout", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD),
            ("guidItem", ctypes.c_ubyte * 16),
        ]

    def _pump() -> None:
        """Keep the worker's window message queue moving."""
        msg = wintypes.MSG()
        PM_REMOVE = 0x1
        while _user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))

    def _create_tray_window() -> int | None:
        """A hidden owner window for the transient tray icon."""
        try:
            return _user32.CreateWindowExW(
                0, "Static", _TRAY_WINDOW_NAME, 0,
                0, 0, 0, 0, _HWND_MESSAGE, None, None, None,
            )
        except Exception:
            log.debug("Could not create the notification window", exc_info=True)
            return None

    def _base_data(window: int, uid: int) -> NOTIFYICONDATAW:
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = window
        data.uID = uid
        data.uCallbackMessage = _WM_TRAY
        return data

    def _add_icon(window: int, uid: int) -> bool:
        """Register an invisible tray icon (message-only, no icon, no tip)."""
        data = _base_data(window, uid)
        data.uFlags = NIF_MESSAGE
        return bool(_shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data)))

    def _show_balloon(
        window: int, uid: int, title: str, message: str, level: str
    ) -> bool:
        """Show or replace the balloon; Windows 10+ renders it as a toast."""
        data = _base_data(window, uid)
        data.uFlags = NIF_INFO
        data.szInfo = message
        data.szInfoTitle = title
        data.dwInfoFlags = _level_flag(level)
        return bool(_shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data)))

    def _hide_balloon(window: int, uid: int) -> bool:
        data = _base_data(window, uid)
        data.uFlags = NIF_MESSAGE
        return bool(_shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data)))

else:

    def _pump() -> None:
        return None

    def _create_tray_window() -> None:
        return None

    def _add_icon(window: Any, uid: int) -> bool:
        return False

    def _show_balloon(window: Any, uid: int, title: str, message: str,
                      level: str) -> bool:
        return False

    def _hide_balloon(window: Any, uid: int) -> bool:
        return False


class DesktopNotifier:
    """Queue-driven toast sender. Safe to call from any thread.

    The worker exists only when the platform can actually show a toast. When
    it cannot, :meth:`show` logs what it would have shown and returns False,
    so callers never need a platform check of their own.
    """

    def __init__(self, app_name: str = "PS3 Wireless Stereo Headset Hub",
                 enabled: bool = True) -> None:
        self.app_name = app_name
        #: Read at call time, so a caller can flip it without a restart.
        self.enabled = enabled and IS_WINDOWS
        self.sent = 0
        self.skipped = 0
        self._window: int | None = None
        self._queue: "queue.Queue[tuple[str, str, str] | None]" = queue.Queue()
        self._stop = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._start_lock = threading.Lock()
        self._worker: threading.Thread | None = None

    # --------------------------------------------------------------- public --

    def show(self, title: str, message: str, level: str = "info") -> bool:
        """Queue one toast. Never blocks, never raises, safe off Windows."""
        if not self.enabled:
            self.skipped += 1
            log.info("Notification unavailable here; would show %s: %s",
                     title, message)
            return False
        try:
            self._queue.put((
                _clip(title or self.app_name, TITLE_LIMIT),
                _clip(message, MESSAGE_LIMIT),
                level,
            ))
            self._start_worker()
            self._idle.clear()
            self.sent += 1
        except Exception:
            log.exception("Could not queue a notification")
            return False
        return True

    def _start_worker(self) -> None:
        """Spawn the toast thread on first use, not at import time."""
        with self._start_lock:
            if self._worker is None or not self._worker.is_alive():
                self._stop.clear()
                self._worker = threading.Thread(
                    target=self._loop, name="toast-worker", daemon=True
                )
                self._worker.start()

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """True once everything queued so far has been delivered."""
        return self._idle.wait(timeout)

    def shutdown(self) -> None:
        self._stop.set()
        try:
            self._queue.put(None)
        except Exception:
            pass
        if self._worker is not None:
            self._worker.join(timeout=2.0)
            self._worker = None

    # -------------------------------------------------------------- worker --

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                # Nothing pending: everything queued so far has been shown.
                self._idle.set()
                _pump()
                continue
            if item is None:
                break
            try:
                self._deliver(item)
            except Exception:
                log.exception("Could not show a notification")
            _pump()
        self._idle.set()

    def _ensure_window(self) -> int | None:
        if self._window is not None:
            return self._window
        self._window = _create_tray_window()
        return self._window

    def _deliver(self, item: tuple[str, str, str]) -> None:
        window = self._ensure_window()
        if window is None or not _add_icon(window, _TRAY_ID):
            log.debug("Tray icon unavailable; dropping a notification")
            return
        try:
            title, message, level = item
            _show_balloon(window, _TRAY_ID, title, message, level)
            deadline = time.monotonic() + HOLD_SECONDS
            while not self._stop.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    nxt = self._queue.get(timeout=min(remaining, 0.1))
                except queue.Empty:
                    time.sleep(0.02)
                    continue
                if nxt is None:
                    self._stop.set()
                    break
                title, message, level = nxt
                _show_balloon(window, _TRAY_ID, title, message, level)
        finally:
            _hide_balloon(window, _TRAY_ID)
