"""Tests for the equalizer: the .fac format, the profile fields, and the DSP.

The DSP assertions are the interesting ones. They do not check that the code
looks right, they measure the audio: a +6 dB band has to actually come out
6 dB louder at its own centre frequency, and must not leak into distant bands.
"""

from __future__ import annotations

import numpy as np
import pytest
from pathlib import Path

from ps3hub.audio import dsp
from ps3hub.audio.fac import (
    FacPreset, build_fac_text, default_band_frequencies, format_frequency,
    format_gain, parse_fac_text, read_fac, write_fac,
)
from ps3hub.audio.loopback_dsp import _FilterChain
from ps3hub.audio.profiles import AudioProfile

SAMPLE_RATE = 48000.0

#: A real FxSound-authored preset, in the exact shape the application writes.
EXTREME_BASS = """CLASS1 : Effect Type
9: Version
Extreme Bass
0: Double Params Flag
1: Total number of elements
38: Main 0
76: Main 1
0: Main 2
64: Main 3
38: Main 4
76: Main 5
0: Element Number
   0: Param 0
   0: Param 1
   0: Param 2
   0: Param 3
   0: Param 4
   0: Param 5
   0: Param 6
7: Number of Application Dependent Integers
0: Number of Application Dependent Reals
0: Number of Application Dependent Strings
1: Integer[0]
1: Integer[1]
1: Integer[2]
1: Integer[3]
1: Integer[4]
0: Integer[5]
2: Integer[6]
15: Number of EQ Bands
1: On/Off Flag
Band 1
   25: CF
   2: Boost/Cut
Band 2
   40: CF
   -1: Boost/Cut
Band 3
   63: CF
   4: Boost/Cut
"""


def _flat_profile(**overrides) -> AudioProfile:
    base = dict(bass=0.0, clarity=0.0, ambience=0.0, surround=0.0,
                dynamic_boost=0.0, master_gain_db=0.0)
    base.update(overrides)
    return AudioProfile(**base)


def _tone(freq: float, seconds: float = 1.0, amp: float = 0.2) -> np.ndarray:
    frames = int(SAMPLE_RATE * seconds)
    t = np.arange(frames) / SAMPLE_RATE
    mono = (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    return np.stack([mono, mono], axis=1)


def _measure(freq: float, profile: AudioProfile) -> float:
    """Level change in dB at ``freq`` produced by the profile."""
    clean = _tone(freq)
    chain = _FilterChain(SAMPLE_RATE)
    chain.update(profile)
    processed = clean.copy()
    chain.process(processed)
    return dsp.spectrum_peak_db(clean, processed, SAMPLE_RATE, freq)


# --------------------------------------------------------------- band table --


@pytest.mark.parametrize("count", [5, 10, 15, 20, 31])
def test_every_supported_band_count_has_that_many_frequencies(count):
    assert len(default_band_frequencies(count)) == count


@pytest.mark.parametrize("count", [5, 10, 15, 20, 31])
def test_band_frequencies_ascend_and_stay_in_audible_range(count):
    frequencies = default_band_frequencies(count)
    assert frequencies == sorted(frequencies)
    assert all(20.0 <= f <= 20000.0 for f in frequencies)


def test_fifteen_band_table_matches_fxsound():
    # Read back from the running application on 2026-09-29.
    assert default_band_frequencies(15) == [
        25.0, 40.0, 63.0, 100.0, 160.0, 250.0, 400.0, 630.0,
        1000.0, 1600.0, 2500.0, 4000.0, 6300.0, 10000.0, 16000.0,
    ]


def test_unknown_band_count_falls_back_to_a_supported_one():
    assert len(default_band_frequencies(7)) in (5, 10, 15, 20, 31)
    assert default_band_frequencies("nonsense") == default_band_frequencies(10)


# ----------------------------------------------------------------- formatting --


def test_whole_frequencies_render_without_a_decimal_point():
    assert format_frequency(25.0) == "25"
    assert format_frequency(16000.0) == "16000"


def test_fractional_frequencies_keep_their_precision():
    assert format_frequency(115.734) == "115.734"


def test_file_gains_are_written_without_a_sign():
    # The file format stores plain numbers; the + sign is a display concern.
    assert format_gain(0.0) == "0"
    assert format_gain(6.0) == "6"
    assert format_gain(-11.0) == "-11"


def test_fractional_gains_are_not_rounded_away():
    assert format_gain(4.72441) == "4.72441"


def test_ui_gain_labels_carry_an_explicit_sign():
    from ps3hub.ui.widget_eq import format_gain as label
    assert label(0.0) == "0"
    assert label(6.0) == "+6"
    assert label(-11.0) == "-11"


# ------------------------------------------------------------------- parsing --


def test_parses_name_effects_and_bands():
    preset = parse_fac_text(EXTREME_BASS)
    assert preset.error == ""
    assert preset.name == "Extreme Bass"
    assert preset.bands == [(25.0, 2.0), (40.0, -1.0), (63.0, 4.0)]
    assert preset.declared_bands == 15


def test_value_precedes_the_colon():
    # "25: CF" is 25 Hz, not the string "CF". Getting this backwards is the
    # single easiest mistake to make with this format.
    preset = parse_fac_text(EXTREME_BASS)
    assert preset.bands[0][0] == 25.0


def test_effect_levels_are_rescaled_from_the_file_scale():
    preset = parse_fac_text(EXTREME_BASS)
    # 38 / 12.7 = 2.99, 76 / 12.7 = 5.98
    assert preset.effects["clarity"] == pytest.approx(2.99, abs=0.01)
    assert preset.effects["bass"] == pytest.approx(5.98, abs=0.01)


def test_a_file_that_is_not_a_preset_is_reported_not_raised():
    preset = parse_fac_text("this is a shopping list\nmilk\nbread\n")
    assert preset.error
    assert preset.bands == []


def test_truncated_file_yields_what_could_be_read():
    preset = parse_fac_text(EXTREME_BASS[:EXTREME_BASS.index("Band 3")])
    assert preset.name == "Extreme Bass"
    assert len(preset.bands) == 2


def test_empty_input_does_not_raise():
    assert parse_fac_text("").error


# ---------------------------------------------------------------- round trip --


def test_writing_a_parsed_preset_reproduces_it_exactly():
    original = EXTREME_BASS.replace("\r\n", "\n").rstrip("\n")
    rebuilt = build_fac_text(parse_fac_text(EXTREME_BASS)).rstrip("\n")
    assert rebuilt == original


def test_round_trip_preserves_a_fractional_band_value():
    preset = FacPreset(name="Odd", bands=[(115.734, 4.72441)])
    again = parse_fac_text(build_fac_text(preset))
    assert again.bands == [(115.734, 4.72441)]


def test_round_trip_preserves_the_application_integers():
    # Integer[2] varies between real presets; carrying it verbatim is what
    # keeps a re-save byte-identical.
    text = EXTREME_BASS.replace("1: Integer[2]", "0: Integer[2]")
    assert build_fac_text(parse_fac_text(text)).rstrip("\n") == text.rstrip("\n")


def test_write_then_read_a_file(tmp_path):
    preset = FacPreset(name="Mine", bands=[(100.0, 3.0), (1000.0, -4.0)])
    target = tmp_path / "mine.fac"
    assert write_fac(target, preset)
    loaded = read_fac(target)
    assert loaded.name == "Mine"
    assert loaded.bands == [(100.0, 3.0), (1000.0, -4.0)]


def test_reading_a_missing_file_reports_an_error(tmp_path):
    loaded = read_fac(tmp_path / "nope.fac")
    assert loaded.error


def test_the_repository_example_preset_loads():
    example = Path(__file__).resolve().parents[2] / "Extreme Bass.fac"
    if not example.exists():
        pytest.skip("example preset is not present")
    preset = read_fac(example)
    assert preset.error == ""
    assert preset.name == "Extreme Bass"
    assert len(preset.bands) == 15
    assert preset.bands[0] == (25.0, 2.0)
    assert preset.bands[-1] == (16000.0, -11.0)


def test_the_example_preset_survives_a_load_and_save(tmp_path):
    example = Path(__file__).resolve().parents[2] / "Extreme Bass.fac"
    if not example.exists():
        pytest.skip("example preset is not present")
    profile, error = AudioProfile.from_fac(example)
    assert error == ""
    target = tmp_path / "again.fac"
    assert profile.write_fac(target)[0]
    assert (target.read_text(encoding="utf-8").replace("\r\n", "\n").rstrip("\n")
            == example.read_text(encoding="utf-8").replace("\r\n", "\n").rstrip("\n"))


# ------------------------------------------------------------------- profile --


def test_profile_clamps_out_of_range_equalizer_values():
    clamped = AudioProfile(eq=[(5.0, 99.0), (99999.0, -99.0)]).clamped()
    assert clamped.eq == [(20.0, 12.0), (20000.0, -12.0)]


def test_profile_sorts_bands_into_ascending_frequency():
    clamped = AudioProfile(eq=[(1000.0, 1.0), (100.0, 2.0)]).clamped()
    assert [f for f, _ in clamped.eq] == [100.0, 1000.0]


def test_profile_rejects_malformed_band_entries():
    clamped = AudioProfile(eq=[(100.0, 1.0), "nonsense", (None, 2.0)]).clamped()
    assert clamped.eq == [(100.0, 1.0)]


@pytest.mark.parametrize("value,expected", [
    ("nonsense", 10), (None, 10), (7, 5), (12, 10), (31, 31),
])
def test_band_count_snaps_to_a_supported_value(value, expected):
    assert AudioProfile(eq_bands=value).clamped().eq_bands == expected


def test_band_count_never_drops_stored_bands():
    # 15 bands stored but eq_bands says 10: the bands must win, or five of
    # them would vanish without warning.
    profile = AudioProfile(eq_bands=10, eq=[(f, 0.0) for f in
                                            default_band_frequencies(15)])
    assert profile.clamped().eq_bands == 15


@pytest.mark.parametrize("field,value,expected", [
    ("filter_q", 99.0, 3.0), ("filter_q", 0.0, 1.0), ("filter_q", "x", 1.0),
    ("volume_leveling_db", -5.0, 0.0), ("volume_leveling_db", 99.0, 4.0),
    ("balance_db", -99.0, -20.0), ("balance_db", 99.0, 20.0),
])
def test_equalizer_controls_are_clamped(field, value, expected):
    assert getattr(AudioProfile(**{field: value}).clamped(), field) == expected


def test_profile_survives_a_dict_round_trip():
    original = AudioProfile(eq=[(100.0, 1.0)], eq_bands=5, filter_q=2.0,
                            volume_leveling_db=1.5, balance_db=-3.0,
                            preset_name="Mine")
    assert AudioProfile.from_dict(original.to_dict()) == original.clamped()


def test_equalizer_changes_are_reported_as_a_difference():
    base = _flat_profile()
    tweaked = _flat_profile(eq=[(1000.0, 3.0)])
    assert not base.is_equivalent_effect(tweaked)
    assert _flat_profile(eq=[(1000.0, 3.0)]).is_equivalent_effect(tweaked)


def test_band_count_alone_counts_as_a_difference():
    assert not _flat_profile().is_equivalent_effect(_flat_profile(eq_bands=31))


# ----------------------------------------------------------------------- DSP --


def test_a_flat_profile_is_bit_transparent():
    assert _measure(1000.0, _flat_profile()) == pytest.approx(0.0, abs=0.01)


@pytest.mark.parametrize("freq", [100.0, 1000.0, 10000.0])
def test_a_boosted_band_really_is_that_much_louder(freq):
    profile = _flat_profile(eq=[(freq, 6.0)], filter_q=1.0)
    assert _measure(freq, profile) == pytest.approx(6.0, abs=0.15)


@pytest.mark.parametrize("freq", [100.0, 1000.0])
def test_a_cut_band_really_is_that_much_quieter(freq):
    profile = _flat_profile(eq=[(freq, -6.0)], filter_q=1.0)
    assert _measure(freq, profile) == pytest.approx(-6.0, abs=0.15)


def test_a_band_does_not_leak_into_distant_bands():
    profile = _flat_profile(eq=[(1000.0, 6.0)], filter_q=1.0)
    assert abs(_measure(100.0, profile)) < 0.5


def test_a_band_at_zero_costs_nothing():
    with_flat = _flat_profile(eq=[(1000.0, 0.0)])
    assert _measure(1000.0, with_flat) == pytest.approx(0.0, abs=0.01)


def test_narrower_q_leaks_less():
    wide = _flat_profile(eq=[(1000.0, 6.0)], filter_q=0.5)
    narrow = _flat_profile(eq=[(1000.0, 6.0)], filter_q=3.0)
    assert abs(_measure(100.0, narrow)) < abs(_measure(100.0, wide))


def test_master_gain_still_applies_alongside_the_equalizer():
    profile = _flat_profile(eq=[(1000.0, 0.0)], master_gain_db=6.0)
    assert _measure(1000.0, profile) == pytest.approx(6.0, abs=0.15)


def test_balance_attenuates_only_one_channel():
    signal = _tone(1000.0)
    chain = _FilterChain(SAMPLE_RATE)
    chain.update(_flat_profile(balance_db=-6.0))
    out = signal.copy()
    chain.process(out)
    window = out.shape[0] // 4
    left = float(np.abs(out[:window, 0]).max())
    right = float(np.abs(out[:window, 1]).max())
    assert left == pytest.approx(0.2, abs=0.01)
    assert right == pytest.approx(0.2 * 10 ** (-6 / 20), abs=0.01)


def test_the_whole_fifteen_band_curve_compiles_into_filters():
    gains = [2, -1, 4, -1, 2, 0, 1, -11, -8, -8, -10, -11, -11, -11, -11]
    profile = _flat_profile(
        eq=list(zip(default_band_frequencies(15), [float(g) for g in gains])),
        filter_q=1.5)
    chain = _FilterChain(SAMPLE_RATE)
    chain.update(profile)
    # 14 non-zero bands become 14 peaking filters.
    assert len(chain.filters) == 14
    # And the curve is audible where it should be.
    assert _measure(63.0, profile) > 3.0
    assert _measure(4000.0, profile) < -9.0


def test_shrinking_the_band_count_bypasses_the_extra_filters():
    chain = _FilterChain(SAMPLE_RATE)
    chain.update(_flat_profile(eq=[(f, 6.0) for f in
                                   default_band_frequencies(31)]))
    assert len(chain.filters) == 31
    chain.update(_flat_profile(eq=[(f, 0.0) for f in
                                   default_band_frequencies(5)]))
    # The old filters must be neutralised, not left running.
    signal = _tone(1000.0)
    out = signal.copy()
    chain.process(out)
    assert dsp.spectrum_peak_db(signal, out, SAMPLE_RATE, 1000.0) == \
        pytest.approx(0.0, abs=0.01)
