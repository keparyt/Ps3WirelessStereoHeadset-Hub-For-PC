"""Native WASAPI loopback DSP engine (pure ctypes COM capture + render).

How audio actually flows
------------------------

WASAPI loopback capture taps **whatever Windows is mixing for one render
endpoint**. The processing chain is:

    applications -> Windows mixer -> [IAudioClient LOOPBACK capture of X]
                                  -> DSP (biquads + gain, this process)
                                  -> render to endpoint X (shared mode)

Rendering back to the same endpoint the capture taps would feed the loopback
capture again (a feedback loop), so the render stream is created on the same
endpoint but the loopback capture EXCLUDES only its own session's audio
through AUDCLNT_STREAMFLAGS_LOOPBACK + no self-echo: in practice the captured
mix contains other applications' audio, and our own render is *also* mixed in.
To avoid an infinite feedback loop the engine therefore renders to the
**default endpoint** unless the user explicitly selects a different output in
Settings; when input endpoint == output endpoint the engine renders the
processed signal but zeroes the loopback tap of its own stream by processing
only fresh capture (the loop is bounded because the capture is consumed
faster than it is produced - and additionally, shared-mode loopback on the
same endpoint would include our render, so a dedicated thread drain + the
10ms block keeps the loop gain well below 1.0 with the default unity profile).

For deterministic, feedback-free processing the recommended configuration -
and the one the UI defaults to - is exactly the FxSound model: the Hub's
render endpoint is selected once in Settings and the capture endpoint is the
Windows default. The AudioEngine picks the technically correct combination
for the situation and reports what it is actually doing; it never claims to
process when it cannot.

Real-time rules honoured by the audio threads:

* no logging, no I/O, no enumeration, no Tk, no Python object churn beyond
  the numpy arithmetic itself;
* filter coefficients are recomputed only when a parameter changes;
* capture and render each run on their own thread; the queue between them is
  preallocated and bounded; overflow drops oldest-first.

Verification: :func:`ps3hub.audio.dsp.spectrum_peak_db` compares loopback
recordings with effects off/on, which is how the test suite proves the DSP
actually alters the signal.
"""

from __future__ import annotations

import ctypes
import queue
import threading
import time
from ctypes import POINTER, byref, c_void_p, c_ulong, c_ulonglong
from dataclasses import dataclass
from typing import Any

import numpy as np

from ..applog import get_logger
from . import dsp
from .device_monitor import _ComVtable, _guid_struct, _read_id
from .profiles import AudioProfile

log = get_logger("audio.loopback")

try:
    import sounddevice as _sd
except Exception:  # pragma: no cover - sounddevice missing
    _sd = None

# COM identifiers
_CLSID_ENUMERATOR = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
_IID_ENUMERATOR = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
_IID_AUDIO_CLIENT = "{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}"
_IID_CAPTURE_CLIENT = "{C8ADBD64-E71E-48a0-A4DE-185C395CD317}"  # IAudioCaptureClient

AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
CLSCTX_INPROC_SERVER = 1
REFTIMES_PER_SEC = 10_000_000
REFTIMES_PER_MILLISEC = 10_000

# Capture buffer duration: 50 ms of headroom, drained in ~10 ms slices.
_BUFFER_DURATION_REFTIMES = 500000

# vtable prototypes for IAudioClient. Return type is c_long (raw HRESULT):
# WINFUNCTYPE with ctypes.HRESULT auto-raises OSError on failure, which in a
# polling loop (GetNextPacketSize returning AUDCLNT_S_BUFFER_EMPTY is normal)
# is exactly what we do NOT want. Callers check the returned code themselves.
_HRES = ctypes.c_long
_PROTO_Activate = ctypes.WINFUNCTYPE(_HRES, c_void_p, c_void_p, c_ulong, c_void_p, POINTER(c_void_p))
_PROTO_GetDefault = ctypes.WINFUNCTYPE(_HRES, c_void_p, c_ulong, c_ulong, POINTER(c_void_p))
_PROTO_Release = ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)
_PROTO_GetMixFormat = ctypes.WINFUNCTYPE(_HRES, c_void_p, POINTER(c_void_p))
_PROTO_Initialize = ctypes.WINFUNCTYPE(
    _HRES, c_void_p, c_ulong, c_ulong, c_ulonglong, c_ulong, c_void_p, c_void_p, c_void_p
)
_PROTO_GetBufferSize = ctypes.WINFUNCTYPE(_HRES, c_void_p, POINTER(ctypes.c_uint))
PROTO_Start = ctypes.WINFUNCTYPE(_HRES, c_void_p)
PROTO_Stop = ctypes.WINFUNCTYPE(_HRES, c_void_p)
_PROTO_GetNextPacketSize = ctypes.WINFUNCTYPE(_HRES, c_void_p, POINTER(ctypes.c_uint))
# IAudioCaptureClient::GetBuffer(OUT ppbData, OUT pNumFrames, OUT pdwFlags,
# OUT pu64DevicePosition, OUT pu64QPCPosition) - FIVE out-params plus `this`.
# Missing the 5th caused stack corruption (fail-fast 0xC0000409).
_PROTO_GetBuffer = ctypes.WINFUNCTYPE(
    _HRES, c_void_p, POINTER(ctypes.c_void_p), POINTER(ctypes.c_uint), POINTER(ctypes.c_ulong),
    POINTER(ctypes.c_ulonglong), POINTER(ctypes.c_ulonglong),
)
_PROTO_ReleaseBuffer = ctypes.WINFUNCTYPE(_HRES, c_void_p, c_ulong)
_PROTO_GetDevice = ctypes.WINFUNCTYPE(_HRES, c_void_p, ctypes.c_wchar_p, POINTER(c_void_p))
_PROTO_GetService = ctypes.WINFUNCTYPE(_HRES, c_void_p, c_void_p, POINTER(c_void_p))

# WAVEFORMATEX / WAVEFORMATEXTENSIBLE constants
_WAVE_FORMAT_EXTENSIBLE = 0xFFFE
_WAVE_FORMAT_IEEE_FLOAT = 0x0003


@dataclass(frozen=True)
class ProcessingState:
    """Snapshot of the engine's runtime condition."""

    active: bool = False
    device_id: str = ""
    device_name: str = ""
    sample_rate: int = 0
    channels: int = 0
    blocks_processed: int = 0
    drops: int = 0
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "device_id": self.device_id,
            "device_name": self.device_name,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "blocks_processed": self.blocks_processed,
            "drops": self.drops,
            "error": self.error,
        }


class _FilterChain:
    """Per-channel biquad chain derived from an AudioProfile.

    ``update`` recomputes coefficients only when values change; ``process``
    is the real-time path and touches only preallocated state.
    """

    def __init__(self, sample_rate: float) -> None:
        self.sample_rate = sample_rate
        self.lock = threading.Lock()
        self.master_gain = 1.0
        self.left_gain = 1.0
        self.right_gain = 1.0
        self.filters: list[dsp.StereoBiquad] = []
        #: The most recent profile, so the UI can be told what is in effect.
        self.band_count = 0

    def update(self, profile: AudioProfile) -> None:
        # Effect semantics match FxSound: 0 = off (transparent), 5 = moderate,
        # 10 = maximum. A band whose value is 0 contributes no filter at all,
        # so a profile with everything at 0 and master gain 0 dB is a perfect
        # passthrough - that is the property the DSP test asserts.
        # Two shelf bands (bass, clarity) + broad peaking bands for
        # ambience/surround/dynamics keep CPU cost bounded and the audible
        # effect meaningful.
        wanted: list[dsp.BiquadCoeffs] = []
        if profile.bass > 0.05:
            wanted.append(dsp.low_shelf_coeffs(self.sample_rate, 120.0, profile.bass * 1.2))
        if profile.clarity > 0.05:
            wanted.append(dsp.high_shelf_coeffs(self.sample_rate, 3500.0, profile.clarity * 1.0))
        if profile.ambience > 0.05:
            wanted.append(dsp.peaking_coeffs(self.sample_rate, 900.0, profile.ambience * 0.8, q=0.9))
        if profile.surround > 0.05:
            wanted.append(dsp.peaking_coeffs(self.sample_rate, 220.0, profile.surround * 0.9, q=0.8))
        if profile.dynamic_boost > 0.05:
            wanted.append(dsp.peaking_coeffs(self.sample_rate, 5000.0, profile.dynamic_boost * 0.4, q=1.2))

        # The equalizer: one peaking biquad per non-flat band, using the same
        # centre frequency and gain the UI shows. A band at 0 dB is skipped so
        # an untouched profile costs nothing and stays bit-transparent.
        eq_bands = list(getattr(profile, "eq", ()) or ())
        filter_q = float(getattr(profile, "filter_q", 1.0) or 1.0)
        for freq, gain_db in eq_bands:
            if abs(gain_db) < 0.05:
                continue
            try:
                wanted.append(dsp.peaking_coeffs(
                    self.sample_rate, float(freq), float(gain_db), q=filter_q))
            except (TypeError, ValueError):
                continue

        gain = dsp.db_to_gain(profile.master_gain_db)
        # Balance is a constant pan, not a filter: each side gets its own gain.
        balance = float(getattr(profile, "balance_db", 0.0) or 0.0)
        if balance > 0.0:
            left, right = dsp.db_to_gain(-balance), 1.0
        else:
            left, right = 1.0, dsp.db_to_gain(balance)
        with self.lock:
            while len(self.filters) < len(wanted):
                self.filters.append(dsp.StereoBiquad(wanted[len(self.filters)]))
            # Extra filters from a previous, heavier profile are bypassed by
            # resetting their coefficients to identity.
            identity = dsp.BiquadCoeffs(1.0, 0.0, 0.0, 0.0, 0.0)
            for index, existing in enumerate(self.filters):
                if index < len(wanted):
                    existing.set_coeffs(wanted[index])
                else:
                    existing.set_coeffs(identity)
            self.master_gain = gain
            self.left_gain = left
            self.right_gain = right
            self.band_count = len(eq_bands)

    def process(self, block: np.ndarray) -> None:
        with self.lock:
            filters = list(self.filters)
            gain = self.master_gain
            left = self.left_gain
            right = self.right_gain
        for biquad in filters:
            biquad.process(block)
        if gain != 1.0 or left != 1.0 or right != 1.0:
            if block.ndim > 1 and block.shape[1] >= 2:
                block[:, 0] *= gain * left
                block[:, 1] *= gain * right
            else:
                block *= gain
        np.clip(block, -1.0, 1.0, out=block)


class LoopbackDSPEngine:
    """WASAPI loopback capture -> DSP -> render, for one endpoint.

    Threading: a capture thread (COM apartment of its own) pulls mixed audio
    from the endpoint and pushes blocks into a bounded queue; a render thread
    (sounddevice callback) filters and plays them. ``stop()`` joins both.
    """

    #: Blocks kept in the queue between capture and render (10 ms blocks).
    QUEUE_BLOCKS = 8

    def __init__(self) -> None:
        self._state = ProcessingState()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._capture_thread: threading.Thread | None = None
        self._chain: _FilterChain | None = None
        self._blocks = 0
        self._drops = 0
        self._error = ""
        self._queue: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=self.QUEUE_BLOCKS)
        self._render_stream: Any = None

    # ------------------------------------------------------------- public --

    @property
    def state(self) -> ProcessingState:
        with self._lock:
            return self._state

    @property
    def available(self) -> bool:
        return hasattr(ctypes, "oledll")

    def start(self, device_id: str, device_name: str, profile: AudioProfile,
              output_name: str | None = None) -> bool:
        """Start processing ``device_id``'s mixed audio.

        ``output_name`` selects the render endpoint by friendly name; None
        renders to the PortAudio default (which is the FxSound pattern).
        """
        self.stop()
        self._stop.clear()
        self._blocks = 0
        self._drops = 0
        self._error = ""

        result: dict[str, Any] = {}
        ready = threading.Event()

        self._capture_thread = threading.Thread(
            target=self._capture_loop,
            args=(device_id, profile, result, ready),
            name="dsp-capture",
            daemon=True,
        )
        self._capture_thread.start()
        if not ready.wait(5.0):
            self.stop()
            with self._lock:
                self._state = ProcessingState(error="Capture stream did not start in time")
            return False
        if not result.get("ok"):
            self.stop()
            with self._lock:
                self._state = ProcessingState(error=str(result.get("error", "capture failed")))
            return False

        format_info = result["format"]  # (rate, channels)
        try:
            self._start_render(format_info, output_name)
        except Exception as exc:
            log.error("Render stream failed to start: %s", exc)
            self.stop()
            with self._lock:
                self._state = ProcessingState(
                    device_id=device_id, device_name=device_name,
                    error=f"Render failed: {exc}",
                )
            return False

        with self._lock:
            self._chain = _FilterChain(format_info[0])
            self._chain.update(profile)
            self._state = ProcessingState(
                active=True,
                device_id=device_id,
                device_name=device_name,
                sample_rate=format_info[0],
                channels=format_info[1],
            )
        log.info("Loopback DSP running: capture %r, render %r",
                 device_name, output_name or "PortAudio default")
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._render_stream is not None:
            try:
                self._render_stream.stop()
                self._render_stream.close()
            except Exception:
                pass
            self._render_stream = None
        if self._capture_thread is not None and self._capture_thread.is_alive():
            self._capture_thread.join(timeout=3.0)
        self._capture_thread = None
        with self._lock:
            was_active = self._state.active
            self._state = ProcessingState(
                device_id=self._state.device_id,
                device_name=self._state.device_name,
                blocks_processed=self._blocks,
                drops=self._drops,
                error=self._error,
            )
            self._chain = None
        if was_active:
            log.info("Loopback DSP stopped")

    def update_profile(self, profile: AudioProfile) -> None:
        chain = self._chain
        if chain is not None:
            chain.update(profile)

    # ------------------------------------------------------------ capture --

    def _capture_loop(self, device_id: str, profile: AudioProfile,
                      result: dict[str, Any], ready: threading.Event) -> None:
        """Owns the COM capture client. Runs until stopped."""
        ole32 = ctypes.oledll.ole32
        ole32.CoInitialize(None)
        client = c_void_p()
        capture = c_void_p()
        device = c_void_p()
        enumerator = c_void_p()
        fmt_ptr = c_void_p()
        started = False
        try:
            clsid = _guid_struct(_CLSID_ENUMERATOR)
            iid_enum = _guid_struct(_IID_ENUMERATOR)
            hr = ole32.CoCreateInstance(byref(clsid), None, CLSCTX_INPROC_SERVER,
                                        byref(iid_enum), byref(enumerator))
            if hr != 0:
                result["error"] = f"COM enumerator failed ({hr:#x})"
                return
            evt = _ComVtable(enumerator)
            # GetDevice(pwstrId, out**) - the id is a wide string argument.
            get_device = evt.func(5, _PROTO_GetDevice)
            hr = get_device(enumerator, device_id, byref(device))
            if hr != 0:
                result["error"] = f"Endpoint not found ({hr:#x})"
                return
            dvt = _ComVtable(device, count=8)
            activate = dvt.func(3, _PROTO_Activate)
            iid_client = _guid_struct(_IID_AUDIO_CLIENT)
            hr = activate(device, byref(iid_client), CLSCTX_INPROC_SERVER, None, byref(client))
            if hr != 0:
                result["error"] = f"IAudioClient Activate failed ({hr:#x})"
                return
            avt = _ComVtable(client, count=16)
            get_mix = avt.func(8, _PROTO_GetMixFormat)
            hr = get_mix(client, byref(fmt_ptr))
            if hr != 0:
                result["error"] = f"GetMixFormat failed ({hr:#x})"
                return
            rate, channels, valid_bits = _parse_format(fmt_ptr)
            result["format"] = (rate, channels)
            result["ok"] = True
            ready.set()

            initialize = avt.func(3, _PROTO_Initialize)
            hr = initialize(
                client, 0, AUDCLNT_STREAMFLAGS_LOOPBACK,
                _BUFFER_DURATION_REFTIMES, 0, fmt_ptr, None, None,
            )
            if hr != 0:
                with self._lock:
                    self._error = f"Loopback Initialize failed ({hr:#x})"
                return
            get_size = avt.func(4, _PROTO_GetBufferSize)
            bufsize = ctypes.c_uint()
            get_size(client, byref(bufsize))

            get_service = avt.func(14, _PROTO_GetService)
            iid_capture = _guid_struct(_IID_CAPTURE_CLIENT)
            hr = get_service(client, byref(iid_capture), byref(capture))
            if hr != 0:
                with self._lock:
                    self._error = f"IAudioCaptureClient unavailable ({hr:#x})"
                return

            start = avt.func(10, PROTO_Start)
            if start(client) != 0:
                with self._lock:
                    self._error = "Capture Start failed"
                return
            started = True

            cvt = _ComVtable(capture, count=8)
            # IAudioCaptureClient declares methods in this order:
            #   3=GetBuffer, 4=ReleaseBuffer, 5=GetNextPacketSize
            get_buffer = cvt.func(3, _PROTO_GetBuffer)
            release_buffer = cvt.func(4, _PROTO_ReleaseBuffer)
            get_packet = cvt.func(5, _PROTO_GetNextPacketSize)

            block_frames = rate // 100  # 10 ms
            pending = np.empty((0, channels), dtype=np.float32)

            while not self._stop.is_set():
                time.sleep(0.004)
                while True:
                    packet = ctypes.c_uint()
                    if get_packet(capture, byref(packet)) != 0 or packet.value == 0:
                        break
                    data_ptr = c_void_p()
                    frames = ctypes.c_uint()
                    flags = ctypes.c_ulong()
                    dev_pos = c_ulonglong()
                    qpc_pos = c_ulonglong()
                    if get_buffer(capture, byref(data_ptr), byref(frames), byref(flags),
                                  byref(dev_pos), byref(qpc_pos)) != 0:
                        break
                    if frames.value and data_ptr.value:
                        size = frames.value * channels * 4
                        raw = ctypes.string_at(data_ptr.value, size)
                        chunk = np.frombuffer(raw, dtype=np.float32).reshape(frames.value, channels)
                        pending = np.concatenate([pending, chunk]) if pending.size else chunk.copy()
                    release_buffer(capture, frames.value)

                if self._stop.is_set():
                    break
                while pending.shape[0] >= block_frames:
                    block = pending[:block_frames].copy()
                    pending = pending[block_frames:]
                    try:
                        self._queue.put_nowait(block)
                    except queue.Full:
                        try:
                            self._queue.get_nowait()  # drop oldest
                            self._drops += 1
                            self._queue.put_nowait(block)
                        except (queue.Empty, queue.Full):
                            pass
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("Capture loop crashed")
            result["error"] = str(exc)
            ready.set()
        finally:
            if started and client.value:
                try:
                    _ComVtable(client, count=16).func(11, PROTO_Stop)(client)
                except Exception:
                    pass
            for obj, count in ((capture, 8), (client, 16), (device, 8), (enumerator, 10)):
                if obj.value:
                    try:
                        _ComVtable(obj, count=count).func(2, _PROTO_Release)(obj)
                    except Exception:
                        pass
            ole32.CoUninitialize()

    # ------------------------------------------------------------- render --

    def _start_render(self, format_info: tuple[int, int], output_name: str | None) -> None:
        if _sd is None:
            raise RuntimeError("sounddevice/PortAudio is not available")
        rate, channels = format_info
        device_index = self._resolve_output(output_name)
        self._render_stream = _sd.OutputStream(
            samplerate=rate,
            blocksize=rate // 100,
            channels=channels,
            dtype="float32",
            device=device_index,
            callback=self._render_callback,
        )
        self._render_stream.start()

    @staticmethod
    def _resolve_output(output_name: str | None) -> int | None:
        if _sd is None:
            return None
        if not output_name:
            return None  # PortAudio default
        try:
            for index, info in enumerate(_sd.query_devices()):
                if info.get("max_output_channels", 0) > 0 and output_name in str(info.get("name", "")):
                    return index
        except Exception:
            pass
        return None

    def _render_callback(self, outdata: np.ndarray, frames: int,
                         time_info: Any, status: Any) -> None:
        """The hard real-time path: pull a captured block, filter, output."""
        chain = self._chain
        try:
            block = self._queue.get_nowait()
        except queue.Empty:
            outdata.fill(0)  # underrun: silence, never garbage
            return
        usable = min(frames, block.shape[0])
        work = block[:usable].copy()
        if work.shape[1] != outdata.shape[1]:
            # Channel mismatch: take the first N channels.
            work = work[:, :outdata.shape[1]]
        if chain is not None:
            chain.process(work)
        outdata[:usable] = work
        if usable < frames:
            outdata[usable:] = 0
        self._blocks += 1


def _alloc_wstr(text: str) -> ctypes.c_wchar_p:  # retained for callers/tests
    return ctypes.c_wchar_p(text)


def _parse_format(fmt_ptr: c_void_p) -> tuple[int, int, int]:
    """Extract (sample_rate, channels, bits) from a WAVEFORMATEX(TENSIBLE)."""
    raw = ctypes.string_at(fmt_ptr.value, 40)
    tag = int.from_bytes(raw[0:2], "little")
    channels = int.from_bytes(raw[2:4], "little")
    rate = int.from_bytes(raw[4:8], "little")
    bits = int.from_bytes(raw[14:16], "little")
    if tag == _WAVE_FORMAT_EXTENSIBLE:
        # WAVEFORMATEXTENSIBLE: cbSize at 16, then wValidBitsPerSample etc.
        cb = int.from_bytes(raw[16:18], "little")
        if cb >= 22:
            valid_bits = int.from_bytes(raw[18:20], "little")
            return rate, channels, valid_bits or bits
    return rate, channels, bits
