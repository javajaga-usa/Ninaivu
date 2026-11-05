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

Where a step knows how far it has got (a move, an import, the copy before an
update, a check of the index), the note says so as ``progress``, between 0
and 1, and the Control Panel draws it as a bar.

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

__all__ = ["STARTING_FILE", "RESTARTING_FILE", "BEAT_SECONDS", "FRESH_SECONDS", "Note",
           "STARTING", "RESTARTING", "begin", "move_to", "step", "end", "read", "fraction"]

#: The name of the file, inside the state folder.
STARTING_FILE = "ninaivu.starting"
#: The restart helper's own note (desktop/relaunch.py): from the moment the
#: console asks for a restart, through the old server finishing what it has in
#: hand, to the new one writing its run file. Without it the Control Panel saw
#: a running server, then a stopped one, then a start, and could not tell the
#: household that all three were one restart. Named so that it is never copied
#: with the index (relocate.py leaves out what begins ``ninaivu.starting``).
RESTARTING_FILE = "ninaivu.starting.restart"
#: The note is rewritten at most this often for a step that only moved on its
#: way (a percentage); a new step is written at once.
WRITE_EVERY = 0.25
#: How often a starting server rewrites it.
BEAT_SECONDS = 2.0
#: How old the file may be and still speak for a server that is starting.
#: Many beats, so a busy disk (the copy before an update is writing to it)
#: that holds one write up does not read as a server that stopped.
FRESH_SECONDS = 30.0


#: On Windows a file cannot be replaced while anything has it open (a
#: waiter reading it, the antivirus or the search indexer looking at it):
#: os.replace and the read are tried again for this long before giving up.
SHARING_PATIENCE = 0.5
_WINDOWS = os.name == "nt"


def _patiently(action):
    """*action*, tried again while Windows says the file is in use."""
    deadline = time.monotonic() + SHARING_PATIENCE
    while True:
        try:
            return action()
        except PermissionError:
            if not _WINDOWS or time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


def fraction(done: Any, total: Any) -> float | None:
    """How far, between 0 and 1, or None when it is not known."""
    try:
        done, total = float(done), float(total)
    except (TypeError, ValueError):
        return None
    if total <= 0 or done != done or total != total:
        return None
    return round(min(1.0, max(0.0, done / total)), 4)


class Note:
    """One process's note in the state folder, kept fresh while it lasts."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._lock = threading.Lock()
        self._current: dict[str, Any] | None = None
        self._wake = threading.Event()
        self._written = 0.0

    def path(self, state_dir: str | Path) -> Path:
        return Path(state_dir) / self.name

    def _write(self, note: dict[str, Any]) -> None:
        target = self.path(note["state_dir"])
        body = {k: v for k, v in note.items() if k != "state_dir"}
        body["at"] = time.time()
        self._written = time.monotonic()
        partial = target.with_name(f"{target.name}.{os.getpid()}.tmp")
        try:
            partial.write_text(json.dumps(body), encoding="utf-8")
            # Given up at the first refusal, the note was simply not there: the
            # Control Panel lost the start it was following for a beat.
            _patiently(lambda: os.replace(partial, target))
        except OSError:
            # Only a waiter's comfort: a start never fails over it.
            try:
                partial.unlink(missing_ok=True)
            except OSError:
                pass

    def _beat(self) -> None:
        while True:
            self._wake.wait(BEAT_SECONDS)
            self._wake.clear()
            with self._lock:
                if self._current is None:
                    return
                self._write(self._current)

    def begin(self, state_dir: str | Path, what: str = "starting") -> None:
        """Start saying how far this has got. Once per process."""
        with self._lock:
            if self._current is not None:
                return
            try:
                Path(state_dir).mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            now = time.time()
            self._current = {"state_dir": str(state_dir), "pid": os.getpid(), "step": what,
                             "since": now, "step_since": now}
            # A wake left by an earlier note's end() would have this one's
            # heartbeat write at once, while a waiter may be reading.
            self._wake.clear()
            self._write(self._current)
        atexit.register(self.end)
        threading.Thread(target=self._beat, daemon=True, name=f"ninaivu-{self.name}").start()

    def move_to(self, state_dir: str | Path) -> None:
        """Keep the same note when its index changes location."""
        with self._lock:
            if self._current is None:
                return
            old = self.path(self._current["state_dir"])
            self._current["state_dir"] = str(state_dir)
            self._write(self._current)
            if old != self.path(state_dir):
                try:
                    old.unlink(missing_ok=True)
                except OSError:
                    pass

    def step(self, what: str, done: Any = None, total: Any = None) -> None:
        """Moved on to *what*, in words for the person waiting; *done* of
        *total* when how far it has got is known, for a progress bar."""
        with self._lock:
            if self._current is None:
                return
            moved_on = self._current.get("step") != what
            if moved_on:
                self._current["step"] = what
                self._current["step_since"] = time.time()
            share = fraction(done, total)
            if share is None:
                self._current.pop("progress", None)
            else:
                self._current["progress"] = share
            # A percentage told for every file would rewrite the note
            # hundreds of times a second; the heartbeat carries the rest.
            if moved_on or time.monotonic() - self._written >= WRITE_EVERY or share == 1.0:
                self._write(self._current)

    def end(self) -> None:
        """Over: serving, or given up."""
        with self._lock:
            note, self._current = self._current, None
        self._wake.set()
        if note is None:
            return
        try:
            self.path(note["state_dir"]).unlink(missing_ok=True)
        except OSError:
            pass

    def read(self, state_dir: str | Path, *, now: float | None = None) -> dict[str, Any] | None:
        """The note as it is now, or None.

        None when there is no note, when it cannot be read, and when it has
        not been rewritten for :data:`FRESH_SECONDS` (stopped moving, or the
        leftover of a process that is gone). Looked for where the index is
        now, and where it was: a restart that switches the index folder
        began its note in the old one.
        """
        from ..storage.relocate import resolve
        given = Path(state_dir)
        try:
            places = [resolve(given)]
        except (OSError, ValueError):
            places = []
        if given not in places:
            places.append(given)
        for place in places:
            path = self.path(place)
            try:
                note = json.loads(_patiently(lambda path=path: path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
            if not isinstance(note, dict):
                continue
            try:
                age = (time.time() if now is None else now) - float(note.get("at") or 0)
            except (TypeError, ValueError):
                continue
            if age <= FRESH_SECONDS:
                return note
        return None


STARTING = Note(STARTING_FILE)
RESTARTING = Note(RESTARTING_FILE)


def begin(state_dir: str | Path, what: str = "starting") -> None:
    """Start saying how far this start has got. Once per process."""
    STARTING.begin(state_dir, what)


def move_to(state_dir: str | Path) -> None:
    """Keep the same start's heartbeat when its index changes location."""
    STARTING.move_to(state_dir)


def step(what: str, done: Any = None, total: Any = None) -> None:
    """The start has moved on to *what*; *done* of *total* when known."""
    STARTING.step(what, done, total)


def end() -> None:
    """The start is over: serving, or given up."""
    STARTING.end()


def read(state_dir: str | Path, *, now: float | None = None) -> dict[str, Any] | None:
    """The note of a server that is starting now, or None (:meth:`Note.read`)."""
    return STARTING.read(state_dir, now=now)
