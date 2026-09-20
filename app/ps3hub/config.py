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
from .inputs import DEBOUNCE_SECONDS, RESYNC_THRESHOLD, SETTLE_SECONDS
from .mappings import Profile
from .protocol import BATTERY_LOW_THRESHOLD

log = get_logger("config")

CONFIG_VERSION = 1
CONFIG_FILENAME = "config.json"


@dataclass
class Settings:
    """User preferences. Every field must survive a round trip through JSON."""

    #: Master switch. When off, inputs are still detected but nothing is run.
    mappings_enabled: bool = True
    start_minimized: bool = False
    #: Open every HID collection, not just the FF01:0020 status collection.
    read_all_collections: bool = False
    low_battery_threshold: int = BATTERY_LOW_THRESHOLD
    settle_seconds: float = SETTLE_SECONDS
    debounce_seconds: float = DEBOUNCE_SECONDS
    resync_threshold: int = RESYNC_THRESHOLD
    verbose_logging: bool = False
    show_raw_reports: bool = True
    window_geometry: str = ""

    def clamped(self) -> "Settings":
        """Keep hand-edited values inside ranges the app can actually honour."""
        return Settings(
            mappings_enabled=bool(self.mappings_enabled),
            start_minimized=bool(self.start_minimized),
            read_all_collections=bool(self.read_all_collections),
            low_battery_threshold=_clamp_int(self.low_battery_threshold, 5, 50,
                                             BATTERY_LOW_THRESHOLD),
            settle_seconds=_clamp_float(self.settle_seconds, 0.0, 3.0, SETTLE_SECONDS),
            debounce_seconds=_clamp_float(self.debounce_seconds, 0.0, 1.0,
                                          DEBOUNCE_SECONDS),
            resync_threshold=_clamp_int(self.resync_threshold, 1, 10, RESYNC_THRESHOLD),
            verbose_logging=bool(self.verbose_logging),
            show_raw_reports=bool(self.show_raw_reports),
            window_geometry=str(self.window_geometry or ""),
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": CONFIG_VERSION,
            "settings": self.settings.to_dict(),
            "profile": self.profile.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppConfig":
        version = data.get("version", CONFIG_VERSION)
        if version != CONFIG_VERSION:
            data = migrate(data, version)
        settings = Settings.from_dict(data.get("settings") or {})
        raw_profile = data.get("profile")
        if isinstance(raw_profile, dict):
            profile = Profile.from_dict(raw_profile)
        else:
            log.warning("No profile in the configuration; loading defaults")
            profile = Profile.default()
        return cls(settings=settings, profile=profile)


def migrate(data: dict[str, Any], from_version: Any) -> dict[str, Any]:
    """Bring an older configuration forward.

    There is only one schema version today. The hook exists so a future
    version can change shape without discarding a user's bindings.
    """
    log.info("Migrating configuration from version %r", from_version)
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
                "Loaded configuration: %d binding(s) from %s",
                len(config.profile), self.path,
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
