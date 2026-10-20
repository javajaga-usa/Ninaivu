"""The library's change journal: what changed, when, and to which file.

The index has always known what the library *is*. It has never known what
*happened* to it: a scan that found 1,200 files gone from the 2026 folder
removed their rows and said "Library up to date", and the only record was a
count in the log. When a folder was replaced by an older copy by accident,
nothing in Ninaivu could say which files, when, or what they had been.

With ``change_journal`` on (Settings → Advanced, 2.0 proposal B, the second
stage after content fingerprints), every change to the index's list of files
is written down as it happens, one row each, in ``library_journal``:

* **added**: a file arrived (a scan found it, an upload or an import put it
  there, the bin gave it back);
* **edited**: its bytes changed (its size or modified time moved: an edit
  saved over it, a rotation, a copy written over it);
* **renamed** and **moved**: the same file under another name, in the same
  folder or another one (the fingerprints say so, storage/fingerprints.py, or
  the date editor moved it);
* **binned** and **restored**: put in Ninaivu's bin and taken out again;
* **removed**: gone from the index, with the fingerprint it had, so it can be
  found again in a backup or the mirror by its bytes.

The rows are written by SQLite triggers on ``assets``, not by the code that
changes it: a scan, an upload, the date editor, the bin and a repair tool all
write through them alike, and a writer added later cannot forget to. The
triggers are there only while the setting is on (:func:`switch`); what was
written stays when it is switched off, and is trimmed to a year
(:func:`trim`).

What reads it:

* :func:`changes_since`, a cursor anything can follow ("what changed since
  row N"), so a feature acts on what changed instead of looking at
  everything again: the console's *Library changes*, Ask Ninaivu's "what is
  new", and the mirror, backup and replica in later steps;
* :func:`summary` and :func:`bursts`, for people: a day's changes by folder,
  and a warning when hundreds of files went or were written over in one
  folder within minutes, which is what an accident looks like;
* :func:`plan_start_up`, on a Mac: the drive's own record of what changed
  while Ninaivu was not running (FSEvents, storage/fsevents.py), so start-up
  reads only those folders instead of walking 208,000 files. Whenever that
  record cannot be trusted completely, the library is walked as before.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import db

log = logging.getLogger(__name__)

#: What a row can say happened, in the order people read them.
KINDS = ("added", "edited", "renamed", "moved", "binned", "restored", "removed")

SCHEMA = """
CREATE TABLE IF NOT EXISTS library_journal (
    seq       INTEGER PRIMARY KEY AUTOINCREMENT,
    at        REAL    NOT NULL,
    kind      TEXT    NOT NULL,
    asset_id  INTEGER,
    root      TEXT    NOT NULL DEFAULT '',
    rel_path  TEXT    NOT NULL DEFAULT '',
    folder    TEXT    NOT NULL DEFAULT '',
    old_path  TEXT    NOT NULL DEFAULT '',
    item_kind TEXT    NOT NULL DEFAULT '',
    size      INTEGER NOT NULL DEFAULT 0,
    digest    TEXT    NOT NULL DEFAULT '',
    visibility INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_journal_at ON library_journal(at);
CREATE INDEX IF NOT EXISTS idx_journal_asset ON library_journal(asset_id);
"""

#: Now, as Unix seconds with a fraction, in SQLite of any age.
_NOW = "((julianday('now') - 2440587.5) * 86400.0)"
#: The fingerprint a row had, if one was taken (storage/fingerprints.py). Read
#: before the row goes: the fingerprint is deleted with it.
_DIGEST = ("COALESCE((SELECT digest FROM content_fingerprints f "
           "WHERE f.asset_id = OLD.id), '')")

#: The triggers, by name. ``removed`` is a BEFORE trigger so the fingerprint is
#: still there to be read; everything else is written once the change is.
TRIGGERS: dict[str, str] = {
    "journal_added": f"""
        CREATE TRIGGER IF NOT EXISTS journal_added AFTER INSERT ON assets
        BEGIN
            INSERT INTO library_journal(at, kind, asset_id, root, rel_path, folder,
                                        item_kind, size, visibility)
            VALUES ({_NOW}, 'added', NEW.id, NEW.root, NEW.rel_path, NEW.folder,
                    NEW.kind, NEW.size, NEW.visibility);
        END""",
    "journal_edited": f"""
        CREATE TRIGGER IF NOT EXISTS journal_edited AFTER UPDATE OF size, mtime ON assets
        WHEN (OLD.size IS NOT NEW.size OR abs(OLD.mtime - NEW.mtime) > 0.000001)
         AND OLD.rel_path IS NEW.rel_path
        BEGIN
            INSERT INTO library_journal(at, kind, asset_id, root, rel_path, folder,
                                        item_kind, size, visibility)
            VALUES ({_NOW}, 'edited', NEW.id, NEW.root, NEW.rel_path, NEW.folder,
                    NEW.kind, NEW.size, NEW.visibility);
        END""",
    "journal_moved": f"""
        CREATE TRIGGER IF NOT EXISTS journal_moved AFTER UPDATE OF rel_path ON assets
        WHEN OLD.rel_path IS NOT NEW.rel_path
        BEGIN
            INSERT INTO library_journal(at, kind, asset_id, root, rel_path, folder,
                                        old_path, item_kind, size, visibility)
            VALUES ({_NOW},
                    CASE WHEN OLD.folder IS NEW.folder THEN 'renamed' ELSE 'moved' END,
                    NEW.id, NEW.root, NEW.rel_path, NEW.folder, OLD.rel_path,
                    NEW.kind, NEW.size, NEW.visibility);
        END""",
    "journal_binned": f"""
        CREATE TRIGGER IF NOT EXISTS journal_binned AFTER UPDATE OF trashed ON assets
        WHEN COALESCE(OLD.trashed, 0) IS NOT COALESCE(NEW.trashed, 0)
        BEGIN
            INSERT INTO library_journal(at, kind, asset_id, root, rel_path, folder,
                                        item_kind, size, visibility)
            VALUES ({_NOW},
                    CASE WHEN COALESCE(NEW.trashed, 0) != 0 THEN 'binned' ELSE 'restored' END,
                    NEW.id, NEW.root, NEW.rel_path, NEW.folder, NEW.kind, NEW.size,
                    NEW.visibility);
        END""",
    "journal_removed": f"""
        CREATE TRIGGER IF NOT EXISTS journal_removed BEFORE DELETE ON assets
        BEGIN
            INSERT INTO library_journal(at, kind, asset_id, root, rel_path, folder,
                                        item_kind, size, digest, visibility)
            VALUES ({_NOW}, 'removed', OLD.id, OLD.root, OLD.rel_path, OLD.folder,
                    OLD.kind, OLD.size, {_DIGEST}, OLD.visibility);
        END""",
}

#: How long rows are kept, and how many at most: a year of an active library
#: is a few hundred thousand rows, a few tens of megabytes.
KEEP_DAYS = 365
KEEP_ROWS = 1_000_000

#: Hundreds of files gone or written over in one folder within this many
#: seconds of each other is worth a word to an administrator.
BURST_WINDOW = 15 * 60
BURST_AT_LEAST = 200
#: The kinds a burst is made of. Files added in bulk are an import, and moved
#: or renamed ones keep everything; neither loses anything.
BURST_KINDS = ("removed", "edited", "binned")


def enabled(cfg: Any) -> bool:
    """Whether this server keeps the journal."""
    return bool(getattr(cfg, "change_journal", False))


def create(conn: sqlite3.Connection) -> None:
    """The table, which is there whether or not the journal is kept, so what
    was written stays readable after the setting is switched off."""
    conn.executescript(SCHEMA)


def switch(conn: sqlite3.Connection, on: bool) -> bool:
    """Put the triggers in place, or take them away. Returns whether the
    journal is now being kept. Safe to call at every start."""
    with db._write_lock:                                  # noqa: SLF001
        create(conn)
        cols = db.columns(conn, "assets")
        if on and {"trashed", "visibility", "folder"} <= cols:
            for sql in TRIGGERS.values():
                conn.execute(sql)
        else:
            for name in TRIGGERS:
                conn.execute(f"DROP TRIGGER IF EXISTS {name}")
            on = False
        conn.commit()
    return on


def keeping(conn: sqlite3.Connection) -> bool:
    """Whether the triggers are in place in this index."""
    found = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'journal_%'")}
    return set(TRIGGERS) <= found


# -- reading it ---------------------------------------------------------------

def _public(row: sqlite3.Row) -> dict[str, Any]:
    item = {k: row[k] for k in ("seq", "at", "kind", "asset_id", "root", "rel_path",
                                "folder", "item_kind", "size")}
    if row["old_path"]:
        item["old_path"] = row["old_path"]
    if row["digest"]:
        item["digest"] = row["digest"]
    return item


def latest(conn: sqlite3.Connection) -> int:
    """The newest row's number: where a reader starting now begins."""
    row = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM library_journal").fetchone()
    return int(row[0] or 0)


def changes_since(conn: sqlite3.Connection, after: int = 0, *, limit: int = 500,
                  roots: Sequence[str] | None = None,
                  kinds: Iterable[str] | None = None) -> dict[str, Any]:
    """The changes after row *after*, oldest first, at most *limit* of them.

    ``next`` is the cursor to pass back for the ones after these, and ``more``
    says whether there are any yet. Rows trimmed away since a reader last
    asked are simply not there: ``gap`` says so, and a careful reader then
    looks at everything once, as it would have without the journal.
    """
    limit = max(1, min(int(limit), 5000))
    where, params = ["seq > ?"], [int(after)]
    if roots:
        where.append(f"root IN ({','.join('?' for _ in roots)})")
        params += list(roots)
    if kinds:
        wanted = [k for k in kinds if k in KINDS]
        if wanted:
            where.append(f"kind IN ({','.join('?' for _ in wanted)})")
            params += wanted
    rows = conn.execute(
        f"SELECT * FROM library_journal WHERE {' AND '.join(where)} "
        "ORDER BY seq LIMIT ?", (*params, limit + 1)).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    oldest = conn.execute("SELECT MIN(seq) FROM library_journal").fetchone()[0]
    return {
        "items": [_public(r) for r in rows],
        "next": rows[-1]["seq"] if rows else max(int(after), 0),
        "more": more,
        "gap": bool(after and oldest is not None and oldest > int(after) + 1),
    }


def _area(folder: str, depth: int = 2) -> str:
    """The part of a folder a person thinks of it by: "2026/10" for
    "2026/10/19". A library is kept year/month/day."""
    parts = [p for p in (folder or "").replace("\\", "/").split("/") if p]
    return "/".join(parts[:depth])


def summary(conn: sqlite3.Connection, *, days: int = 30,
            roots: Sequence[str] | None = None, now: float | None = None) -> list[dict[str, Any]]:
    """The last *days* days of changes, a line for each day, kind and area:
    ``{"day", "kind", "root", "area", "count", "bytes", "first", "last"}``,
    newest day first. A day is the computer's own, as people say it."""
    now = time.time() if now is None else now
    since = now - max(1, int(days)) * 86400
    where, params = ["at >= ?"], [since]
    if roots:
        where.append(f"root IN ({','.join('?' for _ in roots)})")
        params += list(roots)
    rows = conn.execute(
        "SELECT date(at, 'unixepoch', 'localtime') AS day, kind, root, folder, "
        "COUNT(*) AS n, SUM(size) AS bytes, MIN(at) AS first, MAX(at) AS last "
        f"FROM library_journal WHERE {' AND '.join(where)} "
        "GROUP BY day, kind, root, folder", params).fetchall()
    merged: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for r in rows:
        key = (r["day"], r["kind"], r["root"], _area(r["folder"]))
        line = merged.setdefault(key, {"day": key[0], "kind": key[1], "root": key[2],
                                       "area": key[3], "count": 0, "bytes": 0,
                                       "first": r["first"], "last": r["last"]})
        line["count"] += int(r["n"])
        line["bytes"] += int(r["bytes"] or 0)
        line["first"] = min(line["first"], r["first"])
        line["last"] = max(line["last"], r["last"])
    order = {k: i for i, k in enumerate(KINDS)}
    return sorted(merged.values(),
                  key=lambda x: (-_day_number(x["day"]), order.get(x["kind"], 99), x["area"]))


def _day_number(day: str) -> int:
    try:
        return int(day.replace("-", ""))
    except (AttributeError, ValueError):
        return 0


def bursts(conn: sqlite3.Connection, *, days: int = 30, at_least: int = BURST_AT_LEAST,
           window: float = BURST_WINDOW, now: float | None = None) -> list[dict[str, Any]]:
    """Moments when many files went, or were written over, in one place at
    once: ``{"kind", "root", "area", "count", "first", "last"}``, newest first.

    Changes of one kind in one area are a burst while each comes within
    *window* seconds of the one before; one of *at_least* or more is
    reported. It is the shape of a folder replaced by an older copy, a drive
    emptied, or a sync that went the wrong way, and it is also the shape of
    a household tidying up: it is said, never acted on.
    """
    now = time.time() if now is None else now
    since = now - max(1, int(days)) * 86400
    rows = conn.execute(
        "SELECT kind, root, folder, at FROM library_journal "
        f"WHERE at >= ? AND kind IN ({','.join('?' for _ in BURST_KINDS)}) "
        "ORDER BY kind, root, at", (since, *BURST_KINDS)).fetchall()
    open_: dict[tuple[str, str, str], dict[str, Any]] = {}
    found: list[dict[str, Any]] = []

    def close(key: tuple[str, str, str]) -> None:
        run = open_.pop(key, None)
        if run and run["count"] >= at_least:
            found.append(run)

    for r in rows:
        key = (r["kind"], r["root"], _area(r["folder"], 1))
        run = open_.get(key)
        if run is not None and r["at"] - run["last"] > window:
            close(key)
            run = None
        if run is None:
            run = open_[key] = {"kind": key[0], "root": key[1], "area": key[2],
                                "count": 0, "first": r["at"], "last": r["at"]}
        run["count"] += 1
        run["last"] = r["at"]
    for key in list(open_):
        close(key)
    return sorted(found, key=lambda b: -b["last"])


def added_since(conn: sqlite3.Connection, since: float, *, limit: int = 2000) -> list[int]:
    """The items added since *since* (Unix seconds) that are still there,
    newest first. Who may see which of them is the caller's question."""
    rows = conn.execute(
        "SELECT DISTINCT j.asset_id FROM library_journal j "
        "JOIN assets a ON a.id = j.asset_id "
        "WHERE j.kind IN ('added', 'restored') AND j.at >= ? "
        "ORDER BY j.seq DESC LIMIT ?", (float(since), int(limit))).fetchall()
    return [int(r[0]) for r in rows]


def history(conn: sqlite3.Connection, asset_id: int) -> list[dict[str, Any]]:
    """What happened to one item, oldest first."""
    rows = conn.execute("SELECT * FROM library_journal WHERE asset_id=? ORDER BY seq",
                        (int(asset_id),)).fetchall()
    return [_public(r) for r in rows]


def trim(conn: sqlite3.Connection, *, keep_days: int = KEEP_DAYS,
         keep_rows: int = KEEP_ROWS, now: float | None = None) -> int:
    """Forget rows older than *keep_days*, and the oldest beyond *keep_rows*.
    Returns how many went."""
    now = time.time() if now is None else now
    with db._write_lock:                                  # noqa: SLF001
        gone = conn.execute("DELETE FROM library_journal WHERE at < ?",
                            (now - keep_days * 86400,)).rowcount or 0
        newest = latest(conn)
        if newest > keep_rows:
            gone += conn.execute("DELETE FROM library_journal WHERE seq <= ?",
                                 (newest - keep_rows,)).rowcount or 0
        conn.commit()
    return int(gone)


def counts(conn: sqlite3.Connection) -> dict[str, Any]:
    """How much is written down, for the console."""
    row = conn.execute("SELECT COUNT(*), MIN(at), MAX(seq) FROM library_journal").fetchone()
    return {"rows": int(row[0] or 0), "since": row[1], "latest": int(row[2] or 0),
            "keeping": keeping(conn)}


# -- start-up on a Mac: the drive's own record --------------------------------

#: Where the drive's record of a library folder was taken up to, in ``meta``.
MARK_KEY = "journal_mark:{root}"
#: When the whole of a library folder was last read, in ``meta``.
WALKED_KEY = "journal_walked:{root}"
#: However sure the drive's record is, the whole library is read at least
#: this often: the one thing it cannot tell is a drive changed on another
#: computer that keeps no record of its own (FAT, exFAT, a network share).
WALK_EVERY_DAYS = 7


@dataclass(frozen=True)
class StartUp:
    """What start-up reads of one library folder: *folders* inside it (each
    with everything under it), none at all when nothing changed. *mark* is
    remembered once they have been read."""

    folders: frozenset[str]
    mark: dict[str, Any]


def _fsevents():
    from . import fsevents                                 # noqa: PLC0415
    return fsevents


def _folder_identity(root: str | Path) -> list[int] | None:
    """The library folder itself, as the drive knows it: its device and
    inode. A folder renamed away and another (an older copy) renamed into its
    place while Ninaivu was off leaves only its parent in the drive's record,
    so the start-up would read nothing (audit D-14); this tells them apart."""
    try:
        found = os.stat(root)
    except OSError:
        return None
    return [int(found.st_dev), int(found.st_ino)]


def take_mark(root: str | Path, *, fse: Any = None) -> dict[str, Any] | None:
    """Where the drive's record of *root* is now, or ``None`` where there is
    no such record (not a Mac, or a drive that keeps none)."""
    fse = fse or _fsevents()
    try:
        if not fse.available():
            return None
        volume = fse.volume_id(str(root))
        if not volume:
            return None
        return {"volume": volume, "event": int(fse.current_event_id()), "at": time.time(),
                "folder": _folder_identity(root)}
    except Exception:                                      # noqa: BLE001 - only an optimisation
        log.debug("no change record for %s", root, exc_info=True)
        return None


def remember_mark(conn: sqlite3.Connection, root: str | Path,
                  mark: dict[str, Any] | None, *, whole: bool) -> None:
    """Keep *mark* as where the next start-up asks the drive from, once
    everything changed before it has been read. *whole*: all of *root* was."""
    if not mark:
        return
    with db._write_lock:                                  # noqa: SLF001
        db.set_meta(conn, MARK_KEY.format(root=root), json.dumps(mark))
        if whole:
            db.set_meta(conn, WALKED_KEY.format(root=root), str(mark.get("at") or time.time()))
        conn.commit()


def stored_mark(conn: sqlite3.Connection, root: str | Path) -> dict[str, Any] | None:
    try:
        mark = json.loads(db.get_meta(conn, MARK_KEY.format(root=root)) or "null")
    except ValueError:
        return None
    return mark if isinstance(mark, dict) else None


def plan_start_up(conn: sqlite3.Connection, root: str | Path, *, fse: Any = None,
                  now: float | None = None,
                  relative: Any = None) -> StartUp | None:
    """Which folders of *root* changed while Ninaivu was not running, by the
    drive's own record, or ``None`` to read all of it as before.

    ``None`` unless every one of these holds: this is a Mac and the drive
    keeps a record; the last record was taken on this same drive (a drive
    swapped for another with the same name has its own); all of *root* was
    read within :data:`WALK_EVERY_DAYS`; and the drive's answer is complete,
    with nothing dropped, wrapped or rewritten and not too many folders.
    """
    fse = fse or _fsevents()
    now = time.time() if now is None else now
    stored = stored_mark(conn, root)
    if not stored or not stored.get("event"):
        return None
    try:
        walked = float(db.get_meta(conn, WALKED_KEY.format(root=root)) or 0)
    except ValueError:
        walked = 0.0
    if now - walked > WALK_EVERY_DAYS * 86400:
        return None
    fresh = take_mark(root, fse=fse)
    if fresh is None or fresh["volume"] != stored.get("volume"):
        return None
    if fresh.get("folder") is None or fresh.get("folder") != stored.get("folder"):
        # Not the folder the mark was taken on (or a mark from before this was
        # kept): what is in it now was never read.
        log.info("reading all of %s at start-up: the library folder is not the one "
                 "last read", root)
        return None
    try:
        found = fse.changes_since([str(root)], int(stored["event"]))
    except Exception:                                      # noqa: BLE001 - read it all instead
        log.info("could not ask the drive what changed in %s", root, exc_info=True)
        return None
    if not found.complete:
        log.info("reading all of %s at start-up: the drive's record of changes "
                 "is not complete (%s)", root, found.reason or "no reason given")
        return None
    if relative is None:
        from ..media.scanner import _relative_folder as relative  # noqa: PLC0415
    folders: set[str] = set()
    for path in found.folders:
        rel = relative(root, path)
        if rel is None:
            continue
        if rel == "":
            return None                     # the library folder itself: all of it
        folders.add(rel)
    return StartUp(frozenset(folders), fresh)
