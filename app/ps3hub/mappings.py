"""The binding model: which headset input runs which action.

A profile is a named set of bindings. Profiles are plain data and validate
themselves on load, so a hand-edited or partially corrupt configuration file
degrades to "that one binding was dropped" rather than "the app will not
start".
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Iterator

from .actions import ACTION_NONE, ACTIONS, action_definition, describe_action
from .applog import get_logger
from .inputs import DESCRIPTOR_BY_ID, InputId, describe_input

log = get_logger("mappings")

DEFAULT_PROFILE_NAME = "Default"


@dataclass
class Mapping:
    """One input bound to one action."""

    input_id: str
    action_id: str = ACTION_NONE
    params: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True

    @property
    def is_bound(self) -> bool:
        return self.enabled and self.action_id != ACTION_NONE

    @property
    def input_label(self) -> str:
        return describe_input(self.input_id).label

    @property
    def action_label(self) -> str:
        return describe_action(self.action_id, self.params)

    def normalized(self) -> "Mapping":
        """Fill in defaults for any parameter the action expects but lacks."""
        try:
            definition = action_definition(self.action_id)
        except Exception:
            return replace(self, action_id=ACTION_NONE, params={})
        merged = definition.defaults()
        for key, value in (self.params or {}).items():
            if key in merged:
                merged[key] = value
        return replace(self, params=merged)

    def to_dict(self) -> dict[str, Any]:
        return {
            "input": self.input_id,
            "action": self.action_id,
            "params": dict(self.params),
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Mapping | None":
        input_id = str(data.get("input", "")).strip()
        if not input_id:
            log.warning("Dropping a binding with no input id")
            return None
        if input_id not in DESCRIPTOR_BY_ID:
            log.warning("Dropping binding for unknown input %r", input_id)
            return None
        action_id = str(data.get("action", ACTION_NONE)).strip() or ACTION_NONE
        if action_id not in ACTIONS:
            log.warning(
                "Binding for %s references unknown action %r; leaving it unbound",
                input_id, action_id,
            )
            action_id = ACTION_NONE
        raw_params = data.get("params") or {}
        params = dict(raw_params) if isinstance(raw_params, dict) else {}
        enabled = bool(data.get("enabled", True))
        return cls(input_id, action_id, params, enabled).normalized()


#: The examples from the brief, shipped as the out-of-the-box experience.
DEFAULT_BINDINGS: tuple[tuple[str, str, dict[str, Any]], ...] = (
    (InputId.CHATMIX_UP, "media.next", {}),
    (InputId.CHATMIX_DOWN, "media.previous", {}),
    (InputId.VSS_BUTTON, "media.play_pause", {}),
    (InputId.VOLUME_UP, "system.volume_up", {"steps": 1}),
    (InputId.VOLUME_DOWN, "system.volume_down", {"steps": 1}),
)


@dataclass
class Profile:
    """A named collection of bindings."""

    name: str = DEFAULT_PROFILE_NAME
    mappings: dict[str, Mapping] = field(default_factory=dict)

    # ------------------------------------------------------------- access --

    def get(self, input_id: str) -> Mapping | None:
        return self.mappings.get(input_id)

    def bound_for(self, input_id: str) -> Mapping | None:
        mapping = self.mappings.get(input_id)
        return mapping if mapping is not None and mapping.is_bound else None

    def set(self, mapping: Mapping) -> None:
        self.mappings[mapping.input_id] = mapping.normalized()

    def bind(self, input_id: str, action_id: str, params: dict[str, Any] | None = None) -> Mapping:
        mapping = Mapping(input_id, action_id, dict(params or {})).normalized()
        self.mappings[input_id] = mapping
        return mapping

    def unbind(self, input_id: str) -> bool:
        return self.mappings.pop(input_id, None) is not None

    def set_enabled(self, input_id: str, enabled: bool) -> bool:
        mapping = self.mappings.get(input_id)
        if mapping is None:
            return False
        mapping.enabled = enabled
        return True

    def clear(self) -> None:
        self.mappings.clear()

    @property
    def active(self) -> list[Mapping]:
        order = list(DESCRIPTOR_BY_ID)
        bound = [m for m in self.mappings.values() if m.is_bound]
        bound.sort(key=lambda m: order.index(m.input_id) if m.input_id in order else 999)
        return bound

    def __len__(self) -> int:
        return len(self.mappings)

    def __iter__(self) -> Iterator[Mapping]:
        return iter(self.mappings.values())

    # -------------------------------------------------------- persistence --

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "mappings": [m.to_dict() for m in self.mappings.values()],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Profile":
        profile = cls(name=str(data.get("name") or DEFAULT_PROFILE_NAME))
        raw = data.get("mappings")
        if isinstance(raw, dict):  # tolerate an older {input: {...}} shape
            raw = [dict(value, input=key) for key, value in raw.items()]
        if not isinstance(raw, list):
            log.warning("Profile %r has no usable bindings list", profile.name)
            return profile
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            mapping = Mapping.from_dict(entry)
            if mapping is not None:
                profile.mappings[mapping.input_id] = mapping
        return profile

    @classmethod
    def default(cls) -> "Profile":
        profile = cls(name=DEFAULT_PROFILE_NAME)
        for input_id, action_id, params in DEFAULT_BINDINGS:
            profile.bind(input_id, action_id, params)
        return profile

    def copy(self) -> "Profile":
        return Profile.from_dict(self.to_dict())

    def differs_from(self, other: "Profile") -> bool:
        return self.to_dict() != other.to_dict()
