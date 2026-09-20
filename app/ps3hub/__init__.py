"""PS3 Wireless Stereo Headset Hub for Windows.

A production application for the Sony PlayStation Gold Wireless Stereo Headset
(CUHYA-0080) and its CUHYA-0081 USB receiver (VID 0x12BA / PID 0x0035).

Layering (no module imports the one above it):

    protocol     pure byte decoding, no I/O
    hid_reader   native Windows overlapped HID reads
    inputs       state deltas -> discrete input events
    actions      input actions -> Windows key/media injection
    mappings     input id -> action binding model
    config       persistence
    device       orchestration: enumeration, hotplug, dispatch
    ui           presentation only
"""

from __future__ import annotations

__all__ = ["APP_NAME", "APP_SLUG", "APP_VERSION", "APP_AUTHOR_URL"]

APP_NAME = "PS3 Wireless Stereo Headset Hub"
APP_SLUG = "PS3HeadsetHub"
APP_VERSION = "1.0.0"
APP_AUTHOR_URL = "https://github.com/keparyt/Ps3WirelessStereoHeadset-Hub-For-PC"
