"""Finding a library folder again after it has moved.

Everything Ninaivu knows about a library is keyed by its absolute path: every
row in the index and the name of every thumbnail (see ``storage/reroot.py``).
The path changes more often than the library does. A Mac mounts a second disk
with the same name as ``/Volumes/Photos 1``; Windows hands out drive letters
in the order things are plugged in; a new computer has a new home folder. Each
time, the library looked empty until somebody knew to run ``ninaivu reroot``.

So each library folder carries a small file, ``.ninaivu-library``, holding a
random id, and the state folder remembers which id each library folder had.
At start, a library folder that is not there is looked for, by that id, on the
other disks this computer has mounted. What is found is not adopted on its
own: a manual backup copy carries the same id as the library it copies, and
when the library's own drive is merely unplugged the copy is the only folder
with that id, so it looked exactly like a library that had moved. New photos
then went into the backup, and the index followed it. Instead the find is
recorded in ``library-relocation.json`` as a pending relocation
(:func:`pending_relocations`), and a person confirms which folder is the
library (:func:`confirm_relocation`, or ``ninaivu reroot``). The confirmed
move is made at the next start, thumbnails and all, before anything else
opens the index.

Deliberately cautious about where it looks and what it accepts:

* only local disks: a network share is where backups live (the Pi's
  ``Ninaivu`` share), and a backup copy carries the same id as the library it
  copies. Writing new photos into the backup would be worse than an empty
  gallery;
* never on its own: one match may be a copy while the library's drive is
  unplugged, and two folders with the same id are a library and a copy of
  it; nothing here can tell which is which, so it records what it found and
  leaves the choice to a person;
* only at start, holding the server lock, before the index is opened, which
  is the state ``ninaivu reroot`` itself requires.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Callable, Iterable

from . import roots as roots_kit

log = logging.getLogger(__name__)

#: The file at the top of each library folder. A dot name, so the scanner and
#: the gallery step over it, and small enough to go along with any backup.
MARKER = roots_kit.MARKER

#: In the state folder: the id each library folder had when last seen.
KNOWN = roots_kit.KNOWN

#: In the state folder: library folders found elsewhere by id at start, waiting
#: for a person to say which one is the library. ``{old root: {"found": [...],
#: "noticed_at": time, "confirmed": chosen folder or None}}``.
PENDING = "library-relocation.json"

#: Filesystems that are someone else's disk on the network.
NETWORK_FS = frozenset({
    "smbfs", "cifs", "smb3", "nfs", "nfs4", "afpfs", "webdav", "davfs",
    "fuse.sshfs", "fuse.rclone", "9p",
})


def read_id(root: str | Path) -> str | None:
    """The id in *root*'s marker, or None when it has none (or is not there)."""
    try:
        data = json.loads((Path(root) / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get("id") if isinstance(data, dict) else None
    return value if isinstance(value, str) and value else None


def ensure_marker(root: str | Path) -> str | None:
    """*root*'s id, writing a new marker first if it has none.

    None when the folder is not there or cannot be written (an NTFS disk on a
    Mac): such a library simply cannot be found again by id.
    """
    existing = read_id(root)
    if existing:
        return existing
    folder = Path(root)
    if not folder.is_dir():
        return None
    new = uuid.uuid4().hex
    body = json.dumps({"id": new, "note": "Ninaivu uses this to find this "
                       "library folder again if its path changes. Keep it."})
    try:
        with open(folder / MARKER, "x", encoding="utf-8") as out:
            out.write(body + "\n")
    except FileExistsError:
        return read_id(root)
    except OSError:
        return None
    return new


def write_marker(root: str | Path, library: str) -> bool:
    """Give *root* the id *library*, when it has no marker yet.

    For a library rebuilt on a new computer from the copy kept elsewhere
    (mugil/rebuild.py): it is the same library, so it keeps its id, and the
    computer keeping the originals knows it again. True when *root* carries
    that id now; False when it has another, or cannot be written.
    """
    existing = read_id(root)
    if existing:
        return existing == library
    folder = Path(root)
    if not folder.is_dir() or not library:
        return False
    body = json.dumps({"id": library, "note": "Ninaivu uses this to find this "
                       "library folder again if its path changes. Keep it."})
    try:
        with open(folder / MARKER, "x", encoding="utf-8") as out:
            out.write(body + "\n")
    except FileExistsError:
        return read_id(root) == library
    except OSError:
        return False
    return True


def _load_known(state_dir: Path) -> dict[str, str]:
    try:
        data = json.loads((state_dir / KNOWN).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}


def _save_known(state_dir: Path, known: dict[str, str]) -> None:
    tmp = state_dir / f"{KNOWN}.tmp"
    try:
        tmp.write_text(json.dumps(known, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, state_dir / KNOWN)
    except OSError as exc:
        log.warning("could not record the library ids: %s", exc)


def _mounts() -> list[tuple[str, str]]:
    """(mount point, filesystem type) for every mounted filesystem, as far as
    this platform says. Empty when it cannot be found out."""
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/mounts", encoding="utf-8") as table:
                rows = [line.split() for line in table]
        except OSError:
            return []
        from ..utils.mounts import unescape            # noqa: PLC0415
        return [(unescape(r[1]), r[2]) for r in rows if len(r) >= 3]
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["/sbin/mount"], capture_output=True, text=True,
                                 timeout=10, check=False).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        found = []
        for line in out.splitlines():
            # "/dev/disk4s1 on /Volumes/Photos 1 (apfs, local, journaled)"
            if " on " not in line or " (" not in line:
                continue
            point = line.split(" on ", 1)[1].rsplit(" (", 1)[0]
            kind = line.rsplit(" (", 1)[1].split(",", 1)[0].strip(" )")
            found.append((point, kind))
        return found
    return []


def _is_network(path: Path, mounts: list[tuple[str, str]]) -> bool:
    text = str(path)
    if text.startswith("\\\\") or text.startswith("//"):
        return True
    if os.name == "nt":
        try:
            from ..server.config import network_drives             # noqa: PLC0415
            drive = os.path.splitdrive(os.path.abspath(text))[0]
            return bool(drive) and f"{drive}\\" in network_drives()
        except Exception:                                      # noqa: BLE001
            return False
    # The deepest mount point that holds the path decides.
    best, kind = "", ""
    for point, fstype in mounts:
        if (text == point or text.startswith(point.rstrip("/") + "/")) \
                and len(point) > len(best):
            best, kind = point, fstype
    return kind.lower() in NETWORK_FS


def _split_mount(old_root: str) -> tuple[list[str], str]:
    """Where *old_root*'s disk was mounted, split off: ``(parents of mount
    points to look in, path below the mount)``."""
    if len(old_root) > 1 and old_root[1] == ":":
        # A drive letter, split by hand: on a Mac ``os.path.splitdrive``
        # leaves "E:\\Photos" whole.
        return [], old_root[2:].strip("\\/")
    parts = Path(old_root).parts
    if len(parts) >= 3 and parts[1] == "Volumes":
        return ["/Volumes"], "/".join(parts[3:])
    if len(parts) >= 4 and parts[1] == "media":
        return [f"/media/{parts[2]}"], "/".join(parts[4:])
    if len(parts) >= 5 and parts[1:3] == ("run", "media"):
        return [f"/run/media/{parts[3]}"], "/".join(parts[5:])
    if len(parts) >= 3 and parts[1] == "mnt":
        return ["/mnt"], "/".join(parts[3:])
    return [], ""


def _local_mount_parents() -> list[str]:
    """Where this computer puts the disks it mounts."""
    if os.name == "nt":
        return []                       # its drives come from windows_drives
    if sys.platform == "darwin":
        return ["/Volumes"]
    try:
        import getpass                                             # noqa: PLC0415
        user = getpass.getuser()
    except Exception:                                              # noqa: BLE001
        user = ""
    places = [f"/media/{user}", f"/run/media/{user}"] if user else []
    return places + ["/media", "/mnt"]


def candidates(old_root: str, drives: Callable[[], Iterable[Path]] | None = None
               ) -> list[Path]:
    """Folders a library that was at *old_root* may be at now: the same path
    below every other mounted disk, and the top of each disk."""
    parents, tail = _split_mount(old_root)
    # And where this computer mounts its own disks: a library brought from a
    # Mac (/Volumes/…) to Linux (/media/<user>/…) was looked for only under
    # /Volumes, which Linux does not have, so it was never found.
    for place in _local_mount_parents():
        if place not in parents:
            parents.append(place)
    disks: list[Path] = []
    if os.name == "nt":
        if drives is None:
            from ..server.config import windows_drives             # noqa: PLC0415
            drives = windows_drives
        disks = list(drives())
    for parent in parents:
        try:
            disks += [p for p in Path(parent).iterdir() if p.is_dir()]
        except OSError:
            continue
    found: list[Path] = []
    for disk in disks:
        for place in ((disk / tail) if tail else disk, disk):
            if place not in found:
                found.append(place)
    return found


def relocate(cfg, *, look: Callable[[str], list[Path]] = candidates,
             mounts: Callable[[], list[tuple[str, str]]] = _mounts,
             move: Callable[[Path, str, str], object] | None = None) -> list[tuple[str, str]]:
    """Find missing library folders by id, re-root the ones a person has
    confirmed, record the rest as pending, then mark the folders that are here.

    Returns the ``(old, new)`` moves made. *cfg* is updated in memory to match
    what the re-root wrote to ``config.json``. Never raises: a library that
    cannot be found stays missing, as it always did.
    """
    if move is None:
        from .reroot import reroot as _reroot                      # noqa: PLC0415

        def move(state, old, new):
            return _reroot(state, old, new, dry_run=False)

    state = Path(cfg.state_dir)
    roots_kit.remember_state_dir(state)
    known = _load_known(state)
    waiting = pending_relocations(state)
    before = json.dumps(waiting, sort_keys=True)
    moved: list[tuple[str, str]] = []
    table = None
    for root in list(cfg.roots):
        wanted = known.get(root)
        entry = waiting.get(root) or {}
        # Chosen when an index was brought in from another computer
        # (adopt_for_import): the person asked for that, on this computer.
        imported = entry.get("by") == "import" and entry.get("confirmed")
        # Present means its own marker is there, not merely the folder: an
        # unplugged drive under a ``nofail`` mount leaves an empty folder.
        if (not wanted and not imported) or roots_kit.root_present(root, state_dir=state):
            waiting.pop(root, None)
            continue
        if table is None:
            table = mounts()
        try:
            hits = [p for p in look(root)
                    if wanted and read_id(p) == wanted and not _is_network(p, table)]
        except OSError:
            hits = []
        found = [str(p) for p in hits if str(p) not in cfg.roots]
        if (imported and imported not in found and imported not in cfg.roots
                and Path(imported).is_dir() and not _is_network(Path(imported), table)):
            found.append(imported)
        chosen = entry.get("confirmed")
        if not found:
            waiting.pop(root, None)
            continue
        if chosen not in found:
            # Recorded, not adopted: see the module docstring.
            noticed = (waiting.get(root) or {}).get("noticed_at") or time.time()
            waiting[root] = {"found": found, "noticed_at": noticed,
                             "confirmed": None}
            log.warning("library %s is missing and %d folder(s) carry its id "
                        "(%s). Not using any of them until you confirm which is "
                        "the library: a backup copy carries the same id.",
                        root, len(found), ", ".join(found))
            continue
        new = chosen
        try:
            move(state, root, new)
        except Exception as exc:                               # noqa: BLE001
            log.warning("library %s was found at %s, but re-rooting it failed: %s",
                        root, new, exc)
            continue
        log.warning("library %s was not there; found it at %s by its id and "
                    "moved the index there", root, new)
        cfg.roots = [new if r == root else r for r in cfg.roots]
        if cfg.active_root == root:
            cfg.active_root = new
        known.pop(root, None)
        waiting.pop(root, None)
        moved.append((root, new))

    for root in cfg.roots:
        # A recorded library that is not there keeps its recorded id. Minting a
        # new one into the empty folder its absent drive left behind replaced
        # the record, and the real drive, plugged back in, no longer matched.
        if known.get(root) and not roots_kit.root_present(root, state_dir=state):
            continue
        ident = ensure_marker(root)
        if ident:
            known[root] = ident
    _save_known(state, known)
    # Each library folder's thumbnails named by a key of its own rather than
    # its path, so a library that moves keeps them (roots.thumb_key).
    roots_kit.give_thumb_keys(state, cfg.roots)
    waiting = {old: entry for old, entry in waiting.items() if old in cfg.roots}
    if json.dumps(waiting, sort_keys=True) != before:
        _save_pending(state, waiting)
    roots_kit.forget()
    return moved


#: How many of an imported index's files are looked for in a folder, and how
#: many of them must be there, before the folder is taken for that library
#: when it carries no id to go by.
SAMPLE = 200
SAMPLE_FOUND = 0.9


def holds_library(index_db: Path | str, old_root: str, folder: str | Path,
                  sample: int = SAMPLE) -> bool:
    """Whether *folder* holds the files the index has under *old_root*.

    A sample of them, by their path inside the library: nine in ten there is
    the same library; a folder that merely has the same name is not.
    """
    import sqlite3                                                  # noqa: PLC0415
    from contextlib import closing                                  # noqa: PLC0415
    try:
        with closing(sqlite3.connect(Path(index_db).as_uri() + "?mode=ro", uri=True)) as conn:
            rows = [r[0] for r in conn.execute(
                "SELECT rel_path FROM assets WHERE root=? ORDER BY random() LIMIT ?",
                (old_root, sample))]
    except sqlite3.Error:
        return False
    if len(rows) < min(20, sample):
        return False
    base = Path(folder)
    present = sum(1 for rel in rows if (base / rel).exists())
    return present >= SAMPLE_FOUND * len(rows)


def adopt_for_import(state_dir: str | Path, here: Iterable[str], *,
                     look: Callable[[str], list[Path]] = candidates,
                     mounts: Callable[[], list[tuple[str, str]]] = _mounts) -> dict[str, str]:
    """After an index is brought in from another computer, say which folder
    on this computer each of its library folders is. Returns ``{old: new}``.

    The imported settings name the other computer's folders
    (``/Volumes/Photos/Library`` on the Mac it came from), which are not here,
    and on its own :func:`relocate` only records a find for a person to
    confirm on a page they may never open. Here a person has just asked for
    this index on this computer, so the find is confirmed when it is not in
    doubt:

    * a folder chosen as the library on this computer before the import
      (*here*), carrying the library's id — or holding the index's own files
      (a copy made without its dot file, or given an id of its own by a start
      on this computer before the import);
    * otherwise the one folder on this computer's local disks carrying the
      library's id. Two such folders are a library and a copy of it, and are
      left for a person, as at any start.

    The move itself is made by :func:`relocate`, at this same start, before
    anything opens the index. Never raises.
    """
    state = Path(state_dir)
    try:
        settings = json.loads((state / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    roots = [str(r) for r in (settings.get("roots") or []) if isinstance(r, str)]
    here = [str(h) for h in here if h]
    known = _load_known(state)
    waiting = pending_relocations(state)
    table = None
    chosen: dict[str, str] = {}
    for root in roots:
        try:
            if roots_kit.root_present(root, state_dir=state):
                continue                  # the same path on this computer too
        except Exception:                                          # noqa: BLE001
            pass
        if table is None:
            table = mounts()
        wanted = known.get(root)
        options: list[Path] = []
        try:
            places = [Path(h) for h in here] + list(look(root))
        except OSError:
            places = [Path(h) for h in here]
        for place in places:
            try:
                usable = place.is_dir() and not _is_network(place, table)
            except OSError:
                usable = False
            if usable and place not in options and str(place) not in roots \
                    and str(place) not in chosen.values():
                options.append(place)
        by_id = [p for p in options if wanted and read_id(p) == wanted]
        mine = [p for p in by_id if str(p) in here]
        pick = None
        if len(mine) == 1:
            pick = mine[0]
        else:
            # Chosen here and holding the index's own files: whatever id it
            # carries. A start on this computer before the import may have
            # given it one of its own, and the files say more than the id.
            same = [p for p in options if str(p) in here
                    and holds_library(state / "index.db", root, p)]
            if len(same) == 1:
                pick = same[0]
            elif len(by_id) == 1:
                pick = by_id[0]
        if pick is None:
            continue
        waiting[root] = {"found": [str(pick)], "noticed_at": time.time(),
                         "confirmed": str(pick), "by": "import"}
        chosen[root] = str(pick)
        log.warning("the imported library %s is %s on this computer", root, pick)
    if chosen:
        _save_pending(state, waiting)
    return chosen


def notice_moves(cfg, *, look: Callable[[str], list[Path]] = candidates,
                 mounts: Callable[[], list[tuple[str, str]]] = _mounts) -> dict[str, dict]:
    """While Ninaivu runs: look for the library folders that are not there.

    The look :func:`relocate` makes at start, made again when a drive is
    plugged in afterwards: a Mac that mounts the library's disk as
    ``/Volumes/Photos 1`` at noon had it found only at the next restart, and
    until then the console said only that a library folder was not connected.
    What is found is recorded as pending, exactly as at start, for a person
    to confirm; nothing is moved here (the index is open). Returns the
    pending relocations. Never raises.
    """
    try:
        state = Path(cfg.state_dir)
        known = _load_known(state)
        waiting = pending_relocations(state)
        before = json.dumps(waiting, sort_keys=True)
        table = None
        for root in list(cfg.roots):
            wanted = known.get(root)
            if not wanted or roots_kit.root_present(root, state_dir=state):
                waiting.pop(root, None)
                continue
            if table is None:
                table = mounts()
            try:
                hits = [p for p in look(root)
                        if read_id(p) == wanted and not _is_network(p, table)]
            except OSError:
                hits = []
            found = [str(p) for p in hits if str(p) not in cfg.roots]
            if not found:
                waiting.pop(root, None)
                continue
            entry = waiting.get(root) or {}
            chosen = entry.get("confirmed")
            if chosen not in found:
                chosen = None
            if entry.get("found") != found or entry.get("confirmed") != chosen:
                log.warning("library %s is not connected, and %d folder(s) carry its "
                            "id (%s). Not using any of them until you confirm which "
                            "is the library.", root, len(found), ", ".join(found))
            waiting[root] = {"found": found,
                             "noticed_at": entry.get("noticed_at") or time.time(),
                             "confirmed": chosen}
        waiting = {old: entry for old, entry in waiting.items() if old in cfg.roots}
        if json.dumps(waiting, sort_keys=True) != before:
            _save_pending(state, waiting)
        return waiting
    except Exception:                                          # noqa: BLE001
        log.warning("could not look for the library folders that moved", exc_info=True)
        return {}


def pending_relocations(state_dir: str | Path) -> dict[str, dict]:
    """Library folders found elsewhere by id and waiting for a person.

    ``{old root: {"found": [folders carrying its id], "noticed_at": time,
    "confirmed": the folder chosen, or None}}``. Empty when there are none.
    """
    try:
        data = json.loads((Path(state_dir) / PENDING).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items()
            if isinstance(v, dict) and isinstance(v.get("found"), list)}


def confirm_relocation(state_dir: str | Path, old: str, new: str) -> bool:
    """Say that *new*, one of the folders found for *old*, is the library.

    The re-root is made at the next start, which is when the index can be
    moved safely. False when *new* is not one of the folders that were found.
    """
    waiting = pending_relocations(state_dir)
    entry = waiting.get(old)
    if not entry or new not in entry["found"]:
        return False
    entry["confirmed"] = new
    _save_pending(Path(state_dir), waiting)
    return True


def _save_pending(state_dir: Path, waiting: dict[str, dict]) -> None:
    target = state_dir / PENDING
    try:
        if not waiting:
            target.unlink(missing_ok=True)
            return
        tmp = state_dir / f"{PENDING}.tmp"
        tmp.write_text(json.dumps(waiting, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        log.warning("could not record the pending library relocation: %s", exc)
