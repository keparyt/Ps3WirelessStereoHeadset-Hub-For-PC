"""Audio subsystem: DSP math, profiles, config v2 and the FxSound backend.

Live WASAPI capture/render is verified by a separate hardware probe; these
tests cover everything deterministic.
"""

import json
import time
import tkinter as tk
from contextlib import contextmanager
from dataclasses import dataclass, field

import numpy as np
import pytest

from ps3hub.audio import dsp
from ps3hub.audio.loopback_dsp import _FilterChain
from ps3hub.audio.profiles import AudioProfile, ProfileStore


# ------------------------------------------------------------- mapping ---

def test_db_to_gain_is_exact_at_reference_points():
    assert dsp.db_to_gain(0.0) == 1.0
    assert abs(dsp.db_to_gain(6.0) - 10 ** (6.0 / 20.0)) < 1e-9
    assert abs(dsp.db_to_gain(-20.0) - 0.1) < 1e-9


# --------------------------------------------------------------- biquad ---

def test_identity_biquad_is_transparent():
    rate = 48000
    coeffs = dsp.BiquadCoeffs(1.0, 0.0, 0.0, 0.0, 0.0)
    biquad = dsp.Biquad(coeffs)
    t = np.arange(rate) / rate
    tone = 0.25 * np.sin(2 * np.pi * 1000.0 * t)
    out = biquad.process(tone.astype(np.float32))
    np.testing.assert_allclose(out, tone.astype(np.float32), atol=1e-6)


def test_low_shelf_boosts_bass_band_not_midband():
    rate = 48000

    def band_level(freq, coeffs):
        biquad = dsp.Biquad(coeffs)
        t = np.arange(rate) / rate
        tone = 0.25 * np.sin(2 * np.pi * freq * t)
        out = biquad.process(tone.astype(np.float32))
        seg = out[rate // 4:]
        spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
        freqs = np.fft.rfftfreq(len(seg), 1 / rate)
        mask = (freqs >= freq * 0.95) & (freqs <= freq * 1.05)
        return float(np.sqrt(np.mean(spec[mask] ** 2)))

    coeffs = dsp.low_shelf_coeffs(rate, 120.0, 6.0)
    bass = band_level(60.0, coeffs)
    bass_in = band_level(60.0, dsp.BiquadCoeffs(1.0, 0.0, 0.0, 0.0, 0.0))
    mid = band_level(1000.0, coeffs)
    mid_in = band_level(1000.0, dsp.BiquadCoeffs(1.0, 0.0, 0.0, 0.0, 0.0))
    gain_bass = 20 * np.log10(bass / bass_in)
    gain_mid = 20 * np.log10(mid / mid_in)
    assert gain_bass > 2.0, "a 6 dB shelf must lift the bass band"
    assert abs(gain_mid) < 1.0, "a 120 Hz shelf must leave 1 kHz alone"


# ------------------------------------------------------------ chain ---

def test_flat_profile_chain_is_transparent():
    rate = 48000
    chain = _FilterChain(rate)
    chain.update(AudioProfile(bass=0.0, clarity=0.0, ambience=0.0,
                              surround=0.0, dynamic_boost=0.0,
                              master_gain_db=0.0))
    t = np.arange(rate) / rate
    tone = np.column_stack([0.25 * np.sin(2 * np.pi * 1000.0 * t)] * 2)
    block = tone.astype(np.float32).copy()
    chain.process(block)
    np.testing.assert_allclose(block, tone.astype(np.float32), atol=1e-6)


def test_boost_profile_chain_changes_level_exactly():
    rate = 48000
    chain = _FilterChain(rate)
    chain.update(AudioProfile(bass=10.0, clarity=0.0, ambience=0.0,
                              surround=0.0, dynamic_boost=0.0,
                              master_gain_db=6.0))
    t = np.arange(rate) / rate
    tone = np.column_stack([0.25 * np.sin(2 * np.pi * 1000.0 * t)] * 2)
    block = tone.astype(np.float32).copy()
    chain.process(block)

    def band_level(arr, freq):
        mono = arr[:, 0]
        seg = mono[rate // 4:]
        spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
        freqs = np.fft.rfftfreq(len(seg), 1 / rate)
        mask = (freqs >= freq * 0.95) & (freqs <= freq * 1.05)
        return float(np.sqrt(np.mean(spec[mask] ** 2)))

    source = band_level(tone.astype(np.float32), 1000.0)
    result = band_level(block, 1000.0)
    delta = 20 * np.log10(result / source)
    assert abs(delta - 6.0) < 0.2, f"1 kHz must show pure +6 dB gain, saw {delta:+.2f}"


def test_chain_output_never_clips_past_full_scale():
    rate = 48000
    chain = _FilterChain(rate)
    chain.update(AudioProfile(bass=0.0, clarity=0.0, ambience=0.0, surround=0.0,
                              dynamic_boost=0.0, master_gain_db=12.0))
    block = np.full((rate // 100, 2), 0.9, dtype=np.float32)
    chain.process(block)
    assert float(np.abs(block).max()) <= 1.0


def test_chain_updates_coeffs_without_reallocating_filters():
    rate = 48000
    chain = _FilterChain(rate)
    chain.update(AudioProfile(bass=5.0, clarity=0.0, ambience=0.0, surround=0.0,
                              dynamic_boost=0.0, master_gain_db=0.0))
    heavy = chain.update(AudioProfile(bass=5.0, clarity=5.0, ambience=5.0,
                                      surround=5.0, dynamic_boost=5.0,
                                      master_gain_db=0.0))
    assert len(chain.filters) == 5
    # Back to light: extra filters stay in place but are bypassed via identity.
    chain.update(AudioProfile(bass=0.0, clarity=0.0, ambience=0.0, surround=0.0,
                              dynamic_boost=0.0, master_gain_db=0.0))
    assert len(chain.filters) == 5
    t = np.arange(rate) / rate
    tone = np.column_stack([0.25 * np.sin(2 * np.pi * 1000.0 * t)] * 2)
    block = tone.astype(np.float32).copy()
    chain.process(block)
    np.testing.assert_allclose(block, tone.astype(np.float32), atol=1e-6)


def test_spectrum_peak_db_measures_off_vs_on():
    rate = 48000
    t = np.arange(rate) / rate
    tone = np.column_stack(
        [0.25 * np.sin(2 * np.pi * 440.0 * t)] * 2
    ).astype(np.float32)
    boosted = (tone * dsp.db_to_gain(6.0)).astype(np.float32)
    delta = dsp.spectrum_peak_db(tone, boosted, rate, 440.0)
    assert abs(delta - 6.0) < 0.2, f"expected +6 dB, saw {delta:+.2f}"


# ----------------------------------------------------------- profiles ---

def test_profile_clamps_out_of_range_values():
    profile = AudioProfile(bass=99.0, clarity=-3.0, master_gain_db=99.0,
                           eq=[(100.0, 50.0), (10.0, 1.0)]).clamped()
    assert profile.bass == 10.0
    assert profile.clarity == 0.0
    assert profile.master_gain_db == 20.0
    # EQ entries outside frequency/gain bounds are dropped or clamped.
    assert all(-12.0 <= gain <= 12.0 for _f, gain in profile.eq)
    assert all(20.0 <= freq <= 20000.0 for freq, _g in profile.eq)


def test_profile_round_trips_through_json():
    profile = AudioProfile(device_id="dev1", device_name="Speakers", bass=7.5,
                           eq=[(60.0, 3.0)])
    restored = AudioProfile.from_dict(json.loads(json.dumps(profile.to_dict())))
    assert restored == profile.clamped()


def test_profile_from_dict_ignores_unknown_keys_and_rubbish():
    profile = AudioProfile.from_dict({"bass": 4.0, "from_the_future": 1})
    assert profile.bass == 4.0
    fallback = AudioProfile.from_dict("not a dict")
    assert fallback == AudioProfile()


def test_profile_store_keys_by_device_id():
    store = ProfileStore()
    store.set(AudioProfile(device_id="a", bass=1.0))
    store.set(AudioProfile(device_id="b", bass=2.0))
    assert store.get("a").bass == 1.0
    assert store.get("b").bass == 2.0
    assert store.remove("a") is True
    assert store.remove("a") is False
    assert len(store) == 1


def test_profile_store_round_trips_through_dict():
    store = ProfileStore()
    store.set(AudioProfile(device_id="a", bass=3.0, device_name="X"))
    restored = ProfileStore.from_dict(store.to_dict())
    assert restored.get("a").bass == 3.0
    assert restored.get("a").device_name == "X"


def test_profile_store_ignores_entries_without_device_id():
    store = ProfileStore.from_dict({"profiles": [{"bass": 5.0}]})
    assert len(store) == 0


# ------------------------------------------------------- config v2 ---

def test_config_v2_round_trips_audio_profiles(tmp_path):
    from ps3hub.config import AppConfig, ConfigStore

    config = AppConfig()
    config.audio = {"enabled": True, "backend": "native",
                    "profiles": [{"device_id": "dev1", "bass": 7.5}]}
    store = ConfigStore(tmp_path / "config.json")
    assert store.save(config) is True
    loaded = ConfigStore(tmp_path / "config.json").load()
    assert loaded.audio["enabled"] is True
    assert loaded.audio["profiles"][0]["bass"] == 7.5
    assert loaded.audio["profiles"][0]["device_id"] == "dev1"


def test_config_v1_migrates_without_losing_data():
    from ps3hub.config import CONFIG_VERSION, AppConfig

    v1 = {
        "version": 1,
        "settings": {"mappings_enabled": False, "low_battery_threshold": 33},
        "profile": {"mappings": [{"input": "vss_button", "action": "media.next"}]},
    }
    migrated = AppConfig.from_dict(v1)
    assert migrated.audio["backend"] == "native"
    assert migrated.audio["enabled"] is False
    assert migrated.settings.mappings_enabled is False
    assert migrated.settings.low_battery_threshold == 33
    assert len(migrated.profile) == 1
    assert migrated.to_dict()["version"] == CONFIG_VERSION


def test_config_future_version_still_loads_key_by_key():
    from ps3hub.config import AppConfig

    future = {"version": 99, "settings": {"mappings_enabled": False},
              "audio": {"enabled": True, "backend": "weird", "profiles": []}}
    config = AppConfig.from_dict(future)
    assert config.settings.mappings_enabled is False
    # An unknown backend degrades to native instead of breaking.
    assert config.audio["backend"] == "native"
    assert config.audio["enabled"] is True


def test_audio_sanitisation_rejects_wrong_types():
    from ps3hub.config import _sanitise_audio

    clean = _sanitise_audio({"enabled": "yes", "backend": 42, "profiles": "nope"})
    assert clean["enabled"] is True
    assert clean["backend"] == "native"
    assert clean["profiles"] == []
    assert _sanitise_audio(None)["backend"] == "native"


# -------------------------------------------------------- fxsound ---

def test_fxsound_status_parsing():
    from ps3hub.audio.fxsound_backend import FxSoundBackend

    status = FxSoundBackend._parse_status({
        "power": True,
        "selected_output": "Speakers (2- Wireless Stereo Headset)",
        "selected_preset": "Extreme Bass",
        "equalizer": {"master_gain": 3.5},
        "effects": {"bassboost": 8.0, "fidelity": 4.0, "surround": 6.0},
    }, found=True)
    assert status.running is True
    assert status.power is True
    assert status.selected_preset == "Extreme Bass"
    assert status.master_gain == 3.5
    assert status.effects["bass"] == 8.0
    assert status.effects["clarity"] == 4.0
    assert status.effects["surround"] == 6.0


def test_fxsound_set_effects_builds_one_documented_invocation(monkeypatch):
    from ps3hub.audio import fxsound_backend as fb

    calls = []

    def fake_send(self, *arguments):
        calls.append(list(arguments))
        return True

    monkeypatch.setattr(fb.FxSoundBackend, "_send", fake_send)
    backend = fb.FxSoundBackend()
    assert backend.set_effects({"bass": 7.5, "clarity": 2.0}) is True
    assert len(calls) == 1
    assert calls[0][0].startswith("--set_effect=")
    assert "bass:7.5" in calls[0][0]
    assert "fidelity:2.0" in calls[0][0]


def test_fxsound_set_power_uses_documented_flag(monkeypatch):
    from ps3hub.audio import fxsound_backend as fb

    calls = []
    monkeypatch.setattr(fb.FxSoundBackend, "_send",
                        lambda self, *a: calls.append(list(a)) or True)
    backend = fb.FxSoundBackend()
    backend.set_power(True)
    backend.set_power(False)
    assert calls == [["--power=1"], ["--power=0"]]


def test_fxsound_launch_spawns_the_exe_without_arguments(monkeypatch):
    from ps3hub.audio import fxsound_backend as fb

    spawned = []
    monkeypatch.setattr(fb.subprocess, "Popen",
                        lambda argv, **kwargs: spawned.append(argv) or object())
    backend = fb.FxSoundBackend()
    monkeypatch.setattr(fb.FxSoundBackend, "exe_path",
                        property(lambda self: fb.Path("C:/x/fxsound.exe")),
                        raising=False)
    assert backend.launch() is True
    assert spawned == [[str(fb.Path("C:/x/fxsound.exe"))]]


def test_fxsound_launch_without_an_install_is_a_clean_no(monkeypatch):
    from ps3hub.audio import fxsound_backend as fb

    monkeypatch.setattr(fb.FxSoundBackend, "exe_path",
                        property(lambda self: None), raising=False)
    assert fb.FxSoundBackend().launch() is False


def test_fxsound_wait_until_running_polls_until_the_status_file_appears(monkeypatch):
    from ps3hub.audio import fxsound_backend as fb

    backend = fb.FxSoundBackend()
    states = iter([False, False, True])

    def fake_read_status(self, force=False):
        return fb.FxSoundStatus(found=True, running=next(states))

    monkeypatch.setattr(fb.FxSoundBackend, "read_status", fake_read_status)
    monkeypatch.setattr(fb.time, "sleep", lambda _s: None)
    status = backend.wait_until_running(timeout=5.0)
    assert status.running is True


def test_fxsound_wait_gives_up_after_the_timeout(monkeypatch):
    from ps3hub.audio import fxsound_backend as fb

    backend = fb.FxSoundBackend()
    monkeypatch.setattr(fb.FxSoundBackend, "read_status",
                        lambda self, force=False: fb.FxSoundStatus(found=True))
    monkeypatch.setattr(fb.time, "sleep", lambda _s: None)
    monkeypatch.setattr(fb.time, "monotonic",
                        lambda: [0.0, 1.0, 99.0][min(int(fb.time.monotonic_calls), 2)]
                        if hasattr(fb.time, "monotonic_calls") else 99.0)
    status = backend.wait_until_running(timeout=0.0)
    assert status.running is False


def test_download_url_points_at_the_official_site():
    from ps3hub.audio.fxsound_backend import DOWNLOAD_URL
    assert DOWNLOAD_URL.startswith("https://www.fxsound.com")


# ------------------------------------------------ live settings adoption ---


def _status_with(effects=None, equalizer=None, preset="Game"):
    from ps3hub.audio.fxsound_backend import FxSoundStatus

    return FxSoundStatus(
        found=True, running=True, power=True,
        selected_preset=preset,
        effects=dict(effects or {}),
        equalizer=dict(equalizer or {}),
    )


def test_status_values_are_adopted_into_the_active_profile():
    from ps3hub.audio.engine import AudioEngine, _profile_from_status
    from ps3hub.audio.profiles import AudioProfile

    base = AudioProfile(bass=5.0, clarity=5.0, surround=4.0, dynamic_boost=2.0)
    status = _status_with(
        effects={"bass": 8.0, "clarity": 1.0},
        equalizer={"num_bands": 2, "master_gain": -2.5,
                   "bands": [{"index": 0, "frequency": 100.0, "gain": 3.0},
                             {"index": 1, "frequency": 1000.0, "gain": -4.0}]},
        preset="Extreme Bass",
    )
    merged = _profile_from_status(base, status)
    assert merged.bass == 8.0
    assert merged.clarity == 1.0
    assert merged.surround == 4.0, "levels FxSound omits must be kept"
    assert merged.master_gain_db == -2.5
    assert merged.eq == [(100.0, 3.0), (1000.0, -4.0)]
    # FxSound only supports 5/10/15/20/31 bands, so a 2-band status snaps up.
    assert merged.eq_bands == 5
    assert merged.preset_name == "Extreme Bass"


def test_an_empty_status_leaves_the_profile_alone():
    from ps3hub.audio.engine import _profile_from_status
    from ps3hub.audio.profiles import AudioProfile

    base = AudioProfile(bass=5.0, eq=[(100.0, 2.0)], preset_name="Mine")
    merged = _profile_from_status(base, _status_with(preset=""))
    assert merged == base.clamped()


def test_a_running_backend_receives_profile_updates():
    from ps3hub.audio.engine import AudioEngine

    engine = AudioEngine.__new__(AudioEngine)
    calls: list[tuple] = []

    class _Fx:
        def apply_equalizer(self, profile):
            calls.append(("eq", profile))

        def set_effects(self, effects):
            calls.append(("fx", effects))

        def set_preset(self, name):
            calls.append(("preset", name))

        def set_output(self, name):
            calls.append(("output", name))

    class _Endpoint:
        device_id = "{d}"
        name = "Speakers"

    engine._fxsound = _Fx()
    engine._enabled = True
    engine._backend_kind = "fxsound"
    engine.default_endpoint = lambda: _Endpoint()
    engine.set_profile = lambda profile: calls.append(("stored", profile))

    from ps3hub.audio.profiles import AudioProfile
    engine.update_profile(AudioProfile(bass=9.0, preset_name="X"))
    kinds = [kind for kind, _payload in calls]
    assert kinds == ["stored", "eq", "fx", "preset", "output"]


def test_updates_do_not_touch_fxsound_when_it_is_not_the_backend():
    from ps3hub.audio.engine import AudioEngine

    engine = AudioEngine.__new__(AudioEngine)

    class _Fx:
        def __getattr__(self, name):
            raise AssertionError("FxSound must not be driven by the native backend")

    engine._fxsound = _Fx()
    engine._enabled = True
    engine._backend_kind = "native"
    engine.set_profile = lambda profile: None
    engine.update_profile(AudioProfile())  # must not raise


# ---------------------------------------------- status-file mirroring ---


def test_status_stamp_tracks_the_status_file(tmp_path, monkeypatch):
    from ps3hub.audio import fxsound_backend as fb

    monkeypatch.setenv("APPDATA", str(tmp_path))
    backend = fb.FxSoundBackend()
    assert backend.status_file_stamp() is None

    status_path = tmp_path / "FxSound" / "status.json"
    status_path.parent.mkdir()
    status_path.write_text("{}", encoding="utf-8")
    first = backend.status_file_stamp()
    assert first is not None

    import os
    later = first[0] + 1_000_000  # one millisecond later, in ns
    os.utime(status_path, ns=(later, later))
    second = backend.status_file_stamp()
    assert second != first
    assert second[1] == first[1]


@dataclass
class _MirrorStatus:
    found: bool = True
    running: bool = True
    error: str = ""
    selected_preset: str = ""
    selected_output: str = ""
    effects: dict = field(default_factory=dict)
    equalizer: dict = field(default_factory=dict)


@dataclass
class _MirrorProcessingState:
    active: bool = False
    device_name: str = ""
    sample_rate: float | None = None
    channels: int = 0
    blocks_processed: int = 0
    drops: int = 0
    error: str = ""


class _MirrorEngine:
    """Serves pre-loaded statuses and stamps in poll order.

    ``force=True`` reads (the view's startup probe) always get a harmless
    not-found status, so the poll scenario is not polluted by construction
    traffic; the queued ``statuses`` are served to the poll itself.
    """

    def __init__(self, statuses, stamps):
        self._statuses = list(statuses)
        self._stamps = list(stamps)
        self.initial_status = _MirrorStatus(found=False, running=False)
        self.reads = 0
        self.stamp_reads = 0
        self.adopted = []
        # What the view's refresh() touches at construction time.
        self.enabled = False
        self.auto_output = False
        self.processing_state = _MirrorProcessingState()

    def fxsound_status_stamp(self):
        stamp = self._stamps[min(self.stamp_reads, len(self._stamps) - 1)]
        self.stamp_reads += 1
        return stamp

    def fxsound_status(self, force=False):
        if force:
            return self.initial_status
        status = self._statuses[min(self.reads, len(self._statuses) - 1)]
        self.reads += 1
        return status

    def default_endpoint(self):
        return None

    def endpoints(self):
        return []

    def fxsound_download_url(self):
        return "https://www.fxsound.com/download"

    def adopt_fxsound_status(self, status):
        self.adopted.append(status)


@contextmanager
def _mirror_view(engine):
    """An AudioView on a **shared** Tk root.

    Creating and destroying many Tk roots in one process breaks this
    platform's Tcl library intermittently, so every Tk-backed test in this
    module reuses one withdrawn root; only the view widget is torn down.
    """
    from ps3hub.ui.view_audio import AudioView

    root = _shared_root()
    view = AudioView(root, engine_provider=lambda: engine, on_changed=lambda: None)
    try:
        yield view
    finally:
        try:
            view.destroy()
        except tk.TclError:
            pass


_MIRROR_ROOT = None


def _shared_root():
    """One Tk root for the whole test session (created lazily)."""
    global _MIRROR_ROOT
    if _MIRROR_ROOT is None:
        _MIRROR_ROOT = tk.Tk()
        _MIRROR_ROOT.withdraw()
        import atexit

        def _close():
            global _MIRROR_ROOT
            try:
                if _MIRROR_ROOT is not None:
                    _MIRROR_ROOT.destroy()
            except tk.TclError:
                pass
            _MIRROR_ROOT = None

        atexit.register(_close)
    return _MIRROR_ROOT


def test_view_poll_mirrors_an_external_status_change():
    from ps3hub.ui.view_audio import AudioView

    before = _MirrorStatus()
    after = _MirrorStatus(effects={"bass": 7.5},
                          equalizer={"num_bands": 10, "master_gain": 2.0})
    engine = _MirrorEngine([after], [(1, 10), (2, 12), (2, 12)])
    with _mirror_view(engine) as view:
        view._fx_ui_enabled = True
        view._mirror_stamp = (1, 10)
        view._fxsig = view._fx_signature(before)
        view._poll_status_file()
        assert engine.adopted == [after]
        assert view._effect_vars["bass"].get() == 7.5
        # Consumed: the next tick with the same stamp re-reads nothing.
        view._poll_status_file()
        assert engine.reads == 1


def test_view_poll_ignores_the_echo_of_its_own_push():
    same = _MirrorStatus()
    engine = _MirrorEngine([same, same], [(1, 10), (2, 12), (2, 12)])
    with _mirror_view(engine) as view:
        view._fx_ui_enabled = True
        view._mirror_stamp = (1, 10)
        view._fxsig = view._fx_signature(same)
        view._poll_status_file()
        assert engine.adopted == []
        assert engine.reads == 1


def test_view_poll_waits_while_a_band_is_being_dragged():
    engine = _MirrorEngine([_MirrorStatus()], [(1, 10), (2, 12)])
    with _mirror_view(engine) as view:
        view._fx_ui_enabled = True
        view._mirror_stamp = (1, 10)
        view._fxsig = view._fx_signature(_MirrorStatus())
        view._eq_graph._drag_index = 0
        view._poll_status_file()
        assert view._mirror_stamp == (1, 10), "stamp must stay pending"
        assert engine.reads == 0


def test_view_poll_retries_a_file_that_was_read_mid_write():
    broken = _MirrorStatus(running=False, error="Could not read FxSound status")
    engine = _MirrorEngine([broken], [(1, 10), (2, 12)])
    with _mirror_view(engine) as view:
        view._fx_ui_enabled = True
        view._mirror_stamp = (1, 10)
        view._fxsig = view._fx_signature(_MirrorStatus())
        view._poll_status_file()
        assert view._mirror_stamp == (1, 10), "stamp must not be consumed"
        assert engine.adopted == []


def test_view_poll_does_nothing_while_fxsound_is_down():
    engine = _MirrorEngine([_MirrorStatus()], [(1, 10), (2, 12)])
    with _mirror_view(engine) as view:
        view._fx_ui_enabled = False
        view._mirror_stamp = (1, 10)
        view._poll_status_file()
        assert engine.reads == 0


# ------------------------------------------------- tray icon rendering ---


def test_tray_icon_kind_thresholds():
    from ps3hub.tray import tray_icon_kind

    assert tray_icon_kind(False, 90) == "off"
    assert tray_icon_kind(True, None) == "off"
    assert tray_icon_kind(True, 90) == "ok"
    assert tray_icon_kind(True, 30) == "ok"
    assert tray_icon_kind(True, 29) == "low"
    assert tray_icon_kind(True, 15) == "low"
    assert tray_icon_kind(True, 14) == "critical"
    assert tray_icon_kind(True, 5, charging=True) == "charging"
    assert tray_icon_kind(False, 5, charging=True) == "off"


def test_headset_icon_shape_is_symmetric_and_nonempty():
    from ps3hub.tray import headset_icon_pixels

    rows = headset_icon_pixels("ok", 32)
    assert any(any(row) for row in rows), "the drawing must not be blank"
    for row in rows:
        assert row == row[::-1], "a front-facing headset is left/right symmetric"
    # The headband crosses the vertical centre line at the top (its outer
    # edge lands on row 5 for a 32px drawing).
    assert rows[5][16] == 0xFF
    assert rows[0][16] == 0, "nothing above the band"
    # Nothing is drawn in the bottom corners.
    assert rows[31][0] == 0 and rows[31][31] == 0


def test_every_icon_kind_renders_a_distinct_accent():
    from ps3hub.tray import _rgba_bytes_for_kind

    seen = set()
    for kind in ("ok", "low", "critical", "off", "charging"):
        data = _rgba_bytes_for_kind(kind, 32)
        assert len(data) == 32 * 32 * 4
        # Collect one lit pixel's colour: the alpha channel is every 4th byte.
        alpha = data[3::4]
        lit = alpha.index(255) * 4
        seen.add((data[lit + 2], data[lit], data[lit + 1]))  # BGR -> RGB
    assert len(seen) == 5, "each state must carry its own colour"


# ------------------------------------------------- forced windows toasts ---


def test_settings_round_trip_keeps_force_windows_toasts(tmp_path):
    from ps3hub.config import ConfigStore

    store = ConfigStore(tmp_path / "config.json")
    config = store.load()
    config.settings.force_windows_toasts = True
    assert store.save(config)
    reloaded = store.load()
    assert reloaded.settings.force_windows_toasts is True
    assert reloaded.settings.force_windows_toasts != \
        __import__("ps3hub.config", fromlist=["Settings"]).Settings().force_windows_toasts


def test_toast_center_forces_the_windows_route():
    from ps3hub.ui.toast import ToastCenter

    root = _shared_root()
    center = ToastCenter(root, "Hub")
    try:
        center.set_force_windows(True)
        root.deiconify()
        root.update()
        # Deliver synchronously (this is the exact method the pump's timer
        # invokes), so the assertion cannot flake on scheduler latency.
        center._deliver("Forced", "windows even while visible", "ok", None)
        assert center.windows_shown == 1
        assert center.in_app_shown == 0
        # And the default route: visible window, not forced -> in-app card.
        center.set_force_windows(False)
        center._deliver("Local", "on the visible window", "ok", None)
        assert center.in_app_shown == 1
        assert center.windows_shown == 1
    finally:
        center.shutdown()
        try:
            root.withdraw()
        except tk.TclError:
            pass


# ----------------------------------------------------- live equalizer push ---


def test_apply_equalizer_once_is_a_single_invocation(monkeypatch):
    from ps3hub.audio import fxsound_backend as fb

    calls = []
    monkeypatch.setattr(fb.FxSoundBackend, "_send",
                        lambda self, *a: calls.append(list(a)) or True)
    monkeypatch.setattr(fb.FxSoundBackend, "exe_path",
                        property(lambda self: fb.Path("C:/x/fxsound.exe")),
                        raising=False)
    backend = fb.FxSoundBackend()
    profile = AudioProfile(
        eq=[(100.0, 3.0), (1000.0, -4.0)], eq_bands=10,
        master_gain_db=2.0, filter_q=1.5, volume_leveling_db=1.0,
        balance_db=-2.0,
    )
    assert backend.apply_equalizer_once(profile) is True
    # A fresh backend has never seen the application's count, so the
    # num_bands flag goes out defensively; every flag shares one command line.
    assert len(calls) == 1
    line = " ".join(calls[0])
    assert line.startswith("--num_bands=10")
    assert "--set_band_freq=" in line and "0:100" in line and "1:1000" in line
    assert "--set_band_freq=" in line and "0:100" in line and "1:1000" in line
    assert "--set_band_gain=" in line and "0:3" in line and "1:-4" in line
    assert "--master_gain=+2" in line or "--master_gain=2.0" in line
    assert "--filter_q=1.5" in line
    assert "--volume_leveling=1.0" in line
    assert "--balance=-2.0" in line
    # Effect levels ride the same invocation so the five sliders are live
    # (this profile carries the dataclass defaults bass=5, surround=4).
    assert "--set_effect=" in line
    assert "bass:5.00" in line and "surround:4.00" in line
    assert "fidelity:0.00" in line and "dynamicboost:0.00" in line


def test_request_live_push_coalesces_into_one_send(monkeypatch):
    from ps3hub.audio import fxsound_backend as fb

    sends = []
    monkeypatch.setattr(fb.FxSoundBackend, "_send",
                        lambda self, *a: sends.append(list(a)) or True)
    monkeypatch.setattr(fb.FxSoundBackend, "exe_path",
                        property(lambda self: fb.Path("C:/x/fxsound.exe")),
                        raising=False)
    backend = fb.FxSoundBackend()
    backend.LIVE_PUSH_INTERVAL = 0.25  # wider than the whole request burst

    for gain in (1.0, 2.0, 3.0, 6.0):
        backend.request_live_push(AudioProfile(eq=[(1000.0, gain)]))
        time.sleep(0.01)
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and not sends:
        time.sleep(0.01)
    time.sleep(0.2)
    assert len(sends) == 1, f"expected one coalesced send, got {len(sends)}"
    line = " ".join(sends[0])
    assert "0:6.0" in line, "only the latest curve must be sent"
    gain_flags = line.split("--set_band_gain=")[1].split("--")[0]
    assert "1.0" not in gain_flags, "earlier drag positions must not ride along"


def test_focus_guard_restores_the_previous_foreground(monkeypatch):
    from ps3hub.audio import fxsound_backend as fb

    class FakeUser32:
        def __init__(self):
            self.foreground = 111
            self.calls = []

        def GetForegroundWindow(self):
            self.calls.append("get")
            return self.foreground

        def SetForegroundWindow(self, hwnd):
            value = getattr(hwnd, "value", hwnd)  # normalise c_void_p
            self.foreground = value
            self.calls.append(("set", value))

        def keybd_event(self, *args):
            self.calls.append("key")

    fake = FakeUser32()
    monkeypatch.setattr(fb, "_focus_user32", fake)
    monkeypatch.setattr(fb.subprocess, "run",
                        lambda *a, **k: fake.__setattr__("foreground", 222))
    backend = fb.FxSoundBackend()
    monkeypatch.setattr(fb.FxSoundBackend, "exe_path",
                        property(lambda self: fb.Path("C:/x/fxsound.exe")),
                        raising=False)
    backend._send("--status")
    assert int(fake.foreground) == 111, "the user's window must get focus back"
    assert any(c == ("set", 111) for c in fake.calls)


def test_stop_live_push_halts_the_worker():
    from ps3hub.audio import fxsound_backend as fb

    backend = fb.FxSoundBackend()
    backend.request_live_push(AudioProfile())
    backend.stop_live_push()
    assert backend._live_stop.is_set()


def test_band_count_change_rides_the_invocation_when_needed(monkeypatch):
    """The count flag is decided against the app's count, not the profile."""
    from ps3hub.audio import fxsound_backend as fb

    calls = []
    monkeypatch.setattr(fb.FxSoundBackend, "_send",
                        lambda self, *a: calls.append(list(a)) or True)
    monkeypatch.setattr(fb.FxSoundBackend, "exe_path",
                        property(lambda self: fb.Path("C:/x/fxsound.exe")),
                        raising=False)
    backend = fb.FxSoundBackend()
    # Simulate a backend that last saw the application at 15 bands.
    backend._last_status = fb.FxSoundStatus(
        found=True, running=True,
        equalizer={"num_bands": 15,
                   "bands": [{"frequency": f, "gain": 0.0} for f in
                             [25, 40, 63, 100, 160, 250, 400, 630, 1000, 1600,
                              2500, 4000, 6300, 10000, 16000]]})
    profile = AudioProfile(eq=[(f, 0.0) for f in
                               [25, 40, 63, 100, 160, 250, 400, 630, 1000, 1600]],
                          eq_bands=10)
    assert backend.apply_equalizer_once(profile) is True
    assert len(calls) == 1
    line = " ".join(calls[0])
    assert line.startswith("--num_bands=10"), "a real count change must send the flag"
    assert "--set_band_gain=" in line

    # When the application already matches, the flag is omitted entirely.
    calls.clear()
    backend._last_status = fb.FxSoundStatus(
        found=True, running=True,
        equalizer={"num_bands": 10,
                   "bands": [{"frequency": f, "gain": 0.0} for f in
                             [25, 40, 63, 100, 160, 250, 400, 630, 1000, 1600]]})
    assert backend.apply_equalizer_once(profile) is True
    assert len(calls) == 1
    assert "--num_bands" not in " ".join(calls[0])


def test_effect_values_clamped_onto_the_live_push():
    from unittest.mock import patch
    from ps3hub.audio import fxsound_backend as fb

    calls = []
    with patch.object(fb.FxSoundBackend, "_send",
                      lambda self, *a: calls.append(list(a)) or True), \
         patch.object(fb.FxSoundBackend, "exe_path",
                      property(lambda self: fb.Path("C:/x/fxsound.exe"))):
        backend = fb.FxSoundBackend()
        assert backend.apply_equalizer_once(
            AudioProfile(bass=14.0, clarity=-2.0)) is True
    line = " ".join(calls[0])
    assert "bass:10.00" in line, "over-range bass must clamp to 10"
    assert "fidelity:0.00" in line, "under-range clarity must clamp to 0"


def test_view_poll_ignores_push_echoes_and_flat_snapshots():
    from ps3hub.ui.view_audio import AudioView

    flat = _MirrorStatus(equalizer={
        "num_bands": 10, "master_gain": 0.0,
        "bands": [{"frequency": 100.0 * (i + 1), "gain": 0.0}
                  for i in range(10)]})
    external = _MirrorStatus(
        effects={"bass": 6.0},
        equalizer={"num_bands": 10, "master_gain": 2.0,
                   "bands": [{"frequency": 100.0 * (i + 1), "gain": 3.0}
                             for i in range(10)]},
        selected_preset="Game")
    engine = _MirrorEngine([flat, flat, external],
                           [(1, 10), (2, 12), (3, 14), (4, 16)])
    with _mirror_view(engine) as view:
        view._fx_ui_enabled = True
        view._mirror_stamp = (0, 9)
        view._fxsig = view._fx_signature(_MirrorStatus())
        # A push just happened: the echo window is open and this is the
        # fingerprint of what we pushed (a shaped curve).
        view._user_edit_until = time.monotonic() + 10
        view._pushed_fxsig = view._fx_signature(_MirrorStatus(
            equalizer={"num_bands": 10,
                       "bands": [{"frequency": 100.0 * (i + 1), "gain": 3.0}
                                 for i in range(10)]}))
        view._pushed_not_flat = True

        # 1. A rewrite inside the echo window is ignored outright.
        view._poll_status_file()
        assert engine.adopted == []
        # 2. A flat snapshot after the window is the app's transient state,
        #    not a user action: still ignored.
        view._user_edit_until = time.monotonic() - 1
        view._poll_status_file()
        assert engine.adopted == []
        # 3. A genuinely different external state is adopted normally.
        view._poll_status_file()
        assert engine.adopted == [external]
