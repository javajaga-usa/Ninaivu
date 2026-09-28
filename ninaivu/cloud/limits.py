"""How fast the upload may go, and when it is allowed to go at all.

A backup that saturates a household's broadband is not a background task, it is
an outage with a progress bar. The engine already sends one file at a time,
which keeps the connection usable rather than dead — but "usable" is not the
same as "capped", and on a metered line it is not the same as "affordable".

Two limits, kept here rather than in the engine because both are small,
arithmetical, and worth being able to test without a thread or a network.

**A ceiling.** :class:`RateLimiter` is a token bucket: it hands out permission
to send bytes at a fixed average rate and refuses to run ahead. Sitting on top
of it, :func:`chunk_for` shrinks the upload chunk so the ceiling is reached
smoothly instead of in bursts — a cap enforced by sending eight megabytes flat
out and then sleeping for a minute is the same outage, arriving in waves.

**A window.** :class:`Window` answers "may it run now?" and "how long until it
may?". Households on a slow line want the library to go up overnight, and the
useful shape of that is a start and an end in local time, crossing midnight,
with no calendar and no timezone arithmetic to get wrong.

Both are advisory to the engine and neither can hold a pause: every wait here
is broken into slices and every slice watches the stop event, so pressing Pause
during a four-hour wait for the window still takes effect in under a second.
"""

from __future__ import annotations

import threading
import time

__all__ = ["RateLimiter", "Window", "UploadPaused", "chunk_for", "STEP",
           "parse_clock", "format_clock"]

#: Google requires every chunk of a resumable upload but the last to be a
#: multiple of 256 KiB, so this is the unit any computed chunk size rounds to.
STEP = 256 * 1024

#: The longest any single wait blocks before looking at the stop event again.
#: Small enough that Pause feels immediate, large enough not to spin.
SLICE = 0.5

#: How much the bucket may save up, in seconds of transmission. One second
#: means a chunk that arrives late can catch up, without ever letting the
#: connection go flat out for long enough to be noticed in the next room.
BURST_SECONDS = 1.0

MINUTES_PER_DAY = 24 * 60


class UploadPaused(Exception):
    """Raised out of an upload that was stopped part-way on purpose.

    Deliberately not a :class:`~ninaivu.cloud.drive.DriveError`. Nothing went
    wrong: the household asked for the upload to stop, or the window it was
    allowed to run in closed. Sharing an exception type with real failures is
    how a pause ends up counted as an attempt, five pauses turn a file into a
    failure, and the console reports a fault where there was none.
    """


# ---------------------------------------------------------------------------
# A ceiling
# ---------------------------------------------------------------------------

class RateLimiter:
    """A token bucket over bytes. Blocks the caller rather than dropping work.

    ``bytes_per_sec`` of zero or less means no limit at all, and
    :meth:`take` becomes free — the uncapped path costs one comparison, so
    there is no reason for the engine to keep two code paths.
    """

    def __init__(self, bytes_per_sec: float, *,
                 stop: threading.Event | None = None,
                 burst_seconds: float = BURST_SECONDS):
        self.rate = max(0.0, float(bytes_per_sec or 0))
        self.capacity = self.rate * max(0.1, burst_seconds)
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._stop = stop
        #: Several uploads take from one bucket at once.
        self._lock = threading.Lock()

    @property
    def limited(self) -> bool:
        return self.rate > 0

    def take(self, count: int) -> None:
        """Wait until ``count`` bytes may be sent, then account for them.

        A request larger than the whole bucket is not an error and does not
        deadlock: the wait is for a full bucket and the balance is allowed to
        go negative, which the refill then works off. That keeps the *average*
        at the ceiling even when the chunk size is coarser than the cap.
        """
        if not self.limited or count <= 0:
            return
        needed = min(float(count), self.capacity)
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= needed:
                    self._tokens -= float(count)
                    return
                delay = (needed - self._tokens) / self.rate
            if self._wait(min(delay, SLICE)):
                # Stopped. The caller checks the stop event itself and will
                # raise; there is nothing to be gained by holding it here.
                return

    def _refill(self) -> None:
        now = time.monotonic()
        self._tokens = min(self.capacity,
                           self._tokens + (now - self._updated) * self.rate)
        self._updated = now

    def _wait(self, seconds: float) -> bool:
        """Sleep. True if it was cut short by the stop event."""
        if self._stop is not None:
            return self._stop.wait(seconds)
        time.sleep(seconds)
        return False


def chunk_for(bytes_per_sec: float, default: int) -> int:
    """The chunk size to use under a given ceiling.

    About a second of data, rounded down to Google's 256 KiB unit, never above
    the default and never below one unit. The point is smoothness: with the
    default eight-megabyte chunk and a 500 KB/s cap, every chunk would go out
    at line speed and then the limiter would sleep for a quarter of a minute,
    which is a cap on the average and no cap at all on what the household
    experiences.
    """
    if not bytes_per_sec or bytes_per_sec <= 0:
        return default
    steps = int(float(bytes_per_sec) * BURST_SECONDS // STEP)
    return min(default, max(1, steps) * STEP)


# ---------------------------------------------------------------------------
# A window
# ---------------------------------------------------------------------------

def parse_clock(text: str | None) -> int | None:
    """``"23:30"`` to minutes past midnight. ``None`` if it is not a time.

    Unparseable is ``None`` rather than an exception or a guess, because the
    only caller is a window that treats "no time" as "no restriction". A typo
    in a settings field should leave the upload unrestricted and visibly so,
    not stop it at a time nobody chose.
    """
    if text is None:
        return None
    raw = str(text).strip()
    if not raw or ":" not in raw:
        return None
    hours, _, minutes = raw.partition(":")
    try:
        hour, minute = int(hours), int(minutes)
    except ValueError:
        return None
    if not (0 <= hour <= 24 and 0 <= minute < 60):
        return None
    # 24:00 is a way people write midnight at the end of a day; keep it.
    return (hour * 60 + minute) % MINUTES_PER_DAY


def format_clock(minutes: int | None) -> str:
    if minutes is None:
        return ""
    minutes %= MINUTES_PER_DAY
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


class Window:
    """The hours the upload is allowed to run, in local time.

    An empty or unparseable start or end means no window: always open. A start
    later than the end crosses midnight, which is the case this exists for —
    ``22:00`` to ``07:00`` is what "overnight" means and it is not a special
    case to be handled elsewhere.

    A start equal to the end is read as always open rather than never open.
    "23:00 to 23:00" is somebody who has not finished typing, and a backup
    that silently never runs again is the worse of the two readings.
    """

    def __init__(self, start: str | None = "", end: str | None = ""):
        self.start = parse_clock(start)
        self.end = parse_clock(end)

    @property
    def always_open(self) -> bool:
        return (self.start is None or self.end is None
                or self.start == self.end)

    def is_open(self, now: float | None = None) -> bool:
        if self.always_open:
            return True
        minute = self._minute_of_day(now)
        if self.start < self.end:
            return self.start <= minute < self.end
        return minute >= self.start or minute < self.end

    def seconds_until_open(self, now: float | None = None) -> float:
        """How long until it may run. Zero if it may run now."""
        if self.always_open or self.is_open(now):
            return 0.0
        stamp = time.time() if now is None else now
        local = time.localtime(stamp)
        minute = local.tm_hour * 60 + local.tm_min
        ahead = (self.start - minute) % MINUTES_PER_DAY
        return max(0.0, ahead * 60 - local.tm_sec)

    def opens_at(self, now: float | None = None) -> float:
        """Wall-clock time it next opens, or 0 when there is no restriction."""
        if self.always_open:
            return 0.0
        stamp = time.time() if now is None else now
        return stamp + self.seconds_until_open(stamp)

    def label(self) -> str:
        if self.always_open:
            return ""
        return f"{format_clock(self.start)}–{format_clock(self.end)}"

    def _minute_of_day(self, now: float | None) -> int:
        local = time.localtime(time.time() if now is None else now)
        return local.tm_hour * 60 + local.tm_min

    def __repr__(self) -> str:                              # pragma: no cover
        return f"Window({self.label() or 'always open'})"
