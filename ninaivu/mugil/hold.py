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


class Holding:
    """The originals folder on this computer, and its record."""

    def __init__(self, folder: str | Path) -> None:
        self.folder = Path(folder).expanduser()
        self._lock = threading.Lock()

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

    def have(self, library: str, items: list[dict[str, Any]]) -> list[bool]:
        """For each ``{path, size, algo, digest}``, whether that very file is
        kept here, at that path."""
        out: list[bool] = []
        with self._lock, self._ledger() as conn:
            row = conn.execute("SELECT folder FROM libraries WHERE id=?", (library,)).fetchone()
            base = self.folder / row["folder"] if row else None
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
                out.append(ok)
        return out

    def kept_file(self, library: str, rel: str) -> tuple[Path, dict[str, Any]] | None:
        """The file kept at *rel* of *library*, and its record, when it is
        here whole: for the Primary sending for an original it no longer has."""
        clean = clean_path(rel)
        if clean is None or not (self.folder / LEDGER).exists():
            return None
        with self._lock, self._ledger() as conn:
            row = conn.execute("SELECT folder FROM libraries WHERE id=?", (library,)).fetchone()
            kept = conn.execute("SELECT size, algo, digest, mtime FROM kept "
                                "WHERE library=? AND rel_path=?", (library, clean)).fetchone()
        if row is None or kept is None:
            return None
        path = self.folder / row["folder"] / clean
        if not stays_inside(self.folder, path) or path.is_symlink():
            return None
        try:
            if not path.is_file() or path.stat().st_size != int(kept["size"]):
                return None
        except OSError:
            return None
        return path, dict(kept)

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
                outcome = self._put_in_place(partial, target)
                if outcome != "already":
                    os.utime(target, (mtime, mtime))
                    sync_folder(target.parent)
                with self._ledger() as conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO kept (library, rel_path, size, algo, digest, "
                        "mtime, kept_at) VALUES (?,?,?,?,?,?,?)",
                        (library, clean, size, algo, digest, float(mtime), time.time()))
        finally:
            partial.unlink(missing_ok=True)
        return {"kept": outcome, "path": clean}

    def _put_in_place(self, partial: Path, target: Path) -> str:
        """Move *partial* to *target*, setting aside a different file there."""
        if target.is_symlink():
            raise Refused("not_a_file")
        if target.exists():
            if not target.is_file():
                raise Refused("not_a_file")
            if _same_bytes(partial, target):
                return "already"
            when = time.strftime("%Y-%m-%d %H%M%S")
            aside = self.folder / SET_ASIDE / when / target.relative_to(self.folder)
            aside.parent.mkdir(parents=True, exist_ok=True)
            n = 1
            while aside.exists():
                n += 1
                aside = aside.with_name(f"{target.stem} ({n}){target.suffix}")
            os.replace(target, aside)
            os.replace(partial, target)
            return "set_aside_older"
        os.replace(partial, target)
        return "new"


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


__all__ = ["Holding", "Refused", "REASONS", "folder_problem", "clean_path", "hasher", "LEDGER", "SET_ASIDE"]
