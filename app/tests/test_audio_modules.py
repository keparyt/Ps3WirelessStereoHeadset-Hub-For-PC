"""Audio subsystem: DSP math, profiles, config v2 and the FxSound backend.

Live WASAPI capture/render is verified by a separate hardware probe; these
tests cover everything deterministic.
"""

import json

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
