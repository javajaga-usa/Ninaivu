"""How busy this computer is with work that is not Ninaivu's.

The daily catch-up (server/catchup.py) runs the heavy analysis only while the
machine is idle. The processor's own reading cannot say that once the
catch-up has started: the analysis fills every core, and a machine busy with
Ninaivu's own faces and text would look busy, stop the work, look idle again a
minute later, and start it again, all night. So what is measured here is the
*rest*: the processor time every program used, less what Ninaivu's server and
its worker processes (the job engine's, the model host's) used in the same
seconds, as a share of every core.

Read from the totals the system keeps (``psutil``), sampled no more often than
every few seconds, whoever asks. Each process of Ninaivu's is counted by its
own total since the last sample, so a worker that ends between two samples
loses only its last few seconds instead of making its whole lifetime look like
somebody else's. Without ``psutil`` nothing can be measured, and the machine
is taken to be idle: the rules about people using the app still hold.

Ninaivu's own short programs (ffmpeg and ffprobe for a video's moments and
covers) start and end between two samples and were never seen at all: with
the job engine on, most of what they used read as other programs, enough to
push the catch-up past its bar to stop (audit D-18). On a Mac and Linux each
process also keeps what the children it waited for used, which is where that
time ends up, and it is credited to Ninaivu too, less what was already
counted while a process now gone was seen running. Windows keeps no such
total, and there it is as before.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from typing import Any, Callable

__all__ = ["OtherLoad", "measure"]

#: The least time between two samples, however often somebody asks.
SAMPLE_SECONDS = 5.0
#: How much history is kept.
KEEP_SECONDS = 15 * 60.0


def measure() -> tuple[float, dict[tuple[int, float], Any], int] | None:
    """One reading: the processor seconds every program has used (all cores
    together), each of Ninaivu's processes' own total by (pid, start time)
    with what the children it waited for used, and the number of cores. None
    where it cannot be read."""
    try:
        import psutil                                        # noqa: PLC0415
    except ImportError:
        return None
    try:
        times = psutil.cpu_times()
        idle = float(getattr(times, "idle", 0.0)) + float(getattr(times, "iowait", 0.0))
        # guest and guest_nice are already inside user and nice on Linux.
        total = sum(float(getattr(times, name, 0.0)) for name in times._fields
                    if name not in ("guest", "guest_nice"))
        own: dict[tuple[int, float], Any] = {}
        me = psutil.Process()
        for process in [me, *me.children(recursive=True)]:
            try:
                with process.oneshot():
                    used = process.cpu_times()
                    reaped = (float(getattr(used, "children_user", 0.0))
                              + float(getattr(used, "children_system", 0.0)))
                    own[(process.pid, process.create_time())] = (
                        float(used.user) + float(used.system), reaped)
            except (psutil.Error, OSError):
                continue                                     # ended while it was read
        return total - idle, own, int(psutil.cpu_count() or os.cpu_count() or 1)
    except Exception:                                        # noqa: BLE001 - no reading
        return None


def _pair(used: Any) -> tuple[float, float]:
    """A process's own seconds and its waited-for children's, from a reading
    (a bare number is a process with no children total)."""
    if isinstance(used, (tuple, list)):
        return float(used[0]), float(used[1] if len(used) > 1 else 0.0)
    return float(used), 0.0


class OtherLoad:
    """The share of the computer used by programs other than Ninaivu, over a
    recent stretch of time."""

    def __init__(self, sample: Callable[[], Any] = measure,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._sample = sample
        self._clock = clock
        self._lock = threading.Lock()
        #: (when, busy seconds of every program, Ninaivu's own seconds so far, cores)
        self._history: deque[tuple[float, float, float, int]] = deque()
        self._own: dict[Any, tuple[float, float]] = {}
        self._own_total = 0.0
        self._last = -1e18
        self.measurable = True

    def look(self) -> None:
        """Take a sample if the last one is old enough."""
        now = self._clock()
        with self._lock:
            if now - self._last < SAMPLE_SECONDS:
                return
            self._last = now
            reading = self._sample()
            if reading is None:
                self.measurable = False
                return
            self.measurable = True
            busy, own, cores = reading
            own = {key: _pair(used) for key, used in own.items()}
            reaped = 0.0
            for key, (used, children) in own.items():
                # A process seen for the first time counts all it has used:
                # it started since the last sample, or this is the first one,
                # where only differences are ever read.
                before, before_children = self._own.get(key, (0.0, 0.0))
                self._own_total += max(0.0, used - before)
                reaped += max(0.0, children - before_children)
            # What the processes that ended used is now in their parent's
            # children total, the part already counted while they ran too.
            counted = sum(used + children for key, (used, children) in self._own.items()
                          if key not in own)
            self._own_total += max(0.0, reaped - counted)
            self._own = own
            self._history.append((now, float(busy), self._own_total, max(1, int(cores))))
            while self._history and now - self._history[0][0] > KEEP_SECONDS:
                self._history.popleft()

    def share(self, seconds: float) -> float | None:
        """Others' share of every core over (at least) the last *seconds*, from
        0 to 1; None until there is that much history."""
        self.look()
        with self._lock:
            if len(self._history) < 2:
                return None
            newest = self._history[-1]
            older = None
            for entry in reversed(self._history):
                if newest[0] - entry[0] >= seconds:
                    older = entry
                    break
            if older is None:
                return None
            span = newest[0] - older[0]
            others = (newest[1] - older[1]) - (newest[2] - older[2])
            return min(1.0, max(0.0, others / (span * newest[3])))
