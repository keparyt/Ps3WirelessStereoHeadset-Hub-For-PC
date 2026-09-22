"""Desktop notifications.

The notifier must stay quiet, cheap and harmless on machines that cannot show
a toast, and must never block or raise on the thread that asks for one.
"""

from ps3hub.notify import (
    MESSAGE_LIMIT, TITLE_LIMIT, DesktopNotifier, _clip, _level_flag,
)


# ------------------------------------------------------------ level flags --

def test_levels_map_onto_balloon_icons():
    assert _level_flag("info") == _level_flag("ok") == _level_flag("notice")
    assert _level_flag("warn") == _level_flag("warning")
    assert _level_flag("error") == _level_flag("fault")
    assert _level_flag("warn") != _level_flag("error")
    # An unknown tone must not crash; it degrades to the info icon.
    assert _level_flag("nonsense") == _level_flag("info")
    assert _level_flag("") == _level_flag("info")


# ----------------------------------------------------------------- limits --

def test_titles_and_bodies_are_clipped_to_win32_buffers():
    assert _clip("  hello  ", 5) == "hello"
    assert _clip(None, 10) == ""
    assert len(_clip("x" * 500, TITLE_LIMIT)) == TITLE_LIMIT
    assert len(_clip("x" * 500, MESSAGE_LIMIT)) == MESSAGE_LIMIT


# ------------------------------------------------------------------ queue --

def test_disabled_notifier_records_what_it_would_have_shown():
    notifier = DesktopNotifier(enabled=False)
    assert notifier.show("Battery", "5% left", "warn") is False
    assert notifier.skipped == 1
    assert notifier.sent == 0
    assert notifier.show("Battery", "5% left", "warn") is False
    assert notifier.skipped == 2


def test_show_never_raises_even_with_garbage_arguments():
    notifier = DesktopNotifier(enabled=False)
    # None titles and bodies are tolerated by the clipping helper.
    assert notifier.show(None, None, None) is False  # type: ignore[arg-type]


def test_enabled_flag_can_be_flipped_after_construction():
    notifier = DesktopNotifier(enabled=False)
    assert notifier.show("a", "b") is False
    notifier.enabled = True  # the app never does this, but it must not break
    assert notifier.show("a", "b") is True
    assert notifier.shutdown() is None
