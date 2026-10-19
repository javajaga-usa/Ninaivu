"""Worker functions for tests/test_job_engine.py.

They run inside the job engine's worker processes, which import them by
module name, so they live in a module of their own rather than in the test.
"""
from __future__ import annotations

import os
from pathlib import Path


def note(log: str, item: int) -> None:
    with open(log, "a", encoding="utf-8") as out:
        out.write(f"{item}\n")


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
