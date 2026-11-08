"""One place that runs Ninaivu's background work: the supervisor.

Until 4.0 every keeper ran its own loop on its own thread: about eighty of
them, each with its own way of sleeping, catching errors and saying how far
it had got. Two of the fixes in the audit of 5 November 2026 were loops that
had died for good on one unexpected error, and stayed dead until the next
restart with nothing on the screen to say so.

A keeper now describes its work as a :class:`Job` (what to run, when, and
what it needs of the machine) and hands it to the :class:`Supervisor`, which:

* runs each job on its own named thread, as before, so a thread list or a log
  line still says who is busy;
* catches and logs anything a round raises, and tries again after a wait that
  grows while the failures go on, so a loop never dies;
* honours the job's claims with the library scan's own rules
  (``Scanner.defer`` / ``Scanner.resume``, docs/development/background-work.md):
  a job that reads the whole library or keeps the processors busy makes the
  scan stand down, a job that moves files waits while anything else that moves
  files holds the scan, and a job that only reads shares;
* stops everything within the budget it is given, and names what did not stop;
* keeps one list of what every job is doing, for the console
  (``GET /api/admin/jobs/background``).

The scan itself, the archive job, the job engine (ninaivu/engine) and the
model host (ninaivu/runtime/host.py) are not jobs here yet. They are moving
over one at a time, each with its behaviour unchanged.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)

# -- what a job may need of the machine ---------------------------------------

#: The disk the library's originals are on.
LIBRARY_DISK = "library_disk"
#: The archive drive the consolidation writes to.
ARCHIVE_DISK = "archive_disk"
#: Every core, for minutes or more (an AI pass, hashing every original).
CPU = "cpu"
#: Writing to the index in bulk.
INDEX_WRITE = "index_write"
#: Anything that leaves the house. Only ever where the administrator chose,
#: and encrypted; declared so the console can say who is talking.
NETWORK = "network"

CLAIMS = frozenset({LIBRARY_DISK, ARCHIVE_DISK, CPU, INDEX_WRITE, NETWORK})

#: The claims the library scan stands down for (Rule 1 of
#: background-work.md): the disk it walks and the cores it analyses with.
#: ``index_write`` and ``network`` are declared and shown, not arbitrated:
#: SQLite already lets one writer in at a time, and nothing else contends
#: for the network.
SCAN_YIELDS_TO = frozenset({LIBRARY_DISK, ARCHIVE_DISK, CPU})

#: How often a job that moves files looks again while something else that
#: moves files holds the scan.
CLAIM_LOOK_EVERY = 5.0

# -- when a job runs ------------------------------------------------------------

AT_START = "at start"
EVERY = "every"
ON_REQUEST = "on request"
AFTER = "after"


@dataclass(frozen=True)
class Trigger:
    """When a job's rounds happen. Made with :func:`at_start`, :func:`every`,
    :func:`on_request` or :func:`after`."""

    kind: str
    #: For ``every``: seconds between the end of one round and the start of
    #: the next, or something that says so, asked afresh every round (a
    #: setting the console can change while the job runs).
    seconds: float | Callable[[], float] = 0.0
    #: For ``every``: seconds before the first round. None waits one interval,
    #: which is what ``while not stop.wait(N): ...`` did.
    first: float | None = None
    #: For ``after``: the job whose finished rounds start this one.
    job: str = ""

    def interval(self) -> float:
        seconds = self.seconds() if callable(self.seconds) else self.seconds
        return max(0.0, float(seconds))

    def describe(self) -> str:
        if self.kind == EVERY:
            return f"every {self.interval():g} seconds"
        if self.kind == AFTER:
            return f"after {self.job}"
        return self.kind


def at_start() -> Trigger:
    """Once, as soon as the job is added, until a round finishes without error."""
    return Trigger(AT_START)


def every(seconds: float | Callable[[], float], *, first: float | None = None) -> Trigger:
    """A round, then *seconds* of rest, for as long as the job is kept."""
    return Trigger(EVERY, seconds=seconds, first=first)


def on_request() -> Trigger:
    """A round each time :meth:`Supervisor.request` asks; asks made while a
    round is under way come to one more round, not one each."""
    return Trigger(ON_REQUEST)


def after(job: str) -> Trigger:
    """A round each time the job named *job* finishes one without error."""
    return Trigger(AFTER, job=job)


@dataclass
class Job:
    """One piece of background work, as the supervisor sees it."""

    #: Also its thread's name, so the logs and a thread dump say who it is.
    name: str
    #: One round of the work. Raising is safe: it is logged and tried again.
    run: Callable[[], Any]
    trigger: Trigger
    #: What it needs of the machine, from :data:`CLAIMS`.
    claims: frozenset[str] = frozenset()
    #: Why it holds the scan, in the household's words, as the console shows
    #: it ("checking the library's storage"). Needed with a claim the scan
    #: stands down for.
    reason: str = ""
    #: True when it moves or renames files in the library or the archive.
    #: Such a job waits while anything else that moves files holds the scan;
    #: one that only reads shares with everybody.
    moves_files: bool = False
    #: Asks a round under way to stop where it can be picked up again. None
    #: when a round cannot be cut short: a stop then waits for it to finish.
    interrupt: Callable[[], Any] | None = None
    #: How far it has got, for the activity list: anything JSON can carry.
    progress: Callable[[], Any] | None = None
    #: Where its failures are written, so they read as they did before it
    #: moved here; and the words they are written with.
    log: logging.Logger | None = None
    failure: str = ""
    #: The wait after a first failure, doubled after each one that follows,
    #: up to ``retry_most``. A job on a schedule never comes back sooner than
    #: its schedule, so ``retry_most=0`` keeps it to exactly that.
    retry_first: float = 30.0
    retry_most: float = 600.0

    def __post_init__(self) -> None:
        self.claims = frozenset(self.claims)
        unknown = self.claims - CLAIMS
        if unknown:
            raise ValueError(f"{self.name}: no such claim {sorted(unknown)}")
        if self.yields_the_scan and not self.reason:
            raise ValueError(f"{self.name}: say why it holds the scan (reason)")
        if self.yields_the_scan and not self.moves_files:
            # A reader that is not on this list is taken for a mover by
            # everything that asks (an approval would wait hours for it).
            from ..media.scanner import READ_ONLY_CLAIMS             # noqa: PLC0415
            if self.reason not in READ_ONLY_CLAIMS:
                raise ValueError(f"{self.name}: a job that only reads holds the scan "
                                 f"with a claim in READ_ONLY_CLAIMS, not {self.reason!r}")
        if self.trigger.kind == AFTER and not self.trigger.job:
            raise ValueError(f"{self.name}: after which job?")

    @property
    def yields_the_scan(self) -> bool:
        return bool(self.claims & SCAN_YIELDS_TO)

    @property
    def stoppable(self) -> bool:
        return self.interrupt is not None


class Clock:
    """The time, and waiting for it. A test gives the supervisor one it
    turns by hand, so a schedule of a day is checked in a moment."""

    def now(self) -> float:
        return time.monotonic()

    def wait(self, wake: threading.Event, seconds: float | None) -> bool:
        """Wait *seconds* (None: for ever) or until *wake* is set. True if woken."""
        return wake.wait(None if seconds is None else max(0.0, seconds))


# -- the job's record -------------------------------------------------------------

#: What a job is doing, as the activity list says it.
IDLE = "waiting for its time"
WAITING = "waiting for the disk"
RUNNING = "running"
RETRYING = "trying again soon"
PAUSED = "paused"
DONE = "done"
STOPPED = "stopped"


@dataclass
class _Record:
    job: Job
    wake: threading.Event = field(default_factory=threading.Event)
    stopping: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    state: str = IDLE
    since: float = field(default_factory=time.time)
    last_error: str | None = None
    failures: int = 0
    next_at: float | None = None
    requested: bool = False
    paused: bool = False
    holding: bool = False

    @property
    def alive(self) -> bool:
        return self.thread is not None and self.thread.is_alive()


class Supervisor:
    """Owns the background jobs: runs, retries, arbitrates and stops them."""

    def __init__(self, scanner: Any = None, *, clock: Clock | None = None) -> None:
        #: The library scan whose claims are honoured; set once it exists.
        self.scanner = scanner
        self.clock = clock or Clock()
        self._lock = threading.Lock()
        # Taken around "is anything else moving files?" and the claim that
        # follows, so two of these jobs cannot both see the way clear.
        self._claim_lock = threading.Lock()
        self._jobs: dict[str, _Record] = {}
        self._closed = False

    # -- adding and asking ----------------------------------------------------

    def add(self, job: Job) -> bool:
        """Keep *job* from now on. False if one by that name is already kept.

        A job stopped with :meth:`stop_job` can be added again, as a keeper's
        ``start`` after its ``stop`` always could; one whose thread is still
        finishing its round is not doubled.
        """
        with self._lock:
            if self._closed:
                log.debug("not starting %s: Ninaivu is stopping", job.name)
                return False
            old = self._jobs.get(job.name)
            if old is not None and (old.alive or not old.stopping.is_set()):
                return False
            record = _Record(job)
            now = self.clock.now()
            if job.trigger.kind == EVERY:
                first = job.trigger.first
                record.next_at = now + (job.trigger.interval() if first is None else first)
            elif job.trigger.kind == AT_START:
                record.next_at = now
            self._jobs[job.name] = record
            if record.next_at is not None:
                self._launch(record)
        return True

    def request(self, name: str) -> bool:
        """Ask for a round of *name* now. False if no such job is kept."""
        with self._lock:
            record = self._jobs.get(name)
            if record is None or record.stopping.is_set() or self._closed:
                return False
            record.requested = True
            record.wake.set()
            if not record.alive:
                self._launch(record)
        return True

    def pause(self, name: str) -> bool:
        """Hold *name*: a round under way is asked to stop if it can, and no
        new one starts until :meth:`carry_on`."""
        with self._lock:
            record = self._jobs.get(name)
            if record is None:
                return False
            record.paused = True
            record.wake.set()
            running = record.state == RUNNING
        if running and record.job.interrupt is not None:
            self._interrupt(record)
        return True

    def carry_on(self, name: str) -> bool:
        """Let a paused job run again, picking up where it was."""
        with self._lock:
            record = self._jobs.get(name)
            if record is None:
                return False
            record.paused = False
            record.wake.set()
            if not record.alive and not record.stopping.is_set() and (
                    record.requested or record.next_at is not None):
                self._launch(record)
        return True

    # -- stopping ------------------------------------------------------------------

    def stop_job(self, name: str, timeout: float = 5.0) -> bool:
        """Stop one job and wait up to *timeout* for its thread. True if it ended."""
        with self._lock:
            record = self._jobs.get(name)
        if record is None:
            return True
        self._ask_to_stop(record)
        thread = record.thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(max(0.0, timeout))
        if not record.alive:
            self._set(record, STOPPED)
        return not record.alive

    def stop(self, timeout: float = 5.0) -> list[str]:
        """Stop every job within *timeout* seconds in all. Returns the names of
        the jobs still running when it was spent, which are left to finish on
        their own (their threads do not keep the process alive)."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._lock:
            self._closed = True
            records = list(self._jobs.values())
        # Every job is asked before any is waited for: a slow one does not
        # use up the time the quick ones needed to hear about it.
        for record in records:
            self._ask_to_stop(record)
        for record in records:
            thread = record.thread
            if thread is not None and thread is not threading.current_thread():
                thread.join(max(0.0, deadline - time.monotonic()))
            if not record.alive:
                self._set(record, STOPPED)
        left = [record.job.name for record in records if record.alive]
        for name in left:
            log.warning("%s was still running when the stop's %g seconds were spent",
                        name, timeout)
        return left

    def _ask_to_stop(self, record: _Record) -> None:
        record.stopping.set()
        record.wake.set()
        if record.state == RUNNING and record.job.interrupt is not None:
            self._interrupt(record)

    def _interrupt(self, record: _Record) -> None:
        try:
            record.job.interrupt()
        except Exception:                                    # noqa: BLE001
            self._log(record).exception("%s could not be asked to stop", record.job.name)

    # -- what everybody is doing ------------------------------------------------

    def activity(self) -> list[dict[str, Any]]:
        """One row per job: name, state, since, progress, last error, claims."""
        with self._lock:
            records = sorted(self._jobs.values(), key=lambda r: r.job.name)
        rows = []
        for record in records:
            progress = None
            if record.job.progress is not None:
                try:
                    progress = record.job.progress()
                except Exception:                            # noqa: BLE001
                    log.debug("could not ask %s how far it has got", record.job.name,
                              exc_info=True)
            rows.append({
                "name": record.job.name,
                "state": record.state,
                "since": record.since,
                "progress": progress,
                "last_error": record.last_error,
                "claims": sorted(record.job.claims),
                "when": record.job.trigger.describe(),
                "stoppable": record.job.stoppable,
            })
        return rows

    def state(self, name: str) -> str | None:
        record = self._jobs.get(name)
        return None if record is None else record.state

    # -- the thread each job runs on -------------------------------------------

    def _launch(self, record: _Record) -> None:
        # Under self._lock. Daemon, as every keeper's thread was: a stop that
        # runs out of time must not leave the process unable to end.
        record.thread = threading.Thread(target=self._keep, args=(record,),
                                         name=record.job.name, daemon=True)
        record.thread.start()

    def _set(self, record: _Record, state: str) -> None:
        if record.state != state:
            record.state = state
            record.since = time.time()

    def _log(self, record: _Record) -> logging.Logger:
        return record.job.log or log

    def _keep(self, record: _Record) -> None:
        try:
            while self._next_round(record):
                self._round(record)
        finally:
            if record.stopping.is_set():
                self._set(record, STOPPED)

    def _next_round(self, record: _Record) -> bool:
        """Wait until a round is due. False when the job is to end its thread:
        it is stopping, or it waits only on requests and none is owed."""
        while True:
            record.wake.clear()
            if record.stopping.is_set():
                return False
            if record.paused:
                self._set(record, PAUSED)
                self.clock.wait(record.wake, None)
                continue
            now = self.clock.now()
            if record.requested or (record.next_at is not None and now >= record.next_at):
                return True
            if record.next_at is None:
                with self._lock:
                    # Ended under the lock, so a request made a moment later
                    # finds the thread gone and starts another.
                    if not record.requested:
                        if record.state != DONE:
                            self._set(record, IDLE)
                        record.thread = None
                        return False
                continue
            self._set(record, RETRYING if record.failures else IDLE)
            self.clock.wait(record.wake, record.next_at - now)

    def _round(self, record: _Record) -> None:
        job = record.job
        record.requested = False
        if not self._take_claims(record):
            return
        self._set(record, RUNNING)
        failed = False
        try:
            job.run()
        except Exception as exc:                             # noqa: BLE001
            failed = True
            record.failures += 1
            record.last_error = str(exc) or type(exc).__name__
            self._log(record).exception(job.failure or f"{job.name} failed; it will try again")
        finally:
            self._let_go(record)
        now = self.clock.now()
        if failed:
            wait = self._backoff(record)
            if job.trigger.kind == EVERY:
                wait = max(job.trigger.interval(), wait)
            record.next_at = now + wait
            self._set(record, RETRYING)
            return
        record.failures = 0
        if job.trigger.kind == EVERY:
            record.next_at = now + job.trigger.interval()
            self._set(record, IDLE)
        else:
            record.next_at = None
            self._set(record, DONE)
        self._finished(job.name)

    @staticmethod
    def _backoff(record: _Record) -> float:
        job = record.job
        if job.retry_most <= 0:
            return 0.0 if job.trigger.kind == EVERY else max(job.retry_first, 1.0)
        return min(job.retry_first * 2 ** (record.failures - 1), job.retry_most)

    def _finished(self, name: str) -> None:
        """Start whatever runs after *name*."""
        with self._lock:
            followers = [r.job.name for r in self._jobs.values()
                         if r.job.trigger.kind == AFTER and r.job.trigger.job == name]
        for follower in followers:
            self.request(follower)

    # -- claims ------------------------------------------------------------------------

    def _take_claims(self, record: _Record) -> bool:
        """Hold the scan for a job that needs the disk or the processors.
        False if the job was stopped while it waited."""
        job, scanner = record.job, self.scanner
        if not job.yields_the_scan or scanner is None:
            return True
        while True:
            with self._claim_lock:
                others = self._moving_files(job.reason) if job.moves_files else []
                if not others:
                    # The scan's own rules: a running scan stands down, and
                    # one asked for meanwhile is queued, not lost.
                    scanner.defer(job.reason)
                    record.holding = True
                    return True
            self._set(record, WAITING)
            record.wake.clear()
            if record.stopping.is_set():
                return False
            self.clock.wait(record.wake, CLAIM_LOOK_EVERY)
            if record.stopping.is_set():
                return False

    def _let_go(self, record: _Record) -> None:
        if record.holding:
            record.holding = False
            try:
                self.scanner.resume(record.job.reason)
            except Exception:                                # noqa: BLE001
                log.exception("%s could not let go of the scan", record.job.name)

    def _moving_files(self, own: str) -> list[str]:
        """The claims on the scan, other than *own*, of something moving files:
        everything not known to only read (Scanner's READ_ONLY_CLAIMS)."""
        from ..media.scanner import READ_ONLY_CLAIMS                 # noqa: PLC0415
        held = getattr(self.scanner, "deferred", None) or ""
        return [claim for claim in held.split("; ")
                if claim and claim != own and claim not in READ_ONLY_CLAIMS]


def supervisor_of(keeper: Any) -> Supervisor:
    """The supervisor a keeper's jobs run on: the one Ninaivu gave it, or
    one of its own when it is used by itself (as much of the test suite does)."""
    supervisor = getattr(keeper, "supervisor", None)
    if supervisor is None:
        supervisor = keeper.supervisor = Supervisor()
    return supervisor

