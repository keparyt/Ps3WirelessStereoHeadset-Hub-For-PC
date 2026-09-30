"""Update checking against the project's GitHub Releases.

The Hub asks GitHub for the latest published release at most once a day
(and never blocks the UI: the request runs on a daemon thread, the answer
is delivered to a callback on the caller's terms). A release is *newer*
when its tag compares greater than ``APP_VERSION``; draft releases are
ignored, as are malformed tags.

``APP_VERSION`` is dotted (``1.2.10``) while tags carry a ``v`` prefix
(``v1.2.10``); both normalise to integer tuples, compared piecewise, so
``v1.10.0`` correctly beats ``v1.9.9`` - a plain string comparison would
get that wrong.

The result is cached in memory for a day: restarting the app always
re-checks, but flipping to the Settings page fifty times costs one HTTP
request. Failures are silent by design - a user who is offline or behind
a corporate proxy must never see an error about an update check.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import APP_VERSION

log = logging.getLogger("ps3hub.updates")

#: keploy/Ps3WirelessStereoHeadset-Hub-For-PC's latest-release endpoint.
RELEASES_URL = ("https://api.github.com/repos/keparyt/"
                "Ps3WirelessStereoHeadset-Hub-For-PC/releases/latest")
#: Human landing page for the download button.
RELEASES_PAGE = ("https://github.com/keparyt/"
                 "Ps3WirelessStereoHeadset-Hub-For-PC/releases/latest")
#: Ask GitHub at most this often (seconds) while the app stays open.
CHECK_INTERVAL = 24 * 3600
#: Network timeout - short, because this is a background nicety.
TIMEOUT = 8.0
_TAG_RE = re.compile(r"v?(\d+(?:\.\d+)*)")


@dataclass(frozen=True)
class UpdateInfo:
    """What the checker reports when a newer release exists."""

    version: str        # dotted, e.g. "1.3.0"
    url: str            # the release page for the download button

    @property
    def label(self) -> str:
        return f"Version {self.version} is available"


def parse_version(text: str) -> tuple[int, ...] | None:
    """``v1.2.10`` / ``1.2.10`` / ``v1.3`` -> integer tuple; else None.

    Anything after the numeric core (build metadata, release-candidate
    suffixes) is ignored for comparison purposes.
    """
    match = _TAG_RE.search(str(text or ""))
    if not match:
        return None
    try:
        return tuple(int(part) for part in match.group(1).split("."))
    except ValueError:
        return None


def is_newer(remote: str, local: str = APP_VERSION) -> bool:
    """Whether the remote tag/version is strictly newer than ``local``."""
    remote_parts = parse_version(remote)
    local_parts = parse_version(local)
    if remote_parts is None or local_parts is None:
        return False
    width = max(len(remote_parts), len(local_parts))
    remote_parts += (0,) * (width - len(remote_parts))
    local_parts += (0,) * (width - len(local_parts))
    return remote_parts > local_parts


def fetch_latest(timeout: float = TIMEOUT) -> UpdateInfo | None:
    """Ask GitHub about the latest release. None = up to date / unknown.

    Synchronous and network-bound: UI code should use
    :class:`UpdateChecker`, which moves this onto a thread.
    """
    try:
        request = urllib.request.Request(
            RELEASES_URL,
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": "PS3HeadsetHub-update-check"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.debug("Update check skipped: %s", exc)
        return None
    if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
        return None
    tag = str(data.get("tag_name", ""))
    if not is_newer(tag):
        return None
    parts = parse_version(tag)
    version = ".".join(str(p) for p in parts) if parts else tag.lstrip("v")
    return UpdateInfo(
        version=version,
        url=str(data.get("html_url") or RELEASES_PAGE),
    )


class UpdateChecker:
    """Throttled background checker; one request per day per process."""

    def __init__(self, fetch=fetch_latest, interval: float = CHECK_INTERVAL) -> None:
        self._fetch = fetch
        self._interval = interval
        self._lock = threading.Lock()
        self._last_check = 0.0
        self._last_result: UpdateInfo | None = None
        self._in_flight = False

    @property
    def last_result(self) -> UpdateInfo | None:
        with self._lock:
            return self._last_result

    def check(self, force: bool = False,
              on_done=None) -> UpdateInfo | None:
        """Return a cached result, or start a background check.

        Without ``force``, at most one network request per ``interval`` is
        made; calls inside the window return the cached answer (None when
        nothing newer was found). ``on_done(info)`` fires on the worker
        thread when a forced/background check completes.
        """
        with self._lock:
            now = time.monotonic()
            if not force and (now - self._last_check) < self._interval:
                return self._last_result
            if self._in_flight:
                return self._last_result
            self._in_flight = True
            self._last_check = now

        def _worker() -> None:
            try:
                info = self._fetch()
            except Exception:
                log.debug("Update check failed", exc_info=True)
                info = None
            with self._lock:
                self._last_result = info
                self._in_flight = False
                self._last_check = time.monotonic()
            if on_done is not None:
                try:
                    on_done(info)
                except Exception:
                    log.debug("Update callback failed", exc_info=True)

        threading.Thread(target=_worker, name="update-check",
                         daemon=True).start()
        return self._last_result
