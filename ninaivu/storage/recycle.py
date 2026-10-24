"""Deleting a photograph, in a way that can be taken back.

Nothing in Ninaivu has ever removed a file, and this is the one place that does.
So it does the smallest version of it: the file is *moved* into a ``_deleted``
folder at the top of its own library, under the date it went, keeping the path
it had. It leaves the gallery immediately and stops being searchable, but it is
still there — a folder anyone can open in Finder or Explorer, with the original
names intact, no database needed to make sense of it.

That last part is deliberate. A recycle bin that can only be read back by the
program that made it is a trap: uninstall Ninaivu, or corrupt the index, and the
photographs are gone in every way that matters. Here the bin is just folders.

The index remembers enough to put a file back exactly where it came from, and
the console can do that with one click. What Ninaivu will not do on its own is
erase anything: emptying the bin means dragging that folder to the real
recycle bin yourself, which is a decision worth making deliberately, in a file
manager, looking at what is in it.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import base64
import json
import os
import shutil
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence


from ..utils.files import stays_inside, sync_folder
from . import db, new_files

log = logging.getLogger(__name__)

__all__ = ["BIN_NAME", "ORIGINALS", "bin_path", "keep_original", "kept_copy_for",
           "discard_kept", "bin_kept_original",
           "note_rewritten", "recycle",
           "restore", "listing", "count", "purge", "sweep", "expired"]

#: The folder at the top of each library root where deleted files go. It is in
#: ``Config.ignore_dirs``, so a scan steps over it and never re-indexes what
#: was just deleted.
BIN_NAME = "_deleted"


#: Where a file's untouched original is kept the first time Ninaivu rewrites
#: it. Inside the bin, so a scan already steps over it, and laid out with the
#: same relative paths for the same reason the bin is: a folder anybody can
#: open in Explorer and copy back by hand, with no database in the way.
ORIGINALS = "_originals"

#: The tables whose rows go into the bin with an asset and come back with it.
#: Deleting the asset cascades through all of them; the first two are what a
#: person arranged (albums, favourites, ratings), the next two what the
#: pipeline made and people then named (faces) or searched by (the vector),
#: and the last what somebody said about it (storage/stories.py, which keeps
#: the sound while an entry here still holds its row). Then the time capsules
#: it was sealed into, which nobody could see were missing until the capsule
#: opened (audit C-08), and what the engines worked out about it that takes
#: days to work out again: its description, its fingerprint and an upright
#: turn waiting for review.
RESTORED_RELATIONS = ("album_items", "user_assets", "faces", "embeddings", "stories",
                      "capsule_items", "descriptions", "content_fingerprints",
                      "orientation_proposals", "faces_not_carried")

#: For a relation that belongs to something else, the table and column that
#: must still be there for the row to come back.
_OWNERS = {"album_items": ("albums", "album_id"), "user_assets": ("users", "user_id"),
           "capsule_items": ("capsules", "capsule_id"),
           "faces_not_carried": ("people_clusters", "person_id")}


def bin_path(root: str | Path) -> Path:
    return Path(root) / BIN_NAME


def in_its_bin(root: str | Path, path: str | Path) -> bool:
    """Whether *path* is inside the bin of library folder *root*, or inside
    the bin of a folder *root* was folded with (storage/nested_roots.py).

    Folding nested library folders moves a bin entry onto the folder kept,
    and its file stays in the bin it was put in: the outer folder's when the
    inner one is kept, the inner one's when the outer one is (audit D-09).
    So the bin that counts is the nearest ``_deleted`` above *path*, and the
    folder holding it must be *root*, inside it, or around it.
    """
    if stays_inside(bin_path(root), path):
        return True
    real = Path(os.path.realpath(path))
    home = Path(os.path.realpath(root))
    for parent in real.parents:
        if parent.name == BIN_NAME:
            library = parent.parent
            return library.is_relative_to(home) or home.is_relative_to(library)
    return False


#: Room a safety copy must leave free on its disk: the index, its journal,
#: thumbnails and uploads all need some, and a disk filled to the last byte
#: breaks every one of them.
COPY_MARGIN = 64 * 1024 * 1024


def keep_original(root: str | Path, rel_path: str, link: bool = False) -> Path | None:
    """Keep an untouched copy of a file before Ninaivu writes over it.

    Only ever the *first* one. Turning a photograph four times should leave
    one pristine copy of what it was before Ninaivu touched it, not four copies
    of four intermediate states — the version worth keeping is the one the
    camera wrote. So a file that already has a copy here is left alone, and
    the copy is never overwritten.

    "Already has a copy" means *this file* does, not merely this path. A
    camera's IMG_0001.jpg deleted and replaced by a different IMG_0001.jpg at
    the same place is a different photograph, and it gets a safety copy of its
    own beside the first rather than being rewritten with none. Each copy
    carries a note of the file it came from — its identity on disk, and the
    version Ninaivu last wrote (see :func:`note_rewritten`).

    *link* is for a caller that never writes into the file, only replaces it
    (Replace of a video): the copy is then a hard link to the same bytes,
    which takes no room and no time, and a copy is made only where links
    cannot be (another disk, exFAT). A file rewritten in place must not be
    linked, or the "original" would change with it.

    Returns the path of the copy, or ``None`` when one was already there.
    Raises ``OSError`` if it could not be made, because the caller must not
    rewrite the file if the safety net failed. A copy that fails part-way
    leaves nothing behind.
    """
    source = Path(root) / rel_path
    if kept_copy_for(root, rel_path) is not None:
        return None
    target = bin_path(root) / ORIGINALS / rel_path
    if target.exists():
        target = _unique(target)          # another photograph's copy is there
    target.parent.mkdir(parents=True, exist_ok=True)
    if link:
        try:
            os.link(source, target)
        except OSError:
            pass                          # copied below instead
        else:
            sync_folder(target.parent)
            _write_note(target, source)
            return target
    size = source.stat().st_size
    try:
        free = shutil.disk_usage(target.parent).free
    except OSError:
        free = None
    if free is not None and free < size + COPY_MARGIN:
        raise OSError(errno.ENOSPC, "There is not enough free space on that disk "
                      "for a copy of the original", str(target.parent))
    # A temporary name renamed into place, so an interrupted copy never leaves
    # a truncated file sitting where the original is supposed to be.
    partial = target.with_name(target.name + ".part")
    try:
        shutil.copy2(source, partial)
        # Synced before the file is rewritten: the rewrite that follows is synced,
        # and a power cut must not leave the rewritten file and an empty "original".
        with open(partial, "rb+") as handle:
            os.fsync(handle.fileno())
        os.replace(partial, target)
    except BaseException:
        with contextlib.suppress(OSError):
            partial.unlink()
        raise
    sync_folder(target.parent)
    _write_note(target, source)
    return target


def discard_kept(kept: Path) -> None:
    """Take back a safety copy :func:`keep_original` has just made, when the
    rewrite it was made for did not happen after all."""
    for leftover in (kept, _note_path(kept)):
        with contextlib.suppress(OSError):
            leftover.unlink()


def bin_kept_original(conn, kept: Path, row: dict[str, Any],
                      user_id: int | None = None) -> int | None:
    """List the original a Replace kept (*kept*, under ``_originals``) in the
    bin, as a deleted item of its own: Recently deleted shows it, it can be
    put back (as an item beside the smaller one, or at its old name when
    that is free), it can be erased, and the bin's retention erases it in
    time like anything else there. Until then it still takes its full room.

    *row* is the item's index row as it was before the swap. The entry names
    no item (the item lives on, with the smaller file), keeps no thumbnail of
    its own (the item's would be erased with it) and carries the row with
    its stamps cleared, so a restore makes its thumbnails afresh.
    Returns the entry's id, or None when it is listed already.
    """
    if conn.execute("SELECT 1 FROM recycled WHERE bin_path=? AND restored_at IS NULL",
                    (str(kept),)).fetchone():
        return None
    asset = {**row, "thumb": None, "mtime": 0}
    metadata = json.dumps({"asset": asset, "relations": {}},
                          default=lambda value: {"__bytes__": base64.b64encode(value).decode("ascii")})
    with db._write_lock:                          # noqa: SLF001 — same package
        cursor = conn.execute(
            "INSERT INTO recycled(asset_id, root, rel_path, filename, size, "
            "thumb, bin_path, deleted_at, deleted_by, metadata) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (None, row["root"], row["rel_path"], row["filename"],
             int(kept.stat().st_size), None, str(kept), time.time(), user_id, metadata))
        conn.commit()
    return int(cursor.lastrowid)


def _kept_copies(root: str | Path, rel_path: str) -> list[Path]:
    """Every kept copy made at this path: the first, then ``name_1``, ``name_2``…"""
    first = bin_path(root) / ORIGINALS / rel_path
    if not first.exists():
        return []
    copies = [first]
    for counter in range(1, 10000):
        candidate = first.with_name(f"{first.stem}_{counter}{first.suffix}")
        if not candidate.exists():
            break
        copies.append(candidate)
    return copies


def kept_copy_for(root: str | Path, rel_path: str) -> Path | None:
    """The kept copy taken from the file now at this path, if there is one."""
    source = Path(root) / rel_path
    for index, kept in enumerate(_kept_copies(root, rel_path)):
        if _same_file(kept, source, legacy_is_same=index == 0):
            return kept
    return None


def note_rewritten(kept: Path | None, root: str | Path, rel_path: str) -> None:
    """Record that Ninaivu has just rewritten the file *kept* was taken from.

    A rewrite that replaces the file (a remuxed video) gives it a new identity
    on disk; without this note the next turn would take the rewritten file for
    a different photograph and keep a second, already-turned "original".
    """
    if kept is not None and kept.exists():
        _write_note(kept, Path(root) / rel_path)


def _note_path(kept: Path) -> Path:
    return kept.with_name(kept.name + ".ninaivu-source")


def _identity(path: Path) -> tuple[int, int, str | None]:
    from ..utils.source_version import source_version
    stat = path.stat()
    try:
        version = source_version(path)
    except OSError:
        version = None
    return stat.st_ino, stat.st_dev, version


def _write_note(kept: Path, source: Path) -> None:
    ino, dev, version = _identity(source)
    _note_path(kept).write_text(json.dumps({"ino": ino, "dev": dev, "version": version}),
                                encoding="utf-8")


def _read_note(kept: Path) -> dict[str, Any] | None:
    try:
        note = json.loads(_note_path(kept).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return note if isinstance(note, dict) else None


def _same_file(kept: Path, source: Path, legacy_is_same: bool = True) -> bool:
    """Whether the copy at *kept* was taken from the file now at *source*."""
    if not kept.exists():
        return False
    note = _read_note(kept)
    if note is None:
        # Kept before notes existed. The first copy at a path is assumed to be
        # this file's, which is what every earlier version of Ninaivu assumed.
        return legacy_is_same
    # The bytes decide, not the file id. Linux hands a freed inode straight to
    # the next file created, so a new IMG_0001 written where the old one was
    # deleted usually has the old one's id — and was turned with no copy kept.
    # The note records what the file held when it was copied or last
    # rewritten by Ninaivu, so an unchanged file matches and a replaced one
    # does not, whatever its id.
    from ..utils.source_version import same_content
    content = same_content(note.get("version"), source)
    if content is not None:
        return content
    try:
        stat = source.stat()
    except OSError:
        return True
    return (note.get("ino"), note.get("dev")) == (stat.st_ino, stat.st_dev)


def _unique(target: Path) -> Path:
    """A free name at *target*, so two deletions of one filename both survive."""
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for counter in range(1, 10000):
        candidate = target.with_name(f"{stem}_{counter}{suffix}")
        if not candidate.exists():
            return candidate
    raise OSError(f"could not find a free name beside {target}")


#: How many deletes are part way through in this process. Their entries are
#: committed before their files move and their index rows go after, so while
#: one runs an entry can look cut short (file not moved yet) or erased by
#: hand (file not in the bin yet). The repairs below leave everything alone
#: until it is over (D-27).
_deleting_now = 0
_deleting_lock = threading.Lock()


@contextlib.contextmanager
def _deleting():
    global _deleting_now                          # noqa: PLW0603
    with _deleting_lock:
        _deleting_now += 1
    try:
        yield
    finally:
        with _deleting_lock:
            _deleting_now -= 1


def _a_delete_is_moving() -> bool:
    with _deleting_lock:
        return _deleting_now > 0


def _settle_interrupted(conn) -> int:
    """Finish or undo deletions a crash or power cut cut short.

    An entry is written before its file moves (see :func:`recycle`), so an
    entry whose photograph is still in the index marks one that did not
    finish. Where the file reached the bin, the index row goes, as the delete
    meant; where it is still in the library, the entry goes. Anything else (a
    drive that is away, a new file at the old name) is left as it is.
    Returns how many were settled.
    """
    if _a_delete_is_moving():
        return 0
    try:
        rows = conn.execute(
            "SELECT r.id AS entry, r.bin_path, a.id AS asset_id, a.root, a.rel_path "
            "FROM recycled r JOIN assets a ON a.id = r.asset_id AND a.root = r.root "
            "AND a.rel_path = r.rel_path WHERE r.restored_at IS NULL").fetchall()
    except sqlite3.Error:
        return 0
    finished: list[int] = []
    undone: list[int] = []
    for row in rows:
        source = Path(row["root"]) / row["rel_path"]
        binned = Path(row["bin_path"]) if row["bin_path"] else None
        in_bin = binned is not None and binned.exists()
        if source.exists() and not in_bin:
            undone.append(int(row["entry"]))
        elif in_bin and not source.exists():
            finished.append(int(row["asset_id"]))
    if not (finished or undone):
        return 0
    with db._write_lock:                          # noqa: SLF001 — same package
        conn.executemany("DELETE FROM assets WHERE id=?", [(i,) for i in finished])
        conn.executemany("DELETE FROM recycled WHERE id=?", [(i,) for i in undone])
        conn.commit()
    log.warning("recycle bin: settled %d deletion(s) that were cut short",
                len(finished) + len(undone))
    return len(finished) + len(undone)


def _let_go(conn, root: str, rel_path: str) -> bool:
    """Whether this original was let go, kept on the other computer of two."""
    try:
        return conn.execute("SELECT 1 FROM mugil_let_go WHERE root=? AND rel_path=?",
                            (root, rel_path)).fetchone() is not None
    except sqlite3.OperationalError:
        return False                       # an index made before the table was


def recycle(conn, asset_ids: Sequence[int], user_id: int | None = None,
            roots: Iterable[str] | None = None) -> dict[str, Any]:
    """Move these assets' files into their library's bin. Returns a summary.

    Rows are removed from the index only once the file has actually moved, so
    a failure part-way leaves the library describing exactly what is still on
    the disk rather than a half-truth.
    """
    if not asset_ids:
        return {"deleted": 0, "failed": [], "items": []}
    _settle_interrupted(conn)
    with _deleting():
        return _recycle(conn, asset_ids, user_id, roots)


def _recycle(conn, asset_ids: Sequence[int], user_id: int | None,
             roots: Iterable[str] | None) -> dict[str, Any]:
    marks = ",".join("?" * len(asset_ids))
    rows = conn.execute(
        f"SELECT * FROM assets "
        f"WHERE id IN ({marks})", list(asset_ids)).fetchall()

    allowed = set(roots) if roots is not None else None
    stamp = datetime.now().strftime("%Y-%m-%d")
    moved: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    # Everything that hangs off these rows, read once for the batch. It was
    # read per photograph, with the table list re-read from sqlite_master
    # each time: a delete of five thousand was twenty thousand schema reads
    # and as many table scans before a single file moved.
    present = db._tables(conn)                            # noqa: SLF001 — same package
    relations: dict[int, dict[str, list[dict[str, Any]]]] = {}
    ids = [int(r["id"]) for r in rows]
    for table in RESTORED_RELATIONS:
        if table not in present:
            continue
        for start in range(0, len(ids), 500):
            piece = ids[start:start + 500]
            for r in conn.execute(
                    f"SELECT * FROM {table} WHERE asset_id IN ({','.join('?' * len(piece))})",
                    piece):
                relations.setdefault(int(r["asset_id"]), {}).setdefault(table, []).append(dict(r))

    for row in rows:
        if allowed is not None and row["root"] not in allowed:
            failed.append({"name": row["filename"],
                           "why": "outside the folders you can manage"})
            continue

        # A read-only library — an NTFS drive on a Mac — cannot give a file up
        # to the bin. Said in words, not as "[Errno 30] Read-only file system".
        if not new_files.writable(row["root"]) and Path(row["root"]).is_dir():
            failed.append({"name": row["filename"], "why": new_files.READ_ONLY})
            continue

        source = Path(row["root"]) / row["rel_path"]
        target = bin_path(row["root"]) / stamp / row["rel_path"]
        # Both ends inside this library, links followed, as every other writer
        # checks before it moves anything: a linked folder under the root, or
        # a tampered ``rel_path``, would otherwise carry a file that was never
        # in the library into the bin — from where it could not be put back.
        if not stays_inside(row["root"], source) or not stays_inside(bin_path(row["root"]), target):
            failed.append({"name": row["filename"],
                           "why": "that file is not inside the library"})
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target = _unique(target)
            if not source.exists() and _let_go(conn, row["root"], row["rel_path"]):
                # Let go to free space, and kept on the other computer
                # (mugil/free.py): not missing. Deleted as if it were, its
                # row went with nothing in the bin to put back, and the
                # record of the let-go original stayed: unpairing was refused
                # for good and "Bring every original back" brought the
                # deleted photograph back into the library.
                failed.append({"name": row["filename"],
                               "why": "kept only on the other computer; bring it back first"})
                continue
            if not source.exists():
                # The file is already gone from the disk. Removing the row is
                # still the right answer — the library should not list a
                # photograph that is not there — but say so rather than
                # claiming to have moved something.
                failed.append({"name": row["filename"],
                               "why": "already missing from the disk"})
                target = None
        except OSError as exc:
            failed.append({"name": row["filename"], "why": str(exc)})
            continue

        moved.append({
            "id": row["id"], "root": row["root"], "rel_path": row["rel_path"],
            "filename": row["filename"], "size": row["size"] or 0,
            "thumb": row["thumb"],
            "bin_path": str(target) if target else "",
            "metadata": json.dumps({
                "asset": dict(row),
                # Everything that hangs off the row and goes with it when the
                # row is deleted. Faces carry the names people confirmed and
                # the vector carries the AI search; without these a restored
                # photograph came back nameless and unsearchable, and the
                # scanner saw an unchanged file with current stamps and left
                # it that way for good.
                "relations": {table: relations.get(int(row["id"]), {}).get(table, [])
                              for table in RESTORED_RELATIONS if table in present},
            }, default=lambda value: {"__bytes__": base64.b64encode(value).decode("ascii")}),
        })

    if not moved:
        return {"deleted": 0, "failed": failed, "items": []}

    # The bin's entries are written, and committed, before any file moves.
    # Moving first left a gap: a power cut after the move and before the
    # commit put the files in the bin with no entry to restore them from,
    # while the index still pointed at where they had been, and the next scan
    # dropped those rows with their albums, faces and names. Now a cut at any
    # point leaves an entry for every file that may have moved, holding all
    # of that; :func:`_settle_interrupted` finishes or undoes it.
    now = time.time()
    with db._write_lock:                          # noqa: SLF001 — same package
        cursor_ids = []
        for m in moved:
            cursor = conn.execute(
                "INSERT INTO recycled(asset_id, root, rel_path, filename, size, "
                "thumb, bin_path, deleted_at, deleted_by, metadata) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (m["id"], m["root"], m["rel_path"], m["filename"], m["size"],
                 m["thumb"], m["bin_path"], now, user_id, m["metadata"]))
            cursor_ids.append(cursor.lastrowid)
        conn.commit()
    for m, entry in zip(moved, cursor_ids):
        m["entry"] = entry

    went: list[dict[str, Any]] = []
    for m in moved:
        if m["bin_path"]:
            try:
                shutil.move(str(Path(m["root"]) / m["rel_path"]), m["bin_path"])
            except OSError as exc:
                failed.append({"name": m["filename"], "why": str(exc)})
                with db._write_lock:              # noqa: SLF001
                    conn.execute("DELETE FROM recycled WHERE id=?", (m["entry"],))
                    conn.commit()
                continue
            _drop_our_sidecar(Path(m["root"]) / m["rel_path"])
        went.append(m)
    moved = went
    if not moved:
        return {"deleted": 0, "failed": failed, "items": []}

    with db._write_lock:                          # noqa: SLF001 — same package
        try:
            conn.executemany("DELETE FROM assets WHERE id=?",
                             [(m["id"],) for m in moved])
            conn.commit()
            _tell_the_archive([str(Path(m["root"]) / m["rel_path"]) for m in moved
                               if m["bin_path"]], deleted=True)
        except BaseException:
            # The files are in the bin but the index never heard of it: put
            # every file back where it came from, as restore() does for its
            # one, and drop the entries, so the library again describes
            # exactly what is on the disk. A file that cannot be put back is
            # logged with both paths — it is safe in the bin, and its entry is
            # kept so it can still be restored from there.
            conn.rollback()
            back: list[int] = []
            for m in reversed(moved):
                if not m["bin_path"]:
                    back.append(m["entry"])
                    continue                      # it was never there to move
                source = Path(m["root"]) / m["rel_path"]
                try:
                    if source.exists():
                        raise FileExistsError(source)
                    shutil.move(m["bin_path"], str(source))
                    back.append(m["entry"])
                except Exception:                 # noqa: BLE001
                    log.exception("recycle bin: could not put %s back at %s "
                                  "after the index refused the delete",
                                  m["bin_path"], source)
            try:
                conn.executemany("DELETE FROM recycled WHERE id=?", [(i,) for i in back])
                conn.commit()
            except sqlite3.Error:
                log.exception("recycle bin: could not drop the entries put back")
            raise

    return {"deleted": len(moved), "failed": failed, "items": moved,
            "thumbs": [m["thumb"] for m in moved if m["thumb"]]}


def listing(conn, limit: int = 500) -> list[dict[str, Any]]:
    """What is in the bin, newest first, with whether the file is still there."""
    _settle_interrupted(conn)
    rows = conn.execute(
        "SELECT * FROM recycled WHERE restored_at IS NULL "
        "ORDER BY deleted_at DESC, id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["present"] = bool(item["bin_path"]) and os.path.exists(item["bin_path"])
        out.append(item)
    return out


def vanished(conn) -> tuple[list[int], list[tuple[str, str]]]:
    """The entries whose files somebody removed from ``_deleted`` by hand.

    Only reads. Returns their ids, and the (root, rel_path) of those whose
    file is back in the library. A file is taken as gone only when its
    library's ``_deleted`` folder is there to look in; a drive that is away
    keeps its entries, since their files may well come back with it. An
    entry whose photograph is still in the index belongs to a delete that is
    still moving files, or one a power cut stopped, and is never counted.
    """
    if _a_delete_is_moving():
        return [], []
    rows = conn.execute(
        "SELECT r.id, r.root, r.rel_path, r.bin_path FROM recycled r "
        "WHERE r.restored_at IS NULL AND NOT EXISTS ("
        "SELECT 1 FROM assets a WHERE a.id = r.asset_id "
        "AND a.root = r.root AND a.rel_path = r.rel_path)").fetchall()
    reachable: dict[str, bool] = {}
    gone: list[int] = []
    put_back: list[tuple[str, str]] = []
    for row in rows:
        root = row["root"] or ""
        if root not in reachable:
            reachable[root] = bool(root) and bin_path(root).is_dir()
        if not reachable[root]:
            continue
        if row["bin_path"] and os.path.exists(row["bin_path"]):
            continue
        gone.append(int(row["id"]))
        if (Path(root) / row["rel_path"]).exists():
            put_back.append((root, row["rel_path"]))
    return gone, put_back


def forget_vanished(conn) -> dict[str, Any]:
    """Drop the entries whose files somebody removed from ``_deleted`` by hand.

    The bin listed them as "Moved or erased" with their boxes greyed out, so
    nothing on the screen could clear them: 94 rows and 80 GB that were not
    on the disk any more. This writes, so it runs at start-up and when
    somebody restores or erases from the bin, never on the bin's GET (D-23).
    """
    _settle_interrupted(conn)
    gone, put_back = vanished(conn)
    if not gone:
        return {"purged": 0, "failed": [], "thumbs": [], "asset_ids": []}
    # Nobody pressed Erase: a recording these entries held is kept aside in
    # stories/_orphaned/, never deleted (the photograph may be back in the
    # library, dragged out of _deleted in Finder).
    result = purge(conn, gone, erase_stories=False)
    if put_back:
        # Moved back into the library by hand rather than erased: the scan
        # indexes it again, and a restore from the second copy must not
        # hold it back as something the household threw away.
        with db._write_lock:                      # noqa: SLF001
            conn.executemany("DELETE FROM erased WHERE root=? AND rel_path=?", put_back)
            conn.commit()
    log.info("recycle bin: forgot %d entr(ies) whose files were removed by hand",
             result["purged"])
    return result


def expired(conn, days: float) -> list[int]:
    """Entries that have been in the bin longer than *days*."""
    if not days or days <= 0:
        return []
    cutoff = time.time() - float(days) * 86400
    return [int(r["id"]) for r in conn.execute(
        "SELECT id FROM recycled WHERE restored_at IS NULL AND deleted_at < ?",
        (cutoff,))]


def sweep(conn, days: float) -> dict[str, Any]:
    """Erase what has been in the bin too long.

    Off unless a household turns it on, and it says so afterwards rather than
    doing it silently — the bin's whole promise is that deleting is reversible,
    and a policy that quietly ends that had better be loud about it.
    """
    _settle_interrupted(conn)
    ids = expired(conn, days)
    if not ids:
        return {"purged": 0, "failed": [], "thumbs": []}
    result = purge(conn, ids)
    if result["purged"]:
        log.info("recycle bin: erased %d item(s) older than %g days",
                 result["purged"], days)
    return result


def count(conn) -> dict[str, int]:
    row = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(size),0) bytes FROM recycled "
        "WHERE restored_at IS NULL").fetchone()
    return {"items": int(row["n"] or 0), "bytes": int(row["bytes"] or 0)}


def _companions_in_bin(conn, rows: Sequence[Any]) -> list[Any]:
    """The other halves of the pairs among *rows*, still in the bin.

    Deleting a photograph takes its live clip or its RAW with it, as separate
    entries. Restoring only the photograph left the other half behind, and
    the next scan forgot the pair; with the bin set to empty itself, the RAW
    was then erased for good.
    """
    chosen = {int(r["id"]) for r in rows}
    found: list[Any] = []
    for row in rows:
        try:
            saved = json.loads(row["metadata"] or "{}").get("asset") or {}
        except (ValueError, TypeError, AttributeError):
            continue
        for path in (saved.get("raw_path"), saved.get("live_video_path")):
            if not path:
                continue
            for other in conn.execute(
                    "SELECT * FROM recycled WHERE root=? AND rel_path=? "
                    "AND restored_at IS NULL", (row["root"], path)):
                if int(other["id"]) not in chosen:
                    chosen.add(int(other["id"]))
                    found.append(other)
    return found


def restore(conn, entry_ids: Sequence[int], *,
            roots: Iterable[str] | None = None) -> dict[str, Any]:
    """Put files back where they came from. The next scan re-indexes them.

    With *roots* (the library folders listed now), an entry of a folder no
    longer listed is not put back: its row would go under a library nobody
    lists, and the scan after it would walk that folder again (audit D-09).
    """
    if not entry_ids:
        return {"restored": 0, "failed": []}

    marks = ",".join("?" * len(entry_ids))
    rows = conn.execute(
        f"SELECT * FROM recycled WHERE id IN ({marks}) AND restored_at IS NULL",
        list(entry_ids)).fetchall()
    rows += _companions_in_bin(conn, rows)

    done: list[int] = []
    put_back: list[str] = []
    failed: list[dict[str, str]] = []
    listed = set(roots) if roots is not None else None
    for row in rows:
        source = Path(row["bin_path"]) if row["bin_path"] else None
        target = Path(row["root"]) / row["rel_path"]
        if listed is not None and row["root"] not in listed:
            failed.append({"name": row["filename"],
                           "why": "its library folder is no longer listed"})
            continue
        if source is None or not source.exists():
            failed.append({"name": row["filename"],
                           "why": "the file is no longer in the bin"})
            continue
        # Where it goes back to must be inside the library once links are
        # followed — the same check as the sidecar writer, repair and the
        # second copy make before they write. A sub-folder that has become a
        # link to somewhere else, or an entry whose ``rel_path`` was tampered
        # with, would otherwise have the move put the file outside the
        # library, and ``mkdir`` make folders there first. And what moves
        # must be in this library's bin, as purge insists, so an entry
        # pointing at a photograph still in the library cannot move it.
        if not stays_inside(row["root"], target):
            failed.append({"name": row["filename"],
                           "why": "that place is not inside the library"})
            continue
        if not in_its_bin(row["root"], source):
            failed.append({"name": row["filename"],
                           "why": "that file is not inside the recycle bin"})
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Something may have taken the original name back in the meantime;
            # never overwrite it.
            target = _unique(target)
            with db._write_lock:
                conn.execute("SAVEPOINT restore_item")
                moved_file = False
                try:
                    saved = json.loads(row["metadata"] or "{}", object_hook=lambda value:
                        base64.b64decode(value["__bytes__"]) if set(value) == {"__bytes__"} else value)
                    record = saved.get("asset") or db._normalise({
                        "root": row["root"], "rel_path": row["rel_path"],
                        "filename": row["filename"], "kind": "picture",
                        "visibility": 2, "vis_source": "item"})
                    record.pop("id", None)
                    # Bound by the upsert, not a column: it would fail this
                    # insert, and so every entry that has no saved metadata.
                    record.pop("nsfw_given", None)
                    rel = target.relative_to(Path(row["root"])).as_posix()
                    # The entry's own root and place: the saved row may name
                    # a library folder since folded into this one (audit D-09).
                    record.update(root=row["root"], rel_path=rel,
                                  folder=rel.rpartition("/")[0],
                                  filename=target.name, trashed=0,
                                  # Back as a tile of its own until the next
                                  # scan finds its photograph again: a RAW or
                                  # clip restored alone was otherwise in no
                                  # listing at all.
                                  live_clip=0, raw_twin=0)
                    if target.as_posix() != (Path(row["root"]) / row["rel_path"]).as_posix():
                        # Back under a new name because the old one is taken.
                        # Thumbnails are named after the path, so the file now
                        # at the old name has made its own over this one's;
                        # the next scan makes this one's afresh at the new name.
                        record.update(thumb=None, mtime=0)
                    relations = saved.get("relations", {})
                    # An entry put in the bin before faces and vectors were kept
                    # with it has lost them; zero the stamps so the next scan
                    # makes them again rather than taking the row as current.
                    if "faces" not in relations:
                        record["face_version"] = 0
                    if "embeddings" not in relations:
                        record["ai_version"] = 0
                    cols = list(record)
                    cursor = conn.execute(
                        f"INSERT INTO assets (id, {','.join(cols)}) "
                        f"VALUES ({db.NEW_ASSET_ID}, {','.join('?' for _ in cols)})",
                        [record[c] for c in cols])
                    for table, rows_to_restore in relations.items():
                        if table not in RESTORED_RELATIONS or table not in db._tables(conn):
                            continue
                        for relation in rows_to_restore:
                            relation["asset_id"] = cursor.lastrowid
                            if table == "stories":
                                relation.pop("id", None)      # a fresh story id
                            elif table == "faces":
                                relation.pop("id", None)      # a fresh face id
                                # The person it was attached to may have been
                                # merged away since; then it is simply unnamed.
                                if relation.get("person_id") is not None and not conn.execute(
                                        "SELECT 1 FROM people_clusters WHERE id=?",
                                        (relation["person_id"],)).fetchone():
                                    relation["person_id"] = None
                                    relation["source"] = "none"
                            elif table in _OWNERS:
                                owner_table, owner_key = _OWNERS[table]
                                if not conn.execute(f"SELECT 1 FROM {owner_table} WHERE id=?",
                                                    (relation[owner_key],)).fetchone():
                                    continue
                            keys = list(relation)
                            conn.execute(f"INSERT OR IGNORE INTO {table} ({','.join(keys)}) "
                                         f"VALUES ({','.join('?' for _ in keys)})",
                                         [relation[k] for k in keys])
                    shutil.move(str(source), str(target))
                    moved_file = True
                    conn.execute("UPDATE recycled SET restored_at=? WHERE id=?", (time.time(), row["id"]))
                    conn.execute("RELEASE restore_item")
                except Exception:
                    conn.execute("ROLLBACK TO restore_item")
                    conn.execute("RELEASE restore_item")
                    if moved_file:
                        shutil.move(str(target), str(source))
                    raise
        except (OSError, ValueError, sqlite3.Error) as exc:
            failed.append({"name": row["filename"], "why": str(exc)})
            continue
        done.append(row["id"])
        if target.as_posix() == (Path(row["root"]) / row["rel_path"]).as_posix():
            put_back.append(str(target))

    if done:
        with db._write_lock:                      # noqa: SLF001
            conn.executemany("UPDATE recycled SET restored_at=? WHERE id=?",
                             [(time.time(), i) for i in done])
            conn.commit()
        _tell_the_archive(put_back, deleted=False)
    return {"restored": len(done), "failed": failed}


def _tell_the_archive(paths: list[str], deleted: bool) -> None:
    """When the archive is the library, a delete is not damage (see
    ``archive.database.note_library_deletion``). Never fails the delete."""
    if not paths:
        return
    try:
        from ..archive import database as archive_db          # noqa: PLC0415
        archive_db.note_library_deletion(paths, deleted=deleted)
    except Exception:                                     # noqa: BLE001
        log.exception("recycle bin: could not tell the archive about %d file(s)", len(paths))


def purge(conn, entry_ids: Sequence[int], *, erase_stories: bool = True) -> dict[str, Any]:
    """Erase files from the bin. This is the one thing here that is final.

    The bin exists so that deleting is reversible, and for a long time the
    only way past it was to open the folder in Explorer and empty it by hand.
    That is a fine default and a poor only option: somebody clearing space
    should not have to leave the app, find the folder, and work out which of
    the files in it were the ones they meant.

    So this erases, and it erases only what the bin knows about. Every path is
    checked to be inside a ``_deleted`` folder before it is unlinked — a row
    with a mangled ``bin_path`` must not be able to reach a photograph that is
    still in the library.

    Rows are dropped rather than marked: what the bin lists is what is in it,
    and a file that has been erased is not in it. The audit log is where the
    erasing is remembered.

    With *erase_stories* false the entries' voice stories are kept aside in
    ``stories/_orphaned/`` instead of deleted: for entries nobody chose to
    erase, such as a photograph dragged back out of ``_deleted`` in Finder.
    """
    if not entry_ids:
        return {"purged": 0, "failed": [], "thumbs": []}

    marks = ",".join("?" * len(entry_ids))
    rows = conn.execute(
        f"SELECT * FROM recycled WHERE id IN ({marks}) AND restored_at IS NULL",
        list(entry_ids)).fetchall()

    done: list[int] = []
    thumbs: list[str] = []
    failed: list[dict[str, str]] = []
    for row in rows:
        path = Path(row["bin_path"]) if row["bin_path"] else None
        if path is None or not path.exists():
            # Already gone from disk — moved or erased by hand. The row is
            # still stale, so drop it: the bin should never list a file that
            # is not there.
            done.append(row["id"])
            if row["thumb"]:
                thumbs.append(row["thumb"])
            continue
        # Inside *this* library's bin, links followed: a path recorded before
        # the library moved could otherwise reach a file on whatever disk is
        # mounted at the old place now.
        if BIN_NAME not in path.parts or not in_its_bin(row["root"], path):
            failed.append({"name": row["filename"],
                           "why": "that file is not inside the recycle bin"})
            continue
        # The original kept from before a rotation is the same photograph,
        # and erasing one while the other stays under _deleted/_originals is
        # not erasing it. Matched by content, so a copy kept for a different
        # file now at that path stays.
        kept = [copy for index, copy in enumerate(_kept_copies(row["root"], row["rel_path"]))
                if _same_file(copy, path, legacy_is_same=index == 0
                              and not (Path(row["root"]) / row["rel_path"]).exists())
                and in_its_bin(row["root"], copy)]
        try:
            path.unlink()
        except OSError as exc:
            failed.append({"name": row["filename"], "why": str(exc)})
            continue
        for copy in kept:
            for leftover in (copy, _note_path(copy)):
                with contextlib.suppress(OSError):
                    leftover.unlink()
        # The dated folders the bin makes are only ever containers. An empty
        # one left behind is litter, so take it — and only it — away.
        parent = path.parent
        while parent.name and BIN_NAME in parent.parts and parent.name != BIN_NAME:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
        done.append(row["id"])
        if row["thumb"]:
            thumbs.append(row["thumb"])

    if done:
        now = time.time()
        with db._write_lock:                      # noqa: SLF001
            conn.executemany(
                "INSERT OR REPLACE INTO erased(root, rel_path, size, erased_at) "
                "VALUES(?,?,?,?)",
                [(r["root"], r["rel_path"], int(r["size"] or 0), now)
                 for r in rows if r["id"] in done])
            conn.execute(
                f"DELETE FROM recycled WHERE id IN ({','.join('?' * len(done))})",
                done)
            conn.commit()
        # Their stories' sound was kept for a restore that can no longer come.
        # Erased only when somebody erased the photograph; otherwise kept aside.
        from . import stories                                    # noqa: PLC0415
        erased: set[str] = set()
        if erase_stories:
            for r in rows:
                if r["id"] in done:
                    erased |= stories.files_named_in(r["metadata"])
        stories.sweep(conn, erase=erased)
    return {"purged": len(done), "failed": failed, "thumbs": _unused(conn, thumbs),
            "asset_ids": [int(r["asset_id"]) for r in rows
                          if r["id"] in done and r["asset_id"] is not None]}


class Gone:
    """What the household deleted: in the bin now, or erased from it.

    A restore from the second copy or from Google Drive puts back what the
    library has lost, and a deleted photograph is something the library has
    lost. Without this, the passport scanned by mistake, hidden, deleted and
    erased came back with the rest after a disk failure — and, its record
    gone, as an ordinary family photograph. Matched by place and size, so a
    different file saved at the same name later is not held back.
    """

    def __init__(self, conn) -> None:
        self._sizes: dict[tuple[str, str], set[int]] = {}
        for sql in ("SELECT root, rel_path, size FROM recycled WHERE restored_at IS NULL",
                    "SELECT root, rel_path, size FROM erased"):
            try:
                rows = conn.execute(sql).fetchall()
            except sqlite3.Error:
                continue                   # an index from before the table
            for row in rows:
                key = (str(row["root"]), str(row["rel_path"]).replace("\\", "/").casefold())
                self._sizes.setdefault(key, set()).add(int(row["size"] or 0))

    def __bool__(self) -> bool:
        return bool(self._sizes)

    def holds(self, root: str, rel_path: str, size: int | None = None) -> bool:
        sizes = self._sizes.get((str(root), str(rel_path).replace("\\", "/").casefold()))
        if not sizes:
            return False
        return size is None or not int(size) or int(size) in sizes or 0 in sizes


def _drop_our_sidecar(source: Path) -> None:
    """Remove the XMP sidecar Ninaivu wrote beside a file that has just gone.

    It names the people in the photograph, where it was taken and its
    caption, and once the photograph was in the bin nothing tracked it any
    more: it stayed in the library folder, readable over the network share,
    for ever. Ninaivu writes it again if the photograph is put back. A sidecar
    another program wrote is not Ninaivu's to remove.
    """
    from .xmp import MARK, sidecar_for                            # noqa: PLC0415
    sidecar = sidecar_for(source)
    try:
        with open(sidecar, encoding="utf-8", errors="replace") as handle:
            if MARK not in handle.read(8192):
                return
        sidecar.unlink()
    except OSError:
        return


def erase_face_crops(conn, state_dir: str | Path, asset_ids: Iterable[int]) -> int:
    """Remove the face crops cut from erased photographs.

    A crop is a close-up of somebody's face, kept under ``faces/`` in the
    state folder and named for the photograph it came from. Nothing removed
    them, so a face from a photograph erased for good stayed on disk, and in
    every backup of the state folder, for ever. One a face row still names
    (an id used again) is left alone.
    """
    wanted = {int(i) for i in asset_ids}
    if not wanted:
        return 0
    # One listing of the folder, not a glob per photograph: a library's faces
    # folder holds a crop for every face found, and globbing it once for each
    # of 500 erased photographs read it 500 times over (minutes on a big one).
    folder = Path(state_dir) / "faces"
    crops: list[Path] = []
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                head, sep, tail = entry.name.partition("_")
                if (sep and entry.name.endswith(".jpg") and head.isdigit()
                        and int(head) in wanted and tail[:-4].isdigit()):
                    crops.append(Path(entry.path))
    except OSError:
        return 0
    used: set[str] = set()
    names = [crop.name for crop in crops]
    for start in range(0, len(names), 500):
        chunk = names[start:start + 500]
        used.update(row[0] for row in conn.execute(
            f"SELECT thumb FROM faces WHERE thumb IN ({','.join('?' * len(chunk))})", chunk))
    removed = 0
    for crop in crops:
        if crop.name in used:
            continue
        with contextlib.suppress(OSError):
            crop.unlink()
            removed += 1
    return removed


def _unused(conn, thumbs: Sequence[str]) -> list[str]:
    """The thumbnail names nothing else still points at.

    A thumbnail is named after its file's path, so a new photograph saved where
    an erased one used to be has the same name, and so does a second deletion
    of that path still in the bin. Their thumbnails must outlive this one.
    """
    wanted = sorted(set(thumbs))
    used: set[str] = set()
    for start in range(0, len(wanted), 500):
        chunk = wanted[start:start + 500]
        marks = ",".join("?" * len(chunk))
        used.update(row[0] for row in conn.execute(
            f"SELECT thumb FROM assets WHERE thumb IN ({marks}) "
            f"UNION SELECT thumb FROM recycled WHERE restored_at IS NULL AND thumb IN ({marks})",
            chunk + chunk))
    return [t for t in wanted if t not in used]
