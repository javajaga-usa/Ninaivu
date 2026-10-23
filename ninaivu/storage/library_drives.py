"""A library drive unplugged and plugged back in while Ninaivu runs.

Start-up scans and watches the library folders that are there, and only
those. One whose drive was unplugged at the time, or was pulled out and put
back afterwards, stayed unwatched and unread until the next restart or a
press of Rescan: the banner saying it was not connected went away when it came
back, and the photographs copied onto it meanwhile, on another computer or by
hand, did not appear. The folder watcher a drive had when it was pulled out
was left in place too, looking at a mount point that had gone.

This looks every :data:`LibraryDrives.EVERY` seconds. A library folder that
comes back is handed to ``on_back``, which watches it afresh and reads what
changed (the App: ``_library_back``). While one is away, the other disks are
looked over now and then for a folder carrying its id, so a Mac that mounts
the library's disk as ``/Volumes/Photos 1`` is noticed at once and the
console can ask whether that is the library (``library_id.notice_moves``),
rather than at the next restart.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from . import library_id
from . import roots as roots_kit

log = logging.getLogger(__name__)


class LibraryDrives:
    #: Seconds between two looks. A drive plugged back in is read about this
    #: long after it appears; asking whether a folder is there is one stat.
    EVERY = 15.0
    #: Seconds between two looks for a missing library on the other disks.
    MOVES_EVERY = 60.0

    def __init__(self, cfg: Any, on_back: Callable[[list[str]], None], *,
                 notice_moves: Callable[[Any], dict] = library_id.notice_moves,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.cfg = cfg
        self.on_back = on_back
        self.notice_moves = notice_moves
        self.clock = clock
        #: The library folders away at the last look; None before the first.
        self.away: set[str] | None = None
        self._moves_at = float("-inf")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ninaivu-library-drives",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout)

    def _loop(self) -> None:
        while not self._stop.wait(self.EVERY):
            try:
                self.check()
            except Exception:                               # noqa: BLE001
                log.exception("could not look at the library drives")

    def check(self) -> list[str]:
        """One look. Returns the library folders that came back since the last."""
        roots = [str(root) for root in self.cfg.roots]
        # An answer of "away" is not believed for its usual half a minute:
        # a drive plugged back in should be read now, not in thirty seconds.
        for root in self.away or ():
            roots_kit.forget(root)
        away = set(roots_kit.unavailable(roots))
        before, self.away = self.away, away
        if before is None:
            # The first look: start-up has just scanned what is here.
            back: list[str] = []
        else:
            back = [root for root in roots if root in before and root not in away]
            for root in roots:
                if root in away and root not in before:
                    log.warning("library folder %s is not connected; its photographs stay "
                                "listed and are read again when it is back", root)
        if back:
            log.info("library folder%s %s %s connected again; watching and reading "
                     "what changed", "s" if len(back) > 1 else "", ", ".join(back),
                     "are" if len(back) > 1 else "is")
            self.on_back(back)
        if away and self.clock() - self._moves_at >= self.MOVES_EVERY:
            self._moves_at = self.clock()
            self.notice_moves(self.cfg)
        return back
