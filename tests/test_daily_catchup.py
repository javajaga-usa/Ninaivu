"""The daily catch-up: heavy work kept for one stretch of the day, run only while idle.

The rules are in ninaivu/server/workload.py (when analysis may run), the
measuring in server/idle.py (how busy the rest of the computer is), and the
starting in server/catchup.py (what owed work is started in the stretch).
These check each against a clock and a processor reading the test controls.
"""

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ninaivu.media import orientnet, straighten
from ninaivu.server import catchup as catchup_mod
from ninaivu.server import idle
from ninaivu.server import workload as wl


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def local(hour, minute=0):
    """A timestamp at this local time of day, today."""
    now = time.localtime()
    return time.mktime((now.tm_year, now.tm_mon, now.tm_mday, hour, minute, 0,
                        0, 0, -1))


class Processor:
    """What psutil would say: every program's busy seconds and Ninaivu's own,
    moved on by the test."""

    def __init__(self, cores=4):
        self.cores = cores
        self.busy = 0.0
        self.own = {(1, 0.0): 0.0}

    def run(self, seconds, others=0.0, ninaivu=0.0):
        """*seconds* pass, with others and Ninaivu each using that share of every core."""
        self.busy += seconds * self.cores * (others + ninaivu)
        self.own[(1, 0.0)] += seconds * self.cores * ninaivu

    def __call__(self):
        return self.busy, dict(self.own), self.cores


def make(at=None, *, on=True, mode="balanced", start="02:00", hours=6):
    cfg = SimpleNamespace(workload_mode=mode, workload_night_start="23:00",
                          workload_night_end="06:00", daily_catchup=on,
                          daily_catchup_at=start, daily_catchup_hours=hours)
    clock = Clock(at if at is not None else local(3))
    work = wl.Workload(cfg, clock=clock)
    processor = Processor()
    work.others = idle.OtherLoad(sample=processor, clock=clock)
    return work, clock, processor


def quiet(work, clock, processor, seconds=wl.CATCHUP_IDLE_SECONDS + 10, **load):
    """Let *seconds* go by in steps, sampling as the waiting jobs would."""
    for _ in range(int(seconds // idle.SAMPLE_SECONDS) + 1):
        processor.run(idle.SAMPLE_SECONDS, **load)
        clock.now += idle.SAMPLE_SECONDS
        work.others.look()


# -- when the analysis may run ---------------------------------------------------

def test_off_leaves_the_modes_as_they_were():
    work, clock, processor = make(local(14), on=False, mode="overnight")
    assert work.hold(wl.ANALYSIS).startswith("saved for the night")
    assert work.catchup_hold() is None
    assert work.snapshot()["catchup"]["on"] is False


def test_outside_its_stretch_analysis_waits_whatever_the_mode():
    work, clock, processor = make(local(14))
    assert work.hold(wl.ANALYSIS) == "saved for the daily catch-up"
    assert not work.boost(wl.ANALYSIS)
    # New photographs still appear: indexing follows the mode.
    assert work.hold(wl.INDEX) is None


def test_in_its_stretch_it_waits_until_the_computer_has_been_idle_a_while():
    work, clock, processor = make(local(3))
    work.others.look()
    assert work.hold(wl.ANALYSIS) == "this computer is busy with other work", \
        "not yet measured for long enough"
    quiet(work, clock, processor, others=0.05)
    assert work.hold(wl.ANALYSIS) is None
    assert work.boost(wl.ANALYSIS), "its own stretch, idle: flat out"


def test_other_programs_busy_keep_it_waiting():
    work, clock, processor = make(local(3))
    quiet(work, clock, processor, others=0.5)
    assert work.hold(wl.ANALYSIS) == "this computer is busy with other work"


def test_its_own_work_does_not_count_as_the_computer_being_busy():
    """The analysis fills every core; measured as the processor alone, it
    would stop itself as soon as it started."""
    work, clock, processor = make(local(3))
    quiet(work, clock, processor, others=0.02)
    assert work.hold(wl.ANALYSIS) is None
    quiet(work, clock, processor, seconds=120, others=0.02, ninaivu=0.95)
    assert work.hold(wl.ANALYSIS) is None


def test_it_stops_when_the_computer_gets_busy_and_carries_on_when_quiet():
    work, clock, processor = make(local(3))
    quiet(work, clock, processor)
    assert work.hold(wl.ANALYSIS) is None
    # A short blip under the bar to stop does not stop it.
    quiet(work, clock, processor, seconds=10, others=0.25, ninaivu=0.7)
    assert work.hold(wl.ANALYSIS) is None
    quiet(work, clock, processor, seconds=wl.CATCHUP_BUSY_SECONDS + 5, others=0.6)
    assert work.hold(wl.ANALYSIS) == "this computer is busy with other work"
    # And it needs the full quiet stretch again before it starts.
    quiet(work, clock, processor, seconds=60)
    assert work.hold(wl.ANALYSIS) == "this computer is busy with other work"
    quiet(work, clock, processor)
    assert work.hold(wl.ANALYSIS) is None


def test_somebody_opening_the_app_stops_it_for_a_few_minutes():
    work, clock, processor = make(local(3))
    quiet(work, clock, processor)
    work.noticed("/api/segments")
    assert work.hold(wl.ANALYSIS) == "someone is using Ninaivu"
    clock.now += wl.BROWSING_SECONDS + 1
    assert work.hold(wl.ANALYSIS) == "someone is using Ninaivu", \
        "a pause over one photograph is not somebody gone to bed"
    quiet(work, clock, processor, seconds=wl.CATCHUP_QUIET_SECONDS)
    assert work.hold(wl.ANALYSIS) is None


def test_a_photo_frame_or_an_open_tab_does_not_keep_it_waiting():
    """D-17: the frame's timed fetches and the hand-over banner's check are a
    page asking on its own, not somebody using the app."""
    work, clock, processor = make(local(3))
    for _ in range(int((wl.CATCHUP_QUIET_SECONDS + wl.CATCHUP_IDLE_SECONDS) // 30) + 2):
        for path, args in (("/api/frame/next", None), ("/api/frame/picture/7", None),
                           ("/api/thumb/7", {"s": "640", "frame": "1"}),
                           ("/api/handover/claims", None)):
            work.noticed(path, background=wl.background_request(path, {}, args))
        quiet(work, clock, processor, seconds=30)
    assert work.hold(wl.ANALYSIS) is None
    assert work.browsing(), "for the modes a frame on the wall is still somebody looking"
    # A page can mark any other fetch it makes on its own.
    assert wl.background_request("/api/people", {wl.BACKGROUND_HEADER: "1"}, {})
    assert not wl.background_request("/api/thumb/7", {}, {"s": "640"})
    work.noticed("/api/thumb/7")
    assert work.hold(wl.ANALYSIS) == "someone is using Ninaivu"


def test_a_video_playing_stops_it():
    work, clock, processor = make(local(3))
    quiet(work, clock, processor, seconds=wl.CATCHUP_QUIET_SECONDS)
    work.noticed("/api/file/12", ranged=True)
    assert work.hold(wl.ANALYSIS) == "someone is watching a video"


def test_the_stretch_crosses_midnight_and_ends():
    work, clock, processor = make(local(23, 30), start="23:00", hours=4)
    assert work.catchup_window().label() == "23:00–03:00"
    quiet(work, clock, processor)
    assert work.hold(wl.ANALYSIS) is None
    clock.now = local(3, 30)
    assert work.hold(wl.ANALYSIS) == "saved for the daily catch-up"


def test_all_day_waits_only_for_the_computer_to_be_idle():
    work, clock, processor = make(local(14), hours=24)
    assert work.catchup_window().always_open
    quiet(work, clock, processor)
    assert work.hold(wl.ANALYSIS) is None


def test_without_a_reading_only_the_household_rules_hold():
    work, clock, processor = make(local(3))
    work.others = idle.OtherLoad(sample=lambda: None, clock=clock)
    assert work.hold(wl.ANALYSIS) is None
    assert work.snapshot()["catchup"]["measurable"] is False


def test_the_console_is_told_where_it_stands():
    work, clock, processor = make(local(14))
    seen = work.snapshot()["catchup"]
    assert seen["on"] and seen["at"] == "02:00" and seen["label"] == "02:00–08:00"
    assert seen["open"] is False and seen["holding"] == "saved for the daily catch-up"
    analysis = next(job for job in work.snapshot()["jobs"] if job["job"] == wl.ANALYSIS)
    assert analysis["holding"] == "saved for the daily catch-up"


# -- measuring the rest of the computer ---------------------------------------

def test_a_worker_that_ends_is_not_counted_as_somebody_elses_work():
    clock = Clock(1000.0)
    processor = Processor()
    meter = idle.OtherLoad(sample=processor, clock=clock)
    meter.look()
    processor.own[(2, 5.0)] = 0.0                     # a worker starts
    for _ in range(10):
        processor.run(idle.SAMPLE_SECONDS, ninaivu=0.0)
        processor.own[(2, 5.0)] += idle.SAMPLE_SECONDS * processor.cores * 0.5
        processor.busy += idle.SAMPLE_SECONDS * processor.cores * 0.5
        clock.now += idle.SAMPLE_SECONDS
        meter.look()
    del processor.own[(2, 5.0)]                       # and ends
    processor.run(idle.SAMPLE_SECONDS)
    clock.now += idle.SAMPLE_SECONDS
    assert meter.share(40) == pytest.approx(0.0, abs=0.01)


def test_the_real_reading_works_here():
    if idle.measure() is None:
        pytest.skip("psutil is not installed here")
    busy, own, cores = idle.measure()
    assert busy >= 0 and cores >= 1 and own


# -- starting what is owed ---------------------------------------------------------

class FakeScanner:
    def __init__(self):
        self.running = False
        self._thread = None
        self.started = []

    def start(self, roots, **kw):
        self.started.append((list(roots), kw))


class FakeStraightener:
    def __init__(self):
        self.running = False
        self.stopped_by_somebody_at = 0.0
        self.held = ""
        self.asked = []

    def after_scan(self, roots):
        self.asked.append(list(roots))
        return True


def starter(work, clock, owed=("/lib",)):
    cfg = SimpleNamespace(libraries=["/lib"], active_root="/lib")
    scanner, straightener = FakeScanner(), FakeStraightener()
    owed = list(owed)
    job = catchup_mod.DailyCatchUp(cfg, work, scanner, straightener,
                                   owed=lambda: owed, clock=clock)
    return job, scanner, straightener


def test_nothing_starts_outside_the_stretch_or_while_switched_off():
    work, clock, processor = make(local(14))
    job, scanner, straightener = starter(work, clock)
    assert job.look() == "closed"
    work.cfg.daily_catchup = False
    assert job.look() == "off"
    assert not scanner.started and not straightener.asked


def test_in_the_stretch_it_waits_for_idle_then_starts_the_owed_analysis_once():
    work, clock, processor = make(local(3))
    job, scanner, straightener = starter(work, clock)
    assert job.look() == "held"
    quiet(work, clock, processor)
    assert job.look() == "scan"
    roots, kw = scanner.started[0]
    assert roots == ["/lib"] and kw["scopes"] == {Path("/lib"): frozenset()}, \
        "the analysis is carried on without walking the library again"
    scanner.running = True
    assert job.look() == "scanning"
    scanner.running = False
    # Once a stretch: a scan somebody stopped is not started again today.
    assert job.look() == "straighten"
    assert len(scanner.started) == 1
    # Tomorrow's stretch starts it again.
    clock.now += 24 * 3600
    quiet(work, clock, processor)
    assert job.look() == "scan" and len(scanner.started) == 2


def test_with_nothing_owed_it_goes_straight_to_the_straightening_survey():
    work, clock, processor = make(local(3))
    job, scanner, straightener = starter(work, clock, owed=())
    quiet(work, clock, processor)
    assert job.look() == "straighten"
    assert straightener.asked == [["/lib"]] and not scanner.started


def test_the_survey_is_started_once_a_stretch_unless_it_made_way():
    """D-19: a photograph the survey cannot read stays a candidate; the
    catch-up started the survey again every half minute through the stretch."""
    work, clock, processor = make(local(3))
    job, scanner, straightener = starter(work, clock, owed=())
    quiet(work, clock, processor)
    assert job.look() == "straighten"
    assert job.look() == "nothing" and job.look() == "nothing"
    assert len(straightener.asked) == 1
    # One that made way for the household carries on when it is quiet again.
    straightener.held = "someone is using Ninaivu"
    assert job.look() == "straighten" and len(straightener.asked) == 2
    straightener.held = ""
    assert job.look() == "nothing"
    clock.now += 24 * 3600
    quiet(work, clock, processor)
    assert job.look() == "straighten" and len(straightener.asked) == 3


def test_a_survey_somebody_stopped_today_is_left_alone():
    work, clock, processor = make(local(3))
    job, scanner, straightener = starter(work, clock, owed=())
    quiet(work, clock, processor)
    straightener.stopped_by_somebody_at = clock.now
    assert job.look() == "nothing" and not straightener.asked


def test_the_services_start_and_stop_it(cfg):
    from ninaivu import Services

    services = Services(cfg)
    assert isinstance(services.catchup, catchup_mod.DailyCatchUp)
    assert services.straightener._hold == services.workload.catchup_hold
    services.catchup.keep()
    assert services.catchup._thread.is_alive()
    services.catchup.stop(join=True)
    assert not services.catchup._thread.is_alive()


def test_it_is_a_setting_on_the_advanced_page():
    from ninaivu.server import config, settings_groups

    assert config.Config().daily_catchup is False
    assert settings_groups.group_of("daily_catchup") == "Advanced"
    assert "daily_catchup_at" in settings_groups.CLOCK
    assert settings_groups.RANGES["daily_catchup_hours"] == (1, 24)


# -- the straightening survey makes way --------------------------------------------

@pytest.fixture()
def held_straightener(scanned, monkeypatch):
    cfg, conn, _ = scanned
    cfg.straighten_requires_face = False
    cfg.straighten_auto = True
    straighten.init_schema(conn)
    monkeypatch.setattr(orientnet, "available", lambda state_dir=None: True)
    monkeypatch.setattr(orientnet, "predict", lambda img, state_dir=None: (90, 0.99))
    reason = {"now": "saved for the daily catch-up"}
    job = straighten.Straightener(cfg, hold=lambda: reason["now"])
    return job, cfg, conn, reason


def _run(job):
    thread = job._thread
    if thread:
        thread.join(30)


def test_with_the_catch_up_the_survey_after_a_scan_waits_for_it(held_straightener):
    job, cfg, conn, reason = held_straightener
    assert job.after_scan([cfg.active_root]) is False
    assert conn.execute("SELECT COUNT(*) FROM orientation_seen").fetchone()[0] == 0
    reason["now"] = None
    assert job.after_scan([cfg.active_root]) is True
    _run(job)
    assert conn.execute("SELECT COUNT(*) FROM orientation_proposals").fetchone()[0] > 0


def test_a_survey_that_makes_way_keeps_what_it_did_and_carries_on_later(
        held_straightener, monkeypatch):
    job, cfg, conn, reason = held_straightener
    reason["now"] = None
    looked = {"n": 0}
    real = straighten.upright.decide

    def decide(*args, **kwargs):
        looked["n"] += 1
        if looked["n"] == 2:
            reason["now"] = "someone is using Ninaivu"
        return real(*args, **kwargs)

    monkeypatch.setattr(straighten.upright, "decide", decide)
    monkeypatch.setattr(straighten, "readers_for", lambda cfg, held=False: 1)
    total = len(job._candidates(conn, [cfg.active_root]))
    assert job.after_scan([cfg.active_root]) is True
    _run(job)
    assert job.held == "someone is using Ninaivu"
    seen = conn.execute("SELECT COUNT(*) FROM orientation_seen").fetchone()[0]
    assert 0 < seen < total, "kept what it had looked at, and stopped part way"
    reason["now"] = None
    assert job.after_scan([cfg.active_root]) is True
    _run(job)
    assert job.held == ""
    assert conn.execute("SELECT COUNT(*) FROM orientation_seen").fetchone()[0] > seen


def test_a_survey_somebody_started_is_not_held(held_straightener):
    job, cfg, conn, reason = held_straightener
    assert job.survey([cfg.active_root]) is True
    _run(job)
    assert job.held == ""
    assert conn.execute("SELECT COUNT(*) FROM orientation_proposals").fetchone()[0] > 0
