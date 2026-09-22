"""Catch-all HID action handling for every inbound receiver report.

This is intentionally a thin compatibility layer: the normal B0 state decoder
still owns headset state, while consumer-control reports are treated as real
commands even when the previous state did not change.
"""
from __future__ import annotations

from .applog import get_logger
from .device import HeadsetService
from .inputs import DESCRIPTOR_BY_ID, INPUT_DESCRIPTORS, Category, InputDescriptor, InputEvent, InputId

log = get_logger("report_actions")

CONSUMER_PAGE = 0x000C
CONSUMER_USAGE_TO_INPUT = {
    0x00B0: ("consumer_previous", "Previous track"),
    0x00B5: ("consumer_next", "Next track"),
    0x00CD: ("consumer_play_pause", "Play / pause"),
    0x00E2: ("consumer_mute", "Mute"),
    0x00E9: ("consumer_volume_up", "Consumer volume up"),
    0x00EA: ("consumer_volume_down", "Consumer volume down"),
}


def _install_descriptors() -> None:
    descriptors = list(INPUT_DESCRIPTORS)
    existing = set(DESCRIPTOR_BY_ID)
    for input_id, label in CONSUMER_USAGE_TO_INPUT.values():
        if input_id in existing:
            continue
        descriptors.append(InputDescriptor(
            input_id,
            label,
            Category.BUTTON,
            "Direct HID consumer-control command reported by the receiver.",
            repeatable=True,
            hardware_acts=True,
        ))
        DESCRIPTOR_BY_ID[input_id] = descriptors[-1]
    import sys
    module = sys.modules.get("ps3hub.inputs")
    if module is not None:
        module.INPUT_DESCRIPTORS = tuple(descriptors)


def _consumer_usages(report: bytes) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    # Consumer-control reports commonly contain a report ID followed by one or
    # more 16-bit HID usages. Check every adjacent pair so report-ID/padding
    # variants cannot hide a real command.
    for index in range(max(0, len(report) - 1)):
        usage = report[index] | (report[index + 1] << 8)
        mapping = CONSUMER_USAGE_TO_INPUT.get(usage)
        if mapping is not None:
            found.append((mapping[0], usage))
    # Also accept the reverse byte order as a defensive diagnostic path.
    for index in range(max(0, len(report) - 1)):
        usage = (report[index] << 8) | report[index + 1]
        mapping = CONSUMER_USAGE_TO_INPUT.get(usage)
        if mapping is not None:
            item = (mapping[0], usage)
            if item not in found:
                found.append(item)
    return found


def _patch_select() -> None:
    # The receiver exposes several HID collections. Never silently ignore one:
    # the Consumer Control collection is where media commands can arrive.
    HeadsetService._select = staticmethod(lambda devices, read_all: devices)


def _patch_process() -> None:
    original = HeadsetService._process
    if getattr(original, "_report_actions_patched", False):
        return

    def process(self: HeadsetService, path: str, report: bytes) -> None:
        info = self._collections.get(path)
        usage_page = info.usage_page if info else 0
        usage = info.usage if info else 0

        if usage_page == CONSUMER_PAGE:
            matches = _consumer_usages(report)
            if matches:
                for input_id, usage_code in matches:
                    event = InputEvent(
                        input_id,
                        repeat=1,
                        value=usage_code,
                        detail=f"consumer usage 0x{usage_code:04X}; raw={report.hex(' ').upper()}",
                    )
                    log.info(
                        "ACTION REPORT | collection=%04X:%04X | usage=0x%04X | %s",
                        usage_page, usage, usage_code, report.hex(" ").upper(),
                    )
                    # Deliberately bypass EdgeDetector admission. A HID
                    # consumer report IS the edge. Every received command is
                    # delivered, including repeated identical reports.
                    self._handle_input(event)
            else:
                log.info(
                    "ACTION REPORT UNKNOWN | collection=%04X:%04X | raw=%s",
                    usage_page, usage, report.hex(" ").upper(),
                )

        original(self, path, report)

    process._report_actions_patched = True
    HeadsetService._process = process


def install() -> None:
    _install_descriptors()
    _patch_select()
    _patch_process()
    log.info("Catch-all HID report/action handling installed")
