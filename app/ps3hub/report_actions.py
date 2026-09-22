"""Catch-all HID action handling for every inbound receiver report.

This compatibility layer keeps the normal B0 state decoder for headset state,
while opening every HID collection and treating direct consumer-control reports
as real commands. Repeated identical command reports are never debounced.
"""
from __future__ import annotations

from .applog import get_logger
from .device import HeadsetService
from .inputs import DESCRIPTOR_BY_ID, INPUT_DESCRIPTORS, Category, InputDescriptor, InputEvent

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

# Compatibility aliases to the project's existing default bindings.
INPUT_FALLBACKS = {
    "consumer_next": "chatmix_up",
    "consumer_previous": "chatmix_down",
    "consumer_play_pause": "vss_button",
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
    # Check every adjacent pair in both byte orders. This handles common
    # report-ID and padding variants without assuming one exact descriptor.
    for index in range(max(0, len(report) - 1)):
        for usage in (
            report[index] | (report[index + 1] << 8),
            (report[index] << 8) | report[index + 1],
        ):
            mapping = CONSUMER_USAGE_TO_INPUT.get(usage)
            if mapping is not None:
                item = (mapping[0], usage)
                if item not in found:
                    found.append(item)
    return found


def _patch_select() -> None:
    # Never silently ignore a HID collection. Media commands commonly use the
    # Consumer Control collection rather than the B0 status collection.
    HeadsetService._select = staticmethod(lambda devices, read_all: devices)


def _patch_profile_lookup() -> None:
    # Existing profiles already bind chat-mix up/down and VSS to media actions.
    # Let newly decoded direct consumer commands inherit those bindings without
    # requiring the user to recreate their profile.
    from .mappings import Profile
    original = Profile.bound_for
    if getattr(original, "_report_actions_patched", False):
        return

    def bound_for(self, input_id: str):
        mapping = original(self, input_id)
        if mapping is not None:
            return mapping
        fallback_id = INPUT_FALLBACKS.get(input_id)
        return original(self, fallback_id) if fallback_id else None

    bound_for._report_actions_patched = True
    Profile.bound_for = bound_for


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
                        detail=(
                            f"consumer usage 0x{usage_code:04X}; "
                            f"raw={report.hex(' ').upper()}"
                        ),
                    )
                    log.info(
                        "ACTION REPORT | collection=%04X:%04X | usage=0x%04X | %s",
                        usage_page, usage, usage_code, report.hex(" ").upper(),
                    )
                    # The report itself is the edge. Do not pass it through the
                    # state-change/debounce logic, so every command report is
                    # delivered, including identical repeated reports.
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
    _patch_profile_lookup()
    _patch_process()
    log.info("Catch-all HID report/action handling installed")
