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
other disks this computer has mounted, and when it is found in exactly one
place the library is re-rooted there, thumbnails and all, before anything else
opens the index.

Deliberately cautious about where it looks and what it accepts:

* only local disks: a network share is where backups live (the Pi's
  ``Ninaivu`` share), and a backup copy carries the same id as the library it
  copies. Writing new photos into the backup would be worse than an empty
  gallery;
* only one match: two folders with the same id are a library and a copy of
  it, and nothing here can tell which is which, so it says so and leaves both
  alone;
* only at start, holding the server lock, before the index is opened, which
  is the state ``ninaivu reroot`` itself requires.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Callable, Iterable

log = logging.getLogger(__name__)

#: The file at the top of each library folder. A dot name, so the scanner and
#: the gallery step over it, and small enough to go along with any backup.
MARKER = ".ninaivu-library"

#: In the state folder: the id each library folder had when last seen.
KNOWN = "library-ids.json"

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
        return [(r[1].replace("\\040", " "), r[2]) for r in rows if len(r) >= 3]
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


def candidates(old_root: str, drives: Callable[[], Iterable[Path]] | None = None
               ) -> list[Path]:
    """Folders a library that was at *old_root* may be at now: the same path
    below every other mounted disk, and the top of each disk."""
    parents, tail = _split_mount(old_root)
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
    """Find missing library folders by id, re-root them, then mark the rest.

    Returns the ``(old, new)`` moves made. *cfg* is updated in memory to match
    what the re-root wrote to ``config.json``. Never raises: a library that
    cannot be found stays missing, as it always did.
    """
    if move is None:
        from .reroot import reroot as _reroot                      # noqa: PLC0415

        def move(state, old, new):
            return _reroot(state, old, new, dry_run=False)

    state = Path(cfg.state_dir)
    known = _load_known(state)
    moved: list[tuple[str, str]] = []
    table = None
    for root in list(cfg.roots):
        wanted = known.get(root)
        if not wanted or Path(root).is_dir():
            continue
        if table is None:
            table = mounts()
        try:
            hits = [p for p in look(root)
                    if read_id(p) == wanted and not _is_network(p, table)]
        except OSError:
            hits = []
        if len(hits) != 1:
            if hits:
                log.warning("library %s is missing and %d folders carry its id "
                            "(%s); leaving it for you to choose with "
                            "`ninaivu reroot`", root, len(hits),
                            ", ".join(map(str, hits)))
            continue
        new = str(hits[0])
        if new in cfg.roots:
            continue
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
        moved.append((root, new))

    for root in cfg.roots:
        ident = ensure_marker(root)
        if ident:
            known[root] = ident
    _save_known(state, known)
    return moved
