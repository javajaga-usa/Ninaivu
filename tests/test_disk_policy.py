"""Who gets the disk while an archive job is running.

An archive job stops moving bytes for three different reasons and
``is_scanning()`` is true for all of them, so watching that flag alone left
the library indexer stood down and the CPU pinned at full speed while nothing
was happening at all. These tests pin the policy that replaced it.

The policy is a plain state machine with the clock injected, so all of this
runs without threads, sleeps or a real archive job.
"""

import pytest

from ninaivu.api.archive_api import _DiskPolicy


class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t
    def advance(self, seconds): self.t += seconds


@pytest.fixture()
def policy():
    clock = Clock()
    return _DiskPolicy(clock=clock, release_after=25.0), clock


def test_a_running_job_keeps_the_disk_and_full_speed(policy):
    p, _ = policy
    actions = p.step(None)
    assert not any(actions.values())
    assert p.performance is True
    assert p.indexer_deferred is True


def test_a_pause_drops_to_efficient_immediately(policy):
    """Power follows the work with no delay. There is nothing to finish
    sooner while the job is parked, so holding full speed is pure waste."""
    p, _ = policy
    actions = p.step("paused")
    assert actions["to_efficient"] is True
    assert p.performance is False
    # …but the indexer is not handed over on the same tick.
    assert actions["release_indexer"] is False


def test_the_indexer_waits_out_the_debounce_before_taking_the_disk(policy):
    p, clock = policy
    p.step("paused")
    clock.advance(24.0)
    assert p.step("paused")["release_indexer"] is False
    clock.advance(2.0)
    assert p.step("paused")["release_indexer"] is True
    assert p.indexer_deferred is False


def test_resuming_takes_the_disk_back_at_once(policy):
    """Asymmetric on purpose: slow to hand over, instant to take back."""
    p, clock = policy
    p.step("paused")
    clock.advance(30.0)
    p.step("paused")                       # indexer released
    actions = p.step(None)                 # archive resumes
    assert actions["defer_indexer"] is True
    assert actions["to_performance"] is True
    assert p.indexer_deferred is True


def test_a_thermal_park_goes_efficient_but_never_wakes_the_indexer(policy):
    """The rule that is easy to get backwards.

    The pacer parks the job at 90 degrees. Efficient mode is right — more so
    than usual. Starting the library indexer is not: decoding and hashing
    photographs is hotter than the copy the machine just stopped doing, so
    the "fix" would defeat the guardrail it is reacting to.
    """
    p, clock = policy
    actions = p.step("thermal")
    assert actions["to_efficient"] is True

    for _ in range(10):
        clock.advance(60.0)
        assert p.step("thermal")["release_indexer"] is False
    assert p.indexer_deferred is True


def test_waiting_for_a_drive_releases_the_indexer(policy):
    """The case worth the most. Somebody unplugs the enclosure and goes to
    bed; nothing is contending for the disk for the next ten hours."""
    p, clock = policy
    p.step("waiting")
    clock.advance(30.0)
    assert p.step("waiting")["release_indexer"] is True


def test_a_flapping_thermostat_does_not_thrash_the_indexer(policy):
    """The pacer re-decides every two seconds and its thresholds have no
    hysteresis, so a machine sitting at 89-91 degrees oscillates. Each
    release-and-re-defer cycle restarts the indexer, and every restart
    re-walks the library — so the debounce has to survive the flapping."""
    p, clock = policy
    releases = 0
    for _ in range(40):
        clock.advance(2.0)
        releases += p.step("thermal")["release_indexer"]
        clock.advance(2.0)
        releases += p.step(None)["release_indexer"]
    assert releases == 0


def test_a_long_pause_broken_by_brief_activity_restarts_the_clock(policy):
    """Twenty seconds of pause, a moment of work, then twenty more is not
    forty seconds of idleness, and must not release the disk."""
    p, clock = policy
    p.step("paused")
    clock.advance(20.0)
    p.step("paused")             # 20s of idleness banked
    p.step(None)                 # a moment of real work resets everything

    p.step("paused")             # the clock starts again from here
    clock.advance(20.0)
    # 40 seconds have passed in total, but only 20 of them consecutively.
    assert p.step("paused")["release_indexer"] is False
    clock.advance(6.0)
    assert p.step("paused")["release_indexer"] is True


def test_power_transitions_are_edges_not_levels(policy):
    """The power API counts nesting depth, so an unbalanced call would strand
    the process in performance mode for ever. Every state must therefore act
    only on a transition."""
    p, _ = policy
    assert p.step("paused")["to_efficient"] is True
    for _ in range(5):
        actions = p.step("paused")
        assert actions["to_efficient"] is False
        assert actions["to_performance"] is False
    assert p.step(None)["to_performance"] is True
    for _ in range(5):
        assert p.step(None)["to_performance"] is False


def test_moving_between_two_idle_reasons_keeps_the_disk_handed_over(policy):
    """A pause that becomes a drive-wait is still idle; the indexer should not
    be yanked back and forth between two states that both mean 'not busy'."""
    p, clock = policy
    p.step("paused")
    clock.advance(30.0)
    assert p.step("paused")["release_indexer"] is True
    actions = p.step("waiting")
    assert actions["defer_indexer"] is False
    assert p.indexer_deferred is False
