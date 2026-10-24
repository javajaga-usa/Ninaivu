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
  folder existing is all that can be asked, as before;
* on a Mac, a library under ``/Volumes/<disk>`` needs ``/Volumes/<disk>`` to
  be a disk mounted there. Anything that writes to ``/Volumes/Photos`` while
  that disk is unplugged makes a plain folder of that name on the Mac's own
  disk, and the drive, plugged back in, is mounted beside it as
  ``/Volumes/Photos 1``. That folder is never the library, whatever it holds
  (:func:`ghost_volume`).

The marker and the record are written at start by
:func:`ninaivu.storage.library_id.relocate`, which also tells this module
where the state folder is.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

#: How long an answer is believed. See the module docstring.
BELIEVE_FOR = 30.0

_seen: dict[str, tuple[float, bool]] = {}
_guard = threading.Lock()
#: One lock per folder, held by the thread asking the disk about it.
_probing: dict[str, threading.Lock] = {}


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
        probe = _probing.setdefault(key, threading.Lock())
    # One thread asks the disk at a time. Every request thread that found the
    # answer out of date used to ask it at once, and a drive that is going
    # away can take seconds to answer each of them — the whole family's
    # browsing stood still behind it. The others are given the last answer;
    # only a first-ever question, with no answer to give, waits for this one.
    if not probe.acquire(blocking=cached is None):
        return cached[1]
    try:
        with _guard:
            fresh = _seen.get(key)
            if fresh and when - fresh[0] < BELIEVE_FOR:
                return fresh[1]            # answered while this one waited
        there = root_present(key)
        with _guard:
            _seen[key] = (when, there)
        return there
    finally:
        probe.release()


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


#: Where a Mac mounts every disk but the one it started from.
VOLUMES = Path("/Volumes")


def _on_a_mac() -> bool:
    return sys.platform == "darwin"


def volume_of(path: str | Path) -> Path | None:
    """``/Volumes/<disk>`` for a path on a Mac's other disks, else None."""
    if not _on_a_mac():
        return None
    try:
        below = Path(path).relative_to(VOLUMES).parts
    except ValueError:
        return None
    return VOLUMES / below[0] if below else None


def ghost_volume(path: str | Path) -> bool:
    """Whether *path* is under a folder in /Volumes that no disk is mounted at.

    The Mac's own disk is listed in /Volumes as a link to ``/``, and counts as
    mounted. A folder that cannot be asked about is not called a ghost: that
    is for :func:`root_present` to settle, as before.
    """
    volume = volume_of(path)
    if volume is None:
        return False
    try:
        if volume.is_symlink():
            return False
        return volume.is_dir() and not os.path.ismount(volume)
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
    if ghost_volume(folder):
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


#: In the state folder: what each library folder's thumbnails are named by
#: (``{root: key}``), see :func:`thumb_key`.
THUMB_KEYS = "thumb-keys.json"
#: path of thumb-keys.json -> (its mtime, what it held)
_keys_cache: dict[str, tuple[float, dict[str, str]]] = {}


def _thumb_keys(state_dir: str | Path | None = None) -> tuple[Path | None, dict[str, str]]:
    base = Path(state_dir) if state_dir is not None else _default_state()
    if base is None:
        return None, {}
    path = base / THUMB_KEYS
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return path, {}
    cached = _keys_cache.get(str(path))
    if cached is None or cached[0] != stamp:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        cached = (stamp, {str(k): v for k, v in data.items() if isinstance(v, str) and v})
        _keys_cache[str(path)] = cached
    return path, dict(cached[1])


def _save_thumb_keys(path: Path, keys: dict[str, str]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(keys, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    _keys_cache.pop(str(path), None)


def thumb_key(root: str | Path, *, state_dir: str | Path | None = None) -> str:
    """What a library folder's thumbnails are named by (media.thumb_name).

    Before 3.0 it was the folder's absolute path, so a library moved to
    another disk, mounted under another name or drive letter, or opened on
    another computer, had every one of its thumbnails renamed (366,000 files
    on the family's library). Now each library folder is given a key of its
    own at start (:func:`give_thumb_keys`), kept in the state folder beside
    the index and carried along when the library moves (:func:`move_thumb_key`),
    so the names stay as they are. The key belongs to this index, not to the
    folder's ``.ninaivu-library`` marker: a copy of a library added as a
    library of its own carries the same marker, and must never share its
    thumbnails. A folder with no key yet (added since the last start) is
    named by its path, as before; every name is recorded in the index, so the
    two kinds live side by side.
    """
    _, keys = _thumb_keys(state_dir)
    key = keys.get(str(root))
    return f"library:{key}" if key else str(root)


def give_thumb_keys(state_dir: str | Path, roots) -> None:  # noqa: ANN001
    """Give every library folder in *roots* a thumbnail key, once. Called at
    start, before anything opens the index (library_id.relocate)."""
    import secrets                                          # noqa: PLC0415

    path, keys = _thumb_keys(state_dir)
    if path is None:
        return
    missing = [str(r) for r in roots if str(r) not in keys]
    if not missing:
        return
    for root in missing:
        keys[root] = secrets.token_hex(8)
    try:
        _save_thumb_keys(path, keys)
    except OSError as exc:
        log.warning("could not keep the thumbnail names of the library folders: %s", exc)


def move_thumb_key(state_dir: str | Path, old: str, new: str) -> None:
    """The library at *old* is now at *new* (storage/reroot.py): its
    thumbnails keep their names."""
    path, keys = _thumb_keys(state_dir)
    if path is None or str(old) not in keys:
        return
    keys[str(new)] = keys.pop(str(old))
    _save_thumb_keys(path, keys)


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
