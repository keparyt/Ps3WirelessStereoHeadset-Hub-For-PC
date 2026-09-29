"""Per-output-device audio effect profiles.

A profile binds effect settings to **one stable endpoint id** (not a friendly
name), so renames and reconnects do not detach a user's tuning from their
hardware. Profiles are plain data that validate themselves on load, so a
corrupt entry degrades to defaults instead of breaking the app.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any

from ..applog import get_logger
from .fac import VALID_BAND_COUNTS, default_band_frequencies

log = get_logger("audio.profiles")

#: The largest equalizer the application supports.
MAX_BANDS = max(VALID_BAND_COUNTS)
#: Filter Q range, matching the FxSound CLI.
FILTER_Q_MIN = 1.0
FILTER_Q_MAX = 3.0


def _coerce_band_count(value: Any, band_total: int = 0) -> int:
    """Snap a band count onto one FxSound supports, keeping bands consistent.

    A profile whose ``eq`` list has 15 entries but whose ``eq_bands`` says 10
    would silently drop five bands, so the larger of the two wins and a count
    that is not supported snaps to the nearest one that is.
    """
    try:
        count = int(value)
    except (TypeError, ValueError):
        count = band_total or 10
    if band_total:
        count = max(count, min(band_total, MAX_BANDS))
    if count in VALID_BAND_COUNTS:
        return count
    return min(VALID_BAND_COUNTS, key=lambda option: abs(option - count))

#: Effect value range shared by every effect control (matches the UI scale).
EFFECT_MIN = 0.0
EFFECT_MAX = 10.0

DEFAULT_PROFILE_NAME = "PS3 Headset"


@dataclass
class AudioProfile:
    """Effect settings for one output endpoint."""

    device_id: str = ""
    device_name: str = ""
    name: str = DEFAULT_PROFILE_NAME
    #: Master effects switch for this profile.
    enabled: bool = True
    #: Activate automatically when this endpoint becomes the default output.
    auto_activate: bool = True
    # Effect levels on the 0-10 UI scale.
    bass: float = 5.0
    clarity: float = 5.0
    ambience: float = 0.0
    surround: float = 4.0
    dynamic_boost: float = 2.0
    #: Master gain in dB (-20..+20). 0 is unity.
    master_gain_db: float = 0.0
    #: Equalizer bands as (frequency_hz, gain_db) pairs, in ascending
    #: frequency order. Empty means "flat", and is rendered as such.
    eq: list[tuple[float, float]] = field(default_factory=list)
    #: Number of equalizer bands (5, 10, 15, 20 or 31).
    eq_bands: int = 10
    #: EQ filter Q, which sets how wide each band's boost reaches.
    filter_q: float = 1.0
    #: Volume leveling in dB: how far quiet passages are lifted.
    volume_leveling_db: float = 0.0
    #: Left/right balance in dB; negative favours the left channel.
    balance_db: float = 0.0
    #: The FxSound preset this profile was loaded from or saved as.
    preset_name: str = ""

    # ------------------------------------------------------------- helpers --

    def clamped(self) -> "AudioProfile":
        def effect(value: Any, fallback: float) -> float:
            try:
                return max(EFFECT_MIN, min(EFFECT_MAX, float(value)))
            except (TypeError, ValueError):
                return fallback

        try:
            gain = max(-20.0, min(20.0, float(self.master_gain_db)))
        except (TypeError, ValueError):
            gain = 0.0
        eq: list[tuple[float, float]] = []
        for pair in (self.eq or [])[:MAX_BANDS]:
            try:
                freq, gain_db = pair
                freq_f = max(20.0, min(20000.0, float(freq)))
                gain_f = max(-12.0, min(12.0, float(gain_db)))
            except (TypeError, ValueError):
                continue
            eq.append((freq_f, gain_f))
        # Bands must ascend, or the graph would fold back on itself.
        eq.sort(key=lambda item: item[0])

        def bounded(value: Any, low: float, high: float, fallback: float) -> float:
            try:
                return max(low, min(high, float(value)))
            except (TypeError, ValueError):
                return fallback

        return replace(
            self,
            enabled=bool(self.enabled),
            auto_activate=bool(self.auto_activate),
            bass=effect(self.bass, 5.0),
            clarity=effect(self.clarity, 5.0),
            ambience=effect(self.ambience, 0.0),
            surround=effect(self.surround, 4.0),
            dynamic_boost=effect(self.dynamic_boost, 2.0),
            master_gain_db=gain,
            eq=eq,
            eq_bands=_coerce_band_count(self.eq_bands, len(eq)),
            filter_q=bounded(self.filter_q, FILTER_Q_MIN, FILTER_Q_MAX, 1.0),
            volume_leveling_db=bounded(self.volume_leveling_db, 0.0, 4.0, 0.0),
            balance_db=bounded(self.balance_db, -20.0, 20.0, 0.0),
            preset_name=str(self.preset_name or "")[:64],
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["eq"] = [[freq, gain] for freq, gain in self.eq]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AudioProfile":
        if not isinstance(data, dict):
            log.warning("Audio profile entry is not an object; skipped")
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        filtered = {k: v for k, v in data.items() if k in known}
        try:
            return cls(**filtered).clamped()
        except TypeError as exc:
            log.warning("Audio profile could not be read (%s); using defaults", exc)
            return cls()

    def to_fac(self) -> Any:
        """Render this profile as an FxSound ``.fac`` preset.

        The equalizer is written with the full set of centre frequencies for
        its band count so the file describes a complete curve, not just the
        bands the user happened to touch.
        """
        from . import fac as fac_module

        preset = fac_module.FacPreset(
            name=self.preset_name or self.name or "Hub preset",
            effects={
                "bass": self.bass,
                "clarity": self.clarity,
                "ambience": self.ambience,
                "surround": self.surround,
                "dynamic_boost": self.dynamic_boost,
            },
            bands=self.eq or [
                (freq, 0.0) for freq in default_band_frequencies(self.eq_bands)
            ],
            eq_enabled=bool(self.eq) or True,
            declared_bands=self.eq_bands,
        )
        return preset

    def write_fac(self, path: Any) -> tuple[bool, str]:
        """Write this profile to ``path`` as an FxSound preset file."""
        from . import fac as fac_module

        ok = fac_module.write_fac(path, self.to_fac())
        return ok, "" if ok else f"Could not write {path}."

    @classmethod
    def from_fac(cls, path: Any, device_id: str = "", device_name: str = "") -> tuple["AudioProfile", str]:
        """Load a profile from an FxSound ``.fac`` file.

        Returns ``(profile, error)``. On a bad file the caller gets a default
        profile *and* a message, rather than an exception, so a mistyped path
        in a file dialog cannot take the app down.
        """
        from . import fac as fac_module

        preset = fac_module.read_fac(path)
        if preset.error and not preset.bands and not preset.effects:
            return cls(device_id=device_id, device_name=device_name), preset.error

        count = preset.band_count or (len(preset.bands) or 10)
        bands = preset.bands or [(freq, 0.0) for freq in default_band_frequencies(count)]
        profile = cls(
            device_id=device_id,
            device_name=device_name,
            name=preset.name or "Imported preset",
            eq=bands,
            eq_bands=count,
            bass=preset.effects.get("bass", 5.0),
            clarity=preset.effects.get("clarity", 5.0),
            ambience=preset.effects.get("ambience", 0.0),
            surround=preset.effects.get("surround", 4.0),
            dynamic_boost=preset.effects.get("dynamic_boost", 2.0),
            preset_name=preset.name,
        )
        return profile.clamped(), preset.error

    def is_equivalent_effect(self, other: "AudioProfile") -> bool:
        """True when two profiles produce the same DSP settings."""
        a, b = self.clamped(), other.clamped()
        return (
            a.enabled == b.enabled
            and a.bass == b.bass
            and a.clarity == b.clarity
            and a.ambience == b.ambience
            and a.surround == b.surround
            and a.dynamic_boost == b.dynamic_boost
            and a.master_gain_db == b.master_gain_db
            and a.eq == b.eq
            and a.eq_bands == b.eq_bands
            and a.filter_q == b.filter_q
            and a.volume_leveling_db == b.volume_leveling_db
            and a.balance_db == b.balance_db
        )


class ProfileStore:
    """All profiles, keyed by stable device id."""

    def __init__(self, profiles: dict[str, AudioProfile] | None = None) -> None:
        self._profiles: dict[str, AudioProfile] = dict(profiles or {})

    # ------------------------------------------------------------- access --

    def get(self, device_id: str) -> AudioProfile | None:
        return self._profiles.get(device_id)

    def for_device(self, device_id: str, device_name: str = "") -> AudioProfile:
        """Get-or-create the profile for an endpoint."""
        profile = self._profiles.get(device_id)
        if profile is None:
            profile = AudioProfile(device_id=device_id, device_name=device_name)
            self._profiles[device_id] = profile
        elif device_name and profile.device_name != device_name:
            profile.device_name = device_name
        return profile

    def set(self, profile: AudioProfile) -> None:
        self._profiles[profile.device_id] = profile.clamped()

    def remove(self, device_id: str) -> bool:
        return self._profiles.pop(device_id, None) is not None

    def all(self) -> list[AudioProfile]:
        return list(self._profiles.values())

    def __len__(self) -> int:
        return len(self._profiles)

    # -------------------------------------------------------- persistence --

    def to_dict(self) -> dict[str, Any]:
        return {"profiles": [p.to_dict() for p in self._profiles.values()]}

    @classmethod
    def from_dict(cls, data: Any) -> "ProfileStore":
        store = cls()
        raw = data.get("profiles") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            return store
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            profile = AudioProfile.from_dict(entry)
            if profile.device_id:
                store._profiles[profile.device_id] = profile
        return store
