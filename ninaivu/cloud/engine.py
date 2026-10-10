"""The worker that actually sends things up, and knows when to stop.

One file at a time, on one background thread, with resumable progress kept
for each file. When other background jobs are idle, larger chunks reduce
request overhead and the between-file scheduling pause is skipped. The user's
rate ceiling and upload window still apply in either mode.

What it guarantees, and what each guarantee costs:

**Nothing goes twice.** Every file is claimed in the store before a byte moves
and marked done after Drive confirms, so a crash mid-upload leaves a row that
says "pending with a resume point", never one that says "done" for a file that
did not arrive.

**Hidden photographs stay here.** Anything an administrator has marked
admin-only is not sent. Somebody who hid a photograph from their own household
did not thereby ask for it to be copied to Google, and reading that as consent
would be the worst possible inference. It is a setting, and it defaults to off.

**Stopping is safe.** The stop flag is checked between chunks. An in-flight
request must finish first, so the delay depends on chunk size and connection
speed. The partial upload is resumable and picks up where it left off.

**It runs no faster, and no later, than it was told to.** There is a ceiling on the rate and a
window of hours it may run in, both from :mod:`.limits`, and both applied
*between chunks* — which means the window closing part-way through a large
video stops it where it is and carries on the next night, rather than either
overrunning by hours or throwing away the bytes already sent. A stop for either
reason is recorded as still-pending, never as a failure: see
:func:`.store.release`.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import crypto, limits, store
from .drive import CHUNK, DriveClient, DriveError, NeedsReconnect
from .upload_cache import UploadCache

log = logging.getLogger(__name__)

__all__ = ["SyncEngine", "SyncState"]

#: How long to hold between looks at a closed upload window. The stop event
#: cuts any wait short, so this is not about how quickly Pause responds — it is
#: how quickly a window *edited while the engine is waiting* takes effect.
WINDOW_POLL = 30.0

#: How long to wait after a retryable failure, doubling, capped. Google asks
#: for exponential backoff on 429 and 5xx and it is in everybody's interest:
#: hammering a rate limit is how an account gets a longer one.
BACKOFF_START = 4.0
BACKOFF_MAX = 300.0

#: Pause between files. Nothing to do with rate limits — it is a moment for the
#: rest of the house to get a packet in edgeways. Kept short (50 ms) because the
#: rate limiter and upload window already govern sustained throughput; this is
#: only a scheduling yield.
BREATH = 0.05

#: How often an upload making way for the household looks again.
HOLD_POLL = 5.0

#: How often a long run offers the library to the queue again, besides after
#: each scan that found something. Queueing 210,000 files takes 0.4 s.
REQUEUE_EVERY = 3600.0

#: Fewer request round trips while the other background jobs are asleep.
IDLE_CHUNK = 64 * 1024 * 1024

#: How many times the record of a finished upload is tried against a busy
#: database. By then the file is on Drive: giving up would send it again.
RECORD_TRIES = 12


def _is_busy(exc: BaseException) -> bool:
    """A database another thread is writing to, as opposed to a broken one."""
    text = str(exc).lower()
    return isinstance(exc, sqlite3.OperationalError) and (
        "locked" in text or "busy" in text)


def _format_speed(bps: float) -> str:
    if bps <= 0:
        return ""
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if bps < 1024.0 or unit == "GB/s":
            return f"{bps:.1f} {unit}" if unit != "B/s" else f"{bps:.0f} B/s"
        bps /= 1024.0
    return f"{bps:.1f} GB/s"


@dataclass
class SyncState:
    """What the console is shown. Deliberately free of anything secret."""

    running: bool = False
    paused: bool = False
    current: str = ""
    current_sent: int = 0
    current_total: int = 0
    speed_bps: float = 0.0
    last_error: str = ""
    needs_reconnect: bool = False
    started_at: float = 0.0
    uploaded_this_run: int = 0
    bytes_this_run: int = 0
    #: Alive, with work to do, and holding off because the hours set for
    #: uploading have not come round. Distinct from paused, which is a person,
    #: and from stopped, which is neither.
    waiting_for_window: bool = False
    window_opens_at: float = 0.0
    #: Alive, with work to do, and making way for the household — the reason,
    #: in words. See ninaivu/server/workload.py.
    held_for: str = ""
    #: Going through the index to see what still has to go up. On a large
    #: library this takes a moment, and saying nothing about it looks exactly
    #: like an upload that never started.
    queueing: bool = False
    queued_so_far: int = 0
    #: Library folders this run could not reach — an external drive that is
    #: not plugged in. Their files are still owed, and a run that ends with
    #: this set is not a run that finished.
    offline_roots: list[str] = field(default_factory=list)
    _last_sample: tuple[float, int] = field(default=(0.0, 0), repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    #: The files going up at once: key -> [name, sent, total]. `current`,
    #: `current_sent` and `current_total` are these summed, so the console reads
    #: them as it did one file.
    _inflight: dict[str, list[Any]] = field(default_factory=dict, repr=False)
    #: Bytes moved this run, across every file, for the speed.
    _moved: int = field(default=0, repr=False)
    _plain_sent: int = field(default=0, repr=False)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            data = {k: v for k, v in self.__dict__.items()
                    if not k.startswith("_")}
            speed = float(data.get("speed_bps", 0.0))
            sent = int(data.get("current_sent", 0))
            total = int(data.get("current_total", 0))
        data["percent_of_file"] = (
            round(sent * 100 / total, 1) if total else 0.0)
        data["speed_formatted"] = _format_speed(speed)
        data["eta_seconds"] = (
            round((total - sent) / speed, 1) if speed > 0 and total > sent else 0.0)
        return data

    def update(self, **fields: Any) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(self, key, value)

    def bump(self, **fields: int) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(self, key, getattr(self, key) + value)

    def begin(self, key: str, name: str, total: int) -> None:
        """A file starts going up, perhaps beside others."""
        with self._lock:
            self._inflight[key] = [name, 0, int(total)]
            self._summarise()

    def progress(self, sent: int, total: int, key: str = "") -> None:
        now = time.monotonic()
        with self._lock:
            if key in self._inflight:
                entry = self._inflight[key]
                delta_b = max(0, int(sent) - int(entry[1]))
                entry[1], entry[2] = int(sent), int(total)
                self._summarise()
            else:
                delta_b = max(0, int(sent) - self._plain_sent)
                self._plain_sent = int(sent)
                self.current_sent = sent
                self.current_total = total
            self._moved += delta_b
            last_time, last_moved = self._last_sample
            if last_time > 0 and now <= last_time:
                # The clock has not moved since the last sample (Windows
                # counts it in 15 ms steps before Python 3.13): keep that
                # sample, so these bytes count in the next reading instead
                # of vanishing from it.
                return
            if last_time > 0:
                instant_speed = (self._moved - last_moved) / (now - last_time)
                if self.speed_bps <= 0:
                    self.speed_bps = instant_speed
                else:
                    self.speed_bps = 0.7 * self.speed_bps + 0.3 * instant_speed
            self._last_sample = (now, self._moved)

    def finish(self, key: str) -> None:
        """A file is done with, however it ended. The others carry on."""
        with self._lock:
            self._inflight.pop(key, None)
            self._summarise()

    def _summarise(self) -> None:
        names = [entry[0] for entry in self._inflight.values()]
        self.current = (names[0] if len(names) == 1 else
                        f"{names[0]} and {len(names) - 1} more" if names else "")
        self.current_sent = sum(int(entry[1]) for entry in self._inflight.values())
        self.current_total = sum(int(entry[2]) for entry in self._inflight.values())

    def reset_progress(self) -> None:
        with self._lock:
            self._inflight.clear()
            self.current = ""
            self.current_sent = 0
            self.current_total = 0
            self.speed_bps = 0.0
            self._last_sample = (0.0, 0)
            self._moved = 0
            self._plain_sent = 0


class SyncEngine:
    """Uploads what the store says is pending, until there is nothing left."""

    def __init__(self, *, connect: Callable[[], DriveClient],
                 open_db: Callable[[], Any],
                 folder_for: Callable[[dict[str, Any]], list[str]] | None = None,
                 visibility_of: Callable[[str, str], int | None] | None = None,
                 awaiting_check: Callable[[str, str], bool] | None = None,
                 requeue: Callable[[], int] | None = None,
                 rate_kbps: int = 0,
                 window: limits.Window | None = None,
                 encryption: Callable[[], tuple[bytes, bytes] | None] | None = None,
                 cache: UploadCache | None = None,
                 publish_params: Callable[[DriveClient], None] | None = None,
                 on_finished: Callable[[], None] | None = None,
                 idle: Callable[[], bool] | None = None,
                 hold: Callable[[], str | None] | None = None,
                 on_trouble: Callable[[str, str, str], None] | None = None,
                 send_hidden: Callable[[], bool] | None = None,
                 parallel: int = 1,
                 kept_back: Callable[[dict[str, Any]], str | None] | None = None):
        self._connect = connect
        #: The household's rules for what is left out (rules.py) and the
        #: large files waiting for approval (approvals.py): the reason a
        #: queued row is kept back, or None. Asked per file, just before it
        #: goes, so a rule made while a queue drains holds what is still in it.
        self._kept_back = kept_back
        #: Whether hidden things go too, right now (the cloud_hidden setting,
        #: and for "encrypted", whether the backup is). Asked per file, like
        #: the visibility itself.
        self._send_hidden = send_hidden
        self._idle = idle
        #: Why to wait for the household right now, or None (workload.py).
        #: Asked between files and between chunks, like the upload hours.
        self._hold = hold
        self._open_db = open_db
        self._folder_for = folder_for or _date_folders
        #: Hidden things are not uploaded unless the household said they may
        #: be (send_hidden). Asked per file, just before it goes, so something
        #: hidden after it was queued is still kept back.
        self._visibility_of = visibility_of
        #: Whether a picture has yet to be checked for being a screenshot or a
        #: document. Those are hidden once checked, so one that has not been is
        #: held until it has, rather than sent ahead of the check.
        self._awaiting_check = awaiting_check
        #: Offers the library to the queue again: when a run starts, after a
        #: scan found something (library_changed), every REQUEUE_EVERY, and
        #: once more when a run has nothing left. Only at the end, a photograph added on the
        #: first day of a run of weeks waited for all of it: here 2.2 TB, three
        #: weeks. Queued as it arrives, it goes ahead of every video still owed.
        self._requeue = requeue
        self._library_changed = threading.Event()
        #: Kilobytes per second, zero for no limit. Read on every chunk, so a
        #: cap set while an upload is running takes effect within one chunk.
        self.rate_kbps = int(rate_kbps or 0)
        #: The hours uploading is allowed in. Also read live.
        self.window = window or limits.Window()
        #: ``(key, key_id)`` when uploads are to be encrypted, else None. Read
        #: per file, so switching it on applies from the next file.
        self._encryption = encryption
        #: Where each encrypted file is kept until its upload finishes. A resumed
        #: upload must send the same bytes, and encryption makes new ones every
        #: time (a fresh nonce), so the ciphertext is kept, not remade.
        self._cache = cache
        #: Puts the key's public parameters in the Drive folder, once.
        self._publish_params = publish_params
        #: Called when a run ends because there was nothing left to send — not
        #: when it was paused, stopped, or could not go on.
        self._on_finished = on_finished
        #: Called when a run ends in a way somebody would want to hear about:
        #: `(event, summary, detail)`. A backup that stops silently is worse
        #: than no backup, because the household believes they have one — and
        #: this one stopped for seven hours before anybody looked.
        self._on_trouble = on_trouble
        self.state = SyncState()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._folders: dict[str, str] = {}
        self._pace: limits.RateLimiter | None = None
        #: root -> (when it was looked at, whether it was there). See
        #: `_root_is_there`: a dead drive can take seconds to answer, and this
        #: is asked once per file.
        self._roots_seen: dict[str, tuple[float, bool]] = {}
        #: How many files go up at once. Each spends about two seconds waiting
        #: on Google before any of it moves (measured here: 2.2 s a file, then
        #: 32 Mbit/s), so one at a time a photograph library used a third of
        #: the line. One keeps the old order exactly.
        self.parallel = max(1, int(parallel or 1))
        #: Making a Drive folder, and putting the encryption settings in Drive:
        #: each done once, and two uploads doing either at the same moment made
        #: two of them.
        self._setup_lock = threading.Lock()
        self._start_lock = threading.Lock()
        #: Set, under _start_lock, once a run's thread has left its loop and
        #: will look at nothing more: from then on it no longer counts as
        #: running, so a change that arrives is answered with a fresh start
        #: rather than a flag nobody reads (see :meth:`_wind_down`).
        self._loop_over = False

    # -- lifecycle --------------------------------------------------------

    def library_changed(self) -> bool:
        """A scan found something: queue it before the next file, not at the end.

        True when a run will see it. False when no run is going, or the one
        that was has already left its loop: the caller starts a fresh one
        (CloudService.library_changed). Asked under the start lock, so the
        answer cannot go stale between here and the end of the run: either
        the run's last look (:meth:`_wind_down`) sees the flag and goes again,
        or this sees the run over and says so.
        """
        with self._start_lock:
            if self._loop_over or not (self._thread and self._thread.is_alive()):
                return False
            self._library_changed.set()
            return True

    def forget_folders(self) -> None:
        """The Drive folder was changed: look every date folder up again.

        The ids remembered here are of folders under the old one, and a run
        that kept them went on filling the old folder for every month it had
        already seen while new months went to the new one.
        """
        with self._setup_lock:
            self._folders.clear()

    def start(self) -> bool:
        """Begin, unless it is already going. True if this call started it."""
        with self._start_lock:
            return self._start()

    def _start(self) -> bool:
        # Under _start_lock: two Starts at once (a double click, or the console
        # racing the start-up resume) both found no thread and started two
        # loops, which sent the same files to Drive twice. A thread that has
        # left its loop (_loop_over) is only tidying up and counts as gone.
        if self._thread and self._thread.is_alive() and not self._loop_over:
            return False
        self._loop_over = False
        self._stop.clear()
        self._folders.clear()
        self.state.update(running=True, paused=False, last_error="",
                          needs_reconnect=False, started_at=time.time(),
                          uploaded_this_run=0, bytes_this_run=0,
                          waiting_for_window=False, window_opens_at=0.0)
        self._thread = threading.Thread(target=self._run, name="ninaivu-cloud",
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self, join: bool = False, timeout: float = 30.0) -> None:
        self._stop.set()
        self.state.update(paused=True)
        if join and self._thread:
            self._thread.join(timeout)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._loop_over)

    # -- the loop ---------------------------------------------------------

    def _run(self) -> None:
        finished = False
        try:
            finished = bool(self._run_once())
        finally:
            self._wind_down(finished)

    def _wind_down(self, finished: bool) -> None:
        """The run's last look, before its thread ends: go again if owed.

        A scan that found new photographs while a run was finishing (sending
        its "backup finished" note, say) found the thread still alive, set the
        "library changed" flag on it, and that was the end of it: the loop
        that reads the flag had already gone, and nothing started another, so
        those photographs waited for the next scan or a restart. Under the
        start lock, so it and :meth:`library_changed` cannot cross: a change
        that arrived before this point starts a fresh run here, and one that
        arrives after it finds the run over and starts one itself. Only after
        a run that caught up: a paused one stays paused, and one that ended
        on an error or a missing drive is started again as before, by the next
        change or by somebody.
        """
        with self._start_lock:
            if self._thread is not threading.current_thread():
                return              # a direct call (the tests), or already replaced
            self._loop_over = True
            if (finished and self._library_changed.is_set()
                    and not self._stop.is_set()):
                self._library_changed.clear()
                self._start()

    def _run_once(self) -> bool:
        """One run, until nothing is left to send. True when it caught up."""
        conn = None
        finished = False
        requeued = False
        #: Library folders found to be unreachable during this run. Their rows
        #: stay pending and are stepped over, so a second library carries on.
        offline: set[str] = set()
        self._roots_seen.clear()
        self.state.update(offline_roots=[])
        pool = (ThreadPoolExecutor(max_workers=self.parallel, thread_name_prefix="ninaivu-upload")
                if self.parallel > 1 else None)
        try:
            conn = self._open_db()
            self._prune_cache(conn)
            client = self._connect()
            backoff = BACKOFF_START
            # Looked at first thing, too: a run carried on after a restart would
            # otherwise wait an hour for what arrived while Ninaivu was stopped.
            last_queued = float("-inf")

            while not self._stop.is_set():
                try:
                    if self._requeue is not None and (
                            self._library_changed.is_set()
                            or time.monotonic() - last_queued >= REQUEUE_EVERY):
                        # Without the console's "queueing" state: a fraction of
                        # a second between two files.
                        self._library_changed.clear()
                        last_queued = time.monotonic()
                        try:
                            self._requeue()
                        except Exception as exc:         # noqa: BLE001
                            if isinstance(exc, sqlite3.OperationalError) and _is_busy(exc):
                                self._library_changed.set()     # after the wait below
                                raise
                            log.exception("could not look for new files to back up")
                    batch = store.pending_batch(conn, limit=max(10, 3 * self.parallel),
                                                exclude_roots=offline)
                    if not batch and self._requeue is not None and not requeued:
                        # Once per run: a file that can never go (deleted since it
                        # was indexed) is queued and set aside again every time,
                        # and asking repeatedly would never let a run end.
                        requeued = True
                        self.state.update(queueing=True, queued_so_far=0)
                        try:
                            added = self._requeue()
                        finally:
                            self.state.update(queueing=False)
                        if added:
                            continue
                    if not batch:
                        # Nothing left that can be sent. Whether that means the
                        # backup is up to date depends entirely on why: with a
                        # drive unplugged there is a great deal still owed, and
                        # calling that "finished" is how the console came to
                        # report a complete backup that had sent nothing.
                        finished = not offline
                        break

                    # Only once there is something to send: a library that is
                    # fully up to date should finish and let the thread end, not
                    # sit waiting all day for a window it has no use for.
                    if not self._await_window():
                        break

                    progressed = False
                    held = False
                    #: Google turned the account away (a rate limit): already
                    #: waited out below, so not waited for a second time.
                    waited_out = False
                    #: A drive found missing. Not progress, but not a failing
                    #: batch either — the next one steps over that folder, so
                    #: backing off here would wait for nothing.
                    stepped_over = False
                    if pool is not None:
                        waited = False
                        # Stop refilling when it is time to look for new files.
                        def requeue_due(since: float = last_queued) -> bool:
                            return self._requeue is not None and (
                                self._library_changed.is_set()
                                or time.monotonic() - since >= REQUEUE_EVERY)
                        sent_together = self._send_together(pool, client, batch, conn=conn,
                                                            offline=offline, until=requeue_due)
                        for row, outcome in sent_together:
                            if outcome == "sent":
                                progressed = True
                                backoff = BACKOFF_START
                            elif outcome in ("reconnect", "halt"):
                                return False
                            elif outcome == "offline":
                                stepped_over = True
                                if row["root"] not in offline:
                                    offline.add(row["root"])
                                    self.state.update(offline_roots=sorted(offline))
                                    log.warning(
                                        "cloud upload is stepping over %s: the folder "
                                        "is not there. Its files stay in the queue.",
                                        row["root"])
                            elif outcome == "held":
                                held = True
                            elif outcome in ("wait", "backoff"):
                                waited = True
                        if waited and not self._stop.is_set():
                            self._sleep(backoff)
                            backoff = min(BACKOFF_MAX, backoff * 2)
                            # Already waited: the "nothing moved" pause below
                            # would only wait a second, doubled, time.
                            waited_out = True
                        batch = []
                    for row in batch:
                        if self._stop.is_set():
                            break
                        outcome = self._one(conn, client, row)
                        if outcome == "sent":
                            progressed = True
                            backoff = BACKOFF_START
                        elif outcome in ("reconnect", "halt"):
                            return False
                        elif outcome == "backoff":
                            # The account, not the file: every file after it
                            # would be refused the same way. Wait, then start
                            # the batch again rather than walking through it.
                            self._sleep(backoff)
                            backoff = min(BACKOFF_MAX, backoff * 2)
                            waited_out = True
                            break
                        elif outcome == "offline":
                            # Every other file on this drive will say the same,
                            # and there are six figures of them. Set the whole
                            # folder aside for this run rather than asking once
                            # per row.
                            offline.add(row["root"])
                            stepped_over = True
                            self.state.update(offline_roots=sorted(offline))
                            log.warning(
                                "cloud upload is stepping over %s: the folder "
                                "is not there. Its files stay in the queue.",
                                row["root"])
                            break
                        elif outcome == "held":
                            # Paused, or the window shut mid-file. Back to the top
                            # of the loop, which either ends or waits — carrying on
                            # through the rest of the batch would ignore both.
                            held = True
                            break
                        elif outcome == "wait":
                            # Google asked us to slow down. Sleep in slices so a
                            # pause during a five-minute backoff is still instant.
                            self._sleep(backoff)
                            backoff = min(BACKOFF_MAX, backoff * 2)
                            # Waited already: not again, doubled, below.
                            waited_out = True
                        if not self._stop.is_set() and not self._is_idle():
                            self._sleep(BREATH)

                    if (not progressed and not held and not stepped_over
                            and not waited_out and not self._stop.is_set()):
                        # A whole batch and nothing moved: everything in it is
                        # failing. Backing off here stops a tight loop over files
                        # that are not going to work this minute.
                        self._sleep(backoff)
                        backoff = min(BACKOFF_MAX, backoff * 2)
                except sqlite3.OperationalError as exc:
                    if not _is_busy(exc):
                        raise
                    # A scan or a backup is writing and outlasted the busy
                    # timeout. That is a reason to wait, not to end the run:
                    # this used to stop the upload until Ninaivu restarted.
                    self._rollback(conn)
                    log.warning("cloud upload waiting: the library index is busy "
                                "(%s); trying again in %.0fs", exc, backoff)
                    self._sleep(backoff)
                    backoff = min(BACKOFF_MAX, backoff * 2)

        except NeedsReconnect as exc:
            self.state.update(needs_reconnect=True, last_error=str(exc))
        except Exception as exc:                    # noqa: BLE001
            log.exception("cloud sync stopped")
            self.state.update(last_error=str(exc))
        finally:
            if pool is not None:
                pool.shutdown(wait=True)
            self.state.reset_progress()
            self.state.update(running=False, waiting_for_window=False,
                              window_opens_at=0.0, held_for="")
            if offline:
                # Plain words, and not `needs_reconnect`, which is about the
                # Google account. Nothing here is broken and nothing needs
                # fixing: the drive is not plugged in.
                where = ", ".join(sorted(offline))
                self.state.update(
                    last_error=f"{where} is not connected, so the photographs "
                               f"on it could not be backed up. They are still "
                               f"in the queue; plug it in and start again.")
            # The connection is deliberately not closed. `db.connect` hands out
            # one per thread from a cache keyed on the path, so closing it here
            # would leave a *closed* object in that cache for anything else on
            # this thread to be handed next. The worker thread ends with this
            # function, and the connection goes with it.
            del conn
        if finished and self._on_finished is not None:
            try:
                self._on_finished()
            except Exception:                         # noqa: BLE001
                log.exception("could not record the finished upload")
        self._report(finished, offline)
        return finished

    def _send_together(self, pool: ThreadPoolExecutor, client: DriveClient,
                       batch: list[dict[str, Any]], *, conn=None, offline: set[str] | None = None,
                       until: Callable[[], bool] | None = None,
                       ) -> list[tuple[dict[str, Any], str]]:
        """Send files several at a time; ``(row, outcome)`` for each.

        Rolling: as each file ends, the next one is started, taken from the
        batch and then from the queue (with *conn*), so one long video does not
        leave the other workers idle until it is done. A row is offered once
        per call, so none is ever sent twice at the same time, and this answers
        only once every file it started has ended: a file still going up when
        the next batch is fetched would be offered again (the queue puts
        part-sent uploads first). Refilling stops when the queue has nothing
        new, when *until* says so (time to look for new files), and on a stop,
        a pause, a hold, a lost permission or Google asking to slow down: files
        not started then stay pending.
        """
        stop_starting = threading.Event()
        width = max(1, self.parallel)
        away = set(offline or ())

        def attempt(row: dict[str, Any]) -> tuple[dict[str, Any], str]:
            if stop_starting.is_set() or self._stop.is_set():
                return row, "not started"
            # This thread's own connection: db.connect keeps one per thread.
            conn = self._open_db()
            try:
                outcome = self._one(conn, client, row)
            except sqlite3.Error:
                # The pool's threads outlive this batch, and a connection left
                # in a failed transaction would fail every later write on its
                # thread at once (the run's own connection is rolled back by
                # the loop; this one would not be).
                self._rollback(conn)
                raise
            if outcome in ("reconnect", "held", "wait", "backoff", "halt"):
                stop_starting.set()
            elif not self._stop.is_set() and not self._is_idle():
                self._sleep(BREATH)
            return row, outcome

        def key(row: dict[str, Any]) -> tuple[str, str]:
            return row["root"], row["rel_path"]

        def starting() -> bool:
            return not stop_starting.is_set() and not self._stop.is_set()

        results: list[tuple[dict[str, Any], str]] = []
        backlog = list(batch)
        offered = {key(row) for row in backlog}
        running: dict[Any, dict[str, Any]] = {}
        try:
            while True:
                while backlog and len(running) < width and starting():
                    row = backlog.pop(0)
                    running[pool.submit(attempt, row)] = row
                if not running:
                    break
                done, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in done:
                    running.pop(future)
                    row, outcome = future.result()
                    results.append((row, outcome))
                    if outcome == "offline":
                        away.add(row["root"])
                        backlog = [r for r in backlog if r["root"] not in away]
                if (not backlog and conn is not None and starting()
                        and not (until is not None and until())):
                    more = store.pending_batch(conn, limit=3 * width + len(running),
                                               exclude_roots=away)
                    backlog = [row for row in more if key(row) not in offered]
                    offered.update(key(row) for row in backlog)
        finally:
            # Never left going behind the caller's back: the next batch would
            # offer a file still on its way up.
            stop_starting.set()
            wait(running)
        return results

    def _report(self, finished: bool, offline: set[str]) -> None:
        """Tell somebody, if this run ended in a way they would want to know.

        Only the two things a person would act on. Not "a run finished" —
        that is every night, and a notification that arrives every night is
        one nobody reads. The notifier's own quiet window stops a drive left
        unplugged over a weekend from producing one of these an hour.
        """
        if self._on_trouble is None:
            return
        state = self.state.snapshot()
        try:
            if state.get("needs_reconnect"):
                self._on_trouble(
                    "cloud_failed",
                    "Cloud backup has stopped",
                    "Google is no longer accepting the saved permission, so "
                    "nothing is going up. Connect the account again on the "
                    "Cloud page. Everything already uploaded is still there.")
            elif offline:
                self._on_trouble(
                    "cloud_stalled",
                    "Cloud backup is waiting for a drive",
                    f"{', '.join(sorted(offline))} is not connected, so the "
                    f"photographs on it could not be backed up. They are "
                    f"still in the queue.")
            elif not finished and state.get("last_error"):
                self._on_trouble(
                    "cloud_failed", "Cloud backup stopped with an error",
                    str(state["last_error"])[:300])
        except Exception:                             # noqa: BLE001
            # A notifier that throws must not turn "the backup stopped" into
            # "the backup crashed".
            log.exception("could not report that the cloud backup stopped")

    def _sleep(self, seconds: float) -> None:
        """Wait, but wake the moment somebody presses pause."""
        self._stop.wait(seconds)

    def _is_idle(self) -> bool:
        try:
            return bool(self._idle and self._idle())
        except Exception:
            log.debug("could not check background activity", exc_info=True)
            return False

    def _chunk_size(self) -> int:
        # Read both activity and the user's cap before every chunk, including
        # during a large video. An unknown activity state keeps normal pacing.
        ceiling = IDLE_CHUNK if self._is_idle() else CHUNK
        return limits.chunk_for(self._limiter().rate, ceiling)

    # -- the two limits ---------------------------------------------------

    def _await_window(self) -> bool:
        """Hold until uploading is allowed. False if it was stopped instead.

        The wait is broken into slices, not because Pause needs it — the stop
        event cuts any wait short on its own — but so that a window edited
        while the engine is holding is picked up within half a minute rather
        than at the old opening time.
        """
        while not self._stop.is_set():
            delay = self.window.seconds_until_open()
            held = self._held_for() if delay <= 0 else ""
            if delay <= 0 and not held:
                self.state.update(waiting_for_window=False, window_opens_at=0.0,
                                  held_for="")
                return True
            if held:
                self.state.update(held_for=held, current="", current_sent=0,
                                  current_total=0)
                self._sleep(HOLD_POLL)
                continue
            self.state.update(waiting_for_window=True,
                              window_opens_at=time.time() + delay,
                              current="", current_sent=0, current_total=0)
            self._sleep(min(delay, WINDOW_POLL))
        self.state.update(waiting_for_window=False, window_opens_at=0.0, held_for="")
        return False

    def _held_for(self) -> str:
        """The household's reason for the upload to wait, or ''."""
        if self._hold is None:
            return ""
        try:
            return str(self._hold() or "")
        except Exception:                                   # noqa: BLE001
            log.debug("could not ask whether to make way", exc_info=True)
            return ""

    def _limiter(self) -> limits.RateLimiter:
        """The token bucket for the current cap, replaced when the cap changes.

        Replaced rather than retuned because the bucket's capacity is derived
        from the rate: a new cap means a differently sized bucket, and the one
        chunk's worth of slack that comes with a fresh one is not worth the
        arithmetic to avoid.
        """
        wanted = max(0, int(self.rate_kbps or 0)) * 1024
        if self._pace is None or self._pace.rate != float(wanted):
            self._pace = limits.RateLimiter(wanted, stop=self._stop)
        return self._pace

    def _gate(self, count: int) -> None:
        """Called before every chunk. Blocks for the cap, raises for a stop."""
        if self._stop.is_set():
            raise limits.UploadPaused("paused")
        if not self.window.is_open():
            raise limits.UploadPaused(
                "set aside until the hours set for uploading come round")
        if held := self._held_for():
            raise limits.UploadPaused(f"making way: {held}")
        self._limiter().take(count)
        # Checked again on the way out: the wait above can be minutes on a low
        # cap, and sending one more chunk after Pause was pressed is exactly
        # the lag that makes a stop button feel broken.
        if self._stop.is_set():
            raise limits.UploadPaused("paused")

    #: How long a library folder's reachability is believed before it is
    #: looked at again. Long enough that a run does not stat a dead drive once
    #: per file — a failing one can take seconds to answer — and short enough
    #: that plugging the drive back in is noticed within a batch or two.
    ROOT_CHECK_SECONDS = 30.0

    def _root_is_there(self, root: str) -> bool:
        """Is this library folder reachable right now?

        Cached briefly per root: see ROOT_CHECK_SECONDS.
        """
        seen_at, seen = self._roots_seen.get(root, (0.0, True))
        now = time.time()
        if now - seen_at < self.ROOT_CHECK_SECONDS:
            return seen
        # Its own marker, not merely the folder: an unplugged drive under a
        # ``nofail`` mount leaves an empty folder behind (storage/roots.py).
        from ..storage.roots import root_present            # noqa: PLC0415
        there = root_present(root)
        self._roots_seen[root] = (now, there)
        return there

    def _one(self, conn, client: DriveClient, row: dict[str, Any]) -> str:
        """Upload one file.

        Returns 'sent', 'skipped', 'wait', 'reconnect', 'offline', or 'held' —
        the last meaning it stopped part-way on purpose and is still pending —
        or, for a failure that is the account's and not the file's (see
        :meth:`_account_trouble`), 'backoff' to wait and try again or 'halt'
        to end the run.
        """
        root, rel = row["root"], row["rel_path"]
        path = Path(root) / rel

        # Before the disk is asked anything: a rule that keeps back every
        # video should not cost a stat of every video.
        reason = self._kept_back(row) if self._kept_back is not None else None
        if reason:
            store.record_skipped(conn, root, rel, reason)
            return "skipped"

        if not path.is_file():
            if not self._root_is_there(root):
                # The whole library folder is gone, not this file. An external
                # drive that is not plugged in used to be read as every file
                # in the queue having been deleted: a run would mark a hundred
                # thousand of them "the file is no longer there", find nothing
                # left to send, and report the backup complete. Nothing had
                # gone up, and the record saying an upload was still owed was
                # cleared along with it.
                return "offline"
            # Deleted or moved since it was queued. Not a failure and not
            # something to retry — there is nothing to send.
            store.record_skipped(conn, root, rel, "the file is no longer there")
            return "skipped"

        if self._is_hidden(root, rel):
            store.record_skipped(
                conn, root, rel,
                "kept back: this is hidden, and hidden things are not uploaded")
            return "skipped"

        if self._awaiting_check is not None and self._awaiting_check(root, rel):
            # Set aside, not failed: the next time the library is queued, a
            # picture that has been checked and is not hidden goes like any other.
            store.record_skipped(
                conn, root, rel,
                "waiting: not yet checked for being a screenshot or document")
            return "skipped"

        from ..utils.source_version import source_version
        try:
            actual_size = path.stat().st_size
            version = source_version(path)
        except OSError as exc:
            # A file another program holds open, a disk returning read errors,
            # or one that vanished since ``is_file``. Counted like every other
            # read failure below, so it is set aside after MAX_ATTEMPTS. Left
            # to escape, it ended the whole run — and, as the oldest pending
            # row, ended every later run at the same file.
            store.record_failure(conn, root, rel, f"could not read the file: {exc}")
            return "skipped"
        resume_url = row.get("resume_url") or ""
        if row.get("source_version") != version:
            resume_url = ""
        conn.execute("UPDATE cloud_uploads SET source_version=?, size=?, resume_url=? "
                     "WHERE root=? AND rel_path=?", (version, actual_size, resume_url, root, rel))
        conn.commit()

        def check_source(count=0):
            self._gate(count)
            if source_version(path) != version:
                raise DriveError("source changed during upload; restarting", 410, retryable=True)
        send_path, send_name = path, row["filename"] or path.name
        encryption = self._encryption() if self._encryption else None
        cache: Path | None = None
        if encryption is not None:
            try:
                if self._publish_params is not None:
                    with self._setup_lock:
                        self._publish_params(client)
                if self._cache is None:
                    raise OSError("no folder to prepare encrypted uploads in")
                cache = self._cache.path_for(root, rel)
                if not resume_url or not cache.is_file():
                    # No ciphertext to continue from: start a new upload of a
                    # new one. An old session cannot take different bytes.
                    resume_url = ""
                    crypto.encrypt_file_v2(path, cache, *encryption)
            except NeedsReconnect as exc:
                # The permission, not this file: not counted against it.
                store.set_aside(conn, root, rel, str(exc))
                self.state.update(needs_reconnect=True, last_error=str(exc))
                return "reconnect"
            except DriveError as exc:
                if exc.account_wide:
                    return self._account_trouble(conn, root, rel, exc)
                store.record_failure(conn, root, rel, f"could not prepare encryption: {exc}")
                self.state.update(last_error=str(exc))
                return "wait" if exc.retryable else "skipped"
            except (OSError, ValueError, RuntimeError) as exc:
                store.record_failure(conn, root, rel, f"could not encrypt the file: {exc}")
                return "skipped"
            send_path, send_name = cache, f"{send_name}.ninaivu"

        send_size = send_path.stat().st_size
        if resume_url and not self._session_fits(row, cache is not None, send_size):
            # The session was opened for other bytes — plain where these are
            # encrypted, or the other way round, or a different length. Drive
            # would either refuse them or, worse, splice them onto what it
            # already has. A new session costs the bytes already sent; a
            # spliced file costs the photograph.
            resume_url = ""
        encrypted_now = cache is not None
        # Several files can be going up at once: each is its own line in the
        # progress, and ending one leaves the others where they are.
        key = f"{root}\0{rel}"
        self.state.begin(key, row["filename"] or rel, send_size)
        store.mark_uploading(conn, root, rel)
        # Hashed from the bytes as they go past, which costs nothing. Only a
        # hash of the whole file if this pass started at the beginning — a
        # resumed upload has already sent the front of the file and cannot see
        # it again without reading the whole thing a second time, which is not
        # worth it for a record field.
        running = hashlib.sha256() if not resume_url else None

        try:
            parent = self._folder(client, row)
            remote_id = client.upload_file(
                send_path, send_name, parent,
                resume_url=resume_url,
                digest=running,
                chunk_size=self._chunk_size,
                gate=check_source,
                on_progress=lambda sent, total: self.state.progress(sent, total, key),
                on_session=lambda url: store.save_resume(
                    conn, root, rel, url, encrypted=encrypted_now, size=send_size))
            check_source()
        except limits.UploadPaused as exc:
            # Not a failure, so the attempt count is untouched and the resume
            # URL is kept: whatever has already gone up stays gone up, and the
            # next run sends the remainder rather than the file.
            store.release(conn, root, rel)
            self.state.finish(key)
            self.state.update(last_error="")
            log.debug("upload held: %s", exc)
            return "held"
        except NeedsReconnect as exc:
            # The permission, not this file: not counted against it.
            store.set_aside(conn, root, rel, str(exc))
            self.state.finish(key)
            self.state.update(needs_reconnect=True, last_error=str(exc))
            return "reconnect"
        except DriveError as exc:
            if exc.account_wide:
                # The resume URL stays as the session saved it: nothing is
                # wrong with the upload, the account was told to wait.
                self.state.finish(key)
                return self._account_trouble(conn, root, rel, exc)
            # A resume URL that expired must not be kept, or every retry
            # replays the same dead session.
            keep = resume_url if exc.status not in (404, 410) else ""
            store.record_failure(conn, root, rel, str(exc), resume_url=keep)
            if cache is not None and not keep:
                self._cache.discard(cache)
            self.state.finish(key)
            self.state.update(last_error=str(exc))
            return "wait" if exc.retryable else "skipped"
        except OSError as exc:
            store.record_failure(conn, root, rel, f"could not read the file: {exc}")
            self.state.finish(key)
            if cache is not None:
                self._cache.discard(cache)
            return "skipped"

        # Only now, with an id back from Drive, is it done.
        digest = running.hexdigest() if running is not None else ""
        # A resumed upload has no hash of its own of the whole file, so ask
        # Drive for the one it keeps. Without it a restore of this file could
        # check nothing but its size.
        md5 = "" if digest else self._drive_md5(client, remote_id)
        self._record_done(conn, root, rel, remote_id=remote_id, digest=digest,
                          folder=self._folders_key(row), encrypted=cache is not None,
                          md5=md5)
        if cache is not None:
            self._cache.discard(cache)
        self.state.bump(uploaded_this_run=1, bytes_this_run=actual_size)
        self.state.finish(key)
        return "sent"

    # -- helpers ----------------------------------------------------------

    def _account_trouble(self, conn, root: str, rel: str, exc: DriveError) -> str:
        """A failure that belongs to the account, not to this file.

        A rate limit, a full Drive, or Google out of reach: the next file
        would meet the same answer. Counting it as this file's attempt made
        a bad afternoon into a queue of FAILED files with nothing wrong with
        them, five attempts at a time. The file goes back in the queue with
        its count untouched, and the run either waits ('backoff') or, when
        waiting a few minutes will not help, ends and says why ('halt').
        """
        store.set_aside(conn, root, rel, str(exc))
        self.state.update(last_error=str(exc))
        return "backoff" if exc.retryable else "halt"

    def _prune_cache(self, conn) -> None:
        """Clear ciphertext left by uploads that will not be carried on with.

        Only the cache's own files, through the cache (upload_cache.py); one
        a saved resumable session still needs is kept.
        """
        if self._cache is None:
            return
        try:
            keep = {self._cache.path_for(r[0], r[1]).name for r in conn.execute(
                "SELECT root, rel_path FROM cloud_uploads WHERE resume_url != ''")}
            removed = self._cache.prune(keep)
        except (OSError, sqlite3.Error) as exc:
            log.warning("could not tidy the upload cache: %s", exc)
            return
        if removed:
            log.info("removed %d abandoned encrypted upload(s) from the upload cache", removed)

    def _session_fits(self, row: dict[str, Any], encrypted: bool, size: int) -> bool:
        """Can the saved resumable session take the bytes about to be sent?

        A session is opened for a given number of bytes of a given kind. Rows
        saved since ``resume_size`` was kept say so directly. For an older
        row, the ciphertext kept in the upload cache is the evidence: it
        exists only for an encrypted upload, so finding it when encryption
        has since been switched off means the session was for ciphertext.
        """
        known = int(row.get("resume_size") or 0)
        if known:
            return bool(row.get("resume_encrypted")) == encrypted and known == size
        if encrypted or self._cache is None:
            return True
        stale = self._cache.path_for(row["root"], row["rel_path"])
        if stale.is_file():
            self._cache.discard(stale)
            return False
        return True

    @staticmethod
    def _drive_md5(client: DriveClient, remote_id: str) -> str:
        """Drive's MD5 of an uploaded file, or '' when it cannot be had now."""
        if not remote_id:
            return ""
        try:
            return str(client.file_info(remote_id).get("md5Checksum") or "")
        except DriveError:
            # The file is up; a checksum that could not be fetched is a
            # weaker record, not a reason to send it again.
            log.debug("could not read Drive's checksum for %s", remote_id, exc_info=True)
            return ""

    def _record_done(self, conn, root: str, rel: str, **fields: Any) -> None:
        """Write down a finished upload, waiting out a busy database.

        The file is already on Drive. If this write is lost the row stays
        queued and the next run sends the file a second time, so it is
        retried — even after Pause, which only stops the next file.
        """
        for attempt in range(RECORD_TRIES):
            try:
                store.record_done(conn, root, rel, **fields)
                return
            except sqlite3.OperationalError as exc:
                if not _is_busy(exc) or attempt == RECORD_TRIES - 1:
                    raise
                self._rollback(conn)
                log.warning("could not yet record %s as uploaded (%s); retrying",
                            rel, exc)
                time.sleep(min(BACKOFF_MAX, BACKOFF_START * (attempt + 1)))

    @staticmethod
    def _rollback(conn) -> None:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass

    def _is_hidden(self, root: str, rel: str) -> bool:
        """Hidden, and not to be sent: False when hidden things may go."""
        if self._visibility_of is None:
            return False
        level = self._visibility_of(root, rel)
        if level is None or int(level) < 2:
            return False
        return not (self._send_hidden is not None and self._send_hidden())

    def _folders_key(self, row: dict[str, Any]) -> str:
        return "/".join(self._folder_for(row))

    def _folder(self, client: DriveClient, row: dict[str, Any]) -> str:
        """The Drive folder for this file, made once and remembered.

        Every path here starts at :meth:`DriveClient.ninaivu_root`, and the
        client refuses to make a folder or start an upload without a parent.
        Between them there is no route by which this engine can write anywhere
        in the account except inside the one folder Ninaivu made.
        """
        parts = self._folder_for(row)
        key = "/".join(parts)
        if key in self._folders:
            return self._folders[key]
        with self._setup_lock:
            if key not in self._folders:
                root_id = client.ninaivu_root()
                self._folders[key] = client.folder_path(parts, root_id)
            return self._folders[key]


def _date_folders(row: dict[str, Any]) -> list[str]:
    """Mirror the shape the file already has, up to three levels.

    Somebody looking in Drive should recognise what they are looking at, and
    the folders they already sorted their photographs into are the arrangement
    they chose. Three levels holds a full ``YYYY/MM/DD`` date without
    recreating a fifteen-deep tree in a web interface that is bad at deep
    trees. Files sent while this was two levels stay in their ``YYYY/MM``
    folders; a restore walks Drive at any depth, so both shapes come back.
    """
    parts = [p for p in str(row.get("rel_path", "")).replace("\\", "/")
             .split("/")[:-1] if p]
    return parts[:3]
