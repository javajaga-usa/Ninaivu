"""A job that has been stopped does not go on talking.

`stop()` sets an event. A walk between two batches then carries on to the end
of whatever it was doing and announces that — so a scanner somebody had
already stopped went on pushing phases into an event stream that had outlived
it.

That is not theoretical. It failed a full suite run on main:

    assert {'phase': 'indexed', 'status': 'indexing', ...} == ': keep-alive'

A previous test's scanner, stopped but not silent, pushing into the next
test's stream. The test was over-specified and has been fixed separately;
this is the other half, which is that a stopped scan should have nothing to
say in the first place.
"""

from __future__ import annotations


import pytest

from ninaivu.media.scanner import Scanner


@pytest.fixture()
def scanner(cfg):
    watcher = Scanner(cfg)
    yield watcher
    watcher.stop(join=True)


def heard(scanner) -> list[dict]:
    said: list[dict] = []
    scanner.add_listener(said.append)
    return said


def test_a_running_scan_announces_itself(scanner):
    said = heard(scanner)
    scanner._notify({"phase": "indexed"})
    assert [s["phase"] for s in said] == ["indexed"]


def test_a_stopped_scan_says_nothing(scanner):
    """The bug, in one line."""
    said = heard(scanner)
    scanner.stop()
    scanner._notify({"phase": "indexed"})
    assert said == []


def test_a_stopped_scan_may_still_say_it_finished(scanner):
    """A scan that got to the end is allowed to say so, even if the stop
    arrived while it was finishing — otherwise the gallery is left waiting
    for a completion that happened."""
    said = heard(scanner)
    scanner.stop()
    scanner._notify({"phase": "done"})
    assert [s["phase"] for s in said] == ["done"]


def test_starting_again_makes_it_talk_again(scanner, cfg):
    """`start` clears the stop; a scanner stopped once is not stopped for good."""
    said = heard(scanner)
    scanner.stop()
    scanner._notify({"phase": "indexed"})
    assert said == []

    scanner._stop.clear()
    scanner._notify({"phase": "indexed"})
    assert [s["phase"] for s in said] == ["indexed"]


def test_a_listener_that_throws_is_not_the_others_problem(scanner):
    """One bad listener must not silence the stream for everybody."""
    said: list[dict] = []
    scanner.add_listener(lambda _p: (_ for _ in ()).throw(RuntimeError("no")))
    scanner.add_listener(said.append)
    scanner._notify({"phase": "indexed"})
    assert [s["phase"] for s in said] == ["indexed"]
