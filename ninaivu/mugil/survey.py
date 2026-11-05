"""On the Secondary: the files only it has, for the Primary to bring in.

A household that ran the two computers apart for a while, each as one
computer, and then paired them, has photographs on the Secondary's disk that
the Primary never had: imported there, or copied there by hand. The
Secondary keeps every one of them (it never removes anything), but the
Primary does not know they exist, so the family never sees them. This finds
them, so the Primary can bring them in (mugil/bring_in.py).

It walks the originals folder and writes down, in the folder's record
(``only_here``), each file the Primary has not named:

* ``only``: in a library folder, at a path the Primary does not keep, with
  content it does not keep anywhere. Brought in.
* ``other_version``: a file that was at a path before the Primary sent a
  different one there, and was set aside for it (``.ninaivu-set-aside``). A
  file the Primary itself replaced is in the record of what was set aside;
  one copied here by hand, that the Primary never had, is not, and is the
  Secondary's own version. Brought in beside the Primary's.
* ``duplicate``: the Primary keeps that very content at another path.
* ``deleted_folder``: in a library's ``_deleted`` folder, the recycle bin.
* ``not_a_library``: in a folder at the top that is no library of the
  Primary's. Listed, not brought in: which library it belongs to is the
  household's to say (move it into a library folder on this disk, and look
  again).

Each file's fingerprint is the one the Primary uses: read from the record of
files copied here by hand when the file has not changed since, worked out
otherwise and added to that record, so the Primary's next run, naming the
file at its path once it is brought in, keeps it without sending it back.

Nothing on the disk is changed, moved or removed by any of this.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any

from .hold import DISK_ID, LEDGER, SET_ASIDE, clean_path, hasher

log = logging.getLogger(__name__)

STATUS_FILE = "mugil-survey.json"
KINDS = ("only", "other_version", "duplicate", "deleted_folder", "not_a_library")
#: The kinds the Primary brings in.
BROUGHT = ("only", "other_version")
_JUNK = {".DS_Store", "Thumbs.db", "desktop.ini", ".localized"}
_RECYCLE = "_deleted"
_INBOX = ".ninaivu-phone-inbox"
_CHUNK = 1024 * 1024
_BATCH = 500

SCHEMA = """
CREATE TABLE IF NOT EXISTS only_here (
    path     TEXT PRIMARY KEY,
    library  TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    kind     TEXT NOT NULL,
    size     INTEGER NOT NULL,
    mtime    REAL NOT NULL,
    algo     TEXT NOT NULL,
    digest   TEXT NOT NULL,
    same_as  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS only_here_kind ON only_here (kind, library, rel_path);
"""


def _junk(name: str) -> bool:
    return (name in _JUNK or name.startswith("._") or name.startswith(".ninaivu-")
            or "ninaivu-partial" in name)


class Survey:
    """The walk over the originals folder, run in the background."""

    def __init__(self, state_dir: Path | str, folder: str | Path) -> None:
        self.state_dir = Path(state_dir)
        self.folder = Path(folder).expanduser()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- what it found -----------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.folder / LEDGER, timeout=60)
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        return conn

    def status(self) -> dict[str, Any]:
        try:
            told = json.loads((self.state_dir / STATUS_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            told = {}
        told = told if isinstance(told, dict) else {}
        told["running"] = self.running
        return told

    def _tell(self, **changes: Any) -> None:
        state = self.status()
        state.update(changes)
        state.pop("running", None)
        path = self.state_dir / STATUS_FILE
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(state), encoding="utf-8")
        os.replace(temporary, path)

    def items(self, kinds: tuple[str, ...] = BROUGHT, library: str | None = None,
              after: str = "", limit: int = 500) -> list[dict[str, Any]]:
        """What the last walk found of *kinds*, by path, *limit* at a time."""
        if not (self.folder / LEDGER).exists():
            return []
        wanted = [k for k in kinds if k in KINDS] or list(BROUGHT)
        query = (f"SELECT path, library, rel_path, kind, size, mtime, algo, digest, same_as "
                 f"FROM only_here WHERE kind IN ({','.join('?' * len(wanted))}) AND path > ?")
        params: list[Any] = [*wanted, after]
        if library is not None:
            query += " AND library = ?"
            params.append(library)
        query += " ORDER BY path LIMIT ?"
        params.append(max(1, min(int(limit), 2000)))
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(query, params)]

    def file(self, path: str) -> tuple[Path, dict[str, Any]] | None:
        """The file the walk found at *path*, when it is still that file."""
        if not (self.folder / LEDGER).exists():
            return None
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM only_here WHERE path = ?", (str(path),)).fetchone()
        if row is None:
            return None
        from ..utils.files import stays_inside                     # noqa: PLC0415
        target = self.folder / row["path"]
        if not stays_inside(self.folder, target) or target.is_symlink():
            return None
        try:
            st = target.stat()
        except OSError:
            return None
        if st.st_size != int(row["size"]) or abs(st.st_mtime - float(row["mtime"])) > 0.001:
            return None                                  # changed since: look again
        return target, dict(row)

    # -- the walk ----------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> dict[str, Any]:
        with self._lock:
            if not self.running:
                self._stop.clear()
                self._tell(started=time.time(), finished=None, error=None, files_seen=0,
                           bytes_read=0, counts={})
                self._thread = threading.Thread(target=self._run_logged, daemon=True,
                                                name="ninaivu-mugil-survey")
                self._thread.start()
        return self.status()

    def stop(self, join: bool = False) -> None:
        self._stop.set()
        if join and self._thread is not None:
            self._thread.join(timeout=30)

    def _run_logged(self) -> None:
        try:
            counts = self.run()
            self._tell(finished=time.time(), counts=counts,
                       stopped=self._stop.is_set() or None)
        except Exception as exc:                          # noqa: BLE001 — said, not raised
            log.exception("Looking for the files only the Secondary has stopped")
            self._tell(finished=time.time(), error=str(exc))

    def run(self) -> dict[str, dict[str, int]]:
        """Walk the originals folder; what was found of each kind."""
        if not (self.folder / LEDGER).exists() or not (self.folder / DISK_ID).exists():
            raise RuntimeError("The originals folder is not here. Is the disk plugged in?")
        with self._connect() as conn:
            libraries = {row["folder"]: row["id"] for row in conn.execute(
                "SELECT id, folder FROM libraries")}
            kept_algo = conn.execute(
                "SELECT algo FROM kept GROUP BY algo ORDER BY COUNT(*) DESC LIMIT 1").fetchone()
            algo = kept_algo["algo"] if kept_algo else ("blake3" if hasher("blake3") else "blake2b")
            conn.execute("DROP TABLE IF EXISTS temp.walked")
            conn.execute("CREATE TEMP TABLE walked (path TEXT PRIMARY KEY, library TEXT, "
                         "rel_path TEXT, kind TEXT, size INTEGER, mtime REAL)")
            seen = 0
            batch: list[tuple] = []
            for row in self._walk(libraries):
                batch.append(row)
                seen += 1
                if len(batch) >= _BATCH:
                    conn.executemany("INSERT OR IGNORE INTO walked VALUES (?,?,?,?,?,?)", batch)
                    batch.clear()
                    self._tell(files_seen=seen)
                if self._stop.is_set():
                    return {}
            conn.executemany("INSERT OR IGNORE INTO walked VALUES (?,?,?,?,?,?)", batch)
            self._tell(files_seen=seen)
            # What the Primary keeps at that path, of that size, it has.
            conn.execute("DROP TABLE IF EXISTS temp.candidates")
            conn.execute(
                "CREATE TEMP TABLE candidates AS "
                "SELECT w.* FROM walked w LEFT JOIN kept k ON k.library = w.library "
                "AND k.rel_path = w.rel_path AND k.size = w.size "
                "WHERE w.kind != 'aside' AND k.rel_path IS NULL "
                "UNION ALL SELECT w.* FROM walked w WHERE w.kind = 'aside'")
            conn.execute("DELETE FROM only_here")
            conn.commit()
            found = {row["path"]: (int(row["size"]), float(row["mtime"]), row["algo"], row["digest"])
                     for row in conn.execute("SELECT path, size, mtime, algo, digest FROM found")}
            counts: dict[str, dict[str, int]] = {k: {"files": 0, "bytes": 0} for k in KINDS}
            read = 0
            out: list[tuple] = []
            remember: list[tuple] = []
            for row in self._candidates(conn):
                if self._stop.is_set():
                    return {}
                size, mtime = int(row["size"]), float(row["mtime"])
                known = found.get(row["path"])
                if known and known[0] == size and abs(known[1] - mtime) <= 0.001 and known[2] == algo:
                    digest = known[3]
                else:
                    digest = _fingerprint(self.folder / row["path"], algo)
                    if digest is None:
                        continue                         # unreadable now: next time
                    read += size
                    remember.append((row["path"], size, mtime, algo, digest, time.time()))
                kind, same_as = row["kind"], ""
                if kind == "aside":
                    mine = conn.execute(
                        "SELECT 1 FROM set_aside WHERE library = ? AND rel_path = ? AND digest = ?",
                        (row["library"], row["rel_path"], digest)).fetchone()
                    if mine:
                        continue                         # the Primary's own older version
                    kind = "other_version"
                if kind in ("only", "other_version"):
                    twin = conn.execute(
                        "SELECT l.folder, k.rel_path FROM kept k JOIN libraries l ON l.id = k.library "
                        "WHERE k.digest = ? AND k.algo = ? AND k.size = ? LIMIT 1",
                        (digest, algo, size)).fetchone()
                    if twin:
                        kind, same_as = "duplicate", f"{twin['folder']}/{twin['rel_path']}"
                out.append((row["path"], row["library"] or "", row["rel_path"], kind, size, mtime,
                            algo, digest, same_as))
                counts[kind]["files"] += 1
                counts[kind]["bytes"] += size
                if len(out) >= _BATCH:
                    self._flush(conn, out, remember)
                    self._tell(bytes_read=read, counts=counts)
            self._flush(conn, out, remember)
            conn.execute("DROP TABLE IF EXISTS temp.walked")
            conn.execute("DROP TABLE IF EXISTS temp.candidates")
            conn.commit()
        log.info("The Secondary keeps %s files the Primary has not named (%s to bring in)",
                 sum(c["files"] for c in counts.values()),
                 sum(counts[k]["files"] for k in BROUGHT))
        return counts

    @staticmethod
    def _candidates(conn: sqlite3.Connection):
        """The candidates a page at a time: a disk of millions is not held in memory."""
        last = 0
        while True:
            rows = conn.execute("SELECT rowid AS n, * FROM candidates WHERE rowid > ? "
                                "ORDER BY rowid LIMIT 2000", (last,)).fetchall()
            if not rows:
                return
            yield from rows
            last = rows[-1]["n"]

    @staticmethod
    def _flush(conn: sqlite3.Connection, out: list[tuple], remember: list[tuple]) -> None:
        conn.executemany("INSERT OR REPLACE INTO only_here (path, library, rel_path, kind, size, "
                         "mtime, algo, digest, same_as) VALUES (?,?,?,?,?,?,?,?,?)", out)
        # Written down as files copied here by hand are: the Primary naming
        # one at its path keeps it without sending it back (hold.Holding.have).
        conn.executemany("INSERT OR REPLACE INTO found (path, size, mtime, algo, digest, seen_at) "
                         "VALUES (?,?,?,?,?,?)", [r for r in remember if not r[0].startswith(SET_ASIDE)])
        conn.commit()
        out.clear()
        remember.clear()

    def _walk(self, libraries: dict[str, str]):
        """``(path, library, rel_path, kind, size, mtime)`` of every file."""
        top = self.folder
        for entry in sorted(os.listdir(top)):
            here = top / entry
            if entry == SET_ASIDE:
                yield from self._walk_aside(here, libraries)
                continue
            if _junk(entry) or entry == _INBOX or entry.startswith(LEDGER) or entry == DISK_ID:
                continue
            if here.is_symlink():
                continue
            library = libraries.get(entry)
            if here.is_file():
                yield from self._one(here, "", entry, "not_a_library")
                continue
            for path in self._files(here):
                rel = path.relative_to(here).as_posix()
                if library is None:
                    yield from self._one(path, "", f"{entry}/{rel}", "not_a_library")
                elif rel.split("/", 1)[0] == _RECYCLE:
                    yield from self._one(path, library, rel, "deleted_folder")
                else:
                    yield from self._one(path, library, rel, "only")

    def _walk_aside(self, folder: Path, libraries: dict[str, str]):
        """``.ninaivu-set-aside/<when>/<library folder>/<path>``."""
        for path in self._files(folder):
            parts = path.relative_to(folder).parts
            if len(parts) < 3 or parts[1] not in libraries:
                continue
            yield from self._one(path, libraries[parts[1]], "/".join(parts[2:]), "aside")

    def _files(self, folder: Path):
        for root, dirs, names in os.walk(folder):
            dirs[:] = sorted(d for d in dirs if not _junk(d) and not os.path.islink(os.path.join(root, d)))
            for name in sorted(names):
                if not _junk(name):
                    yield Path(root) / name

    def _one(self, path: Path, library: str, rel: str, kind: str):
        try:
            st = path.lstat()
        except OSError:
            return
        if not path.is_file() or path.is_symlink():
            return
        clean = clean_path(unicodedata.normalize("NFC", rel))
        if clean is None:
            return
        yield (path.relative_to(self.folder).as_posix(), library, clean, kind,
               int(st.st_size), float(st.st_mtime))


def _fingerprint(path: Path, algo: str) -> str | None:
    fresh = hasher(algo)
    if fresh is None:
        return None
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(_CHUNK), b""):
                fresh.update(chunk)
    except OSError:
        return None
    return fresh.hexdigest()


__all__ = ["Survey", "KINDS", "BROUGHT", "STATUS_FILE"]
