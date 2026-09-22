"""Device orchestration.

Responsibilities:

* find the receiver and decide which HID collections to open;
* keep a reader alive per collection, and cope with unplug/replug;
* funnel every inbound report through **one** dispatch thread so the edge
  detector sees a strictly ordered stream;
* publish state and events for the UI to poll.

Threading contract, which the UI depends on:

* reader threads only enqueue raw reports, they never touch shared state;
* the dispatch thread owns the edge detector and mutates state under a lock;
* mapped actions run on the dispatch thread, so a media key is sent with the
  least possible latency and never waits for a UI repaint;
* the UI thread only ever calls :meth:`snapshot` and :meth:`poll_events`.

Collection selection deserves a note. The proof of concept found that the
status reports arrive on the vendor collection ``FF01:0020``. That stays the
preferred target. But the receiver sometimes enumerates without usable usage
information, in which case the product also shows up as "Unknown" - and the
proof of concept would then open nothing at all and appear dead. When the
preferred collection is absent, every collection is opened instead, which is
exactly the behaviour that made inputs appear during the investigation.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from .applog import get_logger
from .hid_reader import (
    DeviceGoneError,
    NativeWindowsHIDReader,
    NoInputReportError,
    native_windows_available,
    windows_path,
)
from .inputs import EdgeDetector, InputEvent
from .protocol import (
    TARGET_PID,
    TARGET_VID,
    STATUS_REPORT_ID,
    HeadsetSnapshot,
    ReportFingerprint,
    collection_name,
    hex_bytes,
    is_status_collection,
    parse_status,
)

log = get_logger("device")

SCAN_INTERVAL = 1.0
STALE_SECONDS = 8.0
MAX_UI_EVENTS = 500

try:  # hidapi is the enumeration backend, as in the proof of concept
    import hid
except Exception:  # pragma: no cover - exercised only without the dependency
    hid = None  # type: ignore[assignment]


# ------------------------------------------------------------------ events --

class EventType:
    RECEIVER_ATTACHED = "receiver_attached"
    RECEIVER_DETACHED = "receiver_detached"
    STATUS = "status"
    INPUT = "input"
    RAW_REPORT = "raw_report"
    UNKNOWN_REPORT = "unknown_report"
    ERROR = "error"
    NOTICE = "notice"


@dataclass(frozen=True)
class ServiceEvent:
    type: str
    message: str = ""
    input_event: InputEvent | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class CollectionInfo:
    path: str
    usage_page: int
    usage: int
    interface: int
    product: str
    manufacturer: str
    opened: bool = False
    reports: int = 0
    is_status: bool = False

    @property
    def name(self) -> str:
        return collection_name(self.usage_page, self.usage)


@dataclass
class ServiceState:
    """Everything the UI renders. Copied out under a lock."""

    receiver_present: bool = False
    readers_active: int = 0
    snapshot: HeadsetSnapshot | None = None
    last_status_time: float | None = None
    last_report_time: float | None = None
    last_report_hex: str = ""
    last_report_collection: str = ""
    status_reports: int = 0
    total_reports: int = 0
    total_bytes: int = 0
    report_sequence: int = 0
    inputs_detected: int = 0
    last_input: str = ""
    last_error: str = ""
    collections: tuple[CollectionInfo, ...] = ()
    backend_available: bool = True
    backend_message: str = ""

    @property
    def headset_linked(self) -> bool:
        return bool(self.snapshot and self.snapshot.headset_connected)

    def status_age(self, now: float | None = None) -> float | None:
        if self.last_status_time is None:
            return None
        return max(0.0, (time.monotonic() if now is None else now) - self.last_status_time)

    @property
    def status_stale(self) -> bool:
        age = self.status_age()
        return age is None or age > STALE_SECONDS


# ----------------------------------------------------------------- service --

class HeadsetService:
    """Owns the connection to the receiver."""

    def __init__(
        self,
        input_handler: Callable[[InputEvent], None] | None = None,
        read_all_collections: bool = False,
        settle_seconds: float | None = None,
        debounce_seconds: float | None = None,
        resync_threshold: int | None = None,
        low_battery_threshold: int | None = None,
    ) -> None:
        self._state = ServiceState()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._reports: "queue.Queue[tuple[str, bytes]]" = queue.Queue(maxsize=2048)
        self._ui_events: "queue.Queue[ServiceEvent]" = queue.Queue(maxsize=MAX_UI_EVENTS)
        self._readers: dict[str, NativeWindowsHIDReader] = {}
        self._collections: dict[str, CollectionInfo] = {}
        # Collections with InputReportByteLength=0 are valid HID collections
        # (typically output/control-only) but cannot provide input reports.
        # Remember them so the scanner does not retry them every second.
        self._non_input_collections: set[str] = set()
        self._fingerprints: dict[tuple[int, int, int, int], ReportFingerprint] = {}
        self._scanner: threading.Thread | None = None
        self._dispatcher: threading.Thread | None = None
        self._scan_request = threading.Event()
        self._input_handler = input_handler
        self.read_all_collections = read_all_collections

        detector_kwargs: dict[str, Any] = {}
        if settle_seconds is not None:
            detector_kwargs["settle_seconds"] = settle_seconds
        if debounce_seconds is not None:
            detector_kwargs["debounce_seconds"] = debounce_seconds
        if resync_threshold is not None:
            detector_kwargs["resync_threshold"] = resync_threshold
        if low_battery_threshold is not None:
            detector_kwargs["low_battery_threshold"] = low_battery_threshold
        self._detector = EdgeDetector(**detector_kwargs)

    # ------------------------------------------------------------ plumbing --

    def set_input_handler(self, handler: Callable[[InputEvent], None] | None) -> None:
        self._input_handler = handler

    def configure_detection(
        self, settle_seconds: float, debounce_seconds: float,
        resync_threshold: int, low_battery_threshold: int | None = None,
    ) -> None:
        self._detector.settle_seconds = settle_seconds
        self._detector.debounce_seconds = debounce_seconds
        self._detector.resync_threshold = resync_threshold
        if low_battery_threshold is not None:
            self._detector.low_battery_threshold = low_battery_threshold

    def snapshot(self) -> ServiceState:
        with self._lock:
            return replace(self._state, collections=tuple(self._collections.values()))

    def poll_events(self, limit: int = 200) -> list[ServiceEvent]:
        """Drain pending events. Called from the UI thread only."""
        drained: list[ServiceEvent] = []
        for _ in range(limit):
            try:
                drained.append(self._ui_events.get_nowait())
            except queue.Empty:
                break
        return drained

    def fingerprints(self) -> list[ReportFingerprint]:
        with self._lock:
            return sorted(
                self._fingerprints.values(), key=lambda f: f.count, reverse=True
            )

    def _emit(self, event: ServiceEvent) -> None:
        try:
            self._ui_events.put_nowait(event)
        except queue.Full:
            try:  # drop the oldest so the newest is always visible
                self._ui_events.get_nowait()
                self._ui_events.put_nowait(event)
            except queue.Empty:
                pass

    # ------------------------------------------------------------ lifecycle --

    def start(self) -> None:
        backend_ok = native_windows_available()
        message = ""
        if not backend_ok:
            message = (
                "HID reading needs Windows. The interface still works so you can "
                "review and edit bindings."
            )
        elif hid is None:
            backend_ok = False
            message = (
                "The hidapi package is missing. Install it with: "
                "python -m pip install -r requirements.txt"
            )
        with self._lock:
            self._state.backend_available = backend_ok
            self._state.backend_message = message

        if not backend_ok:
            log.warning("Device backend unavailable: %s", message)
            self._emit(ServiceEvent(EventType.ERROR, message))
            return

        self._stop.clear()
        self._scan_request.clear()
        self._dispatcher = threading.Thread(
            target=self._dispatch_loop, name="hid-dispatch", daemon=True
        )
        self._dispatcher.start()
        self._scanner = threading.Thread(
            target=self._scan_loop, name="hid-scanner", daemon=True
        )
        self._scanner.start()
        log.info("Headset service started (target %04X:%04X)", TARGET_VID, TARGET_PID)

    def request_scan(self) -> None:
        """Request an immediate device enumeration from the scanner thread."""
        if self._scanner is not None and self._scanner.is_alive():
            self._scan_request.set()
            log.info("Device refresh requested")

    def stop(self) -> None:
        log.info("Stopping headset service")
        self._stop.set()
        self._scan_request.set()
        for reader in list(self._readers.values()):
            try:
                reader.stop()
            except Exception:
                log.exception("Reader refused to stop cleanly")
        self._readers.clear()
        for thread in (self._scanner, self._dispatcher):
            if thread is not None and thread.is_alive():
                thread.join(timeout=2.0)
        log.info("Headset service stopped")

    # ------------------------------------------------------------ scanning --

    def _enumerate(self) -> list[dict[str, Any]]:
        if hid is None:
            return []
        try:
            return list(hid.enumerate(TARGET_VID, TARGET_PID))
        except Exception as exc:
            log.error("hid.enumerate failed: %s", exc)
            with self._lock:
                self._state.last_error = f"Device scan failed: {exc}"
            return []

    @staticmethod
    def _select(devices: list[dict[str, Any]], read_all: bool) -> list[dict[str, Any]]:
        """Pick the collections worth opening.

        Prefer the known status collection. If it is not present - which is
        what happens when the receiver enumerates without usable usage
        information - fall back to opening everything rather than nothing.
        """
        if read_all:
            return devices
        preferred = [
            info for info in devices
            if is_status_collection(
                int(info.get("usage_page") or 0), int(info.get("usage") or 0)
            )
        ]
        if preferred:
            return preferred
        if devices:
            log.info(
                "Status collection FF01:0020 not present; opening all %d collection(s)",
                len(devices),
            )
        return devices

    def _scan_loop(self) -> None:
        self._scan_once()
        while not self._stop.is_set():
            self._scan_request.wait(SCAN_INTERVAL)
            if self._stop.is_set():
                break
            self._scan_request.clear()
            try:
                self._scan_once()
            except Exception as exc:  # pragma: no cover - defensive
                log.exception("Device scan raised")
                with self._lock:
                    self._state.last_error = f"Device scan error: {exc}"
        return

    def _scan_once(self) -> None:
        devices = self._enumerate()
        present = bool(devices)

        with self._lock:
            previously_present = self._state.receiver_present
            self._state.receiver_present = present

        if present and not previously_present:
            log.info("Receiver attached (%d collection(s))", len(devices))
            self._emit(ServiceEvent(
                EventType.RECEIVER_ATTACHED, "USB receiver connected"
            ))
        elif not present and previously_present:
            log.info("Receiver detached")
            self._handle_receiver_lost()
            return

        if not present:
            return

        selected = self._select(devices, self.read_all_collections)
        selected_paths = {windows_path(i.get("path", "")) for i in selected if i.get("path")}

        # Record every collection, even ones not opened, for the diagnostics view.
        with self._lock:
            self._collections = {}
            for info in devices:
                path = windows_path(info.get("path", ""))
                if not path:
                    continue
                usage_page = int(info.get("usage_page") or 0)
                usage = int(info.get("usage") or 0)
                existing = self._collections.get(path)
                self._collections[path] = CollectionInfo(
                    path=path,
                    usage_page=usage_page,
                    usage=usage,
                    interface=int(info.get("interface_number") or -1),
                    product=str(info.get("product_string") or "Unknown"),
                    manufacturer=str(info.get("manufacturer_string") or "Unknown"),
                    opened=path in self._readers,
                    reports=existing.reports if existing else 0,
                    is_status=is_status_collection(usage_page, usage),
                )

        for info in selected:
            path = windows_path(info.get("path", ""))
            if (
                not path
                or path in self._readers
                or path in self._non_input_collections
            ):
                continue
            self._open(path, info)

        for path in [p for p in self._readers if p not in selected_paths]:
            log.info("Collection disappeared, closing reader")
            reader = self._readers.pop(path, None)
            if reader is not None:
                reader.stop()

        with self._lock:
            self._state.readers_active = len(self._readers)

    def _open(self, path: str, info: dict[str, Any]) -> None:
        usage_page = int(info.get("usage_page") or 0)
        usage = int(info.get("usage") or 0)
        label = collection_name(usage_page, usage)
        reader = NativeWindowsHIDReader(
            info.get("path"),
            on_report=lambda report, p=path: self._on_report(p, report),
            on_error=lambda exc, p=path, l=label: self._on_reader_error(p, l, exc),
            label=label,
            initial_report_id=(
                STATUS_REPORT_ID
                if is_status_collection(usage_page, usage)
                else None
            ),
        )
        try:
            reader.start()
        except NoInputReportError:
            # This collection has no input report stream by design. Do not
            # report it as an application error and do not retry it every scan.
            self._non_input_collections.add(path)
            log.info(
                "Skipping %s: HID collection has no input reports "
                "(InputReportByteLength=0)",
                label,
            )
            return
        except Exception as exc:
            message = f"Could not open {label}: {exc}"
            log.error(message)
            with self._lock:
                self._state.last_error = message
            self._emit(ServiceEvent(EventType.ERROR, message))
            return

        self._readers[path] = reader
        with self._lock:
            existing = self._collections.get(path)
            if existing is not None:
                self._collections[path] = replace(existing, opened=True)
            self._state.readers_active = len(self._readers)
        log.info("Reading %s", label)
        self._emit(ServiceEvent(EventType.NOTICE, f"Listening on {label}"))

    def _on_reader_error(self, path: str, label: str, exc: Exception) -> None:
        reader = self._readers.pop(path, None)
        if reader is not None:
            try:
                reader.stop()
            except Exception:
                pass
        with self._lock:
            self._state.readers_active = len(self._readers)

        if isinstance(exc, DeviceGoneError):
            # Expected on unplug. The scanner will notice and reset state.
            log.info("%s closed: device gone", label)
            return
        message = f"{label}: {exc}"
        with self._lock:
            self._state.last_error = message
        self._emit(ServiceEvent(EventType.ERROR, message))

    def _handle_receiver_lost(self) -> None:
        for reader in list(self._readers.values()):
            try:
                reader.stop()
            except Exception:
                pass
        self._readers.clear()
        self._non_input_collections.clear()
        self._detector.reset("receiver unplugged")
        with self._lock:
            self._state.readers_active = 0
            self._state.snapshot = None
            self._state.last_status_time = None
            self._collections = {}
        self._emit(ServiceEvent(
            EventType.RECEIVER_DETACHED, "USB receiver disconnected"
        ))

    # ----------------------------------------------------------- dispatch --

    def _on_report(self, path: str, report: bytes) -> None:
        """Called on a reader thread. Must stay cheap and must not block."""
        try:
            self._reports.put_nowait((path, report))
        except queue.Full:
            log.warning("Report queue full; dropping a report")

    def _dispatch_loop(self) -> None:
        while not self._stop.is_set():
            try:
                path, report = self._reports.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._process(path, report)
            except Exception:  # pragma: no cover - defensive
                log.exception("Failed to process a report")

    def _process(self, path: str, report: bytes) -> None:
        now = time.monotonic()
        with self._lock:
            info = self._collections.get(path)
            label = info.name if info else "Unknown collection"
            self._state.total_reports += 1
            self._state.report_sequence += 1
            report_sequence = self._state.report_sequence
            self._state.total_bytes += len(report)
            self._state.last_report_time = now
            self._state.last_report_hex = hex_bytes(report)
            self._state.last_report_collection = label
            if info is not None:
                self._collections[path] = replace(info, reports=info.reports + 1)

        raw_hex = hex_bytes(report)
        # Log every report, including identical decoded states. A repeated
        # physical click may be represented by a changing byte that the
        # current state decoder does not yet assign a semantic meaning to.
        log.info(
            "RAW #%06d | %s | %d bytes | %s",
            report_sequence, label, len(report), raw_hex,
        )
        self._emit(ServiceEvent(
            EventType.RAW_REPORT,
            raw_hex,
            payload={
                "collection": label,
                "length": len(report),
                "sequence": report_sequence,
                "raw": raw_hex,
            },
        ))

        snapshot = parse_status(report)
        if snapshot is None:
            self._record_unknown(path, report, label)
            return

        previous_snapshot = self._detector.last_snapshot
        log.info(
            "DECODE #%06d | B0 | volume=%s (%s%%) | chatmix=%s | battery=%s | "
            "charging=%s | vss=%s | mic_muted=%s | linked=%s | flags=0x%02X | "
            "byte5=0x%02X | byte6=0x%02X | byte7=0x%02X",
            report_sequence, snapshot.volume_level, snapshot.volume_percent,
            snapshot.chat_balance, snapshot.battery_percent, snapshot.charging,
            snapshot.vss, snapshot.mic_muted, snapshot.headset_connected,
            snapshot.flags, snapshot.unknown_bytes[0], snapshot.unknown_bytes[1],
            snapshot.unknown_bytes[2],
        )
        if (
            previous_snapshot is not None
            and previous_snapshot.headset_connected
            and snapshot.headset_connected
            and previous_snapshot.volume_level != snapshot.volume_level
        ):
            log.info(
                "Headset volume state: %s -> %s (%s%% -> %s%%) | raw=%s",
                previous_snapshot.volume_level,
                snapshot.volume_level,
                previous_snapshot.volume_percent,
                snapshot.volume_percent,
                snapshot.raw_hex,
            )

        if previous_snapshot is not None:
            previous_marker = previous_snapshot.unknown_bytes[0]
            current_marker = snapshot.unknown_bytes[0]
            if current_marker != previous_marker and current_marker in (
                0x11, 0x12, 0x13, 0x14
            ):
                inferred = {
                    0x11: "VOLUME_UP candidate",
                    0x12: "VOLUME_DOWN candidate",
                    0x13: "CHATMIX_UP candidate",
                    0x14: "CHATMIX_DOWN candidate",
                }[current_marker]
                log.info(
                    "B0 ACTION CANDIDATE #%06d | byte5 0x%02X -> 0x%02X | "
                    "%s | state volume=%s chatmix=%s | raw=%s",
                    report_sequence,
                    previous_marker,
                    current_marker,
                    inferred,
                    snapshot.volume_level,
                    snapshot.chat_balance,
                    snapshot.raw_hex,
                )

        with self._lock:
            self._state.snapshot = snapshot
            self._state.status_reports += 1
            self._state.last_status_time = now

        self._emit(ServiceEvent(
            EventType.STATUS,
            snapshot.raw_hex,
            payload={
                "collection": label,
                "sequence": report_sequence,
                "snapshot": snapshot,
                "volume_level": snapshot.volume_level,
                "volume_percent": snapshot.volume_percent,
            },
        ))

        for event in self._detector.feed(snapshot, now=now):
            self._handle_input(event)

    def _handle_input(self, event: InputEvent) -> None:
        with self._lock:
            self._state.inputs_detected += 1
            self._state.last_input = str(event)

        self._emit(ServiceEvent(
            EventType.INPUT, str(event), input_event=event,
            payload={"repeat": event.repeat, "detail": event.detail},
        ))
        log.info(
            "INPUT #%06d | %s | repeat=%d | value=%s | detail=%s",
            self._state.inputs_detected, event.input_id, event.repeat,
            event.value, event.detail or "-",
        )

        handler = self._input_handler
        if handler is not None:
            try:
                handler(event)
            except Exception:
                log.exception("Input handler raised for %s", event.input_id)

    def _record_unknown(self, path: str, report: bytes, label: str) -> None:
        if not report:
            return
        with self._lock:
            info = self._collections.get(path)
            usage_page = info.usage_page if info else 0
            usage = info.usage if info else 0
            key = (usage_page, usage, len(report), report[0])
            fingerprint = self._fingerprints.get(key)
            if fingerprint is None:
                fingerprint = ReportFingerprint(usage_page, usage, len(report), report[0])
                self._fingerprints[key] = fingerprint
                first_time = True
            else:
                first_time = False
            fingerprint.observe(report)
            count = fingerprint.count

        if first_time:
            log.info(
                "Unrecognised report on %s: %d bytes starting 0x%02X [%s]",
                label, len(report), report[0], hex_bytes(report),
            )
            self._emit(ServiceEvent(
                EventType.UNKNOWN_REPORT,
                f"New report shape on {label}: {hex_bytes(report)}",
                payload={"collection": label, "count": count},
            ))
