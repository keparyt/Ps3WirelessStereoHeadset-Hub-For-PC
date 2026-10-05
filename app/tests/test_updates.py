"""Update-check logic: version comparison, tag parsing, throttling.

Network access is stubbed out - the fetcher is a callable the tests
control, so these run offline and deterministically.
"""

from __future__ import annotations

import threading
import time

from ps3hub.updates import (
    UpdateChecker,
    UpdateInfo,
    fetch_latest,
    is_newer,
    parse_version,
)


# ------------------------------------------------------------ versioning --

def test_parse_version_strips_the_v_prefix_and_suffixes():
    assert parse_version("v1.2.10") == (1, 2, 10)
    assert parse_version("1.2.10") == (1, 2, 10)
    assert parse_version("v1.3.0-rc1") == (1, 3, 0)
    assert parse_version("release-2.0") == (2, 0)
    assert parse_version("garbage") is None
    assert parse_version("") is None
    assert parse_version(None) is None


def test_is_newer_compares_numerically_not_as_text():
    assert is_newer("v1.2.11", "1.2.10")
    assert is_newer("v1.10.0", "v1.9.9")      # string compare would fail this
    assert is_newer("v2.0", "v1.9.9")         # short tuple pads with zeros
    assert is_newer("v1.2.10.1", "v1.2.10")
    assert not is_newer("v1.2.10", "v1.2.10")
    assert not is_newer("v1.2.09", "v1.2.10")
    assert not is_newer("v0.9", "v1.2.10")
    assert not is_newer("garbage", "1.2.10")
    assert not is_newer("v1.2.11", "not-a-version")


# ----------------------------------------------------------------- fetch --

class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return self._payload


def _release(tag: str, draft: bool = False, prerelease: bool = False) -> bytes:
    import json
    return json.dumps({
        "tag_name": tag, "draft": draft, "prerelease": prerelease,
        "html_url": f"https://example.com/releases/tag/{tag}",
    }).encode("utf-8")


def test_fetch_latest_returns_newer_release(monkeypatch):
    import urllib.request
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda _req, timeout=None: _FakeResponse(_release("v1.99.0")))
    info = fetch_latest()
    assert info is not None
    assert info.version == "1.99.0"
    assert info.url == "https://example.com/releases/tag/v1.99.0"
    assert info.label == "Version 1.99.0 is available"


def test_fetch_latest_ignores_same_older_draft_and_prerelease(monkeypatch):
    import urllib.request
    for tag, kw in (("v1.2.11", {}), ("v1.2.10", {}), ("v1.3.0", {"draft": True}),
                    ("v1.3.0", {"prerelease": True})):
        monkeypatch.setattr(
            urllib.request, "urlopen",
            lambda _req, timeout=None, _tag=tag, _kw=kw: _FakeResponse(
                _release(_tag, **_kw)))
        assert fetch_latest() is None, tag


def test_fetch_latest_survives_a_network_error(monkeypatch):
    import urllib.error
    import urllib.request

    def boom(_req, timeout=None):
        raise urllib.error.URLError("no network")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert fetch_latest(timeout=0.1) is None


# --------------------------------------------------------------- checker --

def _worker_position() -> str:
    """File:line the 'update-check' thread is sitting at right now."""
    import sys
    by_ident = {t.ident: t for t in threading.enumerate()}
    for tid, frame in sys._current_frames().items():
        thread = by_ident.get(tid)
        if thread is not None and thread.name == "update-check":
            chain = []
            while frame is not None:
                chain.append(f"{frame.f_code.co_filename}:"
                             f"{frame.f_lineno} in {frame.f_code.co_name}")
                frame = frame.f_back
            return " <- ".join(chain)
    return "thread-not-found"


def test_a_fresh_checker_is_not_throttled_by_boot_time():
    # time.monotonic() on Windows counts from boot; a fresh CI runner (or a
    # just-rebooted PC) is often up for less than the 24 h interval, and a
    # 0.0 initialization would read as "checked moments ago".
    checker = UpdateChecker(fetch=lambda: None, interval=3600)
    assert time.monotonic() - checker._last_check >= checker._interval


def test_checker_throttles_to_one_fetch_per_interval():
    calls = []

    def fake_fetch():
        calls.append(1)
        return UpdateInfo(version="9.9.9", url="https://example.com")

    checker = UpdateChecker(fetch=fake_fetch, interval=3600)
    first = checker.check()
    # Wait for the background worker to finish (result stored), not merely
    # to have started: on a loaded machine the thread can be preempted
    # between running the fetch and publishing the result.
    deadline = time.monotonic() + 10
    while checker.last_result is None and time.monotonic() < deadline:
        time.sleep(0.01)
    # A second check inside the throttle window returns the cached answer
    # without a new fetch.
    assert checker.check() is not None, (
        f"calls={calls} last={checker.last_result} "
        f"in_flight={checker._in_flight} worker_at={_worker_position()}")
    assert len(calls) == 1
    assert first is None or isinstance(first, UpdateInfo)


def test_checker_force_bypasses_the_throttle():
    calls = []

    def fake_fetch():
        calls.append(1)
        return None

    checker = UpdateChecker(fetch=fake_fetch, interval=3600)
    checker.check()
    # Let the first worker run to completion, otherwise the in-flight guard
    # swallows the forced check and the second fetch never happens.
    deadline = time.monotonic() + 10
    while checker._in_flight and time.monotonic() < deadline:
        time.sleep(0.01)
    checker.check(force=True)
    deadline = time.monotonic() + 10
    while len(calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(calls) == 2, (
        f"calls={calls} in_flight={checker._in_flight} "
        f"worker_at={_worker_position()}")


def test_checker_callback_receives_the_result():
    import threading

    def fake_fetch():
        return UpdateInfo(version="2.0.0", url="https://example.com")

    checker = UpdateChecker(fetch=fake_fetch, interval=3600)
    seen = []
    done = threading.Event()

    def on_done(info):
        seen.append(info)
        done.set()

    checker.check(on_done=on_done)
    # The callback fires on the worker thread right after the result is
    # published; wait for that completion rather than a bare scheduling
    # hope, then give the callback its own generous window.
    deadline = time.monotonic() + 10
    while checker._in_flight and time.monotonic() < deadline:
        time.sleep(0.01)
    assert done.wait(10), (
        f"seen={seen} in_flight={checker._in_flight} "
        f"worker_at={_worker_position()}")
