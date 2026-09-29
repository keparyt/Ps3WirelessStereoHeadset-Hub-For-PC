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
import time
from dataclasses import replace as dataclass_replace
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
        self._last_output_switch = 0.0

    # ----------------------------------------------------------- lifecycle ---

    def start(self) -> None:
        self._monitor.start()

    def fxsound_request_live_push(self, profile: AudioProfile) -> None:
        """Push the equalizer to FxSound live (coalesced, off the UI thread).

        Called while the user drags a band, so the curve the user hears
        updates in real time. Coalescing and the process spawn happen on the
        backend's worker, never on the Tk thread.
        """
        self._fxsound.request_live_push(profile.clamped())

    def stop(self) -> None:
        with self._lock:
            self._dsp.stop()
            self._monitor.stop()
            self._fxsound.stop_live_push()

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
        # When FxSound is the processor, its instance has to hear about the
        # change - the loopback engine picks profiles up on its own.
        if self._enabled and self._backend_kind == "fxsound":
            endpoint = self.default_endpoint()
            self._fxsound.apply_equalizer(profile.clamped())
            self._fxsound.set_effects({
                "bass": profile.bass,
                "clarity": profile.clarity,
                "ambience": profile.ambience,
                "surround": profile.surround,
                "dynamic_boost": profile.dynamic_boost,
            })
            if profile.preset_name:
                self._fxsound.set_preset(profile.preset_name)
            if endpoint is not None:
                self._fxsound.set_output(endpoint.name)

    # ------------------------------------------------------------- fxsound ---

    def fxsound_status(self, force: bool = False) -> FxSoundStatus:
        return self._fxsound.read_status(force=force)

    def fxsound_status_stamp(self) -> tuple[int, int] | None:
        """Identity of FxSound's last status.json write, or None if no file.

        Cheap enough to poll every UI tick: one stat call, no process spawn,
        no COM. A changed stamp means FxSound rewrote its status file.
        """
        return self._fxsound.status_file_stamp()

    def fxsound_is_installed(self) -> bool:
        return self._fxsound.is_installed()

    def fxsound_is_running(self) -> bool:
        """Whether a FxSound instance is up and answering right now."""
        return self.fxsound_status().running

    def fxsound_set_output(self, device_name: str) -> bool:
        """Point the FxSound application at a specific output device."""
        return self._fxsound.set_output(device_name)

    def fxsound_launch(self, timeout: float = 10.0) -> FxSoundStatus:
        """Start FxSound when it is not running and wait for it to answer.

        Idempotent: an already-running instance is simply re-probed. Returns
        the resulting status, with ``running`` set when it worked and a
        non-empty ``error`` explaining it when it did not.
        """
        if self._fxsound.is_installed():
            self._fxsound.launch()
        status = self._fxsound.wait_until_running(timeout)
        if not status.running:
            status.error = status.error or (
                "FxSound did not respond after being started."
            )
        return status

    def fxsound_download_url(self) -> str:
        return self._fxsound.DOWNLOAD_URL

    def apply_profile_via_fxsound(self, profile: AudioProfile,
                                  output_name: str) -> FxSoundStatus:
        return self._fxsound.apply_profile(profile, output_name)

    def adopt_fxsound_status(self, status: FxSoundStatus | None = None) -> AudioProfile:
        """Merge FxSound's *live* settings into the active profile.

        Reads the application's status and overwrites the equalizer and the
        five effect levels of the active profile with what the application is
        actually running - so the Hub shows the same exact configuration as
        FxSound instead of a stale copy. Values FxSound does not report are
        left alone.
        """
        if status is None:
            status = self.fxsound_status(force=True)
        endpoint = self.default_endpoint()
        profile = (self.profile_for(endpoint.device_id, endpoint.name)
                   if endpoint else DEFAULT_PROFILE)
        updated = _profile_from_status(profile, status)
        self.set_profile(updated)
        return updated

    # ------------------------------------------------------- output routing --

    #: Minimum seconds between two default-endpoint changes. Reassigning the
    #: Windows default output is a disruptive operation, and doing it
    #: repeatedly - which is exactly what a flapping device list would cause -
    #: can leave the MMDevice endpoint objects stuck in an uninitialised state
    #: where Windows reports no output device at all. The guard turns a
    #: pathological loop into at most one switch per interval.
    OUTPUT_SWITCH_COOLDOWN = 20.0

    def set_output_device(self, device_id: str, device_name: str = "",
                          force: bool = False) -> tuple[bool, str]:
        """Make ``device_id`` the default output, with a rate limit.

        Returns ``(ok, message)``. A refusal because of the cooldown is
        reported rather than silently swallowed, so the UI can say why.
        """
        from .device_monitor import set_default_render_endpoint

        if not device_id:
            return False, "No output device was selected."
        current = self.default_endpoint()
        if current is not None and current.device_id == device_id:
            return True, ""

        now = time.monotonic()
        if not force:
            elapsed = now - self._last_output_switch
            if self._last_output_switch and elapsed < self.OUTPUT_SWITCH_COOLDOWN:
                wait = int(self.OUTPUT_SWITCH_COOLDOWN - elapsed) + 1
                return False, (f"Leaving the output alone for {wait}s. Repeatedly "
                               "changing it can break audio device detection.")
        # Record the attempt before the call, so a failure that still counts as
        # an attempt cannot be retried in a tight loop.
        self._last_output_switch = now

        ok, message = set_default_render_endpoint(device_id)
        if ok and device_name:
            self._fxsound.set_output(device_name)
        return ok, message

    # --------------------------------------------------------- output route --

    @property
    def auto_output(self) -> bool:
        """Whether the Hub should move the default output to the headset."""
        return self._auto_output

    def set_auto_output(self, enabled: bool) -> None:
        was = self._auto_output
        self._auto_output = bool(enabled)
        if was and not self._auto_output:
            # Switching the toggle off is a good moment to forget any pending
            # cooldown, so re-enabling it is not silently ignored.
            self._last_output_switch = 0.0

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


def _profile_from_status(profile: AudioProfile,
                         status: FxSoundStatus) -> AudioProfile:
    """Overwrite ``profile`` with the live settings FxSound reports.

    Module-level so it can be exercised without an engine, a monitor or any
    audio device. Only well-reported values are copied; anything missing in
    status.json keeps the profile's current value.
    """
    from .profiles import MAX_BANDS

    eq_block = dict(getattr(status, "equalizer", {}) or {})
    bands = eq_block.get("bands") or []
    updates: dict[str, Any] = {}
    if bands:
        frequencies = [float(b.get("frequency", 0.0)) for b in bands]
        gains = [float(b.get("gain", 0.0)) for b in bands]
        try:
            count = int(eq_block.get("num_bands", len(bands)))
        except (TypeError, ValueError):
            count = len(bands)
        count = max(1, min(count, len(bands), MAX_BANDS))
        updates["eq"] = list(zip(frequencies[:count], gains[:count]))
        updates["eq_bands"] = count
    for target, source in (("master_gain_db", "master_gain"),
                           ("volume_leveling_db", "volume_leveling"),
                           ("filter_q", "filter_q"),
                           ("balance_db", "balance")):
        if source in eq_block:
            try:
                updates[target] = float(eq_block[source])
            except (TypeError, ValueError):
                continue
    for name in ("bass", "clarity", "ambience", "surround", "dynamic_boost"):
        if name in (status.effects or {}):
            try:
                updates[name] = float(status.effects[name])
            except (TypeError, ValueError):
                continue
    preset = str(getattr(status, "selected_preset", "") or "")
    if preset:
        updates["preset_name"] = preset
    if not updates:
        return profile
    return dataclass_replace(profile, **updates).clamped()
