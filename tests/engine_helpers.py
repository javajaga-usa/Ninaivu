"""Worker functions for tests/test_job_engine.py.

They run inside the job engine's worker processes, which import them by
module name, so they live in a module of their own rather than in the test.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path


def note(log: str, item: int) -> None:
    """Record that *item* was worked on, as a file of its own in *log*.

    One file per note, because appends to a shared file from two processes
    at once can lose a line on Windows.
    """
    Path(log).mkdir(exist_ok=True)
    (Path(log) / f"{item}-{uuid.uuid4().hex}").touch()


def shout(log: str, item: int, word: str) -> str:
    """The word in capitals, noting that *item* was worked on."""
    note(log, item)
    return word.upper()


def crash_on(log: str, item: int, word: str, bad: int) -> str:
    """As :func:`shout`, but the worker process dies on item *bad*."""
    if item == bad:
        os._exit(3)
    return shout(log, item, word)


def fail_on(log: str, item: int, word: str, bad: int) -> str:
    if item == bad:
        raise ValueError("cannot read this one")
    return shout(log, item, word)


def gone(path: str) -> str:
    return "missing" if not Path(path).exists() else "here"


def slow(log: str, item: int, word: str) -> str:
    import time
    time.sleep(0.2)
    return shout(log, item, word)


def nap(seconds: float) -> None:
    import time
    time.sleep(seconds)


def plan_slowly(db_path: str, roots, model: str = "sface") -> dict:
    """Stands in for grouping a very large library's faces."""
    nap(120)
    return {}


def die(*_args) -> None:
    os._exit(3)


def again(log: str, item: int, word: str) -> str:
    """Not done this time, for a reason that is not the file's."""
    note(log, item)
    return "again"
