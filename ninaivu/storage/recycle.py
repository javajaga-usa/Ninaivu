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

import logging
import base64
import json
import os
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence


from . import db, new_files

log = logging.getLogger(__name__)

__all__ = ["BIN_NAME", "ORIGINALS", "bin_path", "keep_original", "kept_copy_for",
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
#: Deleting the asset cascades through all four; the first two are what a
#: person arranged (albums, favourites, ratings), the last two what the
#: pipeline made and people then named (faces) or searched by (the vector).
RESTORED_RELATIONS = ("album_items", "user_assets", "faces", "embeddings")


def bin_path(root: str | Path) -> Path:
    return Path(root) / BIN_NAME


def keep_original(root: str | Path, rel_path: str) -> Path | None:
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

    Returns the path of the copy, or ``None`` when one was already there.
    Raises ``OSError`` if it could not be made, because the caller must not
    rewrite the file if the safety net failed.
    """
    source = Path(root) / rel_path
    if kept_copy_for(root, rel_path) is not None:
        return None
    target = bin_path(root) / ORIGINALS / rel_path
    if target.exists():
        target = _unique(target)          # another photograph's copy is there
    target.parent.mkdir(parents=True, exist_ok=True)
    # A temporary name renamed into place, so an interrupted copy never leaves
    # a truncated file sitting where the original is supposed to be.
    partial = target.with_name(target.name + ".part")
    shutil.copy2(source, partial)
    os.replace(partial, target)
    _write_note(target, source)
    return target


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


def recycle(conn, asset_ids: Sequence[int], user_id: int | None = None,
            roots: Iterable[str] | None = None) -> dict[str, Any]:
    """Move these assets' files into their library's bin. Returns a summary.

    Rows are removed from the index only once the file has actually moved, so
    a failure part-way leaves the library describing exactly what is still on
    the disk rather than a half-truth.
    """
    if not asset_ids:
        return {"deleted": 0, "failed": [], "items": []}

    marks = ",".join("?" * len(asset_ids))
    rows = conn.execute(
        f"SELECT * FROM assets "
        f"WHERE id IN ({marks})", list(asset_ids)).fetchall()

    allowed = set(roots) if roots is not None else None
    stamp = datetime.now().strftime("%Y-%m-%d")
    moved: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []

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
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target = _unique(target)
            if source.exists():
                shutil.move(str(source), str(target))
            else:
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
                "relations": {table: [dict(r) for r in conn.execute(
                    f"SELECT * FROM {table} WHERE asset_id=?", (row["id"],))]
                    for table in RESTORED_RELATIONS if table in db._tables(conn)},
            }, default=lambda value: {"__bytes__": base64.b64encode(value).decode("ascii")}),
        })

    if not moved:
        return {"deleted": 0, "failed": failed, "items": []}

    now = time.time()
    with db._write_lock:                          # noqa: SLF001 — same package
        try:
            conn.executemany(
                "INSERT INTO recycled(asset_id, root, rel_path, filename, size, "
                "thumb, bin_path, deleted_at, deleted_by, metadata) VALUES(?,?,?,?,?,?,?,?,?,?)",
                [(m["id"], m["root"], m["rel_path"], m["filename"], m["size"],
                  m["thumb"], m["bin_path"], now, user_id, m["metadata"]) for m in moved])
            conn.executemany("DELETE FROM assets WHERE id=?",
                             [(m["id"],) for m in moved])
            conn.commit()
        except BaseException:
            # The files are in the bin but the index never heard of it: no
            # ``recycled`` row to restore them from, and ``assets`` rows that
            # still point at where they were. Put every file back where it
            # came from, as restore() does for its one, so the library again
            # describes exactly what is on the disk. A file that cannot be
            # put back is logged with both paths — it is safe in the bin, and
            # that line is the only record of where.
            conn.rollback()
            for m in reversed(moved):
                if not m["bin_path"]:
                    continue                      # it was never there to move
                source = Path(m["root"]) / m["rel_path"]
                try:
                    if source.exists():
                        raise FileExistsError(source)
                    shutil.move(m["bin_path"], str(source))
                except Exception:                 # noqa: BLE001
                    log.exception("recycle bin: could not put %s back at %s "
                                  "after the index refused the delete",
                                  m["bin_path"], source)
            raise

    return {"deleted": len(moved), "failed": failed, "items": moved,
            "thumbs": [m["thumb"] for m in moved if m["thumb"]]}


def listing(conn, limit: int = 500) -> list[dict[str, Any]]:
    """What is in the bin, newest first, with whether the file is still there."""
    rows = conn.execute(
        "SELECT * FROM recycled WHERE restored_at IS NULL "
        "ORDER BY deleted_at DESC, id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["present"] = bool(item["bin_path"]) and os.path.exists(item["bin_path"])
        out.append(item)
    return out


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


def restore(conn, entry_ids: Sequence[int]) -> dict[str, Any]:
    """Put files back where they came from. The next scan re-indexes them."""
    if not entry_ids:
        return {"restored": 0, "failed": []}

    marks = ",".join("?" * len(entry_ids))
    rows = conn.execute(
        f"SELECT * FROM recycled WHERE id IN ({marks}) AND restored_at IS NULL",
        list(entry_ids)).fetchall()

    done: list[int] = []
    failed: list[dict[str, str]] = []
    for row in rows:
        source = Path(row["bin_path"]) if row["bin_path"] else None
        target = Path(row["root"]) / row["rel_path"]
        if source is None or not source.exists():
            failed.append({"name": row["filename"],
                           "why": "the file is no longer in the bin"})
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
                    record.update(rel_path=target.relative_to(Path(row["root"])).as_posix(),
                                  filename=target.name, trashed=0)
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
                        f"INSERT INTO assets ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
                        [record[c] for c in cols])
                    for table, rows_to_restore in relations.items():
                        if table not in RESTORED_RELATIONS or table not in db._tables(conn):
                            continue
                        for relation in rows_to_restore:
                            relation["asset_id"] = cursor.lastrowid
                            if table == "faces":
                                relation.pop("id", None)      # a fresh face id
                                # The person it was attached to may have been
                                # merged away since; then it is simply unnamed.
                                if relation.get("person_id") is not None and not conn.execute(
                                        "SELECT 1 FROM people_clusters WHERE id=?",
                                        (relation["person_id"],)).fetchone():
                                    relation["person_id"] = None
                                    relation["source"] = "none"
                            elif table != "embeddings":
                                owner_table, owner_key = (("albums", "album_id") if table == "album_items"
                                                          else ("users", "user_id"))
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

    if done:
        with db._write_lock:                      # noqa: SLF001
            conn.executemany("UPDATE recycled SET restored_at=? WHERE id=?",
                             [(time.time(), i) for i in done])
            conn.commit()
    return {"restored": len(done), "failed": failed}


def purge(conn, entry_ids: Sequence[int]) -> dict[str, Any]:
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
        if BIN_NAME not in path.parts:
            failed.append({"name": row["filename"],
                           "why": "that file is not inside the recycle bin"})
            continue
        try:
            path.unlink()
        except OSError as exc:
            failed.append({"name": row["filename"], "why": str(exc)})
            continue
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
        with db._write_lock:                      # noqa: SLF001
            conn.execute(
                f"DELETE FROM recycled WHERE id IN ({','.join('?' * len(done))})",
                done)
            conn.commit()
    return {"purged": len(done), "failed": failed, "thumbs": _unused(conn, thumbs)}


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
