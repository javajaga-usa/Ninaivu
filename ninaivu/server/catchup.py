"""The daily catch-up: the heavy work started once a day, while the computer is idle.

With ``daily_catchup`` on, the household's Workload (server/workload.py) holds
the analysis outside one stretch of the day, and inside it whenever somebody
is using the app or the computer is busy with other work. Holding is half of
it: a scan that is already waiting carries on by itself when its turn comes.
The other half is here. Work can be owed with no scan waiting for it — faces
and words a stopped or restarted scan left, photographs never surveyed for
which way up they go, people found and never grouped — and nothing would
start it before the next photograph arrives. So once the stretch is open and
the computer is idle, this starts:

* **the analysis a library owes**, read the way start-up reads it
  (Services._analysis_owed and _passes_owed), as a scan that carries on the
  analysis without walking the library again. Once a stretch: a scan that
  makes way for somebody waits inside itself and carries on, and one somebody
  stopped is not started again until the next day.
* then **the straightening survey**, as the one after a scan would start
  (Straightener.after_scan), when that is switched on. It makes way by ending,
  as it holds the indexer aside while it runs, so it is started again when
  the computer is idle again, unless somebody pressed Stop on it.

Ninaivu cannot wake a sleeping computer, and does not change how it sleeps.
On a Mac that sleeps, the stretch only comes if macOS wakes it first: since
macOS 13 that is ``sudo pmset repeat wakeorpoweron MTWRFSU 01:55:00`` in
Terminal (a few minutes before the stretch; System Settings no longer has a
schedule), with Ninaivu set to start at log-in. Once awake, Ninaivu keeps the
system from sleeping while it serves (utils/awake.py).
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

__all__ = ["DailyCatchUp"]

#: How often it looks.
LOOK_SECONDS = 30.0


class DailyCatchUp:
    """Starts the day's owed work in the catch-up's stretch."""

    def __init__(self, cfg: Any, workload: Any, scanner: Any, straightener: Any,
                 owed: Callable[[], list[str]],
                 clock: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self.workload = workload
        self.scanner = scanner
        self.straightener = straightener
        #: The library folders that owe analysis now.
        self._owed = owed
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: The day whose stretch has had its scan started.
        self._scanned_for = ""
        #: What the last look did, for the tests and the log.
        self.last = ""

    def keep(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="ninaivu-daily-catchup",
                                        daemon=True)
        self._thread.start()

    def stop(self, join: bool = False, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if join and thread is not None and thread.is_alive():
            thread.join(timeout)

    def _run(self) -> None:
        while not self._stop.wait(LOOK_SECONDS):
            try:
                self.look()
            except Exception:                                # noqa: BLE001 - next look
                log.exception("the daily catch-up could not look for work")

    def _opened_at(self, window: Any) -> float:
        """When the stretch that is open now opened (wall clock)."""
        now = self._clock()
        local = time.localtime(now)
        minute = local.tm_hour * 60 + local.tm_min
        start = window.start if window.start is not None else 0
        return now - ((minute - start) % (24 * 60)) * 60 - local.tm_sec

    def look(self) -> str:
        """Start what is owed, if this is the time for it. Says what it did."""
        self.last = self._look()
        return self.last

    def _look(self) -> str:
        workload = self.workload
        if not workload.catchup_on:
            return "off"
        window = workload.catchup_window()
        if not window.is_open(self._clock()):
            return "closed"
        reason = workload.catchup_hold()
        if reason:
            return "held"
        if self._scan_going():
            return "scanning"
        opened = self._opened_at(window)
        day = time.strftime("%Y-%m-%d %H:%M", time.localtime(opened))
        if self._scanned_for != day:
            self._scanned_for = day
            roots = [str(root) for root in self._owed()]
            if roots:
                log.info("daily catch-up: carrying on the analysis of %s", ", ".join(roots))
                self.scanner.start(roots, scopes={Path(root): frozenset() for root in roots})
                return "scan"
        straightener = self.straightener
        if straightener is None or straightener.running:
            return "nothing"
        if straightener.stopped_by_somebody_at >= opened:
            return "nothing"                 # somebody stopped it today
        roots = [str(root) for root in (self.cfg.libraries
                                        or ([self.cfg.active_root] if self.cfg.active_root
                                            else []))]
        if roots and straightener.after_scan(roots):
            log.info("daily catch-up: looking for photographs that are the wrong way up")
            return "straighten"
        return "nothing"

    def _scan_going(self) -> bool:
        scanner = self.scanner
        thread = getattr(scanner, "_thread", None)
        return bool(getattr(scanner, "running", False)
                    or (thread is not None and thread.is_alive()))
