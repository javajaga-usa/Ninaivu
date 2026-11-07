"""One place that owns Ninaivu's background loops (4.0 proposal 2, step 1).

Ninaivu runs about eighty named threads. Each long-lived loop caught its own
errors, in its own way, and some caught too little: on 5 November 2026 two of
the day's fixes were loops that ended for good on one unexpected answer
(the Pi's check on whether the mini was awake, and the archive's daily health
check), with nothing on any screen to say so.

This first step changes nothing a loop does. Each loop is handed to
:func:`keep_running` exactly as it was written, and the supervisor:

* runs it on a thread of the same name, so the logs read as before;
* if it raises, logs the error once with its traceback, waits (5 s, then
  doubling up to 10 minutes) and runs it again, unless its owner has been
  told to stop in the meantime;
* treats a normal return as the loop's own decision to finish;
* keeps a record of every job it has run, for :func:`jobs`.

Later steps (see the 4.0 proposals) move the schedules, the resource claims
and the compute into the supervisor too; the names given here become the job
names then.

Standard library only: ``ninaivu/cli/offsite_restore.py`` runs from a bare
copy of the source with no other packages, and the off-site copy imports this.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)

#: The first wait after a loop raises, and the longest one.
RETRY_FIRST = 5.0
RETRY_LONGEST = 600.0


@dataclass
class Job:
    """What the supervisor knows about one background loop."""

    name: str
    state: str = "starting"            # running, waiting, finished, stopped
    started_at: float = 0.0
    restarts: int = 0
    last_error: str = ""
    last_error_at: float = 0.0
    thread: threading.Thread | None = field(default=None, repr=False)

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "state": self.state, "started_at": self.started_at,
                "restarts": self.restarts, "last_error": self.last_error,
                "last_error_at": self.last_error_at,
                "alive": bool(self.thread is not None and self.thread.is_alive())}


_lock = threading.Lock()
_jobs: dict[str, Job] = {}


def keep_running(name: str, loop: Callable[[], Any], *, stop: threading.Event,
                 daemon: bool = True, retry_first: float = RETRY_FIRST,
                 retry_longest: float = RETRY_LONGEST,
                 clock: Callable[[], float] = time.monotonic) -> threading.Thread:
    """Start *loop* on a thread called *name* and keep it running.

    *stop* is the owner's own stop event: once it is set, a loop that raised
    is not started again, and the wait before a retry ends at once. The
    thread is returned already started, so owners keep the ``is_alive()``
    checks they had.
    """
    job = Job(name=name)

    def run() -> None:
        wait = retry_first
        job.started_at = time.time()
        while True:
            job.state = "running"
            began = clock()
            try:
                loop()
            except Exception as exc:                       # noqa: BLE001 - the point of this
                job.restarts += 1
                job.last_error = f"{type(exc).__name__}: {exc}"[:500]
                job.last_error_at = time.time()
                # A loop that ran a good while before it failed starts its
                # back-off again: one bad night is not a reason to wait ten
                # minutes after every later failure.
                if clock() - began >= retry_longest:
                    wait = retry_first
                if stop.is_set():
                    log.exception("%s stopped with an error while stopping", name)
                    job.state = "stopped"
                    return
                log.exception("%s stopped with an error; starting it again in %.0f s",
                              name, wait)
                job.state = "waiting"
                if stop.wait(wait):
                    job.state = "stopped"
                    return
                wait = min(wait * 2, retry_longest)
                continue
            job.state = "stopped" if stop.is_set() else "finished"
            return

    thread = threading.Thread(target=run, name=name, daemon=daemon)
    job.thread = thread
    with _lock:
        _jobs[name] = job
    thread.start()
    return thread


def jobs() -> list[dict[str, Any]]:
    """Every background loop the supervisor has started, by name."""
    with _lock:
        return [job.as_dict() for job in sorted(_jobs.values(), key=lambda j: j.name)]


def forget() -> None:
    """Drop the records (for tests)."""
    with _lock:
        _jobs.clear()


__all__ = ["Job", "keep_running", "jobs", "forget", "RETRY_FIRST", "RETRY_LONGEST"]
