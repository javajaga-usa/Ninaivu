"""Where the second copy is kept: what that folder is, and what is in it.

Two questions the console asks of the folder chosen for the second copy
(storage/mirror.py), and one it asks of the copy itself:

* :func:`describe` — before it is used. Is it there, can Ninaivu write in it,
  how much room is free and how much the copy needs, is it a network share
  (a NAS, or the N300 shared over SMB from a Linux VM on the same Mac), and is
  it on the same disk as the library (then it guards against mistakes, not
  against the disk failing).
* :func:`away` — when it is not there: a share that is not connected, or a
  disk not plugged in, said in those words, with what to do.
* :func:`listing` — the copy's folders and files, read-only, for the Folders
  screen: each file with its size and whether it is Ninaivu's copy of a
  library file, a file found there already with the same bytes, or neither.

Nothing here writes, renames or deletes anything at the folder.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from ..utils.files import stays_inside
from ..words import Said
from . import roots as roots_kit
from .library_id import NETWORK_FS

try:                                    # optional, as in storage/fit.py
    import psutil
except Exception:                       # noqa: BLE001
    psutil = None

log = logging.getLogger(__name__)

__all__ = ["describe", "away", "listing", "delete", "mount_of", "is_network", "LISTED"]

#: The most folders and files one folder of the copy lists.
LISTED = 500


def mount_of(path: str | Path) -> tuple[str, str]:
    """The mount point *path* falls under and its file system type, as the
    system names them (``/Volumes/n300``, ``smbfs``). Worked out from the
    path's spelling against the mounted disks, without touching the path;
    ``("", "")`` when that cannot be told."""
    if psutil is None:
        return "", ""
    target = os.path.normcase(os.path.abspath(os.path.expanduser(str(path))))
    try:
        partitions = psutil.disk_partitions(all=True)
    except Exception:                                   # noqa: BLE001
        return "", ""
    best, length = None, -1
    for part in partitions:
        mount = os.path.normcase(os.path.abspath(part.mountpoint))
        inside = target == mount or target.startswith(mount.rstrip("\\/") + os.sep)
        if inside and len(mount) > length:
            best, length = part, len(mount)
    if best is None:
        return "", ""
    return str(best.mountpoint), (best.fstype or "").lower()


def is_network(fstype: str) -> bool:
    return (fstype or "").lower() in NETWORK_FS


def _same_disk(folder: Path, roots: list[str]) -> str:
    """The library folder *folder* shares a disk with, or ''."""
    try:
        device = os.stat(folder).st_dev
    except OSError:
        return ""
    for root in roots:
        try:
            if os.stat(root).st_dev == device:
                return str(root)
        except OSError:
            continue
    return ""


def describe(folder: str, roots: list[str], state_dir: Path | str, *,
             needed_bytes: int = 0, problem: str | None = None,
             copy_id: str = "", library_here: dict[str, Any] | None = None) -> dict[str, Any]:
    """What the console says about *folder* before it becomes the second copy.

    *needed_bytes* is what is still to be copied there; *problem* is what
    :func:`storage.mirror.folder_problem` said; *copy_id* the copy the record
    was made against, so a folder already holding that copy is told apart
    from one holding another library's."""
    from .mirror import MARKER                                  # noqa: PLC0415
    target = Path(str(folder or "")).expanduser()
    mount, fstype = mount_of(target) if folder else ("", "")
    out: dict[str, Any] = {
        "folder": str(folder or ""), "problem": problem, "there": False,
        "writable": False, "free_bytes": None, "total_bytes": None,
        "needed_bytes": int(max(0, needed_bytes)), "short_of_room": False,
        "mount_point": mount, "filesystem": fstype, "network": is_network(fstype),
        "same_disk_as": "", "holds": "", "away": "", "away_said": None,
    }
    if not folder:
        return out
    try:
        there = target.is_dir() and not roots_kit.ghost_volume(target)
    except OSError:
        there = False
    out["there"] = there
    if not there:
        reason = away(str(folder), mount, fstype)
        out["away"] = str(reason or "")
        out["away_said"] = reason.as_json() if reason else None
        return out
    out["writable"] = os.access(target, os.W_OK)
    try:
        usage = shutil.disk_usage(target)
        out["free_bytes"], out["total_bytes"] = int(usage.free), int(usage.total)
    except OSError:
        pass
    if out["free_bytes"] is not None:
        from .mirror import RESERVE                             # noqa: PLC0415
        out["short_of_room"] = out["needed_bytes"] + RESERVE > out["free_bytes"]
    out["same_disk_as"] = _same_disk(target, [*roots])
    try:
        import json                                             # noqa: PLC0415
        found = str(json.loads((target / MARKER).read_text(encoding="utf-8")).get("id") or "")
    except (OSError, ValueError, AttributeError):
        found = ""
    if found:
        out["holds"] = "this" if found == copy_id else "another"
    elif library_here:
        # No copy of Ninaivu's, but the library's own files at their own
        # paths: the library copied there by hand. Taken as it is.
        out["holds"] = "library"
        out["library_here"] = library_here
    return out


def away(folder: str, mount: str = "", fstype: str = "") -> Said | None:
    """Why the second copy's *folder* is not there, in what-to-do words, or
    None when it is. *mount* and *fstype* are what the folder was on when it
    was last seen, so a share that has gone is called a share."""
    target = Path(str(folder or "")).expanduser()
    try:
        if target.is_dir() and not roots_kit.ghost_volume(target):
            return None
    except OSError:
        pass
    where = mount or str(roots_kit.volume_of(target) or "")
    if is_network(fstype):
        return Said("The network share at {where} is not connected. Connect to it again "
                    "(on a Mac: Finder, Go, Connect to Server) and the copy carries on "
                    "by itself within ten minutes.", where=where)
    if where and where not in ("/", "\\"):
        return Said("The disk at {where} is not plugged in. Plug it in and the copy "
                    "carries on by itself within ten minutes.", where=where)
    return Said("That folder is not there. Is the disk plugged in?")


def _rel(path: str) -> str:
    return path.replace("\\", "/").strip("/")


def listing(mirror, conn: sqlite3.Connection, rel: str = "") -> dict[str, Any]:
    """One folder of the second copy, read-only: its folders, and its files
    with what each one is. *rel* is the folder inside the copy, '' its top."""
    target = mirror.folder
    if target is None:
        raise LookupError("The second copy has no folder yet.")
    rel = _rel(str(rel or ""))
    if any(part in ("..", ".") for part in rel.split("/") if part):
        raise ValueError("That is not a folder on the second copy.")
    reason = away(str(target), *mirror.place())
    if reason:
        raise FileNotFoundError(reason)
    here = target / Path(*rel.split("/")) if rel else target
    if not stays_inside(target, here) or not here.is_dir():
        raise FileNotFoundError("That folder is not on the second copy.")

    # What the record says each file of this folder is: written by Ninaivu,
    # or found there already. Asked for this one folder, by the two ways a
    # file's place on the copy is kept (stored_as, or library folder + path).
    known: dict[str, dict[str, Any]] = {}
    prefix = f"{rel}/" if rel else ""
    for row in conn.execute(
            "SELECT root, rel_path, size, origin, stored_as FROM mirror_copies "
            "WHERE stored_as != '' AND stored_as LIKE ? ESCAPE '\\'",
            (_like(prefix) + "%",)):
        known[row["stored_as"]] = dict(row)
    for root in mirror.roots:
        top = mirror._library_folder(root)                     # noqa: SLF001
        inside = library_folder_of(mirror, rel, root)
        if inside is None:
            continue
        start = f"{inside}/" if inside else ""
        for row in conn.execute(
                "SELECT root, rel_path, size, origin, stored_as FROM mirror_copies "
                "WHERE root = ? AND stored_as = '' AND "
                "(REPLACE(rel_path, char(92), '/') LIKE ? ESCAPE '\\')",
                (root, _like(start) + "%")):
            known.setdefault(_under(top, _rel(row["rel_path"])), dict(row))

    dirs: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    more = 0
    try:
        entries = sorted(os.scandir(here), key=lambda e: e.name.lower())
    except PermissionError as exc:
        raise PermissionError("Ninaivu cannot read that folder on the second copy.") from exc
    for entry in entries:
        if entry.name.startswith("."):
            continue                        # the copy's marker, the disk's own
        path = f"{prefix}{entry.name}"
        try:
            if entry.is_dir(follow_symlinks=False):
                if len(dirs) < LISTED:
                    dirs.append({"name": entry.name, "path": path})
                else:
                    more += 1
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
            info = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if len(files) >= LISTED:
            more += 1
            continue
        record = known.get(path)
        if record is None:
            what = "other"
        elif record["size"] < 0:
            what = "bad"                    # a check found it changed: copied again
        else:
            what = "found" if record["origin"] == "found" else "copied"
        files.append({"name": entry.name, "path": path, "size": int(info.st_size),
                      "mtime": float(info.st_mtime), "is": what,
                      "library_path": _rel(record["rel_path"]) if record else "",
                      "library_root": record["root"] if record else ""})
    _what_is_inside(mirror, conn, dirs)
    _library_items(conn, files)
    parent = None
    if rel:
        parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
    active = str(getattr(mirror.cfg, "active_root", "") or "")
    return {"folder": str(target), "path": rel, "parent": parent, "dirs": dirs,
            "files": files, "more": more, "active_root": active,
            # The same folder in the library the Folders screen shows, if any.
            "library_folder": library_folder_of(mirror, rel, active) if active else None}


def _what_is_inside(mirror, conn: sqlite3.Connection, dirs: list[dict[str, Any]]) -> None:
    """For each folder of the copy listed, what the record says is under it:
    how many of the library's files, their size, and a picture of one of them
    to show on its card, as the library's own folders are shown (Folders).

    From the record, not the disk, so a folder of 50,000 files costs one
    range of the index, not a walk of a share. Files found there that are
    not the library's, and ones kept under another name, are not counted."""
    for item in dirs:
        files = size = 0
        cover = None
        for root in mirror.roots:
            inside = library_folder_of(mirror, item["path"], root)
            if inside is None:
                continue
            where, args = "m.root = ? AND m.stored_as = ''", [root]
            if inside:
                # Both ways a folder is written in a path: a range of the
                # primary key each, not a LIKE that reads every row.
                where += (" AND ((m.rel_path >= ? AND m.rel_path < ?)"
                          " OR (m.rel_path >= ? AND m.rel_path < ?))")
                args += [f"{inside}/", f"{inside}0", f"{inside}\\", f"{inside}]"]
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(m.size), 0), "
                "MIN(CASE WHEN a.kind IN ('picture', 'video') AND a.trashed = 0 THEN a.id END) "
                "FROM mirror_copies m LEFT JOIN assets a "
                "ON a.root = m.root AND a.rel_path = m.rel_path "
                f"WHERE {where} AND m.size >= 0", args).fetchone()
            files += int(row[0] or 0)
            size += int(row[1] or 0)
            cover = cover or (int(row[2]) if row[2] is not None else None)
        item.update(n=files, bytes=size, cover=cover)


def _library_items(conn: sqlite3.Connection, files: list[dict[str, Any]]) -> None:
    """The library's own id and kind for each file listed that is a copy of
    one, so its picture can be shown from the library's thumbnails."""
    for item in files:
        item["id"], item["kind"] = None, ""
        if not item.get("library_root") or not item.get("library_path"):
            continue
        # The path as the index keeps it, with either separator: looked up
        # by the index's own key, one file at a time, never by a scan.
        rel = item["library_path"]
        row = conn.execute(
            "SELECT id, kind FROM assets WHERE root = ? AND rel_path IN (?, ?) AND trashed = 0 "
            "LIMIT 1", (item["library_root"], rel, rel.replace("/", "\\"))).fetchone()
        if row is not None:
            item["id"], item["kind"] = int(row[0]), str(row[1] or "")


def _under(top: str, rel: str) -> str:
    return f"{top}/{rel}" if top and rel else (top or rel)


def _like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _record_of(mirror, conn: sqlite3.Connection, path: str):
    """The record of the file at *path* on the copy, or None."""
    row = conn.execute("SELECT root, rel_path FROM mirror_copies WHERE stored_as = ?",
                       (path,)).fetchone()
    if row is not None:
        return row
    for root in mirror.roots:
        top = mirror._library_folder(root)                     # noqa: SLF001
        own = path if not top else (path[len(top) + 1:] if path.startswith(top + "/") else "")
        if own:
            row = conn.execute(
                "SELECT root, rel_path FROM mirror_copies WHERE root = ? AND stored_as = '' "
                "AND REPLACE(rel_path, char(92), '/') = ?", (root, own)).fetchone()
            if row is not None:
                return row
    return None


def delete(mirror, conn: sqlite3.Connection, paths: list[str]) -> dict[str, Any]:
    """Delete files on the second copy, because an admin asked for each one.

    The automatic copy never deletes anything; this is the one way a file on
    it goes, and only a file, never a folder. Its record goes with it, so a
    file still in the library is owed again and the next run copies it back
    (the copy follows the library); one the library no longer has stays gone.
    """
    from .mirror import MARKER, NAMES                           # noqa: PLC0415
    target = mirror.folder
    if target is None:
        raise LookupError("The second copy has no folder yet.")
    reason = away(str(target), *mirror.place())
    if reason:
        raise FileNotFoundError(reason)
    if not (target / MARKER).is_file():
        raise FileNotFoundError("The second copy is not at its folder, so nothing was deleted.")
    deleted: list[str] = []
    failed: list[dict[str, str]] = []
    again = 0
    for raw in paths:
        rel = _rel(str(raw or ""))
        parts = [p for p in rel.split("/") if p]
        if not parts or any(p in (".", "..") or p.startswith(".") for p in parts) \
                or rel in (NAMES,):
            failed.append({"path": rel, "why": "That is not a file on the second copy."})
            continue
        path = target.joinpath(*parts)
        try:
            if not stays_inside(target, path) or path.is_symlink() or not path.is_file():
                failed.append({"path": rel, "why": "That file is not on the second copy."})
                continue
            row = _record_of(mirror, conn, rel)
            path.unlink()
        except OSError as exc:
            log.warning("could not delete %s on the second copy: %s", rel, exc)
            failed.append({"path": rel, "why": "It could not be deleted."})
            continue
        if row is not None:
            conn.execute("DELETE FROM mirror_copies WHERE root = ? AND rel_path = ?",
                         (row["root"], row["rel_path"]))
            if conn.execute("SELECT 1 FROM assets WHERE root = ? AND rel_path = ? AND trashed = 0",
                            (row["root"], row["rel_path"])).fetchone():
                again += 1
        conn.execute("DELETE FROM mirror_found WHERE path = ?", (rel,))
        conn.commit()
        deleted.append(rel)
    return {"deleted": deleted, "failed": failed, "copied_again": again}


def where_on_copy(mirror, conn: sqlite3.Connection, root: str, rel: str = "",
                  folder: str = "") -> dict[str, Any]:
    """Where a library file (*rel*) or folder (*folder*) of *root* is on the
    second copy: the copy's folder to open, and the file's own path there
    ('' when it is not copied yet)."""
    top = mirror._library_folder(root)                         # noqa: SLF001
    if rel:
        row = conn.execute("SELECT stored_as, size FROM mirror_copies WHERE root = ? AND rel_path = ?",
                           (root, rel)).fetchone()
        path = (row["stored_as"] if row is not None and row["stored_as"]
                else _under(top, _rel(rel)))
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        return {"path": parent, "file": path if row is not None and row["size"] >= 0 else "",
                "copied": row is not None and row["size"] >= 0}
    folder = _rel(folder)
    return {"path": _under(top, folder) if folder else top, "file": "", "copied": False}


def library_folder_of(mirror, rel: str, root: str) -> str | None:
    """The folder of library *root* that copy folder *rel* holds, or None."""
    top = mirror._library_folder(root)                         # noqa: SLF001
    rel = _rel(rel)
    if not top:
        return rel                      # the copy has the library's own layout
    if rel == top:
        return ""
    if rel.startswith(top + "/"):
        return rel[len(top) + 1:]
    return None
