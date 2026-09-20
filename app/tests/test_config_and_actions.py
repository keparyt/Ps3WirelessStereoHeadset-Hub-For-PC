"""Bindings, persistence and actions."""

import json

import pytest

from ps3hub.actions import (
    ACTION_NONE, ActionContext, ActionError, ActionRunner, KeySender,
    VK, describe_action, describe_combo, parse_combo,
)
from ps3hub.config import AppConfig, ConfigStore, Settings
from ps3hub.inputs import InputId
from ps3hub.mappings import Mapping, Profile


# ------------------------------------------------------------- mappings --

def test_default_profile_matches_the_documented_examples():
    profile = Profile.default()
    assert profile.get(InputId.CHATMIX_UP).action_id == "media.next"
    assert profile.get(InputId.CHATMIX_DOWN).action_id == "media.previous"
    assert profile.get(InputId.VSS_BUTTON).action_id == "media.play_pause"
    assert profile.get(InputId.VOLUME_UP).action_id == "system.volume_up"
    assert profile.get(InputId.VOLUME_DOWN).action_id == "system.volume_down"


def test_binding_round_trips_through_json():
    profile = Profile.default()
    profile.bind(InputId.MIC_BUTTON, "keyboard.combo", {"keys": "ctrl+shift+m"})
    restored = Profile.from_dict(json.loads(json.dumps(profile.to_dict())))
    mapping = restored.get(InputId.MIC_BUTTON)
    assert mapping is not None
    assert mapping.action_id == "keyboard.combo"
    assert mapping.params["keys"] == "ctrl+shift+m"


def test_unbinding_removes_the_mapping():
    profile = Profile.default()
    assert profile.unbind(InputId.VOLUME_UP) is True
    assert profile.get(InputId.VOLUME_UP) is None
    assert profile.unbind(InputId.VOLUME_UP) is False


def test_disabled_binding_is_not_active():
    profile = Profile.default()
    profile.set_enabled(InputId.VOLUME_UP, False)
    assert profile.get(InputId.VOLUME_UP) is not None
    assert profile.bound_for(InputId.VOLUME_UP) is None


def test_unknown_input_is_dropped_on_load():
    profile = Profile.from_dict({
        "name": "x",
        "mappings": [
            {"input": "not_a_real_input", "action": "media.next"},
            {"input": InputId.VSS_BUTTON, "action": "media.next"},
        ],
    })
    assert len(profile) == 1


def test_unknown_action_degrades_to_unbound_instead_of_failing():
    profile = Profile.from_dict({
        "mappings": [{"input": InputId.VSS_BUTTON, "action": "media.teleport"}]
    })
    mapping = profile.get(InputId.VSS_BUTTON)
    assert mapping is not None
    assert mapping.action_id == ACTION_NONE


def test_missing_params_are_filled_from_the_action_defaults():
    mapping = Mapping(InputId.VOLUME_UP, "system.volume_up", {}).normalized()
    assert mapping.params["steps"] == 1


# --------------------------------------------------------------- actions --

def test_parse_a_simple_combo():
    assert parse_combo("ctrl+shift+m") == [VK["ctrl"], VK["shift"], VK["m"]]


def test_parse_is_whitespace_and_case_tolerant():
    assert parse_combo(" Ctrl + F5 ") == [VK["ctrl"], VK["f5"]]


def test_unknown_key_is_rejected_with_a_readable_message():
    with pytest.raises(ActionError) as excinfo:
        parse_combo("ctrl+nope")
    assert "nope" in str(excinfo.value)


def test_modifier_only_combo_is_rejected():
    with pytest.raises(ActionError):
        parse_combo("ctrl+shift")


def test_empty_combo_is_rejected():
    with pytest.raises(ActionError):
        parse_combo("")


def test_describe_combo_is_human_readable():
    assert describe_combo("ctrl+shift+m") == "Ctrl + Shift + M"


def test_describe_action_includes_the_interesting_parameter():
    assert describe_action("keyboard.combo", {"keys": "ctrl+p"}) == "Press Ctrl + P"
    assert describe_action("media.next") == "Next track"


def _runner() -> tuple[ActionRunner, KeySender, list]:
    sender = KeySender(enabled=False)  # records instead of injecting
    messages: list = []
    context = ActionContext(
        keys=sender, notify=lambda level, msg: messages.append((level, msg))
    )
    return ActionRunner(context), sender, messages


def test_running_a_media_action_sends_one_key():
    runner, sender, _ = _runner()
    assert runner.run("media.play_pause", {}) is True
    assert sender.simulated == [(VK["media_play_pause"],)]


def test_repeat_is_honoured_only_where_it_makes_sense():
    runner, sender, _ = _runner()
    runner.run("media.next", {}, repeat=3)
    assert len(sender.simulated) == 3

    runner, sender, _ = _runner()
    runner.run("media.play_pause", {}, repeat=3)  # a toggle must not repeat
    assert len(sender.simulated) == 1


def test_repeat_is_capped():
    runner, sender, _ = _runner()
    runner.run("media.next", {}, repeat=999)
    assert len(sender.simulated) == runner.max_repeat


def test_the_none_action_does_nothing_and_succeeds():
    runner, sender, _ = _runner()
    assert runner.run(ACTION_NONE, {}) is True
    assert sender.simulated == []


def test_a_broken_binding_reports_instead_of_raising():
    runner, _, messages = _runner()
    assert runner.run("keyboard.combo", {"keys": "ctrl+banana"}) is False
    assert runner.failed == 1
    assert messages and messages[0][0] == "error"


# ---------------------------------------------------------------- config --

def test_save_and_load_round_trip(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    config = AppConfig()
    config.settings.mappings_enabled = False
    config.settings.low_battery_threshold = 33
    config.profile.bind(InputId.MIC_BUTTON, "keyboard.combo", {"keys": "ctrl+m"})

    assert store.save(config) is True
    loaded = ConfigStore(tmp_path / "config.json").load()
    assert loaded.settings.mappings_enabled is False
    assert loaded.settings.low_battery_threshold == 33
    assert loaded.profile.get(InputId.MIC_BUTTON).params["keys"] == "ctrl+m"


def test_missing_file_yields_defaults(tmp_path):
    loaded = ConfigStore(tmp_path / "absent.json").load()
    assert loaded.profile.get(InputId.VSS_BUTTON).action_id == "media.play_pause"


def test_corrupt_file_is_quarantined_and_defaults_load(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{ this is not json", encoding="utf-8")
    store = ConfigStore(path)
    loaded = store.load()

    assert store.last_error
    assert len(loaded.profile) > 0  # the app still starts
    assert list(tmp_path.glob("config.broken-*.json"))  # the original is preserved


def test_out_of_range_settings_are_clamped():
    settings = Settings(low_battery_threshold=9999, resync_threshold=-4,
                        debounce_seconds=99.0).clamped()
    assert settings.low_battery_threshold == 50
    assert settings.resync_threshold == 1
    assert settings.debounce_seconds == 1.0


def test_unknown_setting_keys_are_ignored():
    settings = Settings.from_dict({"mappings_enabled": False, "from_the_future": 1})
    assert settings.mappings_enabled is False


def test_export_and_import_a_profile(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    profile = Profile.default()
    profile.bind(InputId.MIC_BUTTON, "media.stop")
    target = tmp_path / "bindings.json"

    assert store.export_profile(profile, target) is True
    imported = store.import_profile(target)
    assert imported is not None
    assert imported.get(InputId.MIC_BUTTON).action_id == "media.stop"


def test_importing_rubbish_returns_none(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("[]", encoding="utf-8")
    assert ConfigStore(tmp_path / "c.json").import_profile(path) is None
