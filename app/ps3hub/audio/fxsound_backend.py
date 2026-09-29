"""Optional FxSound integration backend.

FxSound (AGPL-3.0) is *not* copied or linked here. This module drives an
**installed** FxSound application exclusively through its documented
command-line interface (``docs/COMMAND_LINE_OPTIONS.md`` in the fxsound-app
repository): options like ``--power``, ``--preset``, ``--set_effect`` and
``--status``, plus the JSON status file it writes. No GUI automation, no
screen scraping, no clicking.

The dependency is explicit and optional: the Hub works fully without FxSound
(see :mod:`loopback_dsp`), and Settings shows whether FxSound was found.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..applog import get_logger
from .fac import (
    BAND_GAIN_MAX_DB, BAND_GAIN_MIN_DB, VALID_BAND_COUNTS,
)

#: Filter Q range the CLI accepts, and its 0.5 step.
FILTER_Q_MIN = 1.0
FILTER_Q_MAX = 3.0

log = get_logger("audio.fxsound")

_STATUS_RELATIVE = Path("FxSound") / "status.json"
_STATUS_SETTLE_SECONDS = 2.5

#: Documented CLI effect names and their allowed ranges (0.0-10.0).
_FXound_EFFECT_KEYS = {
    "bass": ("bass", "bassboost", "bass_boost"),
    "clarity": ("fidelity", "clarity"),
    "ambience": ("ambience",),
    "surround": ("surround",),
    "dynamic_boost": ("dynamicboost", "dynamic_boost"),
}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def _band_pairs(values: "dict[int, float] | list[float]", render) -> list[str]:
    """Render ``{index: value}`` (or a list) as ``index:value`` CLI pairs.

    Values that are not finite numbers are dropped: sending ``nan`` to the
    application would be parsed as a valid token and quietly poison a band.
    """
    if isinstance(values, dict):
        items = values.items()
    else:
        items = enumerate(values)
    pairs: list[str] = []
    for index, value in sorted(items):
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number != number or number in (float("inf"), float("-inf")):
            continue
        pairs.append(f"{int(index)}:{render(number)}")
    return pairs


def _candidate_paths() -> list[Path]:
    """Common FxSound install locations + PATH lookup."""
    candidates: list[Path] = []
    exe = shutil.which("fxsound")
    if exe:
        candidates.append(Path(exe))
    program_dirs = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "FxSound LLC" / "FxSound",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "FxSound",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "FxSound LLC" / "FxSound",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "FxSound",
    ]
    for directory in program_dirs:
        if str(directory) and directory.exists():
            exe_path = directory / "fxsound.exe"
            if exe_path.exists():
                candidates.append(exe_path)
    # Windows Store install exposes an alias under %LOCALAPPDATA%/Microsoft/WindowsApps
    store_alias = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps" / "fxsound.exe"
    if str(store_alias) and store_alias.exists():
        candidates.append(store_alias)
    # De-duplicate preserving order
    seen: set[str] = set()
    unique: list[Path] = []
    for candidate in candidates:
        text = str(candidate)
        if text.lower() not in seen:
            seen.add(text.lower())
            unique.append(candidate)
    return unique


@dataclass
class FxSoundStatus:
    """Parsed ``status.json`` content (the fields the Hub consumes)."""

    found: bool = False
    running: bool = False
    power: bool = False
    selected_output: str = ""
    selected_preset: str = ""
    version: str = ""
    master_gain: float = 0.0
    effects: dict[str, float] = field(default_factory=dict)
    #: The equalizer block of status.json: num_bands, master_gain,
    #: volume_leveling, filter_q, balance and the band list.
    equalizer: dict[str, Any] = field(default_factory=dict)
    #: Preset names, split into the built-in and user-defined lists.
    built_in_presets: list[str] = field(default_factory=list)
    user_presets: list[str] = field(default_factory=list)
    output_devices: list[str] = field(default_factory=list)
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "found": self.found,
            "running": self.running,
            "power": self.power,
            "selected_output": self.selected_output,
            "selected_preset": self.selected_preset,
            "version": self.version,
            "master_gain": self.master_gain,
            "effects": dict(self.effects),
            "equalizer": dict(self.equalizer),
            "built_in_presets": list(self.built_in_presets),
            "user_presets": list(self.user_presets),
            "output_devices": list(self.output_devices),
            "error": self.error,
        }


class FxSoundBackend:
    """Controls an installed FxSound through its documented CLI."""

    def __init__(self) -> None:
        self._exe = _candidate_paths()
        self._last_status: FxSoundStatus = FxSoundStatus()
        self._last_command_time = 0.0

    # ------------------------------------------------------------- probing --

    @property
    def exe_path(self) -> Path | None:
        """First existing fxsound.exe candidate."""
        for candidate in self._exe:
            try:
                if candidate.exists():
                    return candidate
            except OSError:
                continue
        return None

    def is_installed(self) -> bool:
        return self.exe_path is not None

    def read_status(self, force: bool = False) -> FxSoundStatus:
        """Ask the running instance to write status.json, then parse it.

        With ``force`` the CLI ``--status`` request is always sent; otherwise
        the existing file is used when fresh enough.
        """
        status = FxSoundStatus(found=self.is_installed())
        exe = self.exe_path
        if exe is None:
            status.error = "FxSound is not installed"
            self._last_status = status
            return status

        status_path = self._status_path()
        if force or not self._status_fresh(status_path):
            try:
                # CREATE_NO_WINDOW keeps a console from flashing.
                subprocess.run(
                    [str(exe), "--status"],
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    timeout=10,
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                log.debug("FxSound --status request failed: %s", exc)
            deadline = time.monotonic() + _STATUS_SETTLE_SECONDS
            while time.monotonic() < deadline and not self._status_fresh(status_path):
                time.sleep(0.1)

        if status_path is not None and status_path.exists():
            try:
                data = json.loads(status_path.read_text(encoding="utf-8"))
                status = self._parse_status(data, found=True)
            except (OSError, ValueError) as exc:
                status.error = f"Could not read FxSound status: {exc}"
        else:
            status.running = False
            status.error = ""
        self._last_status = status
        return status

    @staticmethod
    def _status_path() -> Path | None:
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return None
        return Path(appdata) / _STATUS_RELATIVE

    @staticmethod
    def _status_fresh(path: Path | None) -> bool:
        if path is None or not path.exists():
            return False
        try:
            age = time.time() - path.stat().st_mtime
            return age < 30.0
        except OSError:
            return False

    @staticmethod
    def _parse_status(data: dict[str, Any], found: bool) -> FxSoundStatus:
        effects: dict[str, float] = {}
        fx = data.get("effects") or {}
        if isinstance(fx, dict):
            for canonical, accepted in _FXound_EFFECT_KEYS.items():
                for key in accepted:
                    if key in fx:
                        try:
                            effects[canonical] = float(fx[key])
                            break
                        except (TypeError, ValueError):
                            pass
        eq = data.get("equalizer")
        equalizer = dict(eq) if isinstance(eq, dict) else {}
        # Keep only well-formed bands so callers can rely on the shape.
        bands = []
        for band in equalizer.get("bands") or []:
            if not isinstance(band, dict):
                continue
            try:
                bands.append({
                    "index": int(band.get("index", len(bands))),
                    "frequency": float(band.get("frequency", 0.0)),
                    "gain": float(band.get("gain", 0.0)),
                })
            except (TypeError, ValueError):
                continue
        equalizer["bands"] = bands
        for key in ("num_bands", "master_gain", "volume_leveling", "filter_q", "balance"):
            try:
                equalizer[key] = float(equalizer.get(key, 0.0))
            except (TypeError, ValueError):
                equalizer[key] = 0.0
        try:
            equalizer["num_bands"] = int(equalizer["num_bands"])
        except (TypeError, ValueError):
            equalizer["num_bands"] = len(bands)

        def _names(group: str) -> list[str]:
            block = data.get("presets") or {}
            entries = block.get(group) if isinstance(block, dict) else None
            if not isinstance(entries, list):
                return []
            return [str(e.get("name", "")) for e in entries
                    if isinstance(e, dict) and e.get("name")]

        devices = data.get("output_devices")
        return FxSoundStatus(
            found=found,
            running=True,
            power=bool(data.get("power", False)),
            selected_output=str(data.get("selected_output", "")),
            selected_preset=str(data.get("selected_preset", "")),
            version=str(data.get("version", "")),
            master_gain=float(equalizer.get("master_gain", 0.0)),
            effects=effects,
            equalizer=equalizer,
            built_in_presets=_names("built_in"),
            user_presets=_names("user_defined"),
            output_devices=[str(d) for d in devices] if isinstance(devices, list) else [],
        )

    # ------------------------------------------------------------- control --

    def _send(self, *arguments: str) -> bool:
        exe = self.exe_path
        if exe is None:
            return False
        # Documented CLI: values attach with '=', no space.
        try:
            subprocess.run(
                [str(exe), *arguments],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=10,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._last_command_time = time.monotonic()
            return True
        except (OSError, subprocess.SubprocessError) as exc:
            log.error("FxSound command failed: %s", exc)
            return False

    def set_power(self, enabled: bool) -> bool:
        return self._send(f"--power={'1' if enabled else '0'}")

    def set_output(self, device_name: str) -> bool:
        if not device_name:
            return False
        return self._send(f"--output={device_name}")

    def set_preset(self, preset_name: str) -> bool:
        if not preset_name:
            return False
        return self._send(f"--preset={preset_name}")

    def save_preset(self, name: str) -> bool:
        """Save the current modified settings as a new user preset."""
        if not name:
            return False
        return self._send(f"--save_preset={name}")

    def overwrite_preset(self) -> bool:
        return self._send("--overwrite_preset")

    def undo_preset(self) -> bool:
        return self._send("--undo_preset")

    def rename_preset(self, name: str) -> bool:
        if not name:
            return False
        return self._send(f"--rename_preset={name}")

    def delete_preset(self) -> bool:
        return self._send("--delete_preset")

    def set_effects(self, effects: dict[str, float]) -> bool:
        """Set effect levels through one ``--set_effect`` invocation."""
        if not effects:
            return True
        pairs = []
        for canonical, value in effects.items():
            keys = _FXound_EFFECT_KEYS.get(canonical)
            if not keys:
                continue
            clamped = max(0.0, min(10.0, float(value)))
            pairs.append(f"{keys[0]}:{clamped:.1f}")
        if not pairs:
            return True
        return self._send("--set_effect=" + ",".join(pairs))

    def set_master_gain(self, gain_db: float) -> bool:
        clamped = max(-20.0, min(20.0, float(gain_db)))
        return self._send(f"--master_gain={clamped:.1f}")

    # --------------------------------------------------------- equalizer --

    def set_num_bands(self, count: int) -> bool:
        """Set the band count (5, 10, 15, 20 or 31).

        FxSound re-derives every centre frequency when this changes, and keeps
        the band gains it can, so the caller should follow a band-count change
        with an explicit :meth:`set_band_gains` to land on the curve it wants.
        """
        count = int(count)
        if count not in VALID_BAND_COUNTS:
            return False
        return self._send(f"--num_bands={count}")

    def set_band_gains(self, gains: dict[int, float] | list[float]) -> bool:
        """Set one or more band boost/cut values in dB."""
        pairs = _band_pairs(gains, lambda value: f"{_clamp(value, BAND_GAIN_MIN_DB, BAND_GAIN_MAX_DB):.1f}")
        if not pairs:
            return True
        return self._send("--set_band_gain=" + ",".join(pairs))

    def set_band_freqs(self, freqs: dict[int, float] | list[float]) -> bool:
        """Set one or more band centre frequencies in Hz.

        FxSound silently ignores a frequency outside the band's allowed range,
        so out-of-range values are dropped here instead of being sent blindly.
        """
        pairs = _band_pairs(freqs, lambda value: f"{_clamp(value, 20.0, 20000.0):.3g}")
        if not pairs:
            return True
        return self._send("--set_band_freq=" + ",".join(pairs))

    def set_filter_q(self, q: float) -> bool:
        # The CLI rounds this to the nearest 0.5, matching the app's slider.
        return self._send(f"--filter_q={_clamp(q, FILTER_Q_MIN, FILTER_Q_MAX):.1f}")

    def set_volume_leveling(self, db: float) -> bool:
        return self._send(f"--volume_leveling={_clamp(db, 0.0, 4.0):.1f}")

    def set_balance(self, db: float) -> bool:
        return self._send(f"--balance={_clamp(db, -20.0, 20.0):.1f}")

    # -------------------------------------------------------------- engine --

    def apply_profile(self, profile: Any, output_name: str) -> FxSoundStatus:
        """Push one audio profile onto the running FxSound instance.

        Order matters: the band count has to be set before the individual
        bands, because FxSound re-derives the centre frequencies (and can
        discard out-of-range indices) when the count changes.
        """
        if not self.is_installed():
            return FxSoundStatus(found=False, error="FxSound is not installed")
        if not self.set_power(bool(getattr(profile, "enabled", True))):
            return FxSoundStatus(found=True, running=False, error="FxSound did not accept commands")
        if output_name:
            self.set_output(output_name)

        effects = {
            "bass": getattr(profile, "bass", 0.0),
            "clarity": getattr(profile, "clarity", 0.0),
            "ambience": getattr(profile, "ambience", 0.0),
            "surround": getattr(profile, "surround", 0.0),
            "dynamic_boost": getattr(profile, "dynamic_boost", 0.0),
        }
        self.set_effects(effects)
        self.apply_equalizer(profile)
        preset = getattr(profile, "preset_name", "")
        if preset:
            self.set_preset(preset)
        return self.read_status(force=True)

    def apply_equalizer(self, profile: Any) -> bool:
        """Push just the equalizer block of a profile.

        Separated from :meth:`apply_profile` because dragging a single EQ point
        should not re-send the effect levels, the output device and the preset
        name on every mouse-move.
        """
        count = int(getattr(profile, "eq_bands", 10) or 10)
        self.set_num_bands(count)

        # The profile stores bands positionally, so a band-count change is
        # followed by an explicit frequency and gain push to land on the
        # intended curve rather than whatever FxSound derived on its own.
        freqs = [freq for freq, _gain in getattr(profile, "eq", []) or []]
        gains = [gain for _freq, gain in getattr(profile, "eq", []) or []]
        if freqs:
            self.set_band_freqs(freqs[:count])
        if gains:
            self.set_band_gains(gains[:count])

        self.set_master_gain(getattr(profile, "master_gain_db", 0.0))
        self.set_filter_q(getattr(profile, "filter_q", 1.0))
        self.set_volume_leveling(getattr(profile, "volume_leveling_db", 0.0))
        self.set_balance(getattr(profile, "balance_db", 0.0))
        return True
