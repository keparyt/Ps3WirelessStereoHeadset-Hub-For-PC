"""Native Windows system-tray integration.

The tray is intentionally implemented with Shell_NotifyIconW instead of adding
another Python dependency. A small worker thread owns the tray window/message
loop, while all Tk operations are marshalled back to the main UI thread.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

log = logging.getLogger(__name__)

IS_WINDOWS = os.name == "nt"

OPEN_COMMAND = 1001
HIDE_COMMAND = 1002
REFRESH_COMMAND = 1003
EXIT_COMMAND = 1004

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)

    _WM_APP = 0x8000
    _WM_TRAY = _WM_APP + 1
    _WM_LBUTTONUP = 0x0202
    _WM_LBUTTONDBLCLK = 0x0203
    _WM_RBUTTONUP = 0x0205
    _WM_CLOSE = 0x0010
    _WM_DESTROY = 0x0002
    _WM_NULL = 0x0000

    _NIM_ADD = 0
    _NIM_MODIFY = 1
    _NIM_DELETE = 2

    _NIF_MESSAGE = 0x00000001
    _NIF_ICON = 0x00000002
    _NIF_TIP = 0x00000004

    _MF_STRING = 0x00000000
    _TPM_RIGHTBUTTON = 0x0002
    _TPM_NONOTIFY = 0x0080
    _TPM_RETURNCMD = 0x0100

    _IMAGE_ICON = 1
    _LR_LOADFROMFILE = 0x00000010
    _LR_DEFAULTSIZE = 0x00000040
    _IDI_APPLICATION = 32512

    _HWND_MESSAGE = wintypes.HWND(-3)

    class NOTIFYICONDATAW(ctypes.Structure):
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

    class POINT(ctypes.Structure):
        _fields_ = [
            ("x", wintypes.LONG),
            ("y", wintypes.LONG),
        ]

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", ctypes.c_void_p),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HCURSOR),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    _WNDPROC = ctypes.WINFUNCTYPE(
        wintypes.LRESULT,
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    )

    # ctypes defaults pointer-returning Win32 calls to a 32-bit C int unless
    # their signatures are declared. Explicit prototypes keep this safe on
    # 64-bit Windows and also allow Unicode strings to cross the boundary.
    _user32.GetCurrentThreadId.restype = wintypes.DWORD
    _user32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    _user32.GetModuleHandleW.restype = wintypes.HMODULE
    _user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    _user32.RegisterClassW.restype = wintypes.ATOM
    _user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]
    _user32.CreateWindowExW.restype = wintypes.HWND
    _user32.LoadImageW.argtypes = [
        wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    _user32.LoadImageW.restype = wintypes.HANDLE
    _user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
    _user32.LoadIconW.restype = wintypes.HICON
    _user32.PeekMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG), wintypes.HWND,
        wintypes.UINT, wintypes.UINT, wintypes.UINT,
    ]
    _user32.PeekMessageW.restype = wintypes.BOOL
    _user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    _user32.TranslateMessage.restype = wintypes.BOOL
    _user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    _user32.DispatchMessageW.restype = wintypes.LRESULT
    _user32.DestroyWindow.argtypes = [wintypes.HWND]
    _user32.DestroyWindow.restype = wintypes.BOOL
    _user32.PostMessageW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ]
    _user32.PostMessageW.restype = wintypes.BOOL
    _user32.DefWindowProcW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ]
    _user32.DefWindowProcW.restype = wintypes.LRESULT
    _user32.CreatePopupMenu.restype = wintypes.HMENU
    _user32.AppendMenuW.argtypes = [
        wintypes.HMENU, wintypes.UINT, wintypes.UINT, wintypes.LPCWSTR,
    ]
    _user32.AppendMenuW.restype = wintypes.BOOL
    _user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
    _user32.GetCursorPos.restype = wintypes.BOOL
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.SetForegroundWindow.restype = wintypes.BOOL
    _user32.TrackPopupMenu.argtypes = [
        wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
        wintypes.UINT, wintypes.HWND, wintypes.LPVOID,
    ]
    _user32.TrackPopupMenu.restype = wintypes.UINT
    _user32.DestroyMenu.argtypes = [wintypes.HMENU]
    _user32.DestroyMenu.restype = wintypes.BOOL
    _shell32.Shell_NotifyIconW.argtypes = [
        wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW),
    ]
    _shell32.Shell_NotifyIconW.restype = wintypes.BOOL

else:
    NOTIFYICONDATAW = Any  # type: ignore[misc,assignment]


def _icon_candidates() -> list[Path]:
    paths: list[Path] = []
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        paths.append(Path(bundled) / "ps3hub.ico")
    paths.append(Path(__file__).resolve().parents[1] / "packaging" / "ps3hub.ico")
    return paths


def default_icon_path() -> Path | None:
    for path in _icon_candidates():
        try:
            if path.exists():
                return path
        except OSError:
            continue
    return None


def format_tray_status(state: Any) -> str:
    """Create a compact tooltip from the authoritative service snapshot."""
    if not getattr(state, "backend_available", True):
        return "PS3 Headset Hub • HID backend unavailable"

    if not getattr(state, "receiver_present", False):
        return "PS3 Headset Hub • No supported headset receiver"

    snapshot = getattr(state, "snapshot", None)
    if snapshot is None:
        return "PS3 Headset Hub • Receiver connected • Headset not connected"

    connected = bool(getattr(snapshot, "headset_connected", False))
    parts = [
        "PS3 Headset Hub • Headset connected"
        if connected
        else "PS3 Headset Hub • Receiver connected • Headset not connected"
    ]

    battery = getattr(snapshot, "battery_percent", None)
    if battery is not None:
        suffix = " • Charging" if getattr(snapshot, "charging", False) else ""
        parts.append(f"Battery {battery}%{suffix}")
    elif getattr(snapshot, "charging", False):
        parts.append("Charging")

    return " • ".join(parts)


class TrayManager:
    """Owns one persistent notification-area icon."""

    def __init__(
        self,
        status_provider: Callable[[], str],
        on_open: Callable[[], None],
        on_hide: Callable[[], None],
        on_refresh: Callable[[], None],
        on_exit: Callable[[], None],
        icon_path: Path | None = None,
        app_name: str = "PS3 Wireless Stereo Headset Hub",
    ) -> None:
        self._status_provider = status_provider
        self._on_open = on_open
        self._on_hide = on_hide
        self._on_refresh = on_refresh
        self._on_exit = on_exit
        self._icon_path = icon_path or default_icon_path()
        self._app_name = app_name

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_ready = threading.Event()
        self._thread_id: int | None = None
        self._hwnd: int | None = None
        self._icon_handle: int | None = None
        self._callback: Any = None
        self._class_name: str | None = None
        self._started = False

    @property
    def available(self) -> bool:
        return IS_WINDOWS and self._started

    def start(self) -> bool:
        if not IS_WINDOWS:
            log.info("System tray is only available on Windows")
            return False
        if self._thread is not None and self._thread.is_alive():
            return True

        self._stop.clear()
        self._thread_ready.clear()
        self._thread = threading.Thread(
            target=self._thread_main,
            name="system-tray",
            daemon=True,
        )
        self._thread.start()
        self._thread_ready.wait(2.0)
        return self._started

    def stop(self) -> None:
        self._stop.set()
        if IS_WINDOWS and self._hwnd is not None:
            try:
                _user32.PostMessageW(
                    wintypes.HWND(self._hwnd), _WM_CLOSE, 0, 0
                )
            except Exception:
                pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None

    def _thread_main(self) -> None:
        if not IS_WINDOWS:
            self._thread_ready.set()
            return

        try:
            self._thread_id = int(_user32.GetCurrentThreadId())
            self._callback = _WNDPROC(self._window_proc)

            hinstance = _user32.GetModuleHandleW(None)
            class_name = f"PS3HeadsetHubTray_{self._thread_id:x}"
            self._class_name = class_name

            wnd_class = WNDCLASSW()
            wnd_class.style = 0
            wnd_class.lpfnWndProc = ctypes.cast(
                self._callback, ctypes.c_void_p
            )
            wnd_class.cbClsExtra = 0
            wnd_class.cbWndExtra = 0
            wnd_class.hInstance = hinstance
            wnd_class.hIcon = self._load_icon()
            wnd_class.hCursor = None
            wnd_class.hbrBackground = None
            wnd_class.lpszMenuName = None
            wnd_class.lpszClassName = class_name

            atom = _user32.RegisterClassW(ctypes.byref(wnd_class))
            if not atom:
                error = ctypes.get_last_error()
                # RegisterClass can report ERROR_CLASS_ALREADY_EXISTS on a
                # harmless re-entry, so only fail for other errors.
                if error != 1410:
                    raise ctypes.WinError(error)

            hwnd = _user32.CreateWindowExW(
                0,
                class_name,
                self._app_name,
                0,
                0,
                0,
                0,
                0,
                _HWND_MESSAGE,
                None,
                hinstance,
                None,
            )
            if not hwnd:
                raise ctypes.WinError(ctypes.get_last_error())

            self._hwnd = int(getattr(hwnd, "value", hwnd) or 0)
            if not self._add_or_update_icon(initial=True):
                raise ctypes.WinError(ctypes.get_last_error() or 1)

            self._started = True
            self._thread_ready.set()

            next_tooltip = 0.0
            while not self._stop.is_set():
                self._pump_messages()

                now = time.monotonic()
                if now >= next_tooltip:
                    self._update_tooltip()
                    next_tooltip = now + 1.0

                time.sleep(0.10)

        except Exception:
            log.exception("System tray could not start")
            self._started = False
            self._thread_ready.set()
        finally:
            try:
                if self._hwnd is not None:
                    self._delete_icon()
                    _user32.DestroyWindow(wintypes.HWND(self._hwnd))
            except Exception:
                log.debug("System tray cleanup failed", exc_info=True)

            self._hwnd = None
            self._thread_id = None
            self._started = False

    def _load_icon(self) -> int:
        icon = None
        if self._icon_path is not None:
            try:
                icon = _user32.LoadImageW(
                    None,
                    str(self._icon_path),
                    _IMAGE_ICON,
                    0,
                    0,
                    _LR_LOADFROMFILE | _LR_DEFAULTSIZE,
                )
            except Exception:
                icon = None

        if not icon:
            resource = ctypes.cast(
                ctypes.c_void_p(_IDI_APPLICATION), wintypes.LPCWSTR
            )
            icon = _user32.LoadIconW(None, resource)

        value = getattr(icon, "value", icon) if icon else None
        self._icon_handle = int(value) if value else None
        return self._icon_handle or 0

    def _make_data(self) -> NOTIFYICONDATAW:
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = wintypes.HWND(self._hwnd)
        data.uID = 1
        data.uCallbackMessage = _WM_TRAY
        data.hIcon = wintypes.HICON(self._icon_handle or 0)
        return data

    def _add_or_update_icon(self, initial: bool = False) -> bool:
        data = self._make_data()
        data.uFlags = _NIF_MESSAGE | _NIF_ICON | _NIF_TIP
        data.szTip = self._clip_tip(self._status_provider())

        if initial:
            ok = bool(_shell32.Shell_NotifyIconW(
                _NIM_ADD, ctypes.byref(data)
            ))
        else:
            ok = bool(_shell32.Shell_NotifyIconW(
                _NIM_MODIFY, ctypes.byref(data)
            ))

        if ok:
            log.info("System tray icon %s", "created" if initial else "updated")
        return ok

    def _update_tooltip(self) -> None:
        if self._hwnd is None:
            return
        try:
            data = self._make_data()
            data.uFlags = _NIF_TIP
            data.szTip = self._clip_tip(self._status_provider())
            _shell32.Shell_NotifyIconW(_NIM_MODIFY, ctypes.byref(data))
        except Exception:
            log.debug("Could not refresh system tray tooltip", exc_info=True)

    def _delete_icon(self) -> None:
        if self._hwnd is None:
            return
        data = self._make_data()
        _shell32.Shell_NotifyIconW(_NIM_DELETE, ctypes.byref(data))

    @staticmethod
    def _clip_tip(value: str) -> str:
        return str(value or "PS3 Headset Hub").strip()[:127]

    def _pump_messages(self) -> None:
        msg = wintypes.MSG()
        while _user32.PeekMessageW(
            ctypes.byref(msg), None, 0, 0, 0x0001
        ):
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))

    def _window_proc(
        self,
        hwnd: int,
        message: int,
        wparam: int,
        lparam: int,
    ) -> int:
        if message == _WM_TRAY:
            event = int(lparam)
            if event in (_WM_LBUTTONUP, _WM_LBUTTONDBLCLK):
                self._on_open()
                return 0
            if event == _WM_RBUTTONUP:
                self._show_menu(hwnd)
                return 0

        if message == _WM_CLOSE:
            _user32.DestroyWindow(wintypes.HWND(hwnd))
            return 0

        if message == _WM_DESTROY:
            return _user32.DefWindowProcW(
                wintypes.HWND(hwnd), message, wparam, lparam
            )

        return _user32.DefWindowProcW(
            wintypes.HWND(hwnd), message, wparam, lparam
        )

    def _show_menu(self, hwnd: int) -> None:
        menu = _user32.CreatePopupMenu()
        if not menu:
            return

        try:
            _user32.AppendMenuW(
                menu, _MF_STRING, OPEN_COMMAND, "Open"
            )
            _user32.AppendMenuW(
                menu, _MF_STRING, HIDE_COMMAND, "Hide"
            )
            _user32.AppendMenuW(
                menu, _MF_STRING, REFRESH_COMMAND, "Reconnect / Refresh"
            )
            _user32.AppendMenuW(
                menu, _MF_STRING, EXIT_COMMAND, "Exit"
            )

            point = POINT()
            if not _user32.GetCursorPos(ctypes.byref(point)):
                return

            _user32.SetForegroundWindow(wintypes.HWND(hwnd))
            command = _user32.TrackPopupMenu(
                menu,
                _TPM_RIGHTBUTTON | _TPM_NONOTIFY | _TPM_RETURNCMD,
                point.x,
                point.y,
                0,
                wintypes.HWND(hwnd),
                None,
            )
            _user32.PostMessageW(wintypes.HWND(hwnd), _WM_NULL, 0, 0)

            handlers = {
                OPEN_COMMAND: self._on_open,
                HIDE_COMMAND: self._on_hide,
                REFRESH_COMMAND: self._on_refresh,
                EXIT_COMMAND: self._on_exit,
            }
            callback = handlers.get(int(command))
            if callback is not None:
                callback()
        except Exception:
            log.exception("System tray menu failed")
        finally:
            _user32.DestroyMenu(menu)


def tray_state_stub() -> Any:
    """Tiny helper used by platform-neutral tests."""
    return SimpleNamespace(
        backend_available=True,
        receiver_present=False,
        snapshot=None,
    )
