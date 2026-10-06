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

"Reachable" is more than "the folder exists". A Linux or Pi mount with
``nofail``, or a Docker bind mount started without its drive, leaves an empty,
writable folder where the drive should be, and everything that took that
folder for the library wrote onto the system disk. :func:`root_present` is the
one answer to "is the library really there", and the rule is:

* the folder must exist and be listable;
* when ``library-ids.json`` in the state folder records an id for it, the
  folder's ``.ninaivu-library`` marker must carry that id. A marker with
  another id is some other disk mounted at this path. No marker at all is
  accepted only when the folder holds something besides: an empty folder
  that lost its marker is what an absent mount looks like, while a library
  whose marker somebody deleted still has its photographs;
* with no id recorded (a library folder added since the last start, or one
  whose marker could not be written, such as an NTFS disk on a Mac), the
  folder existing is all that can be asked, as before.

The marker and the record are written at start by
:func:`ninaivu.storage.library_id.relocate`, which also tells this module
where the state folder is.
"""

from __future__ import annotations

import json
import os
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
    there = root_present(key)
    with _guard:
        _seen[key] = (when, there)
    return there


#: The state folder holding ``library-ids.json``; set at start by
#: :func:`remember_state_dir`, else the default one.
_state_dir: Path | None = None
#: path of library-ids.json -> (its mtime, what it held)
_ids_cache: dict[str, tuple[float, dict[str, str]]] = {}

MARKER = ".ninaivu-library"
KNOWN = "library-ids.json"


def remember_state_dir(state_dir: str | Path) -> None:
    """Where ``library-ids.json`` lives, for callers that do not pass it."""
    global _state_dir
    _state_dir = Path(state_dir)
    forget()


def _default_state() -> Path | None:
    if _state_dir is not None:
        return _state_dir
    try:
        from ..server.config import _default_state_dir     # noqa: PLC0415
        return _default_state_dir()
    except Exception:                                      # noqa: BLE001
        return None


def recorded_id(root: str | Path, state_dir: str | Path | None = None) -> str | None:
    """The marker id last recorded for *root*, if any."""
    base = Path(state_dir) if state_dir is not None else _default_state()
    if base is None:
        return None
    path = base / KNOWN
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return None
    cached = _ids_cache.get(str(path))
    if cached is None or cached[0] != stamp:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        cached = (stamp, {str(k): v for k, v in data.items() if isinstance(v, str)})
        _ids_cache[str(path)] = cached
    return cached[1].get(str(root))


def _marker_id(folder: Path) -> str | None:
    try:
        data = json.loads((folder / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get("id") if isinstance(data, dict) else None
    return value if isinstance(value, str) and value else None


def holds_anything(folder: str | Path) -> bool:
    """Whether *folder* has any entry besides the library marker."""
    try:
        with os.scandir(folder) as entries:
            return any(entry.name != MARKER for entry in entries)
    except OSError:
        return False


def root_present(root: str | Path, *, state_dir: str | Path | None = None) -> bool:
    """Is the library folder *root* really there, not just its mount point?

    See the module docstring for the rule. Uncached: callers about to write
    into *root* (repair, the second copy, uploads, the cloud restore) ask it
    once per run. :func:`available` is the cached form of the same answer.
    """
    folder = Path(root)
    try:
        if not folder.is_dir():
            return False
    except OSError:
        return False
    wanted = recorded_id(root, state_dir)
    if not wanted:
        return True
    found = _marker_id(folder)
    if found is not None:
        return found == wanted
    return holds_anything(folder)


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
