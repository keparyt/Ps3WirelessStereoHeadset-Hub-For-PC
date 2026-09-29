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
import queue

log = logging.getLogger(__name__)

IS_WINDOWS = os.name == "nt"

OPEN_COMMAND = 1001
HIDE_COMMAND = 1002
REFRESH_COMMAND = 1003
EXIT_COMMAND = 1004

# ------------------------------------------------------------ tray icon art --

#: Battery thresholds for the icon colour, in percent. Green when healthy,
#: yellow when getting low, red when critical; gray when the headset is
#: not connected or its battery is unknown.
BATTERY_OK_BELOW = 30
BATTERY_LOW_BELOW = 15
ICON_COLOUR_OK = (0x5B, 0xD1, 0x96)      # green
ICON_COLOUR_LOW = (0xEF, 0xB3, 0x4A)     # yellow
ICON_COLOUR_CRITICAL = (0xEF, 0x6B, 0x6E)  # red
ICON_COLOUR_OFF = (0x9A, 0xA3, 0xAE)     # gray: running but not connected
ICON_COLOUR_CHARGING = (0xCF, 0xAE, 0x3D)  # gold, the app's active-state accent
ICON_BACKGROUND = (0x0A, 0x0B, 0x0D)     # the theme's deepest slate


def tray_icon_kind(connected: bool, battery_percent: int | None,
                   charging: bool = False) -> str:
    """Map headset state onto an icon kind. Pure; unit-testable."""
    if not connected:
        return "off"
    if charging:
        return "charging"
    if battery_percent is None:
        return "off"
    if battery_percent < BATTERY_LOW_BELOW:
        return "critical"
    if battery_percent < BATTERY_OK_BELOW:
        return "low"
    return "ok"


def _ICON_COLOURS() -> dict[str, tuple[int, int, int]]:
    return {
        "ok": ICON_COLOUR_OK,
        "low": ICON_COLOUR_LOW,
        "critical": ICON_COLOUR_CRITICAL,
        "off": ICON_COLOUR_OFF,
        "charging": ICON_COLOUR_CHARGING,
    }


def headset_icon_pixels(kind: str, size: int = 32) -> list[list[int]]:
    """Draw the headset as an RGBA byte grid, fully in code.

    The headset is drawn from simple geometry - headband arc, two oval ear
    cups, stems down from the cups - in the accent colour for ``kind`` on a
    transparent field. Pure function of its arguments, so the shapes and the
    threshold mapping can be unit-tested without a display, and each state
    change needs no asset file on disk.

    Returns ``rows[y][x]`` of 0 (transparent) or 0xFF (opaque).
    """
    colours = _ICON_COLOURS()
    accent = colours.get(kind, ICON_COLOUR_OFF)
    c = size / 2.0
    cup_w = size * 0.24          # ear cup half-width
    cup_h = size * 0.30          # ear cup half-height
    band_r = size * 0.34         # headband radius
    band_w = size * 0.085        # headband thickness
    stem_len = size * 0.14

    rows: list[list[int]] = [[0] * size for _ in range(size)]
    for y in range(size):
        py = y + 0.5
        for x in range(size):
            px = x + 0.5
            opaque = False
            dx, dy = px - c, py - c
            # Headband: an annulus sector across the top (|x| wide enough,
            # above centre, within [r - w, r]).
            r = (dx * dx + dy * dy) ** 0.5
            if abs(dx) <= band_r and dy < 0 and band_r - band_w <= r <= band_r:
                opaque = True
            # Ear cups: two ovals at the band's ends.
            cup_cy = c + size * 0.10
            for cup_cx in (c - band_r, c + band_r):
                ex = (px - cup_cx) / cup_w
                ey = (py - cup_cy) / cup_h
                if ex * ex + ey * ey <= 1.0:
                    opaque = True
            # Stems: short bars below each cup, like the headset's yokes.
            for stem_cx in (c - band_r, c + band_r):
                if abs(px - stem_cx) <= cup_w * 0.55 and \
                        cup_cy + cup_h <= py <= cup_cy + cup_h + stem_len:
                    opaque = True
            rows[y][x] = 0xFF if opaque else 0x00
    return rows


def _rgba_bytes_for_kind(kind: str, size: int = 32) -> bytes:
    """Render one icon kind as 32-bit BGRA rows, top-down.

    Fed to a negative-height (top-down) BITMAPINFOHEADER, so the first row
    in memory is the top row of the drawing.
    """
    accent = _ICON_COLOURS().get(kind, ICON_COLOUR_OFF)
    bg = ICON_BACKGROUND
    rows = headset_icon_pixels(kind, size)
    stride = size * 4
    out = bytearray(size * stride)
    for y in range(size):
        src = rows[y]
        base = y * stride
        for x in range(size):
            alpha = src[x]
            i = base + x * 4
            if alpha:
                b, g, r = accent
                out[i:i + 4] = bytes((b, g, r, alpha))
            else:
                out[i:i + 4] = bytes((bg[0], bg[1], bg[2], 0))
    return bytes(out)

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)

    # Some Python 3.10 wintypes builds do not expose every Win32
    # alias (notably LRESULT/HCURSOR). Define compatibility aliases here
    # instead of depending on the exact CPython version.
    _HANDLE = wintypes.HANDLE
    _HINSTANCE = getattr(wintypes, "HINSTANCE", _HANDLE)
    _HICON = getattr(wintypes, "HICON", _HANDLE)
    _HCURSOR = getattr(wintypes, "HCURSOR", _HANDLE)
    _HBRUSH = getattr(wintypes, "HBRUSH", _HANDLE)
    _HMENU = getattr(wintypes, "HMENU", _HANDLE)
    _HWND = getattr(wintypes, "HWND", _HANDLE)
    _HGLOBAL = getattr(wintypes, "HGLOBAL", _HANDLE)
    _LRESULT = getattr(wintypes, "LRESULT", ctypes.c_ssize_t)

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

    _BI_RGB = 0
    _DIB_RGB_COLORS = 0
    _CBM_INIT = 0x04

    _HWND_MESSAGE = _HWND(-3)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [
            ("bmiHeader", BITMAPINFOHEADER),
            ("bmiColors", wintypes.DWORD * 1),
        ]

    _gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    _gdi32.CreateDIBSection.argtypes = [
        _HANDLE, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
        ctypes.POINTER(ctypes.c_void_p), _HANDLE, wintypes.DWORD,
    ]
    _gdi32.CreateDIBSection.restype = _HANDLE
    _gdi32.DeleteObject.argtypes = [_HANDLE]
    _gdi32.DeleteObject.restype = wintypes.BOOL
    _gdi32.CreateDIBitmap.argtypes = [
        _HANDLE, ctypes.POINTER(BITMAPINFOHEADER), wintypes.DWORD,
        wintypes.LPVOID, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
    ]
    _gdi32.CreateDIBitmap.restype = _HANDLE
    _gdi32.SetDIBits.argtypes = [
        _HANDLE, _HANDLE, wintypes.UINT, wintypes.UINT, wintypes.LPVOID,
        ctypes.POINTER(BITMAPINFO), wintypes.UINT,
    ]
    _gdi32.SetDIBits.restype = ctypes.c_int
    _gdi32.CreateBitmap.argtypes = [
        ctypes.c_int, ctypes.c_int, wintypes.UINT, wintypes.UINT, wintypes.LPVOID,
    ]
    _gdi32.CreateBitmap.restype = _HANDLE

    _HBITMAP = getattr(wintypes, "HBITMAP", _HANDLE)

    class ICONINFO(ctypes.Structure):
        _fields_ = [
            ("fIcon", wintypes.BOOL),
            ("xHotspot", wintypes.DWORD),
            ("yHotspot", wintypes.DWORD),
            ("hbmMask", _HBITMAP),
            ("hbmColor", _HBITMAP),
        ]

    _user32.GetDC.argtypes = [_HWND]
    _user32.GetDC.restype = _HANDLE
    _user32.ReleaseDC.argtypes = [_HWND, _HANDLE]
    _user32.ReleaseDC.restype = ctypes.c_int
    _user32.CreateIconIndirect.argtypes = [ctypes.POINTER(ICONINFO)]
    _user32.CreateIconIndirect.restype = _HICON
    _user32.DestroyIcon.argtypes = [_HICON]
    _user32.DestroyIcon.restype = wintypes.BOOL

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", _HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", _HICON),
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
            ("hInstance", _HINSTANCE),
            ("hIcon", _HICON),
            ("hCursor", _HCURSOR),
            ("hbrBackground", _HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    _WNDPROC = ctypes.WINFUNCTYPE(
        _LRESULT,
        _HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    )

    # ctypes defaults pointer-returning Win32 calls to a 32-bit C int unless
    # their signatures are declared. Explicit prototypes keep this safe on
    # 64-bit Windows and also allow Unicode strings to cross the boundary.
    _kernel32.GetCurrentThreadId.argtypes = []
    _kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    _kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    _kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    _user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    _user32.RegisterClassW.restype = wintypes.ATOM
    _user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        _HWND, _HMENU, _HINSTANCE, wintypes.LPVOID,
    ]
    _user32.CreateWindowExW.restype = _HWND
    _user32.LoadImageW.argtypes = [
        _HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    _user32.LoadImageW.restype = wintypes.HANDLE
    _user32.LoadIconW.argtypes = [_HINSTANCE, wintypes.LPCWSTR]
    _user32.LoadIconW.restype = _HICON
    _user32.PeekMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG), _HWND,
        wintypes.UINT, wintypes.UINT, wintypes.UINT,
    ]
    _user32.PeekMessageW.restype = wintypes.BOOL
    _user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    _user32.TranslateMessage.restype = wintypes.BOOL
    _user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    _user32.DispatchMessageW.restype = _LRESULT
    _user32.DestroyWindow.argtypes = [_HWND]
    _user32.DestroyWindow.restype = wintypes.BOOL
    _user32.PostMessageW.argtypes = [
        _HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ]
    _user32.PostMessageW.restype = wintypes.BOOL
    _user32.DefWindowProcW.argtypes = [
        _HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ]
    _user32.DefWindowProcW.restype = _LRESULT
    _user32.CreatePopupMenu.restype = _HMENU
    _user32.AppendMenuW.argtypes = [
        _HMENU, wintypes.UINT, wintypes.UINT, wintypes.LPCWSTR,
    ]
    _user32.AppendMenuW.restype = wintypes.BOOL
    _user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
    _user32.GetCursorPos.restype = wintypes.BOOL
    _user32.SetForegroundWindow.argtypes = [_HWND]
    _user32.SetForegroundWindow.restype = wintypes.BOOL
    _user32.TrackPopupMenu.argtypes = [
        _HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
        wintypes.UINT, _HWND, wintypes.LPVOID,
    ]
    _user32.TrackPopupMenu.restype = wintypes.UINT
    _user32.DestroyMenu.argtypes = [_HMENU]
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
    """Create a compact tooltip from the authoritative service snapshot.

    Volume comes from the logical headset state when the caller supplies one
    (the ``headset_state`` attribute); otherwise it falls back to the raw
    snapshot so the platform-neutral tests keep working unchanged.
    """
    if not getattr(state, "backend_available", True):
        return "PS3 Headset Hub • HID backend unavailable"

    if not getattr(state, "receiver_present", False):
        return "PS3 Headset Hub • No supported headset receiver"

    snapshot = getattr(state, "snapshot", None)
    if snapshot is None:
        return "PS3 Headset Hub • Receiver connected • Waiting for headset telemetry"

    connected = bool(getattr(snapshot, "headset_connected", False))
    parts = [
        "PS3 Headset Hub • Headset connected"
        if connected
        else "PS3 Headset Hub • Receiver connected • Headset not connected"
    ]

    # Logical volume from the single source of truth, when available.
    logical = getattr(state, "headset_state", None)
    volume_percent = getattr(logical, "volume_percent", None) if logical else None
    if volume_percent is not None:
        parts.append(f"Volume {volume_percent}%")

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
        icon_kind_provider: Callable[[], str] | None = None,
    ) -> None:
        self._status_provider = status_provider
        self._on_open = on_open
        self._on_hide = on_hide
        self._on_refresh = on_refresh
        self._on_exit = on_exit
        self._icon_path = icon_path or default_icon_path()
        self._app_name = app_name
        #: Returns one of the :func:`tray_icon_kind` strings; when given,
        #: the tray icon is re-rendered whenever the kind changes instead
        #: of showing a static file.
        self._icon_kind_provider = icon_kind_provider
        self._icon_kind: str | None = None
        self._kind_requests: "queue.SimpleQueue[str]" = queue.SimpleQueue()

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
                    _HWND(self._hwnd), _WM_CLOSE, 0, 0
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
            self._thread_id = int(_kernel32.GetCurrentThreadId())
            self._callback = _WNDPROC(self._window_proc)

            hinstance = _kernel32.GetModuleHandleW(None)
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

            next_refresh = 0.0
            while not self._stop.is_set():
                self._pump_messages()

                now = time.monotonic()
                if now >= next_refresh:
                    self._update_tooltip()
                    self._poll_icon_kind()
                    next_refresh = now + 1.0

                # State changes pushed from the UI thread take effect at once.
                try:
                    while True:
                        kind = self._kind_requests.get_nowait()
                        self._apply_icon_kind(kind)
                except queue.Empty:
                    pass

                time.sleep(0.10)

        except Exception:
            log.exception("System tray could not start")
            self._started = False
            self._thread_ready.set()
        finally:
            try:
                if self._hwnd is not None:
                    self._delete_icon()
                    _user32.DestroyWindow(_HWND(self._hwnd))
            except Exception:
                log.debug("System tray cleanup failed", exc_info=True)

            self._hwnd = None
            self._thread_id = None
            self._started = False

    # ---------------------------------------------------------- dynamic icon --

    def update_icon_kind(self, kind: str) -> None:
        """Request an icon re-render for a new headset state. Any thread."""
        if kind != self._icon_kind:
            self._kind_requests.put(str(kind))

    def _poll_icon_kind(self) -> None:
        """Ask the provider for the current state (tray thread, 1s cadence)."""
        provider = self._icon_kind_provider
        if provider is None:
            return
        try:
            kind = str(provider() or "off")
        except Exception:
            log.debug("Icon kind provider failed", exc_info=True)
            return
        if kind != self._icon_kind:
            self._apply_icon_kind(kind)

    def _apply_icon_kind(self, kind: str) -> None:
        """Re-render and swap the tray icon (tray thread only)."""
        self._icon_kind = kind
        if self._hwnd is None:
            return
        handle = self._create_state_icon(kind)
        if not handle:
            log.debug("Could not render the %s tray icon", kind)
            return
        data = self._make_data()
        data.uFlags = _NIF_ICON | _NIF_TIP
        data.hIcon = _HICON(handle)
        data.szTip = self._clip_tip(self._status_provider())
        if bool(_shell32.Shell_NotifyIconW(_NIM_MODIFY, ctypes.byref(data))):
            old = self._icon_handle
            self._icon_handle = handle
            if old and old != handle:
                _user32.DestroyIcon(_HICON(old))
            log.debug("Tray icon updated to %s", kind)

    def _create_state_icon(self, kind: str) -> int:
        """Render one headset-state icon as an alpha HICON.

        A 32-bpp DIB section carries the colour data with per-pixel alpha;
        a monochrome mask of the right size pairs with it in ICONINFO. All
        GDI objects are released; only the HICON outlives the call.
        """
        size = 32
        pixels = _rgba_bytes_for_kind(kind, size)
        hdc = _user32.GetDC(None)
        if not hdc:
            return 0
        try:
            bmi = BITMAPINFO()
            header = bmi.bmiHeader
            header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            header.biWidth = size
            header.biHeight = -size  # negative: top-down rows
            header.biPlanes = 1
            header.biBitCount = 32
            header.biCompression = _BI_RGB
            section = ctypes.c_void_p()
            bitmap = _gdi32.CreateDIBSection(
                hdc, ctypes.byref(bmi), _DIB_RGB_COLORS,
                ctypes.byref(section), None, 0)
            if not bitmap or not section:
                return 0
            try:
                ctypes.memmove(section, pixels, len(pixels))
                mask = _gdi32.CreateBitmap(size, size, 1, 1, None)
                if not mask:
                    return 0
                try:
                    icon_info = ICONINFO()
                    icon_info.fIcon = True
                    icon_info.xHotspot = 0
                    icon_info.yHotspot = 0
                    icon_info.hbmMask = _HBITMAP(mask)
                    icon_info.hbmColor = _HBITMAP(bitmap)
                    handle = _user32.CreateIconIndirect(ctypes.byref(icon_info))
                    return int(getattr(handle, "value", handle) or 0)
                finally:
                    _gdi32.DeleteObject(_HBITMAP(mask))
            finally:
                _gdi32.DeleteObject(_HBITMAP(bitmap))
        finally:
            _user32.ReleaseDC(None, hdc)

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
        data.hWnd = _HWND(self._hwnd)
        data.uID = 1
        data.uCallbackMessage = _WM_TRAY
        data.hIcon = _HICON(self._icon_handle or 0)
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
            _user32.DestroyWindow(_HWND(hwnd))
            return 0

        if message == _WM_DESTROY:
            return _user32.DefWindowProcW(
                _HWND(hwnd), message, wparam, lparam
            )

        return _user32.DefWindowProcW(
            _HWND(hwnd), message, wparam, lparam
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

            _user32.SetForegroundWindow(_HWND(hwnd))
            command = _user32.TrackPopupMenu(
                menu,
                _TPM_RIGHTBUTTON | _TPM_NONOTIFY | _TPM_RETURNCMD,
                point.x,
                point.y,
                0,
                _HWND(hwnd),
                None,
            )
            _user32.PostMessageW(_HWND(hwnd), _WM_NULL, 0, 0)

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
