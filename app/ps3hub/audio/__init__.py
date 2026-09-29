"""Audio processing subsystem.

Layering inside this package (no module imports the one above it):

    dsp             pure signal math, no I/O
    device_monitor  Windows endpoint discovery + default-device notifications
    profiles        per-output-device effect profile model
    fxsound_backend optional integration with an installed FxSound (CLI/JSON)
    loopback_dsp    WASAPI loopback -> DSP -> render engine (sounddevice)
    engine          AudioEngine facade: the only API the rest of the app sees

Design constraint: the rest of the application must not know which backend is
active. Everything talks to :class:`ps3hub.audio.engine.AudioEngine`.
"""

from __future__ import annotations

__all__ = [
    "dsp",
    "device_monitor",
    "profiles",
    "fxsound_backend",
    "loopback_dsp",
    "engine",
]


def __getattr__(name: str):
    # Engine imports sounddevice/numpy lazily so the Hub still starts when the
    # optional audio dependencies are missing.
    if name == "AudioEngine":
        from .engine import AudioEngine
        return AudioEngine
    raise AttributeError(name)
