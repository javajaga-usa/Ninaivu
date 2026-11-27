"""A second copy of the library, on another disk.

The cloud backup is one copy somewhere else. One is not many: it sits behind a
single account, it takes weeks to fill over a home connection, and getting a
terabyte back out of it takes as long again. A disk in a drawer has none of
those problems and different ones of its own, which is the point of having
both.

So Ninaivu can keep a plain copy of the library in a folder the household
chooses — an external drive, a second internal disk, a network share that is
mounted. *Plain* is deliberate: the files are the files, in the folders they
are in, readable by anything, with nothing of Ninaivu's needed to get them
back. Pull the disk out, plug it into another computer, and the photographs
are there.

The rules are the cloud backup's, for the same reasons:

**It only ever goes out.** Nothing is read from the copy to change the
library. A file deleted from the library is *not* deleted from the copy — a
second copy that forgets what the first forgot is not a second copy.

**Nothing on the copy is overwritten or removed.** A file that has changed in
the library is copied beside the older one, which is kept under a name that
says when it was set aside. If the change was a disk going bad, the good
version is still there.

**What was on the disk before is left as it was.** A backup folder or a
network share chosen for the copy often holds the photographs already, put
there by hand or by another backup. A file already there with the same bytes
(read and compared, SHA-256) is taken as the copy and not written again,
whether it is at the library's own place or anywhere else on the disk (looked
over once, when the disk is new to the record). A different file at the
library's place is not touched, not renamed and not moved: the library's
goes beside it as ``name (2).jpg``, and NINAIVU-NAMES.txt says so. Only a
file Ninaivu wrote itself is ever set aside.

**A file is whole or absent.** It is written under a temporary name, hashed as
it goes, checked for size, synced, and only then renamed into place.

**The disk is known by a marker, not by its path.** A different disk mounted
at the same path has none of the files the record says were copied; believing
the record would mean backing nothing up and reporting that everything was.
Each destination is given a marker file, and a destination without the one
the record was made against is copied to from the beginning.

Everything goes, hidden things included: it is the household's own disk, and
what they hid from each other they did not mean to lose.

**It goes onto any kind of disk.** A library kept on a Mac or a Linux disk
holds names a Windows-formatted drive (exFAT, NTFS, FAT32) or a Samba share
refuses, such as ``Diwali: day 2`` or ``what?.jpg``. Before copying, the kind
of disk is worked out (storage/fit.py, the same rules Will it fit? reports
with), and a file whose name it cannot keep is written under one it can, with
the record saying where (``stored_as``). Those names are also listed in a
plain text file at the top of the copy (:data:`NAMES`), so the copy can be
read back by hand without Ninaivu. A file larger than the disk can hold at
all (over 4 GB on FAT32) is left out and said so, rather than failing again
on every run.

**The copy is checked, and can be brought back.** Once a month (by default)
every file on it is read again and its fingerprint compared with the one
taken as it was written; one that no longer matches — a disk going bad in a
drawer — is copied again, the bad one set aside beside it. And when the
library loses files, **Restore from this disk** puts back whatever is on the
copy and no longer in the library, each checked against its fingerprint first
and never over a file that is there.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import shutil
import sqlite3
import stat
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable

from ..utils.files import (CHUNK, create_new, move_no_replace, publish_no_replace, sha256_file,
                           stays_inside)
from ..copies import ledger
from . import fit, mirror_place, roots as roots_kit

log = logging.getLogger(__name__)

__all__ = ["Mirror", "folder_problem", "note_move", "MARKER", "NAMES", "RESERVE"]

#: The file, at the top of a destination, that says which copy it is.
MARKER = ".ninaivu-second-copy.json"

#: The plain list, at the top of a destination, of every file kept there under
#: a name other than its own: where it is on the copy, a tab, and its place in
#: the library.
NAMES = "NINAIVU-NAMES.txt"

#: Most files too large for the disk named in the status, as examples.
TOO_LARGE_EXAMPLES = 5

#: Left free on the destination, so the copy never fills a disk to the byte.
RESERVE = 1024 * 1024 * 1024

#: Said when a network share is there but the copy on it is not.
SHARE_EMPTY = ("the share is connected but the second copy is not on it; the disk behind "
               "the share may have dropped. Nothing was written there. If this is a new "
               "disk, press Start afresh here")

#: How often the keeper looks at whether a run is owed, in seconds.
LOOK_EVERY = 10 * 60.0

#: After a run that did not finish (the disk unplugged, stopped, full), how
#: long before another is tried by itself: soon, not a whole day later.
RETRY_AFTER = 60 * 60.0

#: Files checked between saving where a check of the copy has got to.
VERIFY_SAVE_EVERY = 100

#: ``mirror_copies.origin`` of a file Ninaivu wrote itself, and of one that was
#: on the disk already with the same bytes and is taken as the copy. Only the
#: first kind is ever set aside; the second is never touched.
COPIED, FOUND = "copied", "found"

#: Folders and files on a destination that are the disk's or another
#: program's, not photographs: never looked through for one already there.
PASSED_OVER = {"@eaDir", "#recycle", "#snapshot", "$RECYCLE.BIN",
               "System Volume Information", "lost+found"}

#: Files looked at between pauses for the household while looking the
#: destination over.
LOOK_PAUSE_EVERY = 500

SCHEMA = """
CREATE TABLE IF NOT EXISTS mirror_copies (
    root       TEXT NOT NULL,
    rel_path   TEXT NOT NULL,
    size       INTEGER NOT NULL DEFAULT 0,
    mtime      REAL NOT NULL DEFAULT 0,
    sha256     TEXT NOT NULL DEFAULT '',
    copied_at  REAL NOT NULL DEFAULT 0,
    stored_as  TEXT NOT NULL DEFAULT '',
    origin     TEXT NOT NULL DEFAULT 'copied',
    PRIMARY KEY (root, rel_path)
);
CREATE TABLE IF NOT EXISTS mirror_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS mirror_moves (
    id       INTEGER PRIMARY KEY,
    root     TEXT NOT NULL,
    old_rel  TEXT NOT NULL,
    new_rel  TEXT NOT NULL
);
-- Files that were on the destination before Ninaivu wrote anything there, of
-- a size some library file still to be copied has: where to look for the
-- same photograph before writing it again (Mirror._look_over).
CREATE TABLE IF NOT EXISTS mirror_found (
    path       TEXT PRIMARY KEY,
    size       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mirror_found_size ON mirror_found(size);
"""


def note_move(conn: sqlite3.Connection, root: str, old_rel: str, new_rel: str) -> None:
    """Say that a library file now lives at *new_rel*, for the second copy.

    The record of the second copy is kept by the file's place in the library.
    A photograph given another date (media/date_edit.py), or moved in Finder
    and known by its fingerprint (db.respell_asset), kept its record at the
    old place, so **Restore from this disk** put it back there: the same
    photograph twice, on two dates, and the second a new row, shown to the
    family even when the first had been hidden. The copy on the disk is where
    it was and stays there; the next run (:meth:`Mirror._follow_moves`) moves
    the record to the new place, saying where on the disk the file is kept.

    Called inside the caller's transaction; nothing when no second copy was
    ever made.
    """
    if old_rel == new_rel:
        return
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='mirror_copies'").fetchone() is None:
            return
        conn.execute("CREATE TABLE IF NOT EXISTS mirror_moves (id INTEGER PRIMARY KEY, "
                     "root TEXT NOT NULL, old_rel TEXT NOT NULL, new_rel TEXT NOT NULL)")
        conn.execute("INSERT INTO mirror_moves(root, old_rel, new_rel) VALUES(?,?,?)",
                     (str(root), str(old_rel), str(new_rel)))
    except sqlite3.Error as exc:
        log.warning("could not note a move for the second copy: %s", exc)


def folder_problem(folder: str, roots: list[str], state_dir: Path | str) -> str | None:
    """Why *folder* cannot hold the second copy, or None if it can."""
    if not str(folder or "").strip():
        return "Choose the folder the second copy is kept in."
    target = Path(folder).expanduser()
    if not target.is_absolute():
        return "Give the whole path to the folder."
    try:
        resolved = target.resolve()
    except OSError:
        return "That folder cannot be reached."
    for root in [*roots, str(state_dir)]:
        try:
            other = Path(root).expanduser().resolve()
        except OSError:
            continue
        if resolved == other or other in resolved.parents:
            # Inside the library it would be indexed as photographs, and then
            # copied into itself; inside the state folder it is not a copy.
            return (f"That folder is inside {other}, which Ninaivu already uses. "
                    "The second copy belongs on another disk.")
        if resolved in other.parents:
            return f"That folder contains {other}; choose a folder of its own."
    if not target.is_dir() or roots_kit.ghost_volume(target):
        # A folder under /Volumes with no disk mounted at it is the Mac's own
        # disk, not the one that was chosen.
        return "That folder is not there. Is the disk plugged in?"
    if not os.access(target, os.W_OK):
        return "Ninaivu cannot write to that folder."
    return None


def _passed_over(name: str) -> bool:
    """A name on a destination that is never a photograph already there."""
    return (name.startswith(".") or name in PASSED_OVER
            or name.endswith(".ninaivu-part"))


def _under(lib: str, rel: str) -> str:
    """*rel* inside the copy's folder *lib* for one library folder; *lib* is
    '' when the copy holds that library folder's own folders at its top."""
    rel = rel.replace("\\", "/")
    return f"{lib}/{rel}" if lib else rel


def _inside(lib: str, path: str) -> str | None:
    """The part of *path* (on the copy) inside *lib*, or None when it is not
    in it. With *lib* '' everything on the copy is."""
    if not lib:
        return path
    return path[len(lib) + 1:] if path.startswith(lib + "/") else None


#: Seconds a folder's look for the library's files is kept.
LIBRARY_HERE_FOR = 600.0

#: How many library files are looked for at their places on a folder, to
#: tell that it already holds the library laid out as the library is.
LOOK_FOR = 24


def _json_list(text: str) -> list[str]:
    try:
        value = json.loads(text or "[]")
    except ValueError:
        return []
    return [str(v) for v in value] if isinstance(value, list) else []


class _Stop(Exception):
    """Asked to stop, or the household needs the machine."""


class _Yield(Exception):
    """A check of the copy making way for the copy itself."""


class _TooLarge(Exception):
    """A file larger than the copy's kind of disk can hold."""


class Mirror:
    """One per running Ninaivu. Copies the library out, a file at a time."""

    def __init__(self, cfg, connect_db: Callable[[], sqlite3.Connection], *,
                 hold: Callable[[], str | None] | None = None,
                 clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self._connect = connect_db
        self._hold = hold
        self._clock = clock
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        #: The supervisor its keeper runs on; Ninaivu gives it its own.
        self.supervisor = None
        #: Set when the keeper was woken before its ten minutes were up, by a
        #: check of the copy that made way for the copy (see _verify's _Yield).
        self._woken = False
        #: The library scanner, when there is one: a restore indexes what it
        #: put back.
        self.scanner = None
        #: Folder listings on the copy, for _taken_by_another: one per folder
        #: per run rather than one per path part per file. None outside a run.
        self._listings: dict[Path, set[str]] | None = None
        #: Set by _copy_one: the file was on the disk already, nothing written.
        self._found_there = False
        #: The kind of disk the copy is on, as the system names it (``exfat``),
        #: when it should not be worked out from the mounted disks (tests).
        self.fstype: str | None = None
        #: What that kind of disk can keep (storage/fit.py), during a run;
        #: None outside one, when every name is written as it is.
        self._family: fit.Family | None = None
        #: The library folder whose own folders are at the top of the copy
        #: (meta ``flat_root``), read once; None until read.
        self._flat_root: str | None = None
        #: What _library_here found, by folder, and when (looked at again
        #: after LIBRARY_HERE_FOR seconds): it reads a share.
        self._here_cache: dict[str, tuple[float, int, int, str]] = {}
        self._state: dict[str, Any] = {
            "running": False, "job": "", "current": "", "copied_this_run": 0,
            "bytes_this_run": 0, "message": "", "error": "", "waiting": ""}

    # -- settings ---------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "mirror_enabled", False))

    @property
    def folder(self) -> Path | None:
        chosen = str(getattr(self.cfg, "mirror_dir", "") or "").strip()
        return Path(chosen).expanduser() if chosen else None

    @property
    def every(self) -> float:
        return max(0.0, float(getattr(self.cfg, "mirror_every_hours", 24) or 0)) * 3600

    @property
    def verify_every(self) -> float:
        return max(0.0, float(getattr(self.cfg, "mirror_verify_days", 30) or 0)) * 86400

    @property
    def roots(self) -> list[str]:
        return self.cfg.library_roots

    def problem(self) -> str | None:
        return folder_problem(str(self.folder or ""), self.roots, self.cfg.state_dir)

    # -- the record -------------------------------------------------------

    def _db(self) -> sqlite3.Connection:
        conn = self._connect()
        conn.executescript(SCHEMA)
        # ``stored_as`` came later: where on the copy a file went when its own
        # name was taken there by another one (see _copy_one).
        columns = {r[1] for r in conn.execute("PRAGMA table_info(mirror_copies)")}
        if "stored_as" not in columns:
            conn.execute("ALTER TABLE mirror_copies ADD COLUMN stored_as TEXT NOT NULL DEFAULT ''")
            conn.commit()
        # ``origin`` too: whether Ninaivu wrote the file, or found it there.
        if "origin" not in columns:
            conn.execute("ALTER TABLE mirror_copies ADD COLUMN origin TEXT NOT NULL "
                         f"DEFAULT '{COPIED}'")
            conn.commit()
        return conn

    @staticmethod
    def _meta(conn, key: str, default: str = "") -> str:
        row = conn.execute("SELECT value FROM mirror_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    @staticmethod
    def _set_meta(conn, key: str, value: Any) -> None:
        conn.execute("INSERT INTO mirror_meta(key, value) VALUES(?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
        conn.commit()

    def _know_the_disk(self, conn, target: Path) -> None:
        """Make sure the record is about the disk that is actually there."""
        marker = target / MARKER
        known = self._meta(conn, "copy_id")
        found = ""
        try:
            found = str(json.loads(marker.read_text(encoding="utf-8")).get("id") or "")
        except (OSError, ValueError, AttributeError):
            pass
        if found and found == known:
            return
        if known and not found and self._meta(conn, "afresh") != "1" \
                and mirror_place.is_network(self._fstype_at(target)):
            # A share with nothing of the copy on it. A Linux VM sharing a
            # USB disk over SMB keeps sharing its mount point when the disk
            # drops (after the Mac sleeps, say), and what is then shared is
            # the VM's own small disk: starting again there filled it. A new
            # disk behind the share is taken only when a person says so.
            raise OSError(SHARE_EMPTY)
        if known and not found and self._meta(conn, "afresh") != "1" \
                and self._bare_mount_point(target):
            # Nothing at all where the copy was, not even its marker, on the
            # disk Ninaivu itself runs from: the folder an unplugged drive
            # leaves behind under a ``nofail`` mount. Wiping the record and
            # starting again copied the whole library onto the system disk.
            # A new, empty disk mounted there is a disk of its own, and is
            # taken as before.
            raise OSError("the disk the second copy is on is not there; only an empty "
                          "folder is at its place")
        if known:
            # A different disk, or this one wiped: what the record says was
            # copied is not on it.
            log.warning("the second copy at %s is not the one the record was made "
                        "against; copying to it from the beginning", target)
        conn.execute("DELETE FROM mirror_copies")
        conn.execute("DELETE FROM mirror_moves")
        conn.execute("DELETE FROM mirror_found")
        self._forget_the_copy(conn)
        copy_id = found or secrets.token_hex(12)
        if not found:
            marker.write_text(json.dumps({
                "id": copy_id, "made": time.strftime("%Y-%m-%d %H:%M:%S"),
                "note": "A second copy of a Ninaivu library. The files are ordinary files "
                        "in their ordinary folders; nothing of Ninaivu's is needed to "
                        "read them. Leave this file where it is so Ninaivu knows the disk.",
            }, indent=2), encoding="utf-8")
        self._set_meta(conn, "copy_id", copy_id)
        self._set_meta(conn, "afresh", "")

    def _forget_the_copy(self, conn: sqlite3.Connection) -> None:
        """What the record said about a copy that is no longer the one."""
        for key in ("layout", "flat_root", "looked_over", "last_verified", "verify_bad",
                    "verify_missing", "verify_checked", "verify_cursor", "last_finished"):
            self._set_meta(conn, key, "")
        self._flat_root = None

    def _library_here(self, conn: sqlite3.Connection, target: Path,
                      root: str) -> tuple[int, int]:
        """How many of a few of *root*'s files are at *target* already, at
        their own paths and sizes, as the library has them: (found, looked
        at). Only sizes, so a few reads of a share; the bytes are compared
        file by file when the copy runs."""
        rows = conn.execute("SELECT rel_path, size FROM assets WHERE trashed=0 AND root=? "
                            "ORDER BY random() LIMIT ?", (root, LOOK_FOR)).fetchall()
        found = 0
        for rel, size in rows:
            there = target / Path(*str(rel).replace("\\", "/").split("/"))
            try:
                if stays_inside(target, there) and there.is_file() \
                        and there.stat().st_size == int(size or 0):
                    found += 1
            except OSError:
                continue
        return found, len(rows)

    def library_here(self, folder: Path | None = None) -> dict[str, Any] | None:
        """Whether *folder* (the copy's, by default) already holds the
        library's files at their own paths, before anything is recorded
        about it: ``{"found", "looked_at", "root"}``, or None."""
        folder = folder or self.folder
        roots = self.roots
        if folder is None or not roots:
            return None
        key = str(folder)
        now = time.monotonic()
        cached = self._here_cache.get(key)
        if cached is None or now - cached[0] > LIBRARY_HERE_FOR:
            conn = self._db()
            best = (0, 0, "")
            try:
                if folder.is_dir() and not roots_kit.ghost_volume(folder):
                    for root in roots:
                        found, looked = self._library_here(conn, folder, root)
                        if found > best[0]:
                            best = (found, looked, root)
            except OSError:
                pass
            self._here_cache[key] = cached = (now, best[0], best[1], best[2])
        _, found, looked, root = cached
        if not found:
            return None
        return {"found": found, "looked_at": looked, "root": root}

    def _choose_layout(self, conn: sqlite3.Connection, target: Path) -> None:
        """Once for each copy: how the library is laid out on it.

        The copy has the library's own layout: the folder chosen stands for
        the library folder, and 2024/05/01/a.jpg in the library is
        2024/05/01/a.jpg on the copy. A copy already in that layout (the
        library copied there by hand, or by another program) is then found
        file by file at the same paths and taken as it is (_copy_one).

        With more than one library folder, the one whose files are already
        at the top of the copy, or else the first, is laid out there; the
        others go in folders of their own names. A copy Ninaivu began
        before, with each library folder in a folder named after it, keeps
        that layout."""
        if self._meta(conn, "layout"):
            return
        roots = self.roots
        flat = ""
        recorded = conn.execute("SELECT 1 FROM mirror_copies LIMIT 1").fetchone()
        if roots and not recorded:
            here = self.library_here(target)
            if here:
                flat = here["root"]
            else:
                first = roots[0]
                nested = target / (Path(first).name or "library")
                # A copy laid out the old way and its record lost: it stays so.
                if not (nested.is_dir() and roots_kit.holds_anything(nested)):
                    flat = first
        self._set_meta(conn, "flat_root", flat)
        self._set_meta(conn, "layout", "flat" if flat else "nested")
        self._flat_root = flat
        if flat:
            log.info("the second copy at %s has the library's layout (%s)", target, flat)

    def _bare_mount_point(self, target: Path) -> bool:
        """Empty, not a mount point, and on the same disk as the state folder."""
        if roots_kit.holds_anything(target) or os.path.ismount(target):
            return False
        try:
            return os.stat(target).st_dev == os.stat(self.cfg.state_dir).st_dev
        except OSError:
            return False

    def _flat(self) -> str:
        """The library folder laid out at the top of the copy, or ''."""
        if self._flat_root is None:
            try:
                self._flat_root = self._meta(self._db(), "flat_root")
            except sqlite3.Error:
                return ""
        return self._flat_root

    def _library_folder(self, root: str) -> str:
        """The folder inside the destination that one library folder goes to:
        '' for the library folder whose own folders are at the top of the copy
        (:meth:`_choose_layout`), so the copy has the library's layout."""
        if root and root == self._flat():
            return ""
        name = Path(root).name or "library"
        same = [r for r in self.roots if (Path(r).name or "library") == name]
        if len(same) > 1:
            # Two libraries called "Photos" must not be poured together.
            name += "-" + hashlib.sha1(str(root).encode()).hexdigest()[:6]  # noqa: S324
        return name

    def _follow_moves(self, conn: sqlite3.Connection) -> int:
        """Move the records of files that moved in the library (:func:`note_move`).

        The file on the disk is not touched: the record keeps its new place in
        the library and says, in ``stored_as``, where on the disk it is. Then a
        restore puts it back where the library has it now, the recycle bin's
        record of it is found by its new place, and it is not copied again.
        Returns how many records moved."""
        moved = 0
        for move in conn.execute("SELECT id, root, old_rel, new_rel FROM mirror_moves "
                                 "ORDER BY id").fetchall():
            root, old, new = move["root"], move["old_rel"], move["new_rel"]
            row = conn.execute("SELECT stored_as FROM mirror_copies WHERE root=? AND rel_path=?",
                               (root, old)).fetchone()
            if row is not None:
                if conn.execute("SELECT 1 FROM mirror_copies WHERE root=? AND rel_path=?",
                                (root, new)).fetchone():
                    # Copied at its new place already: the old record would
                    # only bring the file back at the old one.
                    conn.execute("DELETE FROM mirror_copies WHERE root=? AND rel_path=?",
                                 (root, old))
                else:
                    kept = row["stored_as"] or _under(self._library_folder(root), old)
                    conn.execute("UPDATE mirror_copies SET rel_path=?, stored_as=? "
                                 "WHERE root=? AND rel_path=?", (new, kept, root, old))
                moved += 1
            conn.execute("DELETE FROM mirror_moves WHERE id=?", (move["id"],))
        conn.commit()
        return moved

    # -- asking -----------------------------------------------------------

    def status(self) -> dict[str, Any]:
        with self._lock:
            state = dict(self._state)
        conn = self._db()
        roots = self.roots
        marks = ",".join("?" * len(roots)) or "''"
        total, size = conn.execute(
            f"SELECT COUNT(*), COALESCE(SUM(size), 0) FROM assets "
            f"WHERE trashed=0 AND root IN ({marks})", roots).fetchone()
        copied, copied_size = conn.execute(
            f"SELECT COUNT(*), COALESCE(SUM(a.size), 0) FROM assets a JOIN mirror_copies m "
            f"ON m.root=a.root AND m.rel_path=a.rel_path AND m.size=a.size "
            f"AND ABS(m.mtime - a.mtime) < 0.001 "
            f"WHERE a.trashed=0 AND a.root IN ({marks})", roots).fetchone()
        folder = self.folder
        problem = self.problem() if (self.enabled or folder) else None
        free = None
        there = bool(folder and folder.is_dir()) and not roots_kit.ghost_volume(folder)
        if there:
            try:
                free = int(shutil.disk_usage(folder).free)
            except OSError:
                free = None
            self._remember_place(conn, folder)
        mount, fstype = self.place(conn)
        away = mirror_place.away(str(folder), mount, fstype) if folder and not there else None
        if away and problem and problem.startswith("That folder is not there"):
            problem = str(away)
        owed_bytes = int(size) - int(copied_size)
        recorded = conn.execute("SELECT 1 FROM mirror_copies LIMIT 1").fetchone() is not None
        last_verified = float(self._meta(conn, "last_verified", "0") or 0)
        checked = self._meta(conn, "verify_checked")
        verified = int(checked) if checked.isdigit() else None
        if not recorded or verified == 0:
            # A check with nothing on the copy to read checked nothing; it
            # is not "every file matched".
            last_verified, verified = 0.0, 0
        # Before the first copy: the library's files already there, at their
        # own paths (a copy made by hand). The copy takes them as they are.
        library_here = (self.library_here(folder)
                        if there and not recorded and self._meta(conn, "layout") != "nested"
                        else None)
        return {
            "library_here": library_here,
            "layout": self._meta(conn, "layout"),
            **state,
            "enabled": self.enabled,
            "folder": str(folder or ""),
            "every_hours": self.every / 3600,
            "there": there,
            # The disk the folder is on, as last seen: a share's (smbfs, nfs)
            # is said to be one, and an unmounted one is called not connected.
            "mount_point": mount, "network": mirror_place.is_network(fstype),
            "away": str(away or ""),
            # The same, for the page to say in its own language.
            "away_said": away.as_json() if away else None,
            "short_of_room": free is not None and owed_bytes + RESERVE > free,
            "same_disk_as": mirror_place._same_disk(folder, roots) if there else "",  # noqa: SLF001
            # On a share, the copy's record but not its marker: see _know_the_disk.
            "copy_missing": bool(there and self._meta(conn, "copy_id")
                                 and not (folder / MARKER).is_file()
                                 and mirror_place.is_network(fstype)),
            "problem": problem,
            "files": int(total), "bytes": int(size),
            "copied": int(copied), "copied_bytes": int(copied_size),
            "owed": int(total) - int(copied), "owed_bytes": owed_bytes,
            "free_bytes": free,
            "last_finished": float(self._meta(conn, "last_finished", "0") or 0),
            "last_run": float(self._meta(conn, "last_run", "0") or 0),
            "last_attempt": float(self._meta(conn, "last_attempt", "0") or 0),
            "last_verified": last_verified,
            "verify_checked": verified,
            "verify_bad": int(self._meta(conn, "verify_bad", "0") or 0),
            "verify_missing": int(self._meta(conn, "verify_missing", "0") or 0),
            "verify_every_days": self.verify_every / 86400,
            # The kind of disk the last run found, and the files it could not
            # take at all (over 4 GB on FAT32), with a few of them named.
            "filesystem": self._meta(conn, "filesystem"),
            "too_large": int(self._meta(conn, "too_large", "0") or 0),
            "too_large_examples": _json_list(self._meta(conn, "too_large_examples")),
            "renamed": int(conn.execute(
                "SELECT COUNT(*) FROM mirror_copies WHERE stored_as != '' AND origin != ?",
                (FOUND,)).fetchone()[0]),
            # Already on the disk with the same bytes, so not written again.
            "already_there": int(conn.execute(
                "SELECT COUNT(*) FROM mirror_copies WHERE origin = ?", (FOUND,)).fetchone()[0]),
        }

    def place(self, conn: sqlite3.Connection | None = None) -> tuple[str, str]:
        """The mount point and file system type the folder was on when last
        seen, so a share that has gone is still known to be a share."""
        conn = conn or self._db()
        return self._meta(conn, "place_mount"), self._meta(conn, "place_fs")

    def _fstype_at(self, target: Path) -> str:
        return self.fstype or mirror_place.mount_of(target)[1]

    def _remember_place(self, conn: sqlite3.Connection, folder: Path) -> None:
        mount, fstype = mirror_place.mount_of(folder)
        if self.fstype:
            fstype = self.fstype
        if not mount and not fstype:
            return
        if (mount, fstype) != self.place(conn):
            self._set_meta(conn, "place_mount", mount)
            self._set_meta(conn, "place_fs", fstype)

    def moved_to(self, folder: str) -> None:
        """The folder setting changed to *folder*. The record was made against
        the old one: it is kept only when the copy there is the same copy (its
        marker says so: a share mounted under another name), and otherwise
        let go of, so the new folder is not taken for the old disk gone
        away. Nothing on either disk is touched."""
        conn = self._db()
        known = self._meta(conn, "copy_id")
        found = ""
        try:
            found = str(json.loads((Path(folder).expanduser() / MARKER).read_text(
                encoding="utf-8")).get("id") or "")
        except (OSError, ValueError, AttributeError, TypeError):
            pass
        if known and found == known:
            return
        for table in ("mirror_copies", "mirror_moves", "mirror_found"):
            conn.execute(f"DELETE FROM {table}")            # noqa: S608 - fixed names
        for key in ("copy_id", "afresh", "place_mount", "place_fs", "verify_cursor"):
            self._set_meta(conn, key, "")
        # What was said of the old folder (checked, copied, laid out so) is
        # not true of this one: "every file matched" was shown for a folder
        # with nothing of the copy on it yet.
        self._forget_the_copy(conn)
        self._here_cache.clear()
        conn.commit()

    def start_afresh(self) -> dict[str, Any]:
        """A person says the empty share is a new disk: copy to it from the
        beginning. Only for the case _know_the_disk refused."""
        if self.running:
            return {"started": False, "reason": "A copy or a check is running."}
        conn = self._db()
        self._set_meta(conn, "afresh", "1")
        self._update(error="")
        return self.start()

    def describe(self, folder: str) -> dict[str, Any]:
        """What the console says about *folder* before it is chosen."""
        conn = self._db()
        roots = self.roots
        marks = ",".join("?" * len(roots)) or "''"
        needed = int(conn.execute(
            f"SELECT COALESCE(SUM(size), 0) FROM assets WHERE trashed=0 AND root IN ({marks})",
            roots).fetchone()[0])
        same = str(self.folder or "") == str(folder or "").strip()
        if same:
            needed = int(self.status()["owed_bytes"])
        return mirror_place.describe(
            folder, roots, self.cfg.state_dir, needed_bytes=needed,
            problem=folder_problem(folder, roots, self.cfg.state_dir),
            copy_id=self._meta(conn, "copy_id"),
            library_here=self.library_here(Path(str(folder or "")).expanduser())
            if str(folder or "").strip() else None)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # -- doing ------------------------------------------------------------

    def start(self) -> dict[str, Any]:
        return self._begin(self._run, "copying", "Looking for what has not been copied yet…")

    def verify(self) -> dict[str, Any]:
        """Read every file on the copy again and compare its fingerprint."""
        return self._begin(self._verify, "checking", "Reading the copy back…")

    def restore(self, folder: str = "") -> dict[str, Any]:
        """Put back what is on the copy and no longer in the library."""
        return self._begin(lambda: self._restore(folder), "restoring",
                           "Looking for what the library has lost…")

    def _begin(self, work, job: str, message: str) -> dict[str, Any]:
        problem = self.problem()
        if problem:
            return {"started": False, "reason": problem}
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {"started": False, "already_running": True,
                        "reason": "The second copy is busy; wait for it to finish."}
            self._stop.clear()
            self._state.update(running=True, job=job, current="", copied_this_run=0,
                               bytes_this_run=0, message=message, error="", waiting="")
            self._thread = threading.Thread(target=work, name=f"ninaivu-second-copy-{job}",
                                            daemon=True)
            self._thread.start()
        return {"started": True}

    def stop(self, join: bool = False) -> None:
        """Stop the run in progress; with *join*, at shutdown, the schedule too.
        Stopping one run from the console used to end the schedule as well,
        so nothing was copied by itself again until a restart."""
        self._stop.set()
        if join:
            from ..server.supervisor import supervisor_of        # noqa: PLC0415
            # Asked, not waited for: a look under way only starts a run, and
            # the run's thread is waited for below.
            supervisor_of(self).stop_job(self.KEEPER, timeout=0)
            if self._thread:
                self._thread.join(30)

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    #: The keeper's thread, as before it moved to the supervisor.
    KEEPER = "ninaivu-second-copy-keeper"

    def keep(self) -> None:
        """Run by itself every so often, and whenever the disk comes back."""
        # Every ten minutes, or at once when a check of the copy stood aside
        # for a copy that was due (look_now). That check used to stop and
        # leave the copy to the next look, up to ten minutes later, with
        # nothing happening and the console saying the copy was about to
        # start. Kept by the supervisor (server/supervisor.py); retry_most=0:
        # a look that fails is tried again at the next, as before.
        from ..server.supervisor import Job, every, supervisor_of  # noqa: PLC0415
        supervisor_of(self).add(Job(
            self.KEEPER, self._look, every(LOOK_EVERY), log=log, retry_most=0,
            failure="the second copy could not start by itself",
            progress=lambda: {"running": self.running}))

    def look_now(self) -> None:
        """Wake the keeper before its ten minutes are up."""
        from ..server.supervisor import supervisor_of            # noqa: PLC0415
        self._woken = True
        supervisor_of(self).request(self.KEEPER)

    def _look(self) -> None:
        woken, self._woken = self._woken, False
        thread = self._thread
        if woken and thread is not None and thread is not threading.current_thread():
            # The check that woke us is on its way out: a start while its
            # thread is still alive would be refused as busy.
            thread.join(60)
        if self.due():
            self.start()
        elif self.verify_due():
            self.verify()

    def due(self) -> bool:
        """Whether a copy is owed now. Cheap enough to ask after every scan:
        the record is read first, and the folder and the household only
        asked about when the time has come."""
        if not self.enabled or self.every <= 0 or self.running:
            return False
        if not self._copy_owed(self._db()):
            return False
        if self.problem():
            return False
        return not (self._hold is not None and self._hold())

    def _copy_owed(self, conn) -> bool:
        """The schedule alone: a day (by default) since the last run that
        finished, and not straight after one that did not."""
        now = self._clock()
        last = float(self._meta(conn, "last_run", "0") or 0)
        if now - last < self.every:
            return False
        tried = float(self._meta(conn, "last_attempt", "0") or 0)
        return tried <= last or now - tried >= min(self.every, RETRY_AFTER)

    def verify_due(self) -> bool:
        if not self.enabled or self.verify_every <= 0 or self.running or self.problem():
            return False
        if self._hold is not None and self._hold():
            return False
        conn = self._db()
        if not conn.execute("SELECT 1 FROM mirror_copies LIMIT 1").fetchone():
            return False
        if self._meta(conn, "verify_cursor"):
            return True                     # one that stopped part-way carries on
        last = float(self._meta(conn, "last_verified", "0") or 0)
        return self._clock() - last >= self.verify_every

    def _update(self, **fields: Any) -> None:
        with self._lock:
            self._state.update(fields)

    def _pause_for_the_household(self) -> None:
        while self._hold is not None:
            reason = self._hold()
            if not reason:
                break
            self._update(waiting=reason)
            if self._stop.wait(5):
                raise _Stop()
        self._update(waiting="")
        if self._stop.is_set():
            raise _Stop()

    def _run(self) -> None:
        copied = failed = found = 0
        message = ""
        try:
            conn = self._db()
            target = self.folder
            assert target is not None
            # Tried, not done: ``last_run`` is written only when the run gets
            # to the end, so one cut short by an unplugged disk or a stop is
            # tried again within the hour rather than the next day.
            self._set_meta(conn, "last_attempt", self._clock())
            self._know_the_disk(conn, target)
            self._choose_layout(conn, target)
            self._follow_moves(conn)
            self._listings = {}
            fstype = self.fstype or fit.filesystem_of(target)
            self._family = fit.family_of(fstype)
            self._set_meta(conn, "filesystem", fstype or "")
            too_large: list[str] = []
            away: set[str] = set()
            roots = self.roots
            marks = ",".join("?" * len(roots))
            # What is owed: in the index, and not recorded as copied at this
            # size and time. Photographs first, as the cloud backup sends them.
            owed = conn.execute(
                f"SELECT a.root, a.rel_path, a.size, a.mtime FROM assets a "
                f"LEFT JOIN mirror_copies m ON m.root=a.root AND m.rel_path=a.rel_path "
                f"AND m.size=a.size AND ABS(m.mtime - a.mtime) < 0.001 "
                f"WHERE a.trashed=0 AND a.root IN ({marks}) AND m.root IS NULL "
                # Let go of here, and kept on the Secondary: nothing to read.
                f"AND NOT EXISTS (SELECT 1 FROM mugil_let_go g WHERE g.root=a.root "
                f"AND g.rel_path=a.rel_path) "
                f"ORDER BY (a.kind != 'picture'), a.id", roots).fetchall()
            self._look_over(conn, target, {int(r["size"] or 0) for r in owed} - {0})
            for row in owed:
                self._pause_for_the_household()
                root, rel = row["root"], row["rel_path"]
                if root in away:
                    continue
                if not roots_kit.root_present(root):
                    away.add(root)                  # a library drive not plugged in
                    continue
                if not (target / MARKER).is_file():
                    # Gone, or a share whose disk dropped behind it: what is at
                    # the folder now is not the copy, and nothing goes there.
                    raise OSError("the disk the second copy is on is no longer there")
                self._update(current=rel, message="Copying…")
                try:
                    size, digest, stored = self._copy_one(target, root, rel, conn)
                except FileNotFoundError:
                    continue                        # gone since it was indexed
                except _TooLarge:
                    too_large.append(rel)
                    continue
                except OSError as exc:
                    if getattr(exc, "errno", None) in (28, 122):    # ENOSPC, EDQUOT
                        raise
                    failed += 1
                    log.warning("could not copy %s to the second copy: %s", rel, exc)
                    continue
                origin = FOUND if self._found_there else COPIED
                conn.execute(
                    "INSERT OR REPLACE INTO mirror_copies(root, rel_path, size, mtime, sha256, "
                    "copied_at, stored_as, origin) VALUES(?,?,?,?,?,?,?,?)",
                    (root, rel, size, float(row["mtime"] or 0), digest, time.time(), stored,
                     origin))
                # And the ledger, in the same transaction (ninaivu/copies/ledger.py).
                ledger.confirm(conn, "mirror", [(root, rel, size, float(row["mtime"] or 0))],
                               at=time.time())
                conn.commit()
                if origin == FOUND:
                    found += 1
                    continue
                copied += 1
                with self._lock:
                    self._state["copied_this_run"] += 1
                    self._state["bytes_this_run"] += size
            self._set_meta(conn, "too_large", len(too_large))
            self._set_meta(conn, "too_large_examples", json.dumps(too_large[:TOO_LARGE_EXAMPLES]))
            self._write_names(conn, target)
            self._set_meta(conn, "last_finished", self._clock())
            self._set_meta(conn, "last_run", self._clock())
            message = (f"Copied {copied:,} files." if copied else "Nothing new to copy.")
            if found:
                message += (f" {found:,} were already on the disk with the same bytes and "
                            "were left as they were.")
            if failed:
                message += f" {failed:,} could not be read and will be tried again."
            if too_large:
                message += (f" {len(too_large):,} are too large for this kind of disk "
                            f"({fit.FAT32_LIMIT // 1024 ** 3 + 1} GB a file at most) and were "
                            "left out; a disk formatted as exFAT takes them.")
            if away:
                message += (" A library folder was not connected, so its files "
                            "are still owed.")
            self._update(error="")
        except _Stop:
            message = f"Stopped after {copied:,} files. It carries on from here next time."
        except OSError as exc:
            full = getattr(exc, "errno", None) in (28, 122) or "no room" in str(exc)
            message = ""
            self._update(error=("The disk the second copy is on is full."
                                if full else f"The second copy stopped: {exc}"))
            log.warning("the second copy stopped: %s", exc)
        except Exception as exc:                            # noqa: BLE001
            log.exception("the second copy stopped")
            self._update(error=f"The second copy stopped: {exc}")
        finally:
            self._listings = None
            self._family = None
            self._update(running=False, current="", waiting="", message=message)
            if message:
                log.info("second copy of the library: %s", message)

    def _on_disk(self, target: Path, root: str, rel: str, stored: str = "") -> Path:
        if stored:
            return target / Path(*stored.split("/"))
        return target / Path(*_under(self._library_folder(root), rel).split("/"))

    def copy_path(self, root: str, rel: str) -> Path:
        """Where the second copy keeps *rel* of *root* (it may not be there)."""
        target = self.folder
        assert target is not None
        row = self._db().execute("SELECT stored_as FROM mirror_copies WHERE root=? AND rel_path=?",
                                 (root, rel)).fetchone()
        return self._on_disk(target, root, rel, row[0] if row else "")

    def _verify(self) -> None:
        """Every recorded file read back and its fingerprint compared.

        One that no longer matches, or is gone, loses its record, so the next
        run copies it again — the bad one set aside beside it, never removed.
        """
        bad = missing = checked = 0
        message = ""
        conn = None
        where: tuple[str, str] | None = None
        finished = False
        #: Set aside for a copy that is due: the keeper is woken to start it.
        yielded = False
        try:
            conn = self._db()
            target = self.folder
            assert target is not None
            self._know_the_disk(conn, target)
            self._follow_moves(conn)
            # Carried on from where the last check stopped, with what it had
            # found so far: a whole disk read back takes many hours, and one
            # stopped (or set aside for the nightly copy) used to start again
            # from the first file. Kept in mirror_meta, beside the schedule.
            sql = ("SELECT root, rel_path, sha256, stored_as, origin FROM mirror_copies "
                   "WHERE sha256 != '' AND size >= 0")
            args: list[Any] = []
            try:
                cursor = json.loads(self._meta(conn, "verify_cursor") or "null")
            except ValueError:
                cursor = None
            if isinstance(cursor, dict) and cursor.get("root") is not None \
                    and conn.execute("SELECT 1 FROM mirror_copies LIMIT 1").fetchone():
                sql += " AND (root > ? OR (root = ? AND rel_path > ?))"
                args = [cursor["root"], cursor["root"], str(cursor.get("rel") or "")]
                bad = int(cursor.get("bad") or 0)
                missing = int(cursor.get("missing") or 0)
                checked = int(cursor.get("checked") or 0)
            rows = conn.execute(sql + " ORDER BY root, rel_path", args).fetchall()
            for row in rows:
                self._pause_for_the_household()
                if where is not None and checked % VERIFY_SAVE_EVERY == 0:
                    self._save_verify_cursor(conn, where, bad, missing, checked)
                    if self._copy_owed(conn):
                        # The nightly copy waits for nothing: it goes now, and
                        # this check carries on from here after it.
                        raise _Yield()
                path = self._on_disk(target, row["root"], row["rel_path"], row["stored_as"])
                self._update(current=row["rel_path"])
                try:
                    same = sha256_file(path) == row["sha256"]
                except FileNotFoundError:
                    if not (target / MARKER).is_file():
                        # The whole copy went, not this file: a check that
                        # carried on would call every file missing.
                        raise OSError("the disk the second copy is on is no longer there") \
                            from None
                    missing += 1
                    same = False
                except OSError:
                    same = False
                if not same:
                    bad += 0 if not path.exists() else 1
                    if row["origin"] == FOUND:
                        # Not Ninaivu's file: changed or moved by whoever put
                        # it there. Copied again beside it, never over it.
                        conn.execute("DELETE FROM mirror_copies WHERE root=? AND rel_path=?",
                                     (row["root"], row["rel_path"]))
                    else:
                        # Ninaivu's own: kept as owed (a size no file has), so
                        # the next run knows the one there is its own, sets it
                        # aside and writes the good one in its place.
                        conn.execute("UPDATE mirror_copies SET size=-1 WHERE root=? AND rel_path=?",
                                     (row["root"], row["rel_path"]))
                    conn.commit()
                checked += 1
                where = (row["root"], row["rel_path"])
                self._bump_copied()
            finished = True
            self._set_meta(conn, "verify_cursor", "")
            self._set_meta(conn, "last_verified", self._clock())
            self._set_meta(conn, "verify_bad", bad)
            self._set_meta(conn, "verify_missing", missing)
            self._set_meta(conn, "verify_checked", checked)
            if not checked:
                message = "Nothing is on the second copy yet to check. Copy now first."
                return
            message = f"Checked {checked:,} files on the second copy."
            message += (f" {bad:,} no longer matched and {missing:,} were missing; they are copied "
                        f"again on the next run." if bad or missing else " Every one read back intact.")
        except _Yield:
            yielded = True
            message = (f"Checked {checked:,} files so far; set aside for the copy that is due, "
                       "and carried on after it.")
        except _Stop:
            message = f"Stopped after checking {checked:,} files. It carries on from here next time."
        except OSError as exc:
            log.warning("the check of the second copy stopped: %s", exc)
            self._update(error=f"The check stopped: {exc}")
        except Exception as exc:                            # noqa: BLE001
            log.exception("the check of the second copy stopped")
            self._update(error=f"The check stopped: {exc}")
        finally:
            if conn is not None and where is not None and not finished:
                try:
                    self._save_verify_cursor(conn, where, bad, missing, checked)
                except sqlite3.Error:
                    pass
            self._update(running=False, current="", waiting="", message=message, job="")
            if message:
                log.info("second copy: %s", message)
            if yielded:
                self.look_now()

    def _save_verify_cursor(self, conn, where: tuple[str, str], bad: int, missing: int,
                            checked: int) -> None:
        self._set_meta(conn, "verify_cursor", json.dumps({
            "root": where[0], "rel": where[1], "bad": bad, "missing": missing,
            "checked": checked}))

    def _bump_copied(self) -> None:
        with self._lock:
            self._state["copied_this_run"] += 1

    def _restore(self, folder: str = "") -> None:
        """What the copy holds and the library has lost, put back."""
        restored = skipped = failed = 0
        touched: set[str] = set()
        away: set[str] = set()
        message = ""
        prefix = "/".join(p for p in str(folder or "").replace("\\", "/").split("/")
                          if p and p not in (".", ".."))
        try:
            conn = self._db()
            target = self.folder
            assert target is not None
            self._know_the_disk(conn, target)
            self._follow_moves(conn)
            # Deleted here (in the bin, or erased from it) is not lost: the
            # second copy never removes anything, and putting the library back
            # must not put those back with it (recycle.Gone).
            from .recycle import Gone                             # noqa: PLC0415
            gone = Gone(conn)
            # Not the ones a check found bad (size -1): what is there is not
            # the photograph, and they are copied again on the next run.
            sql = "SELECT root, rel_path, sha256, mtime, stored_as FROM mirror_copies WHERE size >= 0"
            args: list[Any] = []
            if prefix:
                # The folder's own name escaped, not stripped: "my_trip" with
                # its underscore taken out matched nothing at all.
                like = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                sql += " AND (rel_path = ? OR rel_path LIKE ? ESCAPE '\\')"
                args = [prefix, like + "/%"]
            for row in conn.execute(sql + " ORDER BY root, rel_path", args).fetchall():
                self._pause_for_the_household()
                root = row["root"]
                home = Path(root) / row["rel_path"]
                if root in away or home.exists():
                    continue
                if not roots_kit.root_present(root):
                    away.add(root)          # never into the folder a drive left behind
                    continue
                source = self._on_disk(target, root, row["rel_path"], row["stored_as"])
                self._update(current=row["rel_path"])
                partial: Path | None = None
                try:
                    if not source.is_file():
                        skipped += 1
                        continue
                    if gone and gone.holds(root, row["rel_path"], source.stat().st_size):
                        skipped += 1
                        continue
                    if not stays_inside(root, home):
                        raise OSError("refusing a path outside the library")
                    home.parent.mkdir(parents=True, exist_ok=True)
                    partial = home.with_name(home.name + ".ninaivu-part")
                    digest = hashlib.sha256()
                    with open(source, "rb") as src, create_new(partial) as out:
                        while chunk := src.read(CHUNK):
                            if self._stop.is_set():
                                raise _Stop()
                            out.write(chunk)
                            digest.update(chunk)
                        out.flush()
                        os.fsync(out.fileno())
                    if row["sha256"] and digest.hexdigest() != row["sha256"]:
                        partial.unlink(missing_ok=True)
                        failed += 1
                        continue
                    if row["mtime"]:
                        os.utime(partial, (row["mtime"], row["mtime"]))
                    try:
                        # Never over a file that appeared meanwhile, on a
                        # drive without hard links too (audit F1).
                        publish_no_replace(partial, home)
                    except FileExistsError:
                        partial.unlink(missing_ok=True)
                        continue
                    restored += 1
                    touched.add(root)
                    self._bump_copied()
                except (_Stop, OSError) as exc:
                    # A half-written file stays out of the library folder: it
                    # is not a photograph, and on a full disk it is the space.
                    if partial is not None:
                        try:
                            partial.unlink(missing_ok=True)
                        except OSError:
                            pass
                    if isinstance(exc, _Stop):
                        raise
                    failed += 1
                    log.warning("could not restore %s from the second copy: %s", row["rel_path"], exc)
            message = f"Put back {restored:,} files from the second copy."
            if failed:
                message += f" {failed:,} did not match their fingerprint or could not be read."
            if not restored and not failed:
                message = "Nothing on the second copy is missing from the library."
            if away:
                message += (" A library folder was not connected, so nothing was put "
                            "back into it.")
        except _Stop:
            message = f"Stopped after putting back {restored:,} files."
        except Exception as exc:                            # noqa: BLE001
            log.exception("the restore from the second copy stopped")
            self._update(error=f"The restore stopped: {exc}")
        finally:
            self._update(running=False, current="", waiting="", message=message, job="")
            if touched and self.scanner is not None:
                self.scanner.start(sorted(touched))

    def _copy_one(self, target: Path, root: str, rel: str,
                  conn: sqlite3.Connection | None = None) -> tuple[int, str, str]:
        """Copy one file. Returns its size, its SHA-256 and, when it could not
        go under its own name, where it went instead (relative to *target*).

        :attr:`_found_there` says afterwards whether nothing was written
        because the same bytes were on the disk already, put there by someone
        other than Ninaivu; such a file is recorded as ``FOUND`` and never
        touched afterwards."""
        self._found_there = False
        source = Path(root) / rel
        before = source.stat()
        family = self._family
        if family is not None and fit.too_large(before.st_size, family):
            raise _TooLarge(rel)
        stored = ""
        #: Whether what is at the destination, if anything, is Ninaivu's own
        #: earlier copy of this file, which may be set aside. Anything else
        #: there is someone else's and is left exactly as it is.
        ours = False
        if conn is not None:
            row = conn.execute("SELECT stored_as, origin FROM mirror_copies "
                               "WHERE root=? AND rel_path=?", (root, rel)).fetchone()
            if row is not None and row[1] != FOUND:
                stored, ours = row[0], True
        if not stored and family is not None:
            # A name this kind of disk refuses ("Diwali: day 2" on exFAT, a
            # Samba share or NTFS) goes under one it keeps, and the record
            # and the names list say where.
            plain = _under(self._library_folder(root), rel)
            fitted = fit.fitting_path(plain, family)
            if fitted != "/".join(p for p in plain.split("/") if p):
                stored = self._free_name(conn, target, root, fitted)
        dest = self._on_disk(target, root, rel, stored)
        if not stays_inside(target, dest):
            raise OSError("refusing a path outside the second copy")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not stored and conn is not None and (
                self._taken_by_another(conn, target, root, rel, dest)
                or self._stored_there(conn, target, root, rel, dest)):
            # On a disk that ignores case or Unicode form (exFAT, NTFS, APFS,
            # SMB), "IMG.jpg" and "img.JPG" are one file. Each run set the other
            # one aside as "(before …)" and wrote this one over it, for ever.
            # This one gets a name of its own instead, and the record says where.
            base, n = dest, 2
            dest = base.with_name(f"{base.stem} ({n}){base.suffix}")
            while dest.exists():
                n += 1
                dest = base.with_name(f"{base.stem} ({n}){base.suffix}")
            stored = dest.relative_to(target).as_posix()
            ours = False
        digest_here = ""
        if dest.is_file() and not dest.is_symlink() and dest.stat().st_size == before.st_size:
            # Most likely the same bytes, with only the file's time moved (a
            # library copied between disks without keeping times), or the
            # same photograph put there by another backup. Both read and
            # compared, nothing written: writing the whole file out first
            # and comparing afterwards cost a full write per file on a slow
            # USB disk, for a copy that was already there.
            digest_here = self._hash_unchanged(source, before)
            if sha256_file(dest) == digest_here:
                if ours:
                    os.utime(dest, (before.st_atime, before.st_mtime))
                else:
                    self._found_there = True        # someone else's: not even its time
                return before.st_size, digest_here, stored
        if not ours and conn is not None and conn.execute(
                "SELECT 1 FROM mirror_found WHERE size=? LIMIT 1", (before.st_size,)).fetchone():
            # The same photograph somewhere else on the disk already (a backup
            # folder laid out by year, or by camera): taken as the copy rather
            # than written a second time beside it.
            digest_here = digest_here or self._hash_unchanged(source, before)
            elsewhere = self._found_elsewhere(conn, target, before.st_size, digest_here)
            if elsewhere:
                self._found_there = True
                return before.st_size, digest_here, elsewhere
        if not ours and os.path.lexists(dest):
            # Someone else's file at the library's place, with other bytes:
            # left exactly where it is, and the library's goes beside it.
            stored = self._free_name(conn, target, root, dest.relative_to(target).as_posix())
            dest = target / Path(*stored.split("/"))
        if shutil.disk_usage(target).free < before.st_size + RESERVE:
            raise OSError(28, "no room left on the second copy's disk")
        partial = dest.with_name(dest.name + ".ninaivu-part")
        digest = hashlib.sha256()
        written = 0
        try:
            with open(source, "rb") as src, create_new(partial) as out:
                while chunk := src.read(CHUNK):
                    if self._stop.is_set():
                        raise _Stop()
                    out.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
                out.flush()
                os.fsync(out.fileno())
            after = source.stat()
            if written != after.st_size or after.st_mtime != before.st_mtime:
                raise OSError("the file changed while it was being copied")
            os.utime(partial, (before.st_atime, before.st_mtime))
            if ours and dest.exists():
                # Never overwritten: the version Ninaivu wrote before is set
                # aside, named for when, in case the new one is the damaged one.
                stamp = time.strftime("%Y-%m-%d %H%M%S", time.localtime(dest.stat().st_mtime))
                n = 1
                while True:
                    tail = stamp if n == 1 else f"{stamp} {n}"
                    kept = dest.with_name(f"{dest.stem} (before {tail}){dest.suffix}")
                    if not os.path.lexists(kept):
                        try:
                            move_no_replace(dest, kept)
                            break
                        except FileExistsError:
                            pass
                    n += 1
                self._noted(target, kept)
            try:
                publish_no_replace(partial, dest)
            except FileExistsError:
                # Something was put there while this was being written: never
                # over it, this one goes beside it instead.
                stored = self._free_name(conn, target, root, dest.relative_to(target).as_posix())
                renamed = target / Path(*stored.split("/"))
                os.replace(partial, partial.with_name(renamed.name + ".ninaivu-part"))
                partial = partial.with_name(renamed.name + ".ninaivu-part")
                dest = renamed
                publish_no_replace(partial, dest)
            self._noted(target, dest)
        except BaseException:
            try:
                partial.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return written, digest.hexdigest(), stored

    @staticmethod
    def _hash_unchanged(source: Path, before: os.stat_result) -> str:
        """The SHA-256 of *source*, which must not have changed since *before*."""
        digest = sha256_file(source)
        after = source.stat()
        if after.st_size != before.st_size or after.st_mtime != before.st_mtime:
            raise OSError("the file changed while it was being read")
        return digest

    def _look_over(self, conn: sqlite3.Connection, target: Path, sizes: set[int]) -> None:
        """Once for each disk, what is on it already: the files of a size some
        library file still to be copied has (:data:`mirror_found`), so the same
        photograph is found wherever it is and not written a second time."""
        copy_id = self._meta(conn, "copy_id")
        if self._meta(conn, "looked_over") == copy_id:
            return
        conn.execute("DELETE FROM mirror_found")
        if sizes:
            self._update(current="", message="Looking at what is already on the disk…")
            seen = 0
            found: list[tuple[str, int]] = []
            for here, folders, files in os.walk(target, followlinks=False):
                folders[:] = [f for f in folders if not _passed_over(f)]
                for name in files:
                    if _passed_over(name) or name in (MARKER, NAMES):
                        continue
                    seen += 1
                    if seen % LOOK_PAUSE_EVERY == 0:
                        self._pause_for_the_household()
                    path = Path(here) / name
                    try:
                        info = path.lstat()
                    except OSError:
                        continue
                    if stat.S_ISREG(info.st_mode) and info.st_size in sizes:
                        found.append((path.relative_to(target).as_posix(), info.st_size))
            conn.executemany("INSERT OR IGNORE INTO mirror_found(path, size) VALUES(?, ?)", found)
        self._set_meta(conn, "looked_over", copy_id)

    def _found_elsewhere(self, conn: sqlite3.Connection, target: Path, size: int,
                         digest: str) -> str:
        """Where on the disk a file with these bytes already is, put there by
        someone other than Ninaivu; empty when there is none."""
        for (path,) in conn.execute("SELECT path FROM mirror_found WHERE size=? ORDER BY path",
                                    (size,)).fetchall():
            if self._ninaivus_own(conn, path):
                continue
            there = target / Path(*path.split("/"))
            if not stays_inside(target, there):
                continue
            try:
                if there.is_symlink() or not there.is_file() or there.stat().st_size != size:
                    continue
                if sha256_file(there) == digest:
                    return path
            except OSError:
                continue
        return ""

    def _ninaivus_own(self, conn: sqlite3.Connection, path: str) -> bool:
        """Whether *path* on the copy is a file Ninaivu wrote for another
        library file: that one may be set aside when its library file
        changes, so it is no copy of this one."""
        if conn.execute("SELECT 1 FROM mirror_copies WHERE stored_as=? AND origin != ? LIMIT 1",
                        (path, FOUND)).fetchone():
            return True
        for root in self.roots:
            own = _inside(self._library_folder(root), path)
            if own and conn.execute(
                    "SELECT 1 FROM mirror_copies WHERE root=? AND rel_path=? AND stored_as='' "
                    "AND origin != ? LIMIT 1", (root, own, FOUND)).fetchone():
                return True
        return False

    def _free_name(self, conn: sqlite3.Connection | None, target: Path, root: str,
                   wanted: str) -> str:
        """*wanted* (relative to *target*), or ``name (2).jpg`` and so on when
        it is taken: by a file on the copy, by another file's record, or by a
        library file whose own name it is and which is still to be copied."""
        lib = self._library_folder(root)
        base = Path(wanted)
        candidate, n = wanted, 1

        def taken(path: str) -> bool:
            if (target / path).exists():
                return True
            if conn is None:
                return False
            if conn.execute("SELECT 1 FROM mirror_copies WHERE stored_as=? LIMIT 1",
                            (path,)).fetchone():
                return True
            own = _inside(lib, path)
            return bool(own) and conn.execute(
                "SELECT 1 FROM assets WHERE root=? AND rel_path=? LIMIT 1",
                (root, own)).fetchone() is not None

        while taken(candidate):
            n += 1
            candidate = base.with_name(f"{base.stem} ({n}){base.suffix}").as_posix()
        return candidate

    def _write_names(self, conn: sqlite3.Connection, target: Path) -> None:
        """The plain list of files kept under another name (:data:`NAMES`),
        written whole or not at all; none when there are none."""
        rows = conn.execute("SELECT root, rel_path, stored_as FROM mirror_copies "
                            "WHERE stored_as != '' ORDER BY stored_as").fetchall()
        path = target / NAMES
        if not rows:
            return
        lines = ["# Files this copy keeps under a name of their own, because the disk",
                 "# would not take the library's name, or another file had it, or the",
                 "# same photograph was on the disk already and was left where it was.",
                 "# Each line: where it is on this copy, a tab, where it belongs in the library.",
                 ""]
        lines += [f"{r['stored_as']}\t{_under(self._library_folder(r['root']), r['rel_path'])}"
                  for r in rows]
        text = "\n".join(lines) + "\n"
        try:
            if path.is_file() and path.read_text(encoding="utf-8") == text:
                return
            partial = path.with_name(path.name + ".ninaivu-part")
            partial.write_text(text, encoding="utf-8")
            os.replace(partial, path)
        except OSError as exc:
            log.warning("could not write the list of names on the second copy: %s", exc)

    def _listing(self, folder: Path) -> set[str] | None:
        """The names in *folder* on the copy; None when it cannot be read.
        Read once per folder in a run (see _noted for what the run adds)."""
        cache = self._listings
        if cache is not None and folder in cache:
            return cache[folder]
        try:
            names = set(os.listdir(folder))
        except OSError:
            return None
        if cache is not None:
            cache[folder] = names
        return names

    def _noted(self, target: Path, path: Path) -> None:
        """Keep the run's listings true after *path* was written on the copy."""
        cache = self._listings
        if not cache:
            return
        here = target
        for part in path.relative_to(target).parts:
            if here in cache:
                cache[here].add(part)
            here = here / part

    @staticmethod
    def _stored_there(conn: sqlite3.Connection, target: Path, root: str, rel: str,
                      dest: Path) -> bool:
        """Whether another library file was given *dest* as the name it is kept
        under (``stored_as``): "Diwali: day 2/a.jpg" kept as "Diwali day 2/a.jpg"
        on an exFAT disk, and a real "Diwali day 2/a.jpg" added later. Written
        under its own name, that file set the other one's copy aside and took
        its place, and the record of the first pointed at the wrong photograph
        until the monthly check found it. Compared ignoring case, as the disks
        that need such names do."""
        try:
            there = dest.relative_to(target).as_posix()
        except ValueError:
            return False
        return conn.execute(
            "SELECT 1 FROM mirror_copies WHERE stored_as = ? COLLATE NOCASE "
            "AND NOT (root = ? AND rel_path = ?) LIMIT 1", (there, root, rel)).fetchone() is not None

    def _taken_by_another(self, conn: sqlite3.Connection, target: Path, root: str,
                          rel: str, dest: Path) -> bool:
        """Whether *dest* exists only because the disk ignores case or Unicode
        form, and is really another library file's copy."""
        if not dest.exists():
            return False
        # Each part of the path as the disk spells it. A part spelled
        # differently there (other than by Unicode form, which some disks
        # rewrite on their own) belongs to another file.
        here = target
        for part in dest.relative_to(target).parts:
            names = self._listing(here)
            if names is None:
                return False
            if part not in names:
                wanted = unicodedata.normalize("NFC", part)
                if not any(unicodedata.normalize("NFC", n) == wanted for n in names):
                    return True
            here = here / part
        # Spelled the same up to Unicode form: another library file whose name
        # differs from this one's only in that form is the one that is there.
        forms = {unicodedata.normalize(f, rel) for f in ("NFC", "NFD")} - {rel}
        if not forms:
            return False
        marks = ",".join("?" * len(forms))
        return conn.execute(f"SELECT 1 FROM mirror_copies WHERE root=? AND rel_path IN ({marks}) "
                            "LIMIT 1", (root, *forms)).fetchone() is not None

