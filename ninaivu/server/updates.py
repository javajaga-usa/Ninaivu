"""Is there a newer Ninaivu?

A self-hosted server that cannot say "0.2 is out" stays on the version it was
installed with. This asks GitHub's releases API — the project's own page —
once a day at most, and tells the console. What goes out is one plain GET
with no identifier of any kind: no version, no machine, no cookie. The
answer is remembered in memory and forgotten at the next start.

It is off until the household turns it on (``Config.update_check``, the
Server page): the request carries nothing, but "nothing leaves the house
unless somebody asked" is a promise worth keeping literally. The tray's
*Check for an update* asks once, on request. Nothing is ever downloaded or installed from
here; the console shows the version and a link, and that is all.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.request
from typing import Any, Callable

log = logging.getLogger(__name__)

RELEASES_URL = "https://api.github.com/repos/javajaga-usa/Ninaivu/releases/latest"
EVERY = 24 * 3600
_VERSION = re.compile(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?")


def parse_version(text: str) -> tuple[int, int, int]:
    """'v0.2.1' → (0, 2, 1); anything unreadable → (0, 0, 0)."""
    m = _VERSION.search(str(text or ""))
    if not m:
        return (0, 0, 0)
    return tuple(int(g or 0) for g in m.groups())  # type: ignore[return-value]


def newer(latest: str, current: str) -> bool:
    return parse_version(latest) > parse_version(current)


def _fetch(url: str = RELEASES_URL, timeout: float = 8.0) -> dict[str, Any]:
    request = urllib.request.Request(
        url, headers={"Accept": "application/vnd.github+json",
                      # A generic agent: GitHub asks for one, and this says
                      # nothing about the machine.
                      "User-Agent": "Ninaivu-update-check"})
    with urllib.request.urlopen(request, timeout=timeout) as answer:
        return json.loads(answer.read(200_000).decode("utf-8", "replace"))


def check(current: str, fetch: Callable[[], dict[str, Any]] | None = None) -> dict[str, Any]:
    """One check, as a plain result the console can show."""
    result: dict[str, Any] = {"current": current, "latest": "", "url": "",
                              "available": False, "checked_at": time.time(), "error": ""}
    try:
        data = (fetch or _fetch)()
        tag = str(data.get("tag_name") or data.get("name") or "").strip()
        result["latest"] = tag.lstrip("vV")
        result["url"] = str(data.get("html_url") or "")
        result["available"] = bool(tag) and newer(tag, current)
    except Exception as exc:                            # noqa: BLE001 — offline is normal
        result["error"] = str(exc)[:200]
    return result


class UpdateChecker:
    """Asks once a day while the server runs; the answer is read from ``state``."""

    def __init__(self, cfg, current: str, fetch: Callable[[], dict[str, Any]] | None = None,
                 every: float = EVERY) -> None:
        self.cfg = cfg
        self.current = current
        self.fetch = fetch
        self.every = every
        self.state: dict[str, Any] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "update_check", False))

    def start(self) -> None:
        if self._thread is not None or not self.enabled:
            return
        self._thread = threading.Thread(target=self._loop, name="ninaivu-update-check", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def check_now(self) -> dict[str, Any]:
        self.state = check(self.current, self.fetch)
        if self.state["available"]:
            log.info("Ninaivu %s is available (this is %s): %s",
                     self.state["latest"], self.current, self.state["url"])
        return self.state

    def _loop(self) -> None:
        # A minute after start, not at once: the first minute is for the
        # library, and a server started twice in a row should not ask twice.
        if self._stop.wait(60):
            return
        while not self._stop.is_set():
            if self.enabled:
                self.check_now()
            if self._stop.wait(self.every):
                return

    def describe(self) -> dict[str, Any]:
        base = {"enabled": self.enabled, "current": self.current}
        return {**base, **(self.state or {})}
