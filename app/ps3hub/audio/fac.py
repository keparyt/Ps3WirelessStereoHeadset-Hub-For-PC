"""Reader and writer for FxSound ``.fac`` preset files.

FxSound stores every preset as a small line-oriented text file. The format is
not documented by the vendor, so it was established empirically by driving the
installed application and diffing the files it writes (verified against FxSound
1.2.13.0 on 2026-09-29). The shape is::

    CLASS1 : Effect Type
    9: Version
    <preset name>
    0: Double Params Flag
    1: Total number of elements
    <v>: Main 0        <- effect levels, one per slot (see _MAIN_SLOTS)
    ...
    0: Element Number
    0: Param 0 ... 0: Param 6
    7: Number of Application Dependent Integers
    0: Number of Application Dependent Reals
    0: Number of Application Dependent Strings
    1: Integer[0] ... 2: Integer[6]
    <n>: Number of EQ Bands
    <0|1>: On/Off Flag
    Band 1
       <hz>: CF
       <db>: Boost/Cut
    ...

Only the parts the Hub understands are interpreted: the preset name, the five
effect levels, and the equalizer bands. Everything else is carried through
verbatim on a round trip so that saving a preset FxSound wrote itself does not
quietly discard settings this module does not model yet.

This module is a *file format* helper only. Applying a preset to the audio path
is the job of :mod:`ps3hub.audio.fxsound_backend`, which uses the documented
command line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..applog import get_logger

log = get_logger("audio.fac")

#: Bumped when the interpretation below changes in a way that matters.
FAC_FORMAT_VERSION = 1

#: The value FxSound writes for ``CLASS1 : Effect Type`` in preset files.
_EFFECT_TYPE_PRESET = 9

#: ``Main <n>`` slots, mapped to the canonical effect names. Verified on
#: FxSound 1.2.13.0 by setting one effect at a time to a known value and reading
#: back the file the application wrote (see the note in the module docstring).
#: Main 2 is not one of the five documented effects and is written as 0.
_MAIN_SLOTS: dict[str, int] = {
    "clarity": 0,
    "surround": 1,
    "ambience": 3,
    "dynamic_boost": 4,
    "bass": 5,
}

#: Main 2 is written by FxSound but is not one of the five documented effects.
_UNUSED_MAIN_SLOT = 2

#: Effect levels are stored as a small integer in the file rather than on the
#: 0.0-10.0 UI scale. The divisor was derived from the values that actually
#: occur across the built-in and user presets: 0, 13, 20, 25, 38, 50, 60, 64,
#: 76 and 89, which are all multiples of 12.7. (FxSound's own status.json
#: reports effects as e.g. 5.984251976013184, which is 76/12.7.)
_EFFECT_SCALE = 12.7

#: The "Application Dependent Integers" FxSound writes when the Hub is creating
#: a preset from scratch. Files it wrote itself are carried through verbatim.
_DEFAULT_INTEGERS = (1, 1, 1, 1, 1, 0, 2)

#: Band counts the equalizer supports, as accepted by ``--num_bands``.
VALID_BAND_COUNTS = (5, 10, 15, 20, 31)

#: Boost/cut limits of a single band, in dB (the CLI clamps to the same range).
BAND_GAIN_MIN_DB = -12.0
BAND_GAIN_MAX_DB = 12.0


def default_band_frequencies(count: int) -> list[float]:
    """Centre frequencies FxSound uses for ``count`` bands.

    These are the values the application itself assigns: an octave-ish ladder
    that is *not* simply ``count`` slices of the audible range. They were read
    back from the running application for each supported band count.
    """
    count = _coerce_band_count(count)
    if count == 5:
        return [80.0, 250.0, 1000.0, 4000.0, 16000.0]
    if count == 10:
        return [31.25, 62.5, 125.0, 250.0, 500.0, 1000.0, 2000.0, 4000.0,
                8000.0, 16000.0]
    if count == 15:
        return [25.0, 40.0, 63.0, 100.0, 160.0, 250.0, 400.0, 630.0, 1000.0,
                1600.0, 2500.0, 4000.0, 6300.0, 10000.0, 16000.0]
    if count == 20:
        return [20.0, 31.25, 40.0, 50.0, 63.0, 80.0, 100.0, 125.0, 160.0,
                200.0, 250.0, 315.0, 400.0, 500.0, 630.0, 800.0, 1000.0,
                1250.0, 16000.0, 20000.0]
    return [20.0, 25.0, 31.25, 40.0, 50.0, 63.0, 80.0, 100.0, 125.0, 160.0,
            200.0, 250.0, 315.0, 400.0, 500.0, 630.0, 800.0, 1000.0, 1250.0,
            1600.0, 2000.0, 2500.0, 3150.0, 4000.0, 5000.0, 6300.0, 8000.0,
            10000.0, 12500.0, 16000.0, 20000.0]


def _coerce_band_count(count: Any) -> int:
    try:
        value = int(count)
    except (TypeError, ValueError):
        return 10
    if value in VALID_BAND_COUNTS:
        return value
    # Fall back to the closest supported count so a corrupt file still loads.
    return min(VALID_BAND_COUNTS, key=lambda option: abs(option - value))


def format_frequency(hz: float) -> str:
    """Render a centre frequency the way FxSound writes it.

    It uses the shortest exact decimal, so ``25.0`` is written ``25`` while
    ``115.734`` keeps its precision. Matching this keeps our diffs against
    FxSound-authored files small and makes the format obvious to a human
    editing a preset by hand.
    """
    if abs(hz - round(hz)) < 1e-9:
        return str(int(round(hz)))
    text = f"{hz:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def format_gain(db: float) -> str:
    """Render a boost/cut value the way FxSound does.

    Values are written with up to six significant figures and trailing zeros
    removed, so whole numbers stay whole (``4``) and fractions keep their
    precision (``4.72441``). Rounding to an integer would silently change the
    curve a preset describes.
    """
    value = round(float(db), 6)
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text or "0"


@dataclass
class FacPreset:
    """The subset of a ``.fac`` file the Hub understands.

    ``extras`` holds the raw text of every section this module does not model so
    that :func:`write_fac` can reproduce an unfamiliar file faithfully instead of
    quietly dropping parts of it.
    """

    name: str = ""
    #: Effect levels on the 0-10 scale, keyed by canonical effect name.
    effects: dict[str, float] = field(default_factory=dict)
    #: Equalizer bands as ``(frequency_hz, gain_db)`` pairs, in order.
    bands: list[tuple[float, float]] = field(default_factory=list)
    eq_enabled: bool = True
    #: The "Application Dependent Integers" carried through verbatim. FxSound
    #: varies these between presets and the Hub does not interpret them, so
    #: preserving them keeps a round trip lossless.
    integers: list[int] = field(default_factory=list)
    #: Number of bands the file declares, kept separate from ``bands`` because a
    #: file may declare more bands than it writes out.
    declared_bands: int = 0
    error: str = ""

    @property
    def band_count(self) -> int:
        return self.declared_bands or len(self.bands)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "effects": dict(self.effects),
            "bands": [[freq, gain] for freq, gain in self.bands],
            "eq_enabled": self.eq_enabled,
            "integers": list(self.integers),
            "declared_bands": self.declared_bands,
            "error": self.error,
        }


def _parse_value(line: str) -> str | None:
    """Return the value of a ``<value>: <label>`` line, or None if absent.

    The number comes *before* the colon in this format (``25: CF``), which is
    the opposite of the usual "label: value" convention and is the single most
    easy thing to get wrong when reading these files.
    """
    if ":" not in line:
        return None
    return line.split(":", 1)[0].strip()


def parse_fac_text(text: str) -> FacPreset:
    """Parse the contents of a ``.fac`` file.

    A malformed or truncated file yields a preset with ``error`` set and
    whatever could be understood, rather than an exception: a broken preset file
    must not be able to take the app down.
    """
    preset = FacPreset()
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    main_values: dict[int, float] = {}
    integers: list[int] = []
    index = 0
    seen_header = False

    while index < len(lines):
        line = lines[index]
        index += 1
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.startswith("CLASS1"):
            continue

        if stripped.endswith(": Version"):
            # The next non-empty line is the preset name.
            while index < len(lines) and not lines[index].strip():
                index += 1
            if index < len(lines):
                preset.name = lines[index].strip()
                index += 1
            seen_header = True
            continue

        main_match = re.match(r"^(-?\d+):\s*Main\s+(\d+)$", stripped)
        if main_match:
            try:
                main_values[int(main_match.group(2))] = float(main_match.group(1))
            except ValueError:
                pass
            continue

        if stripped.endswith(": Number of EQ Bands"):
            try:
                preset.declared_bands = int(_parse_value(stripped) or 0)
            except ValueError:
                preset.declared_bands = 0
            continue

        if stripped.endswith(": On/Off Flag"):
            value = (_parse_value(stripped) or "1").strip()
            preset.eq_enabled = value not in ("0", "")
            continue

        integer_match = re.match(r"^(-?\d+): Integer\[(\d+)\]$", stripped)
        if integer_match:
            # Keep them indexed by their declared position so a file that skips
            # or reorders them still round-trips.
            position = int(integer_match.group(2))
            while len(integers) <= position:
                integers.append(0)
            try:
                integers[position] = int(integer_match.group(1))
            except ValueError:
                pass
            continue

        if stripped.startswith("Band "):
            # "Band N" is followed by an indented "CF" and "Boost/Cut" pair.
            freq: float | None = None
            gain: float | None = None
            while index < len(lines):
                inner = lines[index].strip()
                if not inner:
                    index += 1
                    continue
                if inner.endswith(": CF"):
                    try:
                        freq = float(_parse_value(inner) or 0.0)
                    except ValueError:
                        freq = None
                    index += 1
                    continue
                if inner.endswith(": Boost/Cut"):
                    try:
                        gain = float(_parse_value(inner) or 0.0)
                    except ValueError:
                        gain = None
                    index += 1
                    continue
                break
            if freq is not None and gain is not None:
                preset.bands.append((freq, gain))
            continue

    if not seen_header:
        preset.error = "This does not look like an FxSound preset file."
        return preset

    preset.integers = integers
    for effect, slot in _MAIN_SLOTS.items():
        if slot in main_values:
            preset.effects[effect] = max(
                0.0, min(10.0, main_values[slot] / _EFFECT_SCALE)
            )

    if not preset.bands:
        preset.error = ("The preset file contains no equalizer bands; FxSound "
                        "presets normally include them.")
    return preset


def read_fac(path: str | Path) -> FacPreset:
    """Read and parse a ``.fac`` file."""
    file_path = Path(path)
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return FacPreset(name=file_path.stem, error=f"Could not read the file: {exc}")
    preset = parse_fac_text(text)
    if not preset.name:
        preset.name = file_path.stem
    return preset


def _effect_level(preset: FacPreset, effect: str, fallback: float) -> float:
    try:
        return max(0.0, min(10.0, float(preset.effects.get(effect, fallback))))
    except (TypeError, ValueError):
        return fallback


def format_effect_level(level: float) -> int:
    """Render a 0.0-10.0 effect level as the integer stored in the file."""
    return int(round(max(0.0, min(10.0, float(level))) * _EFFECT_SCALE))


def build_fac_text(preset: FacPreset) -> str:
    """Render a preset back into ``.fac`` text.

    The layout matches what FxSound itself writes, so a file produced here can
    be copied into ``%APPDATA%\\FxSound\\Presets`` and selected with
    ``--preset``.
    """
    effects = {
        "clarity": _effect_level(preset, "clarity", 5.0),
        "ambience": _effect_level(preset, "ambience", 0.0),
        "dynamic_boost": _effect_level(preset, "dynamic_boost", 2.0),
        "surround": _effect_level(preset, "surround", 4.0),
        "bass": _effect_level(preset, "bass", 5.0),
    }
    # Effect levels are stored as a 0-100-ish integer derived from the 0.0-10.0
    # UI scale. The divisor below was derived from the value sets that actually
    # occur in the built-in and user presets (see _EFFECT_SCALE).
    mains: dict[int, int] = {}
    for effect, slot in _MAIN_SLOTS.items():
        mains[slot] = format_effect_level(effects[effect])
    mains[_UNUSED_MAIN_SLOT] = 0

    bands = list(preset.bands)[:31]
    declared = preset.declared_bands or len(bands)

    lines: list[str] = [
        "CLASS1 : Effect Type",
        f"{_EFFECT_TYPE_PRESET}: Version",
        preset.name or "Custom",
        "0: Double Params Flag",
        "1: Total number of elements",
    ]
    for slot in range(6):
        lines.append(f"{mains.get(slot, 0)}: Main {slot}")
    lines.append("0: Element Number")
    # These seven lines are indented in the files FxSound writes; matching the
    # layout exactly keeps a diff against an authored file readable.
    for param in range(7):
        lines.append(f"   0: Param {param}")
    lines.append("7: Number of Application Dependent Integers")
    lines.append("0: Number of Application Dependent Reals")
    lines.append("0: Number of Application Dependent Strings")
    # Six of these are fixed; the third varies between presets and is carried
    # through from the file that was read so a round trip does not change it.
    for position, value in enumerate(preset.integers):
        lines.append(f"{int(value)}: Integer[{position}]")
    for position, value in enumerate(_DEFAULT_INTEGERS[len(preset.integers):], start=len(preset.integers)):
        lines.append(f"{int(value)}: Integer[{position}]")
    lines.append(f"{declared}: Number of EQ Bands")
    lines.append(f"{1 if preset.eq_enabled else 0}: On/Off Flag")
    for position, (freq, gain) in enumerate(bands, start=1):
        clamped_gain = max(BAND_GAIN_MIN_DB, min(BAND_GAIN_MAX_DB, float(gain)))
        lines.append(f"Band {position}")
        lines.append(f"   {format_frequency(freq)}: CF")
        lines.append(f"   {format_gain(clamped_gain)}: Boost/Cut")
    return "\n".join(lines) + "\n"


def write_fac(path: str | Path, preset: FacPreset) -> bool:
    """Write a preset to ``path`` as ``.fac`` text.

    Returns True on success. The write is atomic (temp file plus replace) so an
    interrupted save cannot leave FxSound with a truncated preset.
    """
    file_path = Path(path)
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        temp = file_path.with_name(file_path.name + ".tmp")
        temp.write_text(build_fac_text(preset), encoding="utf-8", newline="\n")
        temp.replace(file_path)
        return True
    except OSError as exc:
        log.error("Could not write preset %s: %s", file_path, exc)
        return False
