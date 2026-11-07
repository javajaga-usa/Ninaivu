"""The supervisor that keeps Ninaivu's background loops running (4.0, proposal 2)."""

from __future__ import annotations

import logging
import threading
import time

import pytest

from ninaivu import work


@pytest.fixture(autouse=True)
def _clean():
    work.forget()
    yield
    work.forget()


def _wait(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_a_loop_that_raises_is_started_again_and_logged(caplog):
    stop = threading.Event()
    calls = []

    def loop():
        calls.append(1)
        if len(calls) < 3:
            raise ValueError("one bad answer")
        stop.wait()

    with caplog.at_level(logging.ERROR, logger="ninaivu.work"):
        thread = work.keep_running("test-loop", loop, stop=stop, retry_first=0.01,
                                   retry_longest=0.05)
        assert thread.name == "test-loop"
        assert _wait(lambda: len(calls) == 3)
    (job,) = work.jobs()
    assert job["name"] == "test-loop"
    assert job["restarts"] == 2
    assert job["last_error"] == "ValueError: one bad answer"
    assert job["state"] == "running" and job["alive"]
    assert sum("starting it again" in r.getMessage() for r in caplog.records) == 2
    stop.set()
    thread.join(2)
    assert not thread.is_alive()
    assert work.jobs()[0]["state"] == "stopped"


def test_a_loop_that_returns_is_finished_and_not_started_again():
    stop = threading.Event()
    calls = []
    thread = work.keep_running("test-once", lambda: calls.append(1), stop=stop)
    thread.join(2)
    assert calls == [1]
    assert work.jobs()[0]["state"] == "finished"


def test_stopping_ends_the_wait_before_a_retry():
    stop = threading.Event()

    def loop():
        raise RuntimeError("down")

    thread = work.keep_running("test-stop", loop, stop=stop, retry_first=60)
    assert _wait(lambda: work.jobs()[0]["state"] == "waiting")
    stop.set()
    thread.join(2)
    assert not thread.is_alive()
    assert work.jobs()[0]["state"] == "stopped"


def test_no_retry_once_the_owner_is_stopping():
    stop = threading.Event()
    calls = []

    def loop():
        calls.append(1)
        stop.set()
        raise RuntimeError("while stopping")

    work.keep_running("test-stopping", loop, stop=stop, retry_first=0.01).join(2)
    assert calls == [1]
    assert work.jobs()[0]["state"] == "stopped"


def test_the_wait_doubles_up_to_the_longest():
    stop = threading.Event()
    stamps = []

    def loop():
        stamps.append(time.monotonic())
        if len(stamps) >= 5:
            stop.wait()
            return
        raise RuntimeError("again")

    thread = work.keep_running("test-backoff", loop, stop=stop, retry_first=0.02,
                               retry_longest=0.08)
    assert _wait(lambda: len(stamps) >= 5)
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert gaps[1] >= 0.035 and gaps[2] >= 0.07 and gaps[3] >= 0.07
    stop.set()
    thread.join(2)


def test_the_supervised_loops_keep_their_thread_names():
    """Every loop moved under the supervisor still runs under its old name,
    so the logs and anything looking for the thread read as before."""
    from pathlib import Path

    root = Path(work.__file__).resolve().parents[1]
    names = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        names += [line.split('keep_running("', 1)[1].split('"', 1)[0]
                  for line in text.splitlines() if 'keep_running("' in line]
    assert len(names) >= 21
    assert len(set(names)) == len(names)
    for expected in ("archive-guardian", "mugil-keeper", "ninaivu-backup", "bin-sweep",
                     "ninaivu-xmp-schedule", "ninaivu-xmp-read-timer"):
        assert expected in names
