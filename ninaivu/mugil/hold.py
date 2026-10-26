"""On the Secondary: keeping the originals the Primary sends, exactly as sent.

The originals folder (``mugil_originals_dir``) is plain: one folder per
library, named after the Primary's library folder, and in it the files at the
same paths as on the Primary. Anything can read it, with nothing of
Ninaivu's needed; pull the disk out and the photographs are on it.

Beside them, at the top of the folder, ``.ninaivu-originals.db`` remembers
each file kept: its library, path, size, fingerprint and when it came. It
lives on the disk with the files, so another disk mounted at the same path
does not inherit a record of files it does not have.

A file arrives under a temporary name, is hashed as it is written, and is
kept only when its size and fingerprint are the ones the Primary named; then
it is synced, given the Primary's modified time and renamed into place. A
different file already at that path is never written over: it is moved into
``.ninaivu-set-aside/<when>/`` first, at the same path, so both are kept.
Nothing here deletes a kept file.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from ..storage.fingerprints import CHUNK
from ..utils.files import create_new, stays_inside, sync_folder

try:                                                     # pragma: no cover - which one is installed
    import blake3 as _blake3
except ImportError:                                      # pragma: no cover
    _blake3 = None

#: The record of what is kept, at the top of the originals folder.
LEDGER = ".ninaivu-originals.db"

#: Where a file that was at a path before a newer one came is kept.
SET_ASIDE = ".ninaivu-set-aside"

#: Left free on the disk, so the originals never fill it to the byte.
RESERVE = 1024 * 1024 * 1024

#: The temporary name a file is written under until it checks out.
PARTIAL = ".ninaivu-partial"

#: At the top of the originals folder: which disk it is.
DISK_ID = ".ninaivu-originals-id"

#: In the state folder: the disk each originals folder was last kept on.
KNOWN_DISKS = "mugil-originals.json"

_LIBRARY_ID = re.compile(r"^[A-Za-z0-9_-]{6,80}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS libraries (
    id     TEXT PRIMARY KEY,
    folder TEXT NOT NULL UNIQUE,
    name   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kept (
    library  TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    size     INTEGER NOT NULL,
    algo     TEXT NOT NULL,
    digest   TEXT NOT NULL,
    mtime    REAL NOT NULL,
    kept_at  REAL NOT NULL,
    PRIMARY KEY (library, rel_path)
);
-- A kept file a different one later took the place of: where it was set
-- aside, so the Primary can still send for it by its fingerprint (an
-- original it let go, whose path a new file has since).
CREATE TABLE IF NOT EXISTS set_aside (
    library  TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    size     INTEGER NOT NULL,
    algo     TEXT NOT NULL,
    digest   TEXT NOT NULL,
    mtime    REAL NOT NULL,
    aside    TEXT NOT NULL,
    set_at   REAL NOT NULL,
    PRIMARY KEY (library, rel_path, digest)
);
-- Files put on the disk by hand (a copy made with the computer's own file
-- manager), fingerprinted here before the Primary sends anything
-- (mugil/adopt.py). Their path is from the top of the originals folder.
-- One the Primary then names, at that path with that content, is kept
-- without crossing the network.
CREATE TABLE IF NOT EXISTS found (
    path     TEXT PRIMARY KEY,
    size     INTEGER NOT NULL,
    mtime    REAL NOT NULL,
    algo     TEXT NOT NULL,
    digest   TEXT NOT NULL,
    seen_at  REAL NOT NULL
);
"""


#: Why a file was not kept: the status to answer, and the words for the log.
#: Answered from here by name, never from the exception itself.
REASONS: dict[str, tuple[int, str]] = {
    "library": (400, "That is not a library Ninaivu knows."),
    "path": (400, "That path is not one a library holds."),
    "algo": (400, "This computer cannot check that kind of fingerprint."),
    "size": (400, "That is not a size."),
    "away": (503, "The originals folder cannot be reached. Is the disk plugged in?"),
    "full": (507, "The disk that keeps the originals is full."),
    "mismatch": (422, "The file that arrived is not the one that was sent."),
    "not_a_file": (409, "Something other than a file is at that path."),
    "gone": (404, "No file with that content is kept here to copy."),
}


class Refused(Exception):
    """A file that was not kept; ``why`` names the reason in :data:`REASONS`."""

    def __init__(self, why: str) -> None:
        super().__init__(why)
        self.why = why
        self.status = REASONS[why][0]


def hasher(algo: str):
    """A fresh fingerprint of *algo*, the way storage/fingerprints.py makes one."""
    if algo == "blake3" and _blake3 is not None:
        return _blake3.blake3()
    if algo == "blake2b":
        return hashlib.blake2b(digest_size=32)
    return None


def folder_problem(folder: str, roots: list[str], state_dir: Path | str) -> str | None:
    """Why *folder* cannot keep the originals, or None if it can."""
    if not str(folder or "").strip():
        return "Choose the folder the originals are kept in."
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
        if resolved == other or other in resolved.parents or resolved in other.parents:
            # Inside this computer's own library it would be scanned as
            # photographs; inside the state folder it is not a disk of its own.
            return "Keep the originals in a folder of their own, apart from Ninaivu's library and its state folder."
    if not target.is_dir():
        return "That folder is not there. Is the disk plugged in?"
    if not os.access(target, os.W_OK):
        return "Ninaivu cannot write to that folder."
    return disk_problem(resolved, state_dir)


def _bare_mount_point(folder: Path, state_dir: Path | str) -> bool:
    """Empty, not a mount point, and on the same disk as the state folder."""
    from ..storage import roots as roots_kit                     # noqa: PLC0415
    if roots_kit.holds_anything(folder) or os.path.ismount(folder):
        return False
    try:
        return os.stat(folder).st_dev == os.stat(state_dir).st_dev
    except OSError:
        return False


def disk_problem(folder: Path, state_dir: Path | str) -> str | None:
    """Why the disk at *folder* is not the one the originals were kept on, or None.

    An unplugged disk under a ``nofail`` mount leaves an empty folder at its
    place, and that folder can be written to: the system disk. Taken for the
    originals folder, the record of what is kept started afresh there, and
    the Primary sent every original again onto the system disk. So the
    folder carries an id (:data:`DISK_ID`), the state folder remembers it,
    and an empty folder where that id was is the disk being away. A new
    disk mounted there, or the folder's first use, is given an id of its own.
    """
    known_path = Path(state_dir) / KNOWN_DISKS
    try:
        known = json.loads(known_path.read_text(encoding="utf-8"))
        known = known if isinstance(known, dict) else {}
    except (OSError, ValueError):
        known = {}
    marker = folder / DISK_ID
    try:
        found = marker.read_text(encoding="utf-8").strip()
    except OSError:
        found = ""
    wanted = str(known.get(str(folder)) or "")
    if found and found == wanted:
        return None
    if wanted and not found and _bare_mount_point(folder, state_dir):
        return "The disk the originals are kept on is not plugged in: only an empty folder is at its place."
    disk = found or secrets.token_hex(12)
    try:
        if not found:
            with open(marker, "x", encoding="utf-8") as out:
                out.write(disk + "\n")
        known[str(folder)] = disk
        from ..utils.files import atomic_write                      # noqa: PLC0415
        atomic_write(known_path, json.dumps(known, indent=2))
    except FileExistsError:
        return None                       # written beside this by another request
    except OSError:
        return "Ninaivu cannot write to that folder."
    return None


def clean_path(rel: str) -> str | None:
    """*rel* as a relative path with ``/`` between its parts, or None when it
    is not one: absolute, empty, with ``..`` or a Ninaivu name in it."""
    rel = unicodedata.normalize("NFC", str(rel or "")).replace("\\", "/")
    parts = PurePosixPath(rel).parts
    if not parts or rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
        return None
    for part in parts:
        if part in ("", ".", "..") or "\0" in part or part.startswith(".ninaivu-"):
            return None
    return "/".join(parts)


_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def lock_for(what: str, folder: str | Path) -> threading.Lock:
    """One lock for *what* in *folder*, the same for every object made for
    it: each request makes its own Holding, and a lock of its own serialised
    nothing between two requests."""
    try:
        key = f"{what}:{Path(folder).expanduser().resolve()}"
    except OSError:
        key = f"{what}:{Path(folder).expanduser()}"
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


class Holding:
    """The originals folder on this computer, and its record."""

    def __init__(self, folder: str | Path) -> None:
        self.folder = Path(folder).expanduser()
        self._lock = lock_for("originals", self.folder)

    # -- the record --------------------------------------------------------

    @contextlib.contextmanager
    def _ledger(self):
        conn = sqlite3.connect(self.folder / LEDGER, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(_SCHEMA)
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _library_folder(self, conn: sqlite3.Connection, library: str, name: str) -> Path:
        row = conn.execute("SELECT folder FROM libraries WHERE id=?", (library,)).fetchone()
        if row is None:
            base = clean_path(name) or "Library"
            base = base.split("/")[-1]
            folder = base
            # Two libraries with the same folder name on the Primary each get
            # a folder of their own here.
            while conn.execute("SELECT 1 FROM libraries WHERE folder=?", (folder,)).fetchone():
                folder = f"{base} ({library[:8]})"
                if conn.execute("SELECT 1 FROM libraries WHERE folder=?", (folder,)).fetchone():
                    folder = f"{base} ({library})"
            conn.execute("INSERT INTO libraries (id, folder, name) VALUES (?,?,?)",
                         (library, folder, name))
            conn.commit()
        else:
            folder = row["folder"]
        return self.folder / folder

    def have(self, library: str, items: list[dict[str, Any]], name: str = "") -> list[bool]:
        """For each ``{path, size, algo, digest}``, whether that very file is
        kept here, at that path.

        A file copied onto the disk by hand and fingerprinted since
        (:meth:`record_found`) counts when it is at that path with that very
        content, and is kept from now on: it is given the Primary's modified
        time (``mtime``, when the item names it), since a copy made by hand
        may not have kept it, and nothing else about it changes. *name* is
        the Primary's library folder name, so the folder copied by hand is
        known as that library before anything has been sent to it."""
        out: list[bool] = []
        with self._lock, self._ledger() as conn:
            row = conn.execute("SELECT folder FROM libraries WHERE id=?", (library,)).fetchone()
            if row is None and name and _LIBRARY_ID.match(str(library or "")) \
                    and conn.execute("SELECT 1 FROM found LIMIT 1").fetchone():
                base = self._library_folder(conn, library, name)
                folder = base.name
            else:
                folder = row["folder"] if row else None
                base = self.folder / folder if row else None
            for item in items:
                rel = clean_path(item.get("path", ""))
                kept = conn.execute(
                    "SELECT size, algo, digest FROM kept WHERE library=? AND rel_path=?",
                    (library, rel)).fetchone() if rel and base else None
                ok = bool(kept and kept["digest"] == item.get("digest")
                          and kept["algo"] == item.get("algo")
                          and int(kept["size"]) == int(item.get("size") or -1))
                if ok:
                    try:
                        ok = (base / rel).stat().st_size == int(kept["size"])
                    except OSError:
                        ok = False
                elif rel and base is not None:
                    ok = self._take_found(conn, library, folder, rel, item)
                out.append(ok)
        return out

    def _take_found(self, conn: sqlite3.Connection, library: str, folder: str, rel: str,
                    item: dict[str, Any]) -> bool:
        """Keep the file found at *rel* by hand, if it is the one *item*
        describes and has not changed since it was fingerprinted."""
        path = f"{folder}/{rel}"
        found = conn.execute("SELECT size, mtime, algo, digest FROM found WHERE path=?",
                             (path,)).fetchone()
        try:
            size = int(item.get("size") or -1)
        except (TypeError, ValueError):
            return False
        if found is None or found["digest"] != item.get("digest") \
                or found["algo"] != item.get("algo") or int(found["size"]) != size:
            return False
        target = self.folder / folder / rel
        if not stays_inside(self.folder, target) or target.is_symlink():
            return False
        try:
            now = target.stat()
        except OSError:
            return False
        if now.st_size != size or abs(now.st_mtime - float(found["mtime"])) > 0.001:
            return False                               # changed since: sent instead
        mtime = now.st_mtime
        try:
            wanted = float(item.get("mtime")) if item.get("mtime") is not None else None
        except (TypeError, ValueError):
            wanted = None
        if wanted is not None and abs(wanted - now.st_mtime) > 0.001:
            try:
                os.utime(target, (wanted, wanted))
                mtime = wanted
            except OSError:
                pass
        conn.execute(
            "INSERT OR REPLACE INTO kept (library, rel_path, size, algo, digest, mtime, kept_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (library, rel, size, found["algo"], found["digest"], float(mtime), time.time()))
        conn.execute("DELETE FROM found WHERE path=?", (path,))
        return True

    # -- what was copied here by hand ----------------------------------------

    def kept_paths(self) -> set[str]:
        """Every kept file's path from the top of the originals folder."""
        if not (self.folder / LEDGER).exists():
            return set()
        with self._lock, self._ledger() as conn:
            return {f"{r[0]}/{r[1]}" for r in conn.execute(
                "SELECT l.folder, k.rel_path FROM kept k JOIN libraries l ON l.id=k.library")}

    def found_index(self) -> dict[str, tuple[int, float, str]]:
        """``path: (size, mtime, algo)`` of every file fingerprinted already."""
        if not (self.folder / LEDGER).exists():
            return {}
        with self._lock, self._ledger() as conn:
            return {r[0]: (int(r[1]), float(r[2]), r[3])
                    for r in conn.execute("SELECT path, size, mtime, algo FROM found")}

    def record_found(self, rows: list[tuple[str, int, float, str, str]]) -> None:
        """``(path, size, mtime, algo, digest)`` of files fingerprinted here."""
        if not rows:
            return
        now = time.time()
        with self._lock, self._ledger() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO found (path, size, mtime, algo, digest, seen_at) "
                "VALUES (?,?,?,?,?,?)", [(*r, now) for r in rows])

    def found_summary(self) -> dict[str, int]:
        if not (self.folder / LEDGER).exists():
            return {"files": 0, "bytes": 0}
        with self._lock, self._ledger() as conn:
            row = conn.execute("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM found").fetchone()
        return {"files": int(row[0]), "bytes": int(row[1])}

    def kept_file(self, library: str, rel: str,
                  digest: str = "") -> tuple[Path, dict[str, Any]] | None:
        """The file kept at *rel* of *library*, and its record, when it is
        here whole: for the Primary sending for an original it no longer has.

        *digest*, when given, is the fingerprint the Primary wants: when the
        file at *rel* now is another one, the one set aside from there with
        that fingerprint is the answer, and with neither there is none."""
        clean = clean_path(rel)
        if clean is None or not (self.folder / LEDGER).exists():
            return None
        with self._lock, self._ledger() as conn:
            row = conn.execute("SELECT folder FROM libraries WHERE id=?", (library,)).fetchone()
            kept = conn.execute("SELECT size, algo, digest, mtime FROM kept "
                                "WHERE library=? AND rel_path=?", (library, clean)).fetchone()
            aside = conn.execute(
                "SELECT size, algo, digest, mtime, aside FROM set_aside "
                "WHERE library=? AND rel_path=? AND digest=?", (library, clean, digest)
            ).fetchone() if digest and (kept is None or kept["digest"] != digest) else None
        if aside is not None:
            path = self.folder / aside["aside"]
            kept = aside
        elif row is None or kept is None or (digest and kept["digest"] != digest):
            return None
        else:
            path = self.folder / row["folder"] / clean
        if not stays_inside(self.folder, path) or path.is_symlink():
            return None
        try:
            if not path.is_file() or path.stat().st_size != int(kept["size"]):
                return None
        except OSError:
            return None
        found = dict(kept)
        found.pop("aside", None)
        return path, found

    def same(self, library: str, rel: str, *, size: int, algo: str, digest: str) -> bool:
        """Whether the file kept at *rel* is that very file, read again end to
        end now: what the Primary asks before it lets its own copy go."""
        found = self.kept_file(library, rel)
        if found is None:
            return False
        path, kept = found
        if (kept["algo"], kept["digest"], int(kept["size"])) != (algo, digest, int(size)):
            return False
        fresh = hasher(algo)
        if fresh is None:
            return False
        try:
            with open(path, "rb") as handle:
                got = 0
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    fresh.update(chunk)
                    got += len(chunk)
        except OSError:
            return False
        return got == int(size) and fresh.hexdigest() == digest

    def kept_elsewhere(self, items: list[dict[str, Any]]) -> list[bool]:
        """For each ``{size, algo, digest}``, whether a file with that very
        content is kept here already, at any path of any library."""
        out: list[bool] = []
        if not (self.folder / LEDGER).exists():
            return [False] * len(items)
        with self._lock, self._ledger() as conn:
            for item in items:
                out.append(conn.execute(
                    "SELECT 1 FROM kept WHERE digest=? AND algo=? AND size=? LIMIT 1",
                    (str(item.get("digest", "")), str(item.get("algo", "")),
                     int(item.get("size") or -1))).fetchone() is not None)
        return out

    def receive_from_kept(self, library: str, name: str, rel: str, *, size: int, algo: str,
                          digest: str, mtime: float) -> dict[str, Any]:
        """Keep *rel* by copying, here, a kept file with the same content.

        An exact copy of a photograph already kept (the same picture in two
        folders) need not cross the network again. The bytes are read from
        the kept file and checked as if they had arrived (:meth:`receive`),
        so a kept file that has gone bad is never copied as good."""
        with self._lock, self._ledger() as conn:
            rows = conn.execute(
                "SELECT k.rel_path, l.folder FROM kept k JOIN libraries l ON l.id=k.library "
                "WHERE k.digest=? AND k.algo=? AND k.size=?", (digest, algo, int(size))).fetchall()
        for row in rows:
            source = self.folder / row["folder"] / row["rel_path"]
            if not stays_inside(self.folder, source) or source.is_symlink():
                continue
            try:
                handle = open(source, "rb")
            except OSError:
                continue
            with handle:
                try:
                    return self.receive(library, name, rel, size=size, algo=algo, digest=digest,
                                        mtime=mtime, body=handle)
                except Refused as exc:
                    if exc.why != "mismatch":
                        raise
        raise Refused("gone")

    def kept_page(self, library: str, after: str = "", limit: int = 500) -> list[dict[str, Any]]:
        """What is kept of *library*, by path, *limit* at a time from after
        *after*: for a new Primary rebuilding the library from here."""
        if not (self.folder / LEDGER).exists():
            return []
        with self._lock, self._ledger() as conn:
            rows = conn.execute(
                "SELECT rel_path AS path, size, algo, digest, mtime FROM kept "
                "WHERE library=? AND rel_path>? ORDER BY rel_path LIMIT ?",
                (library, after, max(1, min(int(limit), 2000)))).fetchall()
        return [dict(r) for r in rows]

    def last_kept(self) -> float | None:
        """When a file was last kept here, or None."""
        if not (self.folder / LEDGER).exists():
            return None
        with self._lock, self._ledger() as conn:
            row = conn.execute("SELECT MAX(kept_at) FROM kept").fetchone()
        return float(row[0]) if row and row[0] else None

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {"folder": str(self.folder), "files": 0, "bytes": 0,
                               "libraries": [], "free_bytes": None}
        try:
            out["free_bytes"] = shutil.disk_usage(self.folder).free
        except OSError:
            return out
        if not (self.folder / LEDGER).exists():
            return out
        with self._lock, self._ledger() as conn:
            for row in conn.execute(
                    "SELECT l.id, l.folder, l.name, COUNT(k.rel_path) AS files, "
                    "COALESCE(SUM(k.size), 0) AS bytes FROM libraries l "
                    "LEFT JOIN kept k ON k.library=l.id GROUP BY l.id ORDER BY l.folder"):
                out["libraries"].append(dict(row))
                out["files"] += row["files"]
                out["bytes"] += row["bytes"]
        return out

    # -- a file arriving ---------------------------------------------------

    def receive(self, library: str, name: str, rel: str, *, size: int, algo: str,
                digest: str, mtime: float, body: BinaryIO) -> dict[str, Any]:
        """Keep the file read from *body*, if it is the one described.

        Raises :class:`Refused` with the status to answer when it is not
        kept; nothing it leaves behind is a whole-looking file."""
        if not _LIBRARY_ID.match(str(library or "")):
            raise Refused("library")
        clean = clean_path(rel)
        if clean is None:
            raise Refused("path")
        fresh = hasher(algo)
        if fresh is None:
            raise Refused("algo")
        if size < 0:
            raise Refused("size")
        try:
            free = shutil.disk_usage(self.folder).free
        except OSError:
            raise Refused("away") from None
        if free - size < RESERVE:
            raise Refused("full")
        with self._lock, self._ledger() as conn:
            base = self._library_folder(conn, library, name)
        target = base / clean
        if not stays_inside(self.folder, target):
            raise Refused("path")
        target.parent.mkdir(parents=True, exist_ok=True)
        if not stays_inside(self.folder, target.parent):
            raise Refused("path")
        partial = target.with_name(f".{target.name}{PARTIAL}-{secrets.token_hex(4)}")
        got = 0
        try:
            with create_new(partial) as handle:
                while True:
                    chunk = body.read(min(CHUNK, size - got + 1))
                    if not chunk:
                        break
                    got += len(chunk)
                    if got > size:
                        break
                    fresh.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if got != size or fresh.hexdigest() != digest:
                raise Refused("mismatch")
            with self._lock:
                outcome, aside = self._put_in_place(partial, target)
                if outcome != "already":
                    os.utime(target, (mtime, mtime))
                    sync_folder(target.parent)
                with self._ledger() as conn:
                    before = conn.execute("SELECT size, algo, digest, mtime FROM kept "
                                          "WHERE library=? AND rel_path=?",
                                          (library, clean)).fetchone()
                    if aside is not None and before is not None and before["digest"] != digest:
                        conn.execute(
                            "INSERT OR REPLACE INTO set_aside (library, rel_path, size, algo, "
                            "digest, mtime, aside, set_at) VALUES (?,?,?,?,?,?,?,?)",
                            (library, clean, before["size"], before["algo"], before["digest"],
                             before["mtime"], aside.relative_to(self.folder).as_posix(),
                             time.time()))
                    conn.execute(
                        "INSERT OR REPLACE INTO kept (library, rel_path, size, algo, digest, "
                        "mtime, kept_at) VALUES (?,?,?,?,?,?,?)",
                        (library, clean, size, algo, digest, float(mtime), time.time()))
        finally:
            partial.unlink(missing_ok=True)
        return {"kept": outcome, "path": clean}

    def _put_in_place(self, partial: Path, target: Path) -> tuple[str, Path | None]:
        """Move *partial* to *target*, setting aside a different file there:
        the outcome, and where the different file went."""
        if target.is_symlink():
            raise Refused("not_a_file")
        if target.exists():
            if not target.is_file():
                raise Refused("not_a_file")
            if _same_bytes(partial, target):
                return "already", None
            when = time.strftime("%Y-%m-%d %H%M%S")
            aside = self.folder / SET_ASIDE / when / target.relative_to(self.folder)
            aside.parent.mkdir(parents=True, exist_ok=True)
            n = 1
            while aside.exists():
                n += 1
                aside = aside.with_name(f"{target.stem} ({n}){target.suffix}")
            os.replace(target, aside)
            os.replace(partial, target)
            return "set_aside_older", aside
        os.replace(partial, target)
        return "new", None


def _same_bytes(a: Path, b: Path) -> bool:
    if a.stat().st_size != b.stat().st_size:
        return False
    with open(a, "rb") as one, open(b, "rb") as other:
        while True:
            left, right = one.read(CHUNK), other.read(CHUNK)
            if left != right:
                return False
            if not left:
                return True


__all__ = ["Holding", "Refused", "REASONS", "folder_problem", "clean_path", "hasher", "lock_for",
           "LEDGER", "SET_ASIDE"]
