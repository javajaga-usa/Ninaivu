"""How far a start has got, for whoever is waiting on it.

The Control Panel, and the helper behind the console's Restart, start the
server and then wait for its run file (runfile.py). They used to give up
after a fixed minute and say "Startup is taking longer than expected". A
start can rightly take longer than that: the first start after an update
copies the settings and the whole index first (storage/upgrade.py), and on a
library of 210,000 photographs that copy alone is a 2.7 GB index and about a
minute; an index brought in from another computer is put in place first, too.
The server was fine, and the alert said it was not.

So while it starts, the server keeps one small file in the state folder
saying which step it is on, and rewrites it every couple of seconds. A waiter
that sees the file fresh knows the server is alive and what it is doing, and
waits as long as that stays true; a file gone stale is a server that stopped
moving, which a fixed limit could never tell apart from a slow one.

The file goes once the run file is written, and with the process.
"""

from __future__ import annotations

import atexit
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

__all__ = ["STARTING_FILE", "BEAT_SECONDS", "FRESH_SECONDS", "begin", "step", "end", "read"]

#: The name of the file, inside the state folder.
STARTING_FILE = "ninaivu.starting"
#: How often a starting server rewrites it.
BEAT_SECONDS = 2.0
#: How old the file may be and still speak for a server that is starting.
#: Many beats, so a busy disk (the copy before an update is writing to it)
#: that holds one write up does not read as a server that stopped.
FRESH_SECONDS = 30.0

_lock = threading.Lock()
_current: dict[str, Any] | None = None
_wake = threading.Event()


def _path(state_dir: str | Path) -> Path:
    return Path(state_dir) / STARTING_FILE


def _write(note: dict[str, Any]) -> None:
    target = _path(note["state_dir"])
    body = {k: v for k, v in note.items() if k != "state_dir"}
    body["at"] = time.time()
    partial = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        partial.write_text(json.dumps(body), encoding="utf-8")
        os.replace(partial, target)
    except OSError:
        # Only a waiter's comfort: a start never fails over it.
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass


def _beat() -> None:
    while True:
        _wake.wait(BEAT_SECONDS)
        _wake.clear()
        with _lock:
            if _current is None:
                return
            _write(_current)


def begin(state_dir: str | Path, what: str = "starting") -> None:
    """Start saying how far this start has got. Once per process."""
    global _current
    with _lock:
        if _current is not None:
            return
        try:
            Path(state_dir).mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        _current = {"state_dir": str(state_dir), "pid": os.getpid(), "step": what,
                    "since": time.time(), "step_since": time.time()}
        _write(_current)
    atexit.register(end)
    threading.Thread(target=_beat, daemon=True, name="ninaivu-starting").start()


def step(what: str) -> None:
    """The start has moved on to *what*, in words for the person waiting."""
    with _lock:
        if _current is None:
            return
        _current["step"] = what
        _current["step_since"] = time.time()
        _write(_current)


def end() -> None:
    """The start is over: serving, or given up."""
    global _current
    with _lock:
        note, _current = _current, None
    _wake.set()
    if note is None:
        return
    try:
        _path(note["state_dir"]).unlink(missing_ok=True)
    except OSError:
        pass


def read(state_dir: str | Path, *, now: float | None = None) -> dict[str, Any] | None:
    """The note of a server that is starting now, or None.

    None when there is no note, when it cannot be read, and when it has not
    been rewritten for :data:`FRESH_SECONDS` (a start that stopped moving, or
    the leftover of a process that is gone).
    """
    try:
        note = json.loads(_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(note, dict):
        return None
    try:
        age = (time.time() if now is None else now) - float(note.get("at") or 0)
    except (TypeError, ValueError):
        return None
    if age > FRESH_SECONDS:
        return None
    return note
