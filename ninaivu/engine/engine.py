"""The job engine: owed work as small tasks, worked on by every core.

One run (:meth:`JobEngine.run`) takes a library folder and the derivations
asked for, fills the task list from what the index says is owed, and then:

* hands tasks to worker processes, a few more than there are workers, the
  highest priority first — so text and faces are worked on side by side
  instead of one pass after the other;
* gathers the answers and writes them in groups (``db.write_group``): one
  hold of the index's write lock and one commit for many photographs;
* settles each task in ``engine.db`` only once its answer is committed, so
  a stop, a crash or a power cut at any moment loses at most the answers in
  hand, which are handed out again next time;
* survives a worker that dies: every task that was in flight is tried again
  on a fresh pool, one at a time, so the file that brought the worker down
  is told apart from the ones that only shared the pool with it. A file that
  brings a worker down on its own twice (:data:`tasks.MAX_ATTEMPTS`) is set
  aside instead of taking the scan, or in 1.x the whole server, with it;
* asks the household's turn (``take_turn``) between answers, so a video
  somebody starts watching still comes first.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from ..storage import db
from . import work
from .derivations import DERIVATIONS, Context, Derivation
from .pool import BrokenProcessPool, WorkerPool, processes_for, server_setup
from .tasks import TaskStore

log = logging.getLogger(__name__)

#: Answers written in one group at most, and the longest an answer waits
#: before its group is written. Small enough that the console's counts move
#: every second and a family member's write waits a few milliseconds.
GROUP_SIZE = 64
GROUP_SECONDS = 0.5

#: Tasks in hand per worker: one working, one waiting, so no worker idles
#: while the next is being handed out.
AHEAD = 2

#: How often a run asks whether it may have more worker processes (audit
#: D-22): a run that started while another held them all had one.
GROW_SECONDS = 30.0


def database_path(cfg: Any) -> Path:
    """``engine.db``, beside the index."""
    return Path(cfg.db_path).with_name("engine.db")


@dataclass
class RunReport:
    """What one run did, by derivation."""

    done: dict[str, int] = field(default_factory=dict)
    found: dict[str, int] = field(default_factory=dict)
    failed: dict[str, int] = field(default_factory=dict)
    left: int = 0
    stopped: bool = False
    gone: bool = False
    restarts: int = 0

    def total_done(self) -> int:
        return sum(self.done.values())


@dataclass
class _Task:
    id: int
    kind: str
    item: int
    attempts: int


@dataclass
class _Sent:
    task: _Task
    fn: Callable[..., Any]
    args: tuple
    #: The pool's restart count when it was sent: a broken-pool error from an
    #: older pool must not restart the new one.
    generation: int
    #: Sent on its own, after a worker died with it in flight.
    alone: bool = False


class JobEngine:
    """The task list of one index and the runs that work through it."""

    def __init__(self, cfg: Any, store: TaskStore | None = None) -> None:
        self.cfg = cfg
        self.store = store or TaskStore(database_path(cfg))
        #: The runs going now, by id: what they hold is theirs alone.
        self._live: set[str] = set()
        self._live_lock = threading.Lock()
        recovered = self.store.recover()
        if recovered:
            log.info("job engine: %d tasks left running last time are queued again", recovered)

    def close(self) -> None:
        self.store.close()

    # -- filling ----------------------------------------------------------
    def fill(self, conn, root: str, kinds: Iterable[str]) -> dict[str, int]:  # noqa: ANN001
        """Queue what the index says is owed for *kinds* in *root*, and drop
        what is not owed any more. Returns how many are owed, by kind."""
        owed: dict[str, int] = {}
        for kind in kinds:
            derivation = DERIVATIONS[kind]
            version = derivation.version()
            items = (derivation.owed(conn, root, version, self.cfg)
                     if derivation.owed_with_cfg else derivation.owed(conn, root, version))
            self.store.prune(kind, version, keep=(i for i, _ in items), root=root)
            added = self.store.add(kind, root, version, items, derivation.priority)
            owed[kind] = len(items)
            if added:
                log.info("job engine: %s new %s tasks in %s", f"{added:,}", kind, root)
        return owed

    # -- running ----------------------------------------------------------
    def run(self, conn, ctx: Context, kinds: list[str], *,  # noqa: ANN001, C901, PLR0912, PLR0915
            should_stop: Callable[[], bool] = lambda: False,
            take_turn: Callable[[], bool] = lambda: True,
            on_progress: Callable[[int, int, RunReport], None] | None = None,
            pool: WorkerPool | None = None, fill: bool = True,
            holding: Callable[[], bool] | None = None,
            grow: Callable[[int], int] | None = None) -> RunReport:
        """Work through the owed *kinds* in ``ctx.root`` until none is left
        or *should_stop* says so. *take_turn* is asked between answers and
        may block while the household comes first; False ends the run.
        ``fill=False`` when :meth:`fill` was just called for the same kinds.

        *holding*, when given, says without waiting whether *take_turn* is
        about to hold. Then the workers are ended before it does, and what
        they had in hand goes back on the list: held, they went on with up to
        two tasks each (audit D-36), and a run held until the next night or
        catch-up kept every worker and its models for most of a day (D-20).
        A fresh pool is started when the turn comes back.

        *grow*, when given, is asked every :data:`GROW_SECONDS` with the
        pool's size and answers how many workers the run may have now. When
        that is more, no new task is handed out until the ones in hand are
        answered, and the next pool is started at the larger size."""
        report = RunReport(done={k: 0 for k in kinds}, found={k: 0 for k in kinds},
                           failed={k: 0 for k in kinds})
        if not kinds:
            return report
        owner = uuid.uuid4().hex
        with self._live_lock:
            # Tasks a run that is over left running (its hand-back failed)
            # are queued again; another run's going now are left to it.
            stale = self.store.recover_stale(self._live)
            self._live.add(owner)
        if stale:
            log.info("job engine: %d tasks a finished run still held are queued again", stale)
        try:
            return self._run(conn, ctx, kinds, report, owner, should_stop=should_stop,
                             take_turn=take_turn, on_progress=on_progress, pool=pool, fill=fill,
                             holding=holding, grow=grow)
        finally:
            with self._live_lock:
                self._live.discard(owner)

    def _run(self, conn, ctx: Context, kinds: list[str], report: RunReport,  # noqa: ANN001, C901, PLR0912, PLR0915
             owner: str, *, should_stop: Callable[[], bool], take_turn: Callable[[], bool],
             on_progress: Callable[[int, int, RunReport], None] | None,
             pool: WorkerPool | None, fill: bool,
             holding: Callable[[], bool] | None = None,
             grow: Callable[[int], int] | None = None) -> RunReport:
        root = ctx.root
        if fill:
            self.fill(conn, root, kinds)
        counts = self.store.counts(root)
        total = sum(counts.get(k, {}).get("queued", 0) for k in kinds)
        if not total:
            return report
        own_pool = pool is None
        pool = pool or WorkerPool(processes_for(self.cfg), server_setup(self.cfg))
        window = pool.size * AHEAD
        log.info("job engine: %s tasks (%s) in %s on %d worker processes", f"{total:,}",
                 ", ".join(f"{k} {counts.get(k, {}).get('queued', 0):,}" for k in kinds),
                 root, pool.size)
        started = time.monotonic()

        inflight: dict[Future, _Sent] = {}
        suspects: deque[_Sent] = deque()   # in flight when a worker died
        answers: list[tuple[_Task, Any]] = []
        give_ups: list[_Task] = []
        parked: list[int] = []           # owed but not settled this run
        held: set[int] = set()           # claimed, not yet settled or handed back
        last_write = time.monotonic()
        settled = 0
        exhausted = False
        last_grow = time.monotonic()
        growing = 0                      # the size the next pool is to have

        def write() -> None:
            nonlocal last_write, settled
            last_write = time.monotonic()
            if not answers and not give_ups:
                return
            finished: list[int] = []
            with db.write_group(conn) as group:
                for task, answer in answers:
                    derivation = DERIVATIONS[task.kind]
                    before = group.failed
                    found = False
                    with group.item() as c:
                        found = bool(derivation.store(ctx, c, task.item, answer))
                    if group.failed == before:
                        report.done[task.kind] += 1
                        report.found[task.kind] += int(found)
                        finished.append(task.id)
                    else:
                        parked.append(task.id)       # tried again on a later run
                for task in give_ups:
                    with group.item() as c:
                        DERIVATIONS[task.kind].give_up(ctx, c, task.item)
            self.store.done(finished)
            settled += len(finished) + len(give_ups)
            held.difference_update(t.id for t, _ in answers)
            held.difference_update(t.id for t in give_ups)
            answers.clear()
            give_ups.clear()
            if on_progress is not None:
                on_progress(settled, total, report)

        def failed(task: _Task, why: str) -> None:
            nonlocal exhausted
            held.discard(task.id)
            if self.store.failed(task.id, why, task.attempts):
                log.warning("job engine: %s of item %d failed %d times, set aside: %s",
                            task.kind, task.item, task.attempts, why)
                report.failed[task.kind] += 1
                give_ups.append(task)
            else:
                exhausted = False                    # queued again: tried later

        def put_down() -> None:
            """End the workers before a hold; their tasks go back, their
            attempts not counted, for a fresh pool after it."""
            nonlocal exhausted
            write()
            back = [sent.task.id for sent in inflight.values()]
            back += [sent.task.id for sent in suspects]
            inflight.clear()
            suspects.clear()
            pool.terminate()
            self.store.release(set(back))
            held.difference_update(back)
            exhausted = False
            log.info("job engine: making way; %d tasks in hand go back on the list "
                     "and the worker processes are ended until it carries on", len(back))

        def send(sent: _Sent) -> None:
            future = pool.submit(sent.fn, *sent.args)
            sent.generation = pool.restarts
            inflight[future] = sent

        try:
            while True:
                if should_stop():
                    report.stopped = True
                    break
                if grow is not None and not growing and \
                        time.monotonic() - last_grow >= GROW_SECONDS:
                    last_grow = time.monotonic()
                    wanted = int(grow(pool.size))
                    if wanted > pool.size:
                        growing = wanted
                if growing and not inflight and not suspects:
                    # The ones in hand answered: the next pool is larger.
                    log.info("job engine: %d worker processes free now; going on with %d",
                             growing - pool.size, growing)
                    pool.shutdown(wait=False)
                    pool.size = growing
                    window = pool.size * AHEAD
                    growing = 0
                if suspects:
                    # After a worker died: what it might have had, one at a time.
                    if not inflight:
                        sent = suspects.popleft()
                        sent.alone = True
                        send(sent)
                elif len(inflight) < window and not exhausted and not growing:
                    claimed, capped = self._claim(ctx, kinds, inflight.values(),
                                                  window - len(inflight), owner)
                    if not claimed and not capped:
                        exhausted = True
                    held.update(t.id for t in claimed)
                    by_kind: dict[str, list[_Task]] = {}
                    for task in claimed:
                        by_kind.setdefault(task.kind, []).append(task)
                    for kind, tasks in by_kind.items():
                        derivation: Derivation = DERIVATIONS[kind]
                        jobs = derivation.jobs(ctx, conn, [t.item for t in tasks])
                        gone_by = []
                        for task in tasks:
                            job = jobs.get(task.item)
                            if job is None:
                                gone_by.append(task.id)   # hidden, deleted or done
                                continue
                            send(_Sent(task, job[0], tuple(job[1]), 0))
                        self.store.done(gone_by)
                        held.difference_update(gone_by)
                        settled += len(gone_by)
                if not inflight:
                    if exhausted and not suspects:
                        break
                    continue
                finished, _ = wait(list(inflight), timeout=GROUP_SECONDS,
                                   return_when=FIRST_COMPLETED)
                broken = False
                for future in finished:
                    sent = inflight.pop(future)
                    task = sent.task
                    try:
                        answer = future.result()
                    except BrokenProcessPool:
                        broken = broken or sent.generation == pool.restarts
                        if sent.alone:
                            failed(task, "a worker process stopped while working on it")
                        else:
                            suspects.append(sent)
                        continue
                    except Exception as exc:  # noqa: BLE001 - one photograph
                        log.debug("job engine: %s of item %d failed: %s",
                                  task.kind, task.item, exc)
                        failed(task, f"{type(exc).__name__}: {exc}")
                        continue
                    derivation = DERIVATIONS[task.kind]
                    if answer == work.AGAIN:
                        parked.append(task.id)       # owed again next run
                        held.discard(task.id)
                        continue
                    if answer == work.MISSING and not derivation.missing_settles:
                        if not Path(root).is_dir():
                            # The library is not there any more (a drive
                            # unplugged): every answer from now on would be
                            # "missing" too. Stop and leave it all owed.
                            log.warning("job engine: %s is not there any more; stopping", root)
                            report.gone = True
                        parked.append(task.id)
                        held.discard(task.id)
                        continue
                    try:
                        answers.append((task, derivation.prepare(ctx, conn, task.item, answer)))
                    except Exception as exc:  # noqa: BLE001
                        log.warning("job engine: could not keep %s of item %d: %s",
                                    task.kind, task.item, exc)
                        parked.append(task.id)
                        held.discard(task.id)
                if broken:
                    log.warning("job engine: a worker process stopped; %d tasks are tried "
                                "again one at a time", len(suspects))
                    pool.restart()
                if len(answers) >= GROUP_SIZE or give_ups or \
                        time.monotonic() - last_write >= GROUP_SECONDS:
                    write()
                if report.gone:
                    break
                if holding is not None and (inflight or suspects) and holding():
                    put_down()
                if not take_turn():
                    report.stopped = True
                    break
        finally:
            for future in inflight:
                future.cancel()
            try:
                write()
            finally:
                # What was in hand and not answered goes back, its attempt
                # not counted: a stop is not the photograph's fault.
                self.store.release(set(parked) | held)
                report.restarts = pool.restarts
                if own_pool:
                    pool.shutdown(wait=False)
        counts = self.store.counts(root)
        report.left = sum(counts.get(k, {}).get("queued", 0) for k in kinds)
        log.info("job engine: %s settled in %.1f s (%s), %s left%s", f"{settled:,}",
                 time.monotonic() - started,
                 ", ".join(f"{k} {report.done[k]:,} done/{report.found[k]:,} found"
                           f"/{report.failed[k]:,} set aside" for k in kinds),
                 f"{report.left:,}", ", stopped" if report.stopped else "")
        return report

    def _claim(self, ctx: Context, kinds: list[str], inflight: Iterable[_Sent],
               room: int, owner: str | None = None) -> tuple[list[_Task], bool]:
        """Take up to *room* tasks, the highest priority first, no kind past
        what it may have in flight at once (:attr:`Derivation.at_once`).

        Returns the tasks and whether a kind was left waiting at its limit,
        which is not the list running out."""
        busy: dict[str, int] = {}
        for sent in inflight:
            busy[sent.task.kind] = busy.get(sent.task.kind, 0) + 1
        open_kinds: list[str] = []
        capped = False
        for kind in sorted(kinds, key=lambda k: -DERIVATIONS[k].priority):
            limit = DERIVATIONS[kind].at_once
            if limit is not None and busy.get(kind, 0) >= max(1, int(limit(ctx))):
                capped = True
                continue
            open_kinds.append(kind)
        if not any(DERIVATIONS[k].at_once for k in open_kinds):
            # Nothing held to a limit: one claim over every kind, by priority
            # and then rank, as 2.0 takes them.
            return [_Task(*row) for row in self.store.claim(open_kinds, root=ctx.root,
                                                            limit=room, owner=owner)], capped
        claimed: list[_Task] = []
        for kind in open_kinds:
            if room <= 0:
                break
            limit = DERIVATIONS[kind].at_once
            take = room if limit is None else min(room, max(1, int(limit(ctx))) - busy.get(kind, 0))
            got = [_Task(*row) for row in self.store.claim([kind], ctx.root, take, owner)]
            claimed.extend(got)
            room -= len(got)
        return claimed, capped

    def status(self) -> dict[str, dict[str, int]]:
        """``{kind: {state: count}}`` over every library folder."""
        return self.store.counts()
