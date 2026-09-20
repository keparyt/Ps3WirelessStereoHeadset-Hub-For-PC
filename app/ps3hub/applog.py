"""Application logging.

Two sinks are installed:

* a rotating file in the per-user data directory, for post-mortem debugging;
* an in-memory ring buffer the Diagnostics view reads, so the user never has
  to find a log file to report a problem.

The PoC printed everything to a console window. A packaged GUI application has
no console, so that stream would have been lost entirely.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Deque, Iterable

from . import APP_SLUG

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)-18s %(message)s"
DATE_FORMAT = "%H:%M:%S"
MAX_BYTES = 1_000_000
BACKUP_COUNT = 3
RING_CAPACITY = 4000

_configured = False
_ring: "RingHandler | None" = None


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


def configure(level: int = logging.INFO, to_console: bool | None = None) -> None:
    """Install handlers once. Safe to call repeatedly."""
    global _configured, _ring
    if _configured:
        logging.getLogger().setLevel(level)
        return

    formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)
    root = logging.getLogger()
    root.setLevel(level)

    _ring = RingHandler()
    _ring.setFormatter(formatter)
    root.addHandler(_ring)

    try:
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir() / "ps3hub.log",
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except Exception as exc:  # read-only install dir, roaming profile issues
        root.addHandler(logging.NullHandler())
        root.warning("File logging unavailable: %s", exc)

    if to_console is None:
        to_console = sys.stderr is not None and sys.stderr.isatty()
    if to_console:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        root.addHandler(stream)

    _configured = True


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
