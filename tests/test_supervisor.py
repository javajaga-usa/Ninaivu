"""The supervisor: one place that runs the background loops (4.0, proposal 2).

* a round that raises is logged and tried again, and the loop never dies;
* a schedule is kept to the second, on a clock the test turns by hand;
* a stop keeps to its budget, and names what did not stop in it;
* a claim on the disk makes the library scan stand down, and a job that
  moves files waits for another that does;
* the activity list says who is doing what;
* the loops moved onto it keep their thread names and schedules.
"""
from __future__ import annotations

import logging
import threading
import time
from types import SimpleNamespace

import pytest

from ninaivu.server import supervisor as sup
from ninaivu.server.supervisor import Job, Supervisor


class TurnedClock(sup.Clock):
    """A clock that moves only when the test says so."""

    def __init__(self) -> None:
        self.t = 0.0
        self._cond = threading.Condition()
        #: {thread name: the time it is waiting for}, None for "until woken".
        self.sleeping: dict[str, float | None] = {}

    def now(self) -> float:
        with self._cond:
            return self.t

    def wait(self, wake, seconds):
        name = threading.current_thread().name
        with self._cond:
            until = None if seconds is None else self.t + max(0.0, seconds)
            self.sleeping[name] = until
            try:
                while not wake.is_set() and (until is None or self.t < until):
                    self._cond.wait(0.005)
            finally:
                self.sleeping.pop(name, None)
        return wake.is_set()

    def advance(self, seconds: float) -> None:
        with self._cond:
            self.t += seconds
            self._cond.notify_all()

    def asleep_until(self, name: str, when: float | None, timeout: float = 5.0) -> None:
        """Wait (really) until *name* is asleep waiting for *when*."""
        until(lambda: name in self.sleeping and self.sleeping[name] == when, timeout)


def until(check, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting")
        time.sleep(0.005)


@pytest.fixture()
def clock():
    return TurnedClock()


@pytest.fixture()
def supervisor(clock):
    keeper = Supervisor(clock=clock)
    yield keeper
    assert keeper.stop(timeout=5) == []


# -- a schedule is kept ------------------------------------------------------------

def test_an_interval_is_counted_from_the_end_of_each_round(supervisor, clock):
    rounds = []
    supervisor.add(Job("tick", lambda: rounds.append(clock.now()), sup.every(100, first=10)))
    clock.asleep_until("tick", 10)
    clock.advance(9.9)
    time.sleep(0.05)
    assert rounds == []
    clock.advance(0.1)
    until(lambda: rounds == [10.0])
    clock.asleep_until("tick", 110)
    clock.advance(99)
    time.sleep(0.05)
    assert rounds == [10.0]
    clock.advance(1)
    until(lambda: rounds == [10.0, 110.0])
    # The thread is the job's, by name, as every keeper's was.
    assert "tick" in [t.name for t in threading.enumerate()]


def test_without_a_first_wait_the_first_round_comes_one_interval_in(supervisor, clock):
    """``while not stop.wait(N): ...``, which is what the keepers did."""
    rounds = []
    supervisor.add(Job("tick", lambda: rounds.append(clock.now()), sup.every(60)))
    clock.asleep_until("tick", 60)
    assert rounds == []


def test_an_interval_read_afresh_each_round(supervisor, clock):
    gap = {"seconds": 10.0}
    rounds = []
    supervisor.add(Job("tick", lambda: rounds.append(clock.now()),
                       sup.every(lambda: gap["seconds"])))
    clock.asleep_until("tick", 10)
    gap["seconds"] = 50.0
    clock.advance(10)
    until(lambda: rounds == [10.0])
    clock.asleep_until("tick", 60)


def test_on_request_and_after(supervisor, clock):
    order = []
    supervisor.add(Job("first", lambda: order.append("first"), sup.on_request()))
    supervisor.add(Job("second", lambda: order.append("second"), sup.after("first")))
    time.sleep(0.05)
    assert order == []
    # Nothing waits on a request with a thread of its own.
    assert "first" not in [t.name for t in threading.enumerate()]
    assert supervisor.request("first")
    until(lambda: order == ["first", "second"])
    assert supervisor.request("first")
    until(lambda: order == ["first", "second", "first", "second"])
    assert not supervisor.request("nobody")


def test_a_job_is_not_kept_twice(supervisor, clock):
    assert supervisor.add(Job("tick", lambda: None, sup.every(60)))
    assert not supervisor.add(Job("tick", lambda: None, sup.every(60)))
    assert supervisor.stop_job("tick", timeout=5)
    # Stopped, it can be started again, as a keeper's start after its stop.
    assert supervisor.add(Job("tick", lambda: None, sup.every(60)))


# -- a loop never dies ---------------------------------------------------------------

def test_a_round_that_raises_is_logged_and_tried_again(supervisor, clock, caplog):
    rounds = []

    def flaky() -> None:
        rounds.append(clock.now())
        if len(rounds) < 3:
            raise OSError(f"the disk went away ({len(rounds)})")

    caplog.set_level(logging.ERROR)
    supervisor.add(Job("flaky", flaky, sup.at_start(), failure="could not do the thing"))
    until(lambda: rounds == [0.0])
    # Tried again after thirty seconds, then sixty: the wait grows.
    clock.asleep_until("flaky", 30)
    assert supervisor.state("flaky") == sup.RETRYING
    clock.advance(30)
    until(lambda: rounds == [0.0, 30.0])
    clock.asleep_until("flaky", 90)
    clock.advance(60)
    until(lambda: supervisor.state("flaky") == sup.DONE)
    assert rounds == [0.0, 30.0, 90.0]
    failures = [r for r in caplog.records if r.getMessage() == "could not do the thing"]
    assert len(failures) == 2 and all(r.exc_info for r in failures)
    row = next(r for r in supervisor.activity() if r["name"] == "flaky")
    assert row["last_error"] == "the disk went away (2)"


def test_a_scheduled_loop_carries_on_after_an_error(supervisor, clock, caplog):
    """The 5 November audit: two loops died for good on one exception."""
    rounds = []

    def broken() -> None:
        rounds.append(clock.now())
        raise RuntimeError("unexpected")

    caplog.set_level(logging.ERROR)
    # retry_most=0: the next try is the next round of the schedule, no later.
    supervisor.add(Job("look", broken, sup.every(15), retry_most=0))
    for when in (15, 30, 45):
        clock.asleep_until("look", when)
        clock.advance(15)
        until(lambda when=when: rounds[-1:] == [float(when)])
    assert rounds == [15.0, 30.0, 45.0]
    assert supervisor.state("look") == sup.RETRYING


def test_a_scheduled_loop_backs_off_but_never_sooner_than_its_schedule(supervisor, clock):
    rounds = []

    def broken() -> None:
        rounds.append(clock.now())
        raise RuntimeError("unexpected")

    supervisor.add(Job("look", broken, sup.every(10), retry_first=30, retry_most=60))
    clock.asleep_until("look", 10)
    clock.advance(10)
    until(lambda: rounds == [10.0])
    clock.asleep_until("look", 40)          # 30 seconds, not 10
    clock.advance(30)
    until(lambda: rounds == [10.0, 40.0])
    clock.asleep_until("look", 100)         # 60
    clock.advance(60)
    until(lambda: rounds == [10.0, 40.0, 100.0])
    clock.asleep_until("look", 160)         # still 60: the most it waits


# -- stopping --------------------------------------------------------------------------

def test_stop_keeps_to_its_budget_and_names_what_did_not_stop():
    supervisor = Supervisor()
    cut_short = threading.Event()
    stubborn = threading.Event()
    started = threading.Barrier(3, timeout=5)

    def stoppable() -> None:
        started.wait()
        cut_short.wait(30)

    def unstoppable() -> None:
        started.wait()
        stubborn.wait(30)

    supervisor.add(Job("quiet", lambda: None, sup.every(3600)))
    supervisor.add(Job("polite", stoppable, sup.at_start(), interrupt=cut_short.set))
    supervisor.add(Job("stubborn", unstoppable, sup.at_start()))
    started.wait()
    began = time.monotonic()
    try:
        left = supervisor.stop(timeout=0.5)
        took = time.monotonic() - began
    finally:
        stubborn.set()
    assert left == ["stubborn"]
    assert took < 1.5
    assert supervisor.state("quiet") == sup.STOPPED
    assert supervisor.state("polite") == sup.STOPPED
    # Stopped is stopped: nothing new is started afterwards.
    assert not supervisor.add(Job("late", lambda: None, sup.at_start()))


def test_pause_and_carry_on(supervisor, clock):
    rounds = []
    supervisor.add(Job("tick", lambda: rounds.append(clock.now()), sup.every(10)))
    clock.asleep_until("tick", 10)
    supervisor.pause("tick")
    clock.asleep_until("tick", None)
    clock.advance(30)
    time.sleep(0.05)
    assert rounds == [] and supervisor.state("tick") == sup.PAUSED
    supervisor.carry_on("tick")
    until(lambda: rounds == [30.0])


# -- claims ------------------------------------------------------------------------------

def test_a_claim_makes_the_scan_stand_down(cfg):
    from ninaivu.media.scanner import CLAIM_STORAGE_CHECK, Scanner

    scanner = Scanner(cfg)
    scanner.watch = lambda roots=(): None
    supervisor = Supervisor(scanner)
    seen = {}
    started = []

    def check() -> None:
        seen["held"] = scanner.deferred
        # A scan asked for meanwhile is queued, not started and not lost.
        scanner.start(full=False)
        seen["running"] = scanner.running
        seen["waiting"] = scanner.waiting
        # What starts it once the claim goes: recorded, not a real scan.
        scanner.start = lambda *a, **k: started.append((a, k))

    try:
        supervisor.add(Job("check", check, sup.at_start(), claims={sup.LIBRARY_DISK, sup.CPU},
                           reason=CLAIM_STORAGE_CHECK))
        until(lambda: supervisor.state("check") == sup.DONE)
    finally:
        assert supervisor.stop(timeout=5) == []
    assert seen == {"held": CLAIM_STORAGE_CHECK, "running": False, "waiting": True}
    assert scanner.deferred is None and len(started) == 1


class _Held:
    """The scan's claims, as Scanner keeps them; those given as plain text
    here were made by a reason alone (holder None)."""

    def __init__(self) -> None:
        self.claims: list = []

    def defer(self, reason, holder=None):
        self.claims.append(reason if holder is None else (reason, holder))

    def resume(self, reason=None, holder=None):
        self.claims.remove(reason if holder is None else (reason, holder))

    def holding(self):
        return [c if isinstance(c, tuple) else (c, None) for c in self.claims]

    @property
    def deferred(self):
        return "; ".join(dict.fromkeys(r for r, _ in self.holding())) or None


def test_a_job_that_moves_files_waits_for_another(clock):
    from ninaivu.media.scanner import CLAIM_STORAGE_CHECK

    scanner = _Held()
    scanner.claims += ["a consolidation is running", CLAIM_STORAGE_CHECK]
    supervisor = Supervisor(scanner, clock=clock)
    ran = []
    supervisor.add(Job("tidy", lambda: ran.append(scanner.deferred), sup.at_start(),
                       claims={sup.LIBRARY_DISK}, reason="tidying the library",
                       moves_files=True))
    try:
        clock.asleep_until("tidy", sup.CLAIM_LOOK_EVERY)
        assert supervisor.state("tidy") == sup.WAITING and ran == []
        # A read-only claim does not keep it waiting; the consolidation does.
        scanner.claims.remove("a consolidation is running")
        clock.advance(sup.CLAIM_LOOK_EVERY)
        until(lambda: supervisor.state("tidy") == sup.DONE)
        assert ran == [f"{CLAIM_STORAGE_CHECK}; tidying the library"]
        assert scanner.claims == [CLAIM_STORAGE_CHECK]
    finally:
        assert supervisor.stop(timeout=5) == []


def test_two_jobs_moving_files_for_the_same_reason_take_turns(clock):
    """The same reason is not the same holder: the second waits for the first."""
    scanner = _Held()
    supervisor = Supervisor(scanner, clock=clock)
    gate, ran = threading.Event(), []

    def first() -> None:
        ran.append("first")
        gate.wait(5)

    tidy = dict(claims={sup.LIBRARY_DISK}, reason="tidying the library", moves_files=True)
    try:
        supervisor.add(Job("one", first, sup.at_start(), interrupt=gate.set, **tidy))
        until(lambda: supervisor.state("one") == sup.RUNNING)
        supervisor.add(Job("two", lambda: ran.append("second"), sup.at_start(), **tidy))
        clock.asleep_until("two", sup.CLAIM_LOOK_EVERY)
        assert supervisor.state("two") == sup.WAITING
        gate.set()
        until(lambda: supervisor.state("one") == sup.DONE)
        clock.advance(sup.CLAIM_LOOK_EVERY)
        until(lambda: supervisor.state("two") == sup.DONE)
        assert ran == ["first", "second"] and scanner.claims == []
    finally:
        assert supervisor.stop(timeout=5) == []


def test_a_shared_reason_is_one_claim_per_holder(cfg):
    """The first slice's risk: claims were keyed by reason, so a job and the
    Storage page's check, both 'checking the library's storage', let go of
    each other's claim and the scan came back while one of them still read."""
    from ninaivu.media.scanner import CLAIM_STORAGE_CHECK, Scanner

    scanner = Scanner(cfg)
    scanner.watch = lambda roots=(): None
    supervisor = Supervisor(scanner)
    inside, carry_on = threading.Event(), threading.Event()

    def check() -> None:
        inside.set()
        carry_on.wait(5)

    try:
        scanner.defer(CLAIM_STORAGE_CHECK)                  # the Storage page's check
        supervisor.add(Job("check", check, sup.at_start(), claims={sup.CPU},
                           reason=CLAIM_STORAGE_CHECK, interrupt=carry_on.set))
        assert inside.wait(5)
        assert scanner.deferred == CLAIM_STORAGE_CHECK      # read as one reason
        assert len(scanner.holding()) == 2
        scanner.resume(CLAIM_STORAGE_CHECK)                 # the page's check ends
        assert scanner.deferred == CLAIM_STORAGE_CHECK      # the job still holds it
        # Two jobs with one reason: each holds and lets go of its own.
        scanner.defer(CLAIM_STORAGE_CHECK, holder="other")
        carry_on.set()
        until(lambda: supervisor.state("check") == sup.DONE)
        assert scanner.holding() == [(CLAIM_STORAGE_CHECK, "other")]
        scanner.resume(CLAIM_STORAGE_CHECK, holder="other")
        assert scanner.deferred is None
    finally:
        carry_on.set()
        assert supervisor.stop(timeout=5) == []


def test_claims_are_checked_when_a_job_is_declared():
    with pytest.raises(ValueError, match="no such claim"):
        Job("x", lambda: None, sup.at_start(), claims={"gpu"})
    with pytest.raises(ValueError, match="say why"):
        Job("x", lambda: None, sup.at_start(), claims={sup.CPU})
    with pytest.raises(ValueError, match="READ_ONLY_CLAIMS"):
        Job("x", lambda: None, sup.at_start(), claims={sup.CPU}, reason="reading things")
    Job("x", lambda: None, sup.at_start(), claims={sup.NETWORK, sup.INDEX_WRITE})


# -- the activity list -----------------------------------------------------------------

def test_the_activity_list(supervisor, clock):
    gate = threading.Event()
    supervisor.add(Job("sending", lambda: gate.wait(5), sup.at_start(), claims={sup.NETWORK},
                       progress=lambda: {"done": 3, "of": 10}, interrupt=gate.set))
    supervisor.add(Job("looking", lambda: None, sup.every(60)))
    until(lambda: supervisor.state("sending") == sup.RUNNING)
    clock.asleep_until("looking", 60)
    rows = supervisor.activity()
    assert [r["name"] for r in rows] == ["looking", "sending"]
    looking, sending = rows
    assert sending["state"] == sup.RUNNING
    assert sending["progress"] == {"done": 3, "of": 10}
    assert sending["claims"] == ["network"] and sending["stoppable"] is True
    assert sending["last_error"] is None
    assert looking["state"] == sup.IDLE and looking["when"] == "every 60 seconds"
    assert looking["progress"] is None and looking["stoppable"] is False
    assert abs(sending["since"] - time.time()) < 60
    gate.set()
    until(lambda: supervisor.state("sending") == sup.DONE)


def test_a_progress_callback_that_raises_does_not_spoil_the_list(supervisor):
    supervisor.add(Job("odd", lambda: None, sup.on_request(), progress=lambda: 1 / 0))
    assert supervisor.activity()[0]["progress"] is None


def test_the_console_reads_the_activity_list(app, as_admin, as_family):
    services = app.config["MV_SERVICES"]
    services._keep_the_bin_swept()
    try:
        response = as_admin.get("/api/admin/jobs/background")
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        row = next(r for r in response.get_json()["jobs"] if r["name"] == "bin-sweep")
        assert row["when"] == "every 86400 seconds" and row["claims"] == ["index_write"]
        assert as_family.get("/api/admin/jobs/background").status_code == 403
    finally:
        assert services.supervisor.stop(timeout=5) == []


# -- the loops moved onto it ----------------------------------------------------------

def _keepers(tmp_path):
    from ninaivu.cloud.index_copy import IndexCopy
    from ninaivu.cloud.restore_test import RestoreTester
    from ninaivu.storage.library_drives import LibraryDrives
    from ninaivu.utils.digest import DigestKeeper

    cfg = SimpleNamespace(state_dir=str(tmp_path), roots=[])
    return [
        (DigestKeeper(cfg, connect=lambda: None, roots=lambda: []),
         "ninaivu-digest", "every 1200 seconds"),
        (LibraryDrives(cfg, lambda roots: None), "ninaivu-library-drives", "every 15 seconds"),
        (IndexCopy(cfg, None, connect_db=lambda: None), "ninaivu-index-copy",
         "every 1800 seconds"),
        (RestoreTester(cfg, None, connect_db=lambda: None), "ninaivu-restore-test",
         "every 3600 seconds"),
    ]


def test_the_keepers_keep_their_threads_and_schedules(tmp_path):
    shared = Supervisor()
    keepers = _keepers(tmp_path)
    try:
        for keeper, _, _ in keepers:
            keeper.supervisor = shared
            keeper.start()
            keeper.start()                      # a second start is no second loop
        names = [t.name for t in threading.enumerate()]
        rows = {r["name"]: r for r in shared.activity()}
        for _keeper, thread, when in keepers:
            assert names.count(thread) == 1
            assert rows[thread]["when"] == when
        for keeper, thread, _ in keepers:
            keeper.stop(timeout=5)
            assert shared.state(thread) == sup.STOPPED
        assert not {thread for _, thread, _ in keepers} & {
            t.name for t in threading.enumerate()}
        # And started again after a stop, as before.
        for keeper, thread, _ in keepers:
            keeper.start()
            assert shared.state(thread) == sup.IDLE
    finally:
        assert shared.stop(timeout=5) == []


def test_a_keeper_used_by_itself_has_a_supervisor_of_its_own(tmp_path):
    keeper, thread, _ = _keepers(tmp_path)[0]
    assert keeper.supervisor is None
    keeper.start()
    try:
        assert keeper.supervisor is not None and keeper.supervisor.state(thread) == sup.IDLE
    finally:
        keeper.stop(timeout=5)
    assert thread not in [t.name for t in threading.enumerate()]


def test_the_index_copy_stop_still_reaches_an_upload_under_way(tmp_path):
    keeper, _, _ = _keepers(tmp_path)[2]
    keeper.start()
    assert not keeper._stop.is_set()
    keeper.stop(timeout=5)
    # The flag its upload paces itself by: a stop cut an upload short, and does.
    assert keeper._stop.is_set()
    keeper.start()
    assert not keeper._stop.is_set()
    keeper.stop(timeout=5)
