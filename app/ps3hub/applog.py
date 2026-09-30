"""Application logging.

Sinks:

* **Per-domain rotating files** in the per-user data directory - one log per
  subsystem (``app``, ``device``, ``audio``, ``ui``, ``windows``) plus the
  everything-log ``ps3hub.log`` - at DEBUG level, so a crash or a "it stopped
  working" report can be debugged without reproducing it first.
* an in-memory ring buffer the Diagnostics view reads, so the user never has
  to find a log file to report a problem.
* **Crash capture**: an uncaught exception on any thread - or in a Tk
  callback - is written to ``panic.log`` with full traceback and a session
  header, and the ring buffer records it too.

Files live in ``%APPDATA%\\PS3HeadsetHub\\logs``.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
import time
import traceback
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Deque, Iterable

from . import APP_SLUG, APP_VERSION

LOG_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-7s [%(name)s] %(message)s"
DATE_FORMAT = "%H:%M:%S"
MAX_BYTES = 2_000_000
BACKUP_COUNT = 4
RING_CAPACITY = 6000
#: DEBUG in the files, INFO on the console; the ring records everything it
#: is given (the root logger gates at DEBUG).
FILE_LEVEL = logging.DEBUG
CONSOLE_LEVEL = logging.INFO

_configured = False
_ring: "RingHandler | None" = None
_panic_path: Path | None = None
_crash_count = 0
_boot_time = time.time()
_pid = os.getpid()
_applog_lock = threading.Lock()


def data_dir() -> Path:
    """Per-user writable directory for config and logs."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
    else:  # development / testing on other platforms
        base = os.environ.get("XDG_CONFIG_HOME")
        root = Path(base) if base else Path.home() / ".config"
    target = root / APP_SLUG
    target.mkdir(parents=True, exist_ok=True)
    return target


def log_dir() -> Path:
    target = data_dir() / "logs"
    target.mkdir(parents=True, exist_ok=True)
    return target


#: Domain -> log file stem. Every logger under ``ps3hub.<domain>`` (and the
#: domain's own logger) routes into its file, and everything also lands in
#: the everything-log. Domains outside the table fall back to ``app``.
DOMAIN_FILES = {
    "app": "ps3hub.log",          # everything, plus the app domain itself
    "device": "device.log",       # HID receiver, collections, raw reports
    "audio": "audio.log",         # engine, FxSound integration, DSP
    "ui": "ui.log",               # views, toasts, window lifecycle
    "windows": "windows.log",     # Win32 integration: tray, actions, keys
}


def _domain_of(logger_name: str) -> str:
    """``ps3hub.audio.engine`` -> ``audio``; unknown prefixes -> ``app``."""
    parts = logger_name.split(".")
    if len(parts) >= 2 and parts[0] == "ps3hub" and parts[1] in DOMAIN_FILES:
        return parts[1]
    if len(parts) == 1 and parts[0] in DOMAIN_FILES:
        return parts[0]
    return "app"


class _DomainFilter(logging.Filter):
    """Route records to one domain file (and let ps3hub.log take all)."""

    def __init__(self, domain: str) -> None:
        super().__init__()
        self.domain = domain

    def filter(self, record: logging.LogRecord) -> bool:
        return _domain_of(record.name) == self.domain


class RingHandler(logging.Handler):
    """Keeps the most recent records in memory for the Diagnostics view."""

    def __init__(self, capacity: int = RING_CAPACITY) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._records: Deque[str] = deque(maxlen=capacity)
        self._sequence = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:  # never let logging break the app
            return
        with self._lock:
            self._sequence += 1
            self._records.append(line)

    def snapshot(self) -> list[str]:
        with self._lock:
            return list(self._records)

    @property
    def sequence(self) -> int:
        with self._lock:
            return self._sequence

    def clear(self) -> None:
        with self._lock:
            self._records.clear()


def _write_panic(kind: str, detail: str) -> None:
    """Append one crash entry to panic.log. Never raises."""
    global _crash_count, _panic_path
    with _applog_lock:
        _crash_count += 1
        if _panic_path is None:
            try:
                _panic_path = log_dir() / "panic.log"
            except Exception:
                return
    try:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with _panic_path.open("a", encoding="utf-8") as handle:
            handle.write(
                f"\n===== crash #{_crash_count} | {stamp} | pid {_pid} "
                f"| v{APP_VERSION} "
                f"| {kind} =====\n{detail.rstrip()}\n"
            )
    except Exception:
        pass


def _install_crash_hooks() -> None:
    """Capture crashes everywhere exceptions can escape uncaught."""

    def _log(kind: str, exc: BaseException) -> None:
        detail = "".join(traceback.format_exception(
            type(exc), exc, exc.__traceback__))
        logging.getLogger("ps3hub.app").critical(
            "CRASH (%s): %s", kind, detail.rstrip())
        _write_panic(kind, detail)

    previous_hook = sys.excepthook

    def _sys_hook(exc_type, exc_value, exc_tb):
        # Chain to any hook someone else installed, then log ourselves.
        try:
            if previous_hook is not None and previous_hook is not sys.__excepthook__:
                previous_hook(exc_type, exc_value, exc_tb)
        except Exception:
            pass
        if issubclass(exc_type, KeyboardInterrupt):
            return
        _log("uncaught", exc_value)

    sys.excepthook = _sys_hook

    if hasattr(threading, "excepthook"):
        _previous_thread_hook = threading.excepthook

        def _thread_hook(args):
            try:
                if _previous_thread_hook is not None:
                    _previous_thread_hook(args)
            except Exception:
                pass
            if isinstance(args.exc_type, type) and issubclass(args.exc_type, SystemExit):
                return
            _log(f"thread {args.thread.name if args.thread else '?'}",
                 args.exc_value)

        threading.excepthook = _thread_hook

    def _tk_handler(self, exc, val, tb):
        # Bound like a method: Tk calls instance.report_callback_exception(
        # exc, val, tb), and the class attribute receives the widget as self.
        logging.getLogger("ps3hub.ui").error(
            "UI callback failed", exc_info=(exc, val, tb))
        _write_panic("tkinter-callback",
                     "".join(traceback.format_exception(exc, val, tb)))

    try:
        import tkinter
        # Tk defines its own report_callback_exception, which shadows the
        # Misc one for the main window - both need the handler.
        tkinter.Tk.report_callback_exception = _tk_handler  # type: ignore[attr-defined]
        tkinter.Misc.report_callback_exception = _tk_handler  # type: ignore[attr-defined]
        tkinter.Menu.report_callback_exception = _tk_handler  # type: ignore[attr-defined]
    except Exception:
        pass


def configure(level: int = logging.DEBUG, to_console: bool | None = None) -> None:
    """Install handlers once. Safe to call repeatedly.

    The root logger runs at DEBUG so nothing is lost; the *files* decide
    verbosity (DEBUG) and the console stays quiet (INFO).
    """
    global _configured, _ring, _panic_path
    if _configured:
        logging.getLogger().setLevel(level)
        return

    try:
        _panic_path = log_dir() / "panic.log"
    except Exception:
        _panic_path = None

    formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)
    root = logging.getLogger()
    root.setLevel(level)

    _ring = RingHandler()
    _ring.setFormatter(formatter)
    root.addHandler(_ring)

    console_handler = None
    if to_console is None:
        to_console = sys.stderr is not None and sys.stderr.isatty()
    if to_console:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.setLevel(CONSOLE_LEVEL)
        root.addHandler(console_handler)

    # One rotating file per domain at DEBUG, plus the everything-log.
    installed: list[logging.Handler] = []
    for domain, stem in DOMAIN_FILES.items():
        try:
            handler = logging.handlers.RotatingFileHandler(
                log_dir() / stem,
                maxBytes=MAX_BYTES,
                backupCount=BACKUP_COUNT,
                encoding="utf-8",
            )
            handler.setFormatter(formatter)
            handler.setLevel(FILE_LEVEL)
            handler.addFilter(_DomainFilter(domain))
            root.addHandler(handler)
            installed.append(handler)
        except Exception as exc:  # read-only install dir, roaming profile issues
            root.addHandler(logging.NullHandler())
            root.warning("File logging for %s unavailable: %s", domain, exc)
            break

    _install_crash_hooks()
    _configured = True
    logging.getLogger("ps3hub.app").debug(
        "Logging configured: files=%s console=%s level(file)=%s pid=%d",
        [Path(getattr(h, "baseFilename", "?")).name for h in installed],
        bool(console_handler), logging.getLevelName(FILE_LEVEL), _pid,
    )


def log_files() -> "dict[str, Path]":
    """Domain -> log file path, for the Diagnostics view."""
    try:
        directory = log_dir()
    except Exception:
        return {}
    files: dict[str, Path] = {}
    for domain, stem in DOMAIN_FILES.items():
        path = directory / stem
        if path.exists():
            files[domain] = path
    panic = directory / "panic.log"
    if panic.exists():
        files["panic"] = panic
    return files


def ring() -> RingHandler:
    if _ring is None:
        configure()
    assert _ring is not None
    return _ring


def recent(limit: int | None = None) -> Iterable[str]:
    lines = ring().snapshot()
    return lines if limit is None else lines[-limit:]


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"ps3hub.{name}")
