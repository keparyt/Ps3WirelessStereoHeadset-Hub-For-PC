"""Configuration: settings plus the saved binding profile.

Written atomically (temp file then ``os.replace``) so a crash or a power cut
mid-save cannot leave a half-written file where the bindings used to be. A
file that will not parse is moved aside rather than deleted, so the user can
recover hand-made bindings and the app still starts.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .applog import data_dir, get_logger
from .audio.profiles import AudioProfile, ProfileStore
from .inputs import DEBOUNCE_SECONDS, RESYNC_THRESHOLD, SETTLE_SECONDS
from .mappings import Profile
from .protocol import BATTERY_LOW_THRESHOLD

#: The user-facing low-battery warning level. The protocol module's constant
#: (20) stays as the decoding fallback; the app default is deliberately lower
#: so the warning fires when there is still a little charge left.
DEFAULT_LOW_BATTERY_THRESHOLD = 5

log = get_logger("config")

CONFIG_VERSION = 2
CONFIG_FILENAME = "config.json"

#: Audio settings stored alongside the profiles in the ``audio`` key.
AUDIO_BACKENDS = ("native", "fxsound")


@dataclass
class Settings:
    """User preferences. Every field must survive a round trip through JSON."""

    #: Master switch. When off, inputs are still detected but nothing is run.
    mappings_enabled: bool = True
    start_minimized: bool = False
    #: Open every HID collection, not just the FF01:0020 status collection.
    read_all_collections: bool = False
    #: Battery percentage at or below which the Battery low input fires and a
    #: Windows toast warns the user.
    low_battery_threshold: int = DEFAULT_LOW_BATTERY_THRESHOLD
    #: Show a Windows notification when the battery reaches the threshold.
    low_battery_toast: bool = True
    #: Show a Windows notification every time a binding runs an action.
    action_toast: bool = True
    settle_seconds: float = SETTLE_SECONDS
    debounce_seconds: float = DEBOUNCE_SECONDS
    resync_threshold: int = RESYNC_THRESHOLD
    verbose_logging: bool = False
    show_raw_reports: bool = True
    window_geometry: str = ""
    #: Toast categories (the rules engine can be tuned per category; these are
    #: the switches the Settings page exposes).
    notify_connection: bool = True
    notify_volume: bool = True
    notify_audio: bool = True
    #: Deliver every toast through Windows even while the window is visible,
    #: instead of using the in-app corner overlay.
    force_windows_toasts: bool = False

    def clamped(self) -> "Settings":
        """Keep hand-edited values inside ranges the app can actually honour."""
        return Settings(
            mappings_enabled=bool(self.mappings_enabled),
            start_minimized=bool(self.start_minimized),
            read_all_collections=bool(self.read_all_collections),
            low_battery_toast=bool(self.low_battery_toast),
            action_toast=bool(self.action_toast),
            low_battery_threshold=_clamp_int(
                self.low_battery_threshold, 5, 50, DEFAULT_LOW_BATTERY_THRESHOLD
            ),
            settle_seconds=_clamp_float(self.settle_seconds, 0.0, 3.0, SETTLE_SECONDS),
            debounce_seconds=_clamp_float(self.debounce_seconds, 0.0, 1.0,
                                          DEBOUNCE_SECONDS),
            resync_threshold=_clamp_int(self.resync_threshold, 1, 10, RESYNC_THRESHOLD),
            verbose_logging=bool(self.verbose_logging),
            show_raw_reports=bool(self.show_raw_reports),
            window_geometry=str(self.window_geometry or ""),
            notify_connection=bool(self.notify_connection),
            notify_volume=bool(self.notify_volume),
            notify_audio=bool(self.notify_audio),
            force_windows_toasts=bool(self.force_windows_toasts),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Settings":
        known = {f for f in cls.__dataclass_fields__}
        filtered = {k: v for k, v in (data or {}).items() if k in known}
        try:
            return cls(**filtered).clamped()
        except TypeError as exc:
            log.warning("Settings could not be read (%s); using defaults", exc)
            return cls()


def _clamp_int(value: Any, low: int, high: int, fallback: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return fallback


def _clamp_float(value: Any, low: float, high: float, fallback: float) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return fallback


@dataclass
class AppConfig:
    settings: Settings = field(default_factory=Settings)
    profile: Profile = field(default_factory=Profile.default)
    #: Per-endpoint audio effect profiles plus the engine switches.
    audio: dict[str, Any] = field(
        default_factory=lambda: {"enabled": False, "backend": "native",
                                 "profiles": []}
    )

    def audio_store(self) -> ProfileStore:
        """The per-device profiles as a :class:`ProfileStore`."""
        return ProfileStore.from_dict(self.audio)

    def set_audio_store(self, store: ProfileStore, enabled: bool | None = None,
                        backend: str | None = None) -> None:
        payload = store.to_dict()
        payload["enabled"] = (
            bool(self.audio.get("enabled", False)) if enabled is None else bool(enabled)
        )
        current_backend = str(self.audio.get("backend", "native"))
        payload["backend"] = current_backend if backend is None else str(backend)
        if payload["backend"] not in AUDIO_BACKENDS:
            payload["backend"] = "native"
        self.audio = payload

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": CONFIG_VERSION,
            "settings": self.settings.to_dict(),
            "profile": self.profile.to_dict(),
            "audio": dict(self.audio),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppConfig":
        version = data.get("version", 1)
        if version != CONFIG_VERSION:
            data = migrate(data, version)
        settings = Settings.from_dict(data.get("settings") or {})
        raw_profile = data.get("profile")
        if isinstance(raw_profile, dict):
            profile = Profile.from_dict(raw_profile)
        else:
            log.warning("No profile in the configuration; loading defaults")
            profile = Profile.default()
        audio = _sanitise_audio(data.get("audio"))
        return cls(settings=settings, profile=profile, audio=audio)


def _sanitise_audio(raw: Any) -> dict[str, Any]:
    """Validate the audio payload so a corrupt entry degrades to defaults."""
    audio: dict[str, Any] = {
        "enabled": False, "backend": "native", "profiles": [], "auto_output": False,
    }
    if not isinstance(raw, dict):
        return audio
    backend = str(raw.get("backend", "native"))
    audio["backend"] = backend if backend in AUDIO_BACKENDS else "native"
    audio["enabled"] = bool(raw.get("enabled", False))
    audio["auto_output"] = bool(raw.get("auto_output", False))
    profiles = raw.get("profiles")
    audio["profiles"] = profiles if isinstance(profiles, list) else []
    return audio


def migrate(data: dict[str, Any], from_version: Any) -> dict[str, Any]:
    """Bring an older configuration forward without losing user data.

    v1 -> v2: the ``audio`` key (per-endpoint effect profiles and engine
    switches) is new. Everything a v1 file contained is kept as-is; only the
    missing key is added. Unknown future versions keep their shape and are
    validated key-by-key on load, so a downgrade is not destructive either.
    """
    log.info("Migrating configuration from version %r", from_version)
    if isinstance(data, dict) and "audio" not in data:
        data["audio"] = {"enabled": False, "backend": "native", "profiles": [],
                         "auto_output": False}
    return data


def config_path() -> Path:
    return data_dir() / CONFIG_FILENAME


class ConfigStore:
    """Loads and saves the configuration file."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else config_path()
        self.last_error: str = ""

    # ----------------------------------------------------------- loading --

    def load(self) -> AppConfig:
        self.last_error = ""
        if not self.path.exists():
            log.info("No configuration yet; starting with defaults")
            return AppConfig()
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("the configuration file is not a JSON object")
            config = AppConfig.from_dict(data)
            log.info(
                "Loaded configuration: %d binding(s), %d audio profile(s) from %s",
                len(config.profile), len(config.audio.get("profiles", [])), self.path,
            )
            return config
        except Exception as exc:
            self.last_error = str(exc)
            backup = self._quarantine()
            log.error(
                "Could not read %s (%s). It was moved to %s and defaults were loaded.",
                self.path, exc, backup,
            )
            return AppConfig()

    def _quarantine(self) -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = self.path.with_name(f"{self.path.stem}.broken-{stamp}.json")
        try:
            shutil.copy2(self.path, backup)
        except OSError as exc:
            log.warning("Could not preserve the unreadable configuration: %s", exc)
        return backup

    # ------------------------------------------------------------ saving --

    def save(self, config: AppConfig) -> bool:
        self.last_error = ""
        payload = json.dumps(config.to_dict(), indent=2, ensure_ascii=False)
        temp = self.path.with_suffix(".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(temp, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
            log.info("Saved configuration to %s", self.path)
            return True
        except Exception as exc:
            self.last_error = str(exc)
            log.error("Could not save the configuration: %s", exc)
            try:
                if temp.exists():
                    temp.unlink()
            except OSError:
                pass
            return False

    def export_profile(self, profile: Profile, destination: Path) -> bool:
        try:
            Path(destination).write_text(
                json.dumps(profile.to_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            return True
        except Exception as exc:
            self.last_error = str(exc)
            log.error("Could not export the profile: %s", exc)
            return False

    def import_profile(self, source: Path) -> Profile | None:
        try:
            data = json.loads(Path(source).read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("the file is not a JSON object")
            return Profile.from_dict(data)
        except Exception as exc:
            self.last_error = str(exc)
            log.error("Could not import the profile: %s", exc)
            return None
