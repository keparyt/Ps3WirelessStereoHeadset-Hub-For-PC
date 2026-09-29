"""AudioEngine: the audio subsystem facade.

The rest of the application sees exactly this class - never the loopback
engine, device monitor or profile store directly. It owns the subsystem's
lifetime and answers the three questions the UI and the state layer ask:

* is processing running, and on what endpoint?
* which endpoints exist, and which is the Windows default?
* what did the optional FxSound integration find?

Threading: :meth:`start`, :meth:`stop` and :meth:`update_profile` are safe to
call from the Tk thread; the loopback engine handles its own capture/render
threads internally. Endpoint lists come from the monitor's cached copy, so
reading them never touches COM.
"""

from __future__ import annotations

import threading
from typing import Any

from ..applog import get_logger
from .device_monitor import AudioDeviceMonitor, EndpointInfo
from .fxsound_backend import FxSoundBackend, FxSoundStatus
from .loopback_dsp import LoopbackDSPEngine, ProcessingState
from .profiles import AudioProfile, ProfileStore

log = get_logger("audio.engine")

#: The processing profile used when the user has never tuned an endpoint.
DEFAULT_PROFILE = AudioProfile()


class AudioEngine:
    """Owns device monitoring, the loopback DSP and the FxSound backend."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._monitor = AudioDeviceMonitor()
        self._dsp = LoopbackDSPEngine()
        self._fxsound = FxSoundBackend()
        self._store = ProfileStore()
        self._enabled = False
        self._backend_kind = "native"
        self._active_device_id: str | None = None
        self._auto_output = False

    # ----------------------------------------------------------- lifecycle ---

    def start(self) -> None:
        self._monitor.start()

    def stop(self) -> None:
        with self._lock:
            self._dsp.stop()
            self._monitor.stop()

    def shutdown(self) -> None:
        self.stop()

    # -------------------------------------------------------------- state ---

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def backend_kind(self) -> str:
        return self._backend_kind

    @property
    def processing_state(self) -> ProcessingState:
        return self._dsp.state

    def is_processing(self) -> bool:
        return self._dsp.state.active

    # ---------------------------------------------------------- endpoints ---

    def endpoints(self) -> list[EndpointInfo]:
        return self._monitor.endpoints

    def default_endpoint(self) -> EndpointInfo | None:
        return self._monitor.default_endpoint()

    def find_endpoint(self, device_id: str) -> EndpointInfo | None:
        for endpoint in self.endpoints():
            if endpoint.device_id == device_id:
                return endpoint
        return None

    def refresh_devices(self) -> None:
        self._monitor.refresh()

    # ------------------------------------------------------------ profiles ---

    def profile_for(self, device_id: str, device_name: str = "") -> AudioProfile:
        with self._lock:
            profile = self._store.for_device(device_id, device_name)
            return profile.clamped()

    def all_profiles(self) -> list[AudioProfile]:
        with self._lock:
            return self._store.all()

    def set_profile(self, profile: AudioProfile) -> None:
        with self._lock:
            self._store.set(profile)
        # A running engine picks the change up without a restart.
        self._dsp.update_profile(profile.clamped())

    def remove_profile(self, device_id: str) -> bool:
        with self._lock:
            return self._store.remove(device_id)

    # ------------------------------------------------------------- control ---

    def set_enabled(self, enabled: bool, backend: str = "native") -> bool:
        """Enable or disable audio processing.

        With ``native`` the loopback engine runs. With ``fxsound`` the Hub
        drives the installed FxSound application through its CLI instead and
        the loopback engine stays idle; the FxSound status is returned so the
        caller can surface errors honestly.
        """
        self._backend_kind = backend if backend in ("native", "fxsound") else "native"
        self._enabled = bool(enabled)
        if not self._enabled:
            self._dsp.stop()
            self._active_device_id = None
            return True

        if self._backend_kind == "native":
            endpoint = self.default_endpoint()
            if endpoint is None:
                # The monitor may still be starting; try once synchronously.
                self.refresh_devices()
                endpoint = self.default_endpoint()
            if endpoint is None:
                self._dsp.stop()
                log.warning("Audio processing requested but no output endpoint is known")
                return False
            return self._start_native(endpoint.device_id, endpoint.name,
                                      self.profile_for(endpoint.device_id, endpoint.name))

        # FxSound backend: push the active profile onto the running instance.
        endpoint = self.default_endpoint()
        output_name = endpoint.name if endpoint else ""
        profile = (self.profile_for(endpoint.device_id, endpoint.name)
                   if endpoint else DEFAULT_PROFILE)
        status = self._fxsound.apply_profile(profile, output_name)
        if status.error:
            log.warning("FxSound integration failed: %s", status.error)
        return not status.error or status.running

    def _start_native(self, device_id: str, device_name: str,
                      profile: AudioProfile) -> bool:
        started = self._dsp.start(device_id, device_name, profile)
        if started:
            self._active_device_id = device_id
        else:
            self._active_device_id = None
        return started

    def stop_processing(self) -> None:
        with self._lock:
            self._dsp.stop()
            self._active_device_id = None

    def update_profile(self, profile: AudioProfile) -> None:
        self.set_profile(profile)

    # ------------------------------------------------------------- fxsound ---

    def fxsound_status(self, force: bool = False) -> FxSoundStatus:
        return self._fxsound.read_status(force=force)

    def fxsound_is_installed(self) -> bool:
        return self._fxsound.is_installed()

    def fxsound_set_output(self, device_name: str) -> bool:
        """Point the FxSound application at a specific output device."""
        return self._fxsound.set_output(device_name)

    # --------------------------------------------------------- output route --

    @property
    def auto_output(self) -> bool:
        """Whether the Hub should move the default output to the headset."""
        return self._auto_output

    def set_auto_output(self, enabled: bool) -> None:
        self._auto_output = bool(enabled)

    def apply_profile_via_fxsound(self, profile: AudioProfile,
                                  output_name: str) -> FxSoundStatus:
        return self._fxsound.apply_profile(profile, output_name)

    # -------------------------------------------------------- persistence ---

    def export_state(self) -> dict[str, Any]:
        """The ``audio`` config key: profiles plus the engine switches."""
        with self._lock:
            payload = self._store.to_dict()
            payload["enabled"] = self._enabled
            payload["backend"] = self._backend_kind
            payload["auto_output"] = self._auto_output
            return payload

    def import_state(self, data: dict[str, Any]) -> None:
        with self._lock:
            self._store = ProfileStore.from_dict(data or {})
            self._enabled = bool((data or {}).get("enabled", False))
            backend = str((data or {}).get("backend", "native"))
            self._backend_kind = backend if backend in ("native", "fxsound") else "native"
            self._auto_output = bool((data or {}).get("auto_output", False))

    # ------------------------------------------------------------- helpers ---

    def as_dict(self) -> dict[str, Any]:
        state = self._dsp.state
        return {
            "enabled": self._enabled,
            "backend": self._backend_kind,
            "processing": state.as_dict(),
            "fxsound_installed": self.fxsound_is_installed(),
        }
