"""Whether a library folder is actually there, asked in one place.

The index is the part of Ninaivu a rescan cannot rebuild: album membership,
visibility decisions, the names somebody put to ten face clusters, dates they
corrected by hand. The photographs can be scanned again; none of that can.

What threatens it is a drive that is not plugged in. `db.delete_missing`
guards against that with three rules, and two of them are sound — a walk that
raised any error is not evidence, and a walk that found nothing at all is a
mountpoint rather than an empty library. The third is
:data:`db.PRUNE_CEILING`, which allows removing up to half a root in one
pass. That is a *heuristic*, and on a machine whose drives have a history of
dropping off the bus it is a bet.

This is the thing that turns the bet into an answer. A root that is not
reachable is not a root whose files were deleted, and nothing may conclude
otherwise — whatever proportion of a walk appears to be missing.

Asked through a short cache because the callers ask per file and a dead drive
can take seconds to answer. Thirty seconds is long enough that a scan does
not stat a disconnected disk once per photograph, and short enough that
plugging it back in is noticed almost at once.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

#: How long an answer is believed. See the module docstring.
BELIEVE_FOR = 30.0

_seen: dict[str, tuple[float, bool]] = {}
_guard = threading.Lock()


def available(root: str | Path, *, now: float | None = None) -> bool:
    """Is this library folder reachable right now?

    A folder that cannot be listed, a drive letter with nothing behind it and
    a network share that has gone away all answer False. So does a path that
    raises — a device that is mid-reset raises rather than returning, and
    "it errored" is not "the photographs are gone".
    """
    key = str(root)
    when = time.time() if now is None else now
    with _guard:
        cached = _seen.get(key)
        if cached and when - cached[0] < BELIEVE_FOR:
            return cached[1]
    try:
        there = Path(key).is_dir()
    except OSError:
        there = False
    with _guard:
        _seen[key] = (when, there)
    return there


def unavailable(roots) -> list[str]:
    """The ones that are not there, in the order given."""
    return [str(root) for root in roots if not available(root)]


def forget(root: str | Path | None = None) -> None:
    """Drop what is remembered, so the next ask goes to the disk.

    Called when something has changed that makes the cached answer a lie: a
    library folder added or removed, or a test that has just created one.
    """
    with _guard:
        if root is None:
            _seen.clear()
        else:
            _seen.pop(str(root), None)
