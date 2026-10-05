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

**A file is whole or absent.** It is written under a temporary name, hashed as
it goes, checked for size, synced, and only then renamed into place.

**The disk is known by a marker, not by its path.** A different disk mounted
at the same path has none of the files the record says were copied; believing
the record would mean backing nothing up and reporting that everything was.
Each destination is given a marker file, and a destination without the one
the record was made against is copied to from the beginning.

Everything goes, hidden things included: it is the household's own disk, and
what they hid from each other they did not mean to lose.

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
import threading
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

__all__ = ["Mirror", "folder_problem", "MARKER", "RESERVE"]

#: The file, at the top of a destination, that says which copy it is.
MARKER = ".ninaivu-second-copy.json"

#: Left free on the destination, so the copy never fills a disk to the byte.
RESERVE = 1024 * 1024 * 1024

#: How often the keeper looks at whether a run is owed, in seconds.
LOOK_EVERY = 10 * 60.0

CHUNK = 4 * 1024 * 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS mirror_copies (
    root       TEXT NOT NULL,
    rel_path   TEXT NOT NULL,
    size       INTEGER NOT NULL DEFAULT 0,
    mtime      REAL NOT NULL DEFAULT 0,
    sha256     TEXT NOT NULL DEFAULT '',
    copied_at  REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (root, rel_path)
);
CREATE TABLE IF NOT EXISTS mirror_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '');
"""


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
    if not target.is_dir():
        return "That folder is not there. Is the disk plugged in?"
    if not os.access(target, os.W_OK):
        return "Ninaivu cannot write to that folder."
    return None


class _Stop(Exception):
    """Asked to stop, or the household needs the machine."""


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
        self._keeper: threading.Thread | None = None
        self._stop = threading.Event()
        self._asleep = threading.Event()
        #: The library scanner, when there is one: a restore indexes what it
        #: put back.
        self.scanner = None
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
        return list(self.cfg.roots or ([self.cfg.active_root] if self.cfg.active_root else []))

    def problem(self) -> str | None:
        return folder_problem(str(self.folder or ""), self.roots, self.cfg.state_dir)

    # -- the record -------------------------------------------------------

    def _db(self) -> sqlite3.Connection:
        conn = self._connect()
        conn.executescript(SCHEMA)
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
        if known:
            # A different disk, or this one wiped: what the record says was
            # copied is not on it.
            log.warning("the second copy at %s is not the one the record was made "
                        "against; copying to it from the beginning", target)
        conn.execute("DELETE FROM mirror_copies")
        copy_id = found or secrets.token_hex(12)
        if not found:
            marker.write_text(json.dumps({
                "id": copy_id, "made": time.strftime("%Y-%m-%d %H:%M:%S"),
                "note": "A second copy of a Ninaivu library. The files are ordinary files "
                        "in their ordinary folders; nothing of Ninaivu's is needed to "
                        "read them. Leave this file where it is so Ninaivu knows the disk.",
            }, indent=2), encoding="utf-8")
        self._set_meta(conn, "copy_id", copy_id)

    def _library_folder(self, root: str) -> str:
        """The folder inside the destination that one library folder goes to."""
        name = Path(root).name or "library"
        same = [r for r in self.roots if (Path(r).name or "library") == name]
        if len(same) > 1:
            # Two libraries called "Photos" must not be poured together.
            name += "-" + hashlib.sha1(str(root).encode()).hexdigest()[:6]  # noqa: S324
        return name

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
        if folder is not None and folder.is_dir():
            try:
                free = int(shutil.disk_usage(folder).free)
            except OSError:
                free = None
        return {
            **state,
            "enabled": self.enabled,
            "folder": str(folder or ""),
            "every_hours": self.every / 3600,
            "there": bool(folder and folder.is_dir()),
            "problem": problem,
            "files": int(total), "bytes": int(size),
            "copied": int(copied), "copied_bytes": int(copied_size),
            "owed": int(total) - int(copied), "owed_bytes": int(size) - int(copied_size),
            "free_bytes": free,
            "last_finished": float(self._meta(conn, "last_finished", "0") or 0),
            "last_run": float(self._meta(conn, "last_run", "0") or 0),
            "last_verified": float(self._meta(conn, "last_verified", "0") or 0),
            "verify_bad": int(self._meta(conn, "verify_bad", "0") or 0),
            "verify_missing": int(self._meta(conn, "verify_missing", "0") or 0),
            "verify_every_days": self.verify_every / 86400,
        }

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
            self._asleep.set()
            if self._thread:
                self._thread.join(30)

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def keep(self) -> None:
        """Run by itself every so often, and whenever the disk comes back."""
        if self._keeper and self._keeper.is_alive():
            return
        self._asleep.clear()
        self._keeper = threading.Thread(target=self._loop, name="ninaivu-second-copy-keeper",
                                        daemon=True)
        self._keeper.start()

    def _loop(self) -> None:
        while not self._asleep.wait(LOOK_EVERY):
            try:
                if self.due():
                    self.start()
                elif self.verify_due():
                    self.verify()
            except Exception:                               # noqa: BLE001
                log.exception("the second copy could not start by itself")

    def due(self) -> bool:
        if not self.enabled or self.every <= 0 or self.running or self.problem():
            return False
        if self._hold is not None and self._hold():
            return False
        last = float(self._meta(self._db(), "last_run", "0") or 0)
        return self._clock() - last >= self.every

    def verify_due(self) -> bool:
        if not self.enabled or self.verify_every <= 0 or self.running or self.problem():
            return False
        if self._hold is not None and self._hold():
            return False
        conn = self._db()
        if not conn.execute("SELECT 1 FROM mirror_copies LIMIT 1").fetchone():
            return False
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
        copied = failed = 0
        message = ""
        try:
            conn = self._db()
            target = self.folder
            assert target is not None
            self._know_the_disk(conn, target)
            self._set_meta(conn, "last_run", self._clock())
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
                f"ORDER BY (a.kind != 'picture'), a.id", roots).fetchall()
            for row in owed:
                self._pause_for_the_household()
                root, rel = row["root"], row["rel_path"]
                if root in away:
                    continue
                if not Path(root).is_dir():
                    away.add(root)                  # a library drive not plugged in
                    continue
                if not target.is_dir():
                    raise OSError("the disk the second copy is on is no longer there")
                self._update(current=rel, message="Copying…")
                try:
                    size, digest = self._copy_one(target, root, rel)
                except FileNotFoundError:
                    continue                        # gone since it was indexed
                except OSError as exc:
                    if getattr(exc, "errno", None) in (28, 122):    # ENOSPC, EDQUOT
                        raise
                    failed += 1
                    log.warning("could not copy %s to the second copy: %s", rel, exc)
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO mirror_copies(root, rel_path, size, mtime, sha256, "
                    "copied_at) VALUES(?,?,?,?,?,?)",
                    (root, rel, size, float(row["mtime"] or 0), digest, time.time()))
                conn.commit()
                copied += 1
                with self._lock:
                    self._state["copied_this_run"] += 1
                    self._state["bytes_this_run"] += size
            self._set_meta(conn, "last_finished", self._clock())
            message = (f"Copied {copied:,} files." if copied else "Nothing new to copy.")
            if failed:
                message += f" {failed:,} could not be read and will be tried again."
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
            self._update(running=False, current="", waiting="", message=message)
            if message:
                log.info("second copy of the library: %s", message)

    def _on_disk(self, target: Path, root: str, rel: str) -> Path:
        return target / self._library_folder(root) / Path(*rel.replace("\\", "/").split("/"))

    def _verify(self) -> None:
        """Every recorded file read back and its fingerprint compared.

        One that no longer matches, or is gone, loses its record, so the next
        run copies it again — the bad one set aside beside it, never removed.
        """
        bad = missing = checked = 0
        message = ""
        try:
            conn = self._db()
            target = self.folder
            assert target is not None
            self._know_the_disk(conn, target)
            rows = conn.execute("SELECT root, rel_path, sha256 FROM mirror_copies "
                                "WHERE sha256 != '' ORDER BY root, rel_path").fetchall()
            for row in rows:
                self._pause_for_the_household()
                path = self._on_disk(target, row["root"], row["rel_path"])
                self._update(current=row["rel_path"])
                try:
                    same = _hash(path) == row["sha256"]
                except FileNotFoundError:
                    missing += 1
                    same = False
                except OSError:
                    same = False
                if not same:
                    bad += 0 if not path.exists() else 1
                    conn.execute("DELETE FROM mirror_copies WHERE root=? AND rel_path=?",
                                 (row["root"], row["rel_path"]))
                    conn.commit()
                checked += 1
                self._bump_copied()
            self._set_meta(conn, "last_verified", self._clock())
            self._set_meta(conn, "verify_bad", bad)
            self._set_meta(conn, "verify_missing", missing)
            message = f"Checked {checked:,} files on the second copy."
            message += (f" {bad:,} no longer matched and {missing:,} were missing; they are copied "
                        f"again on the next run." if bad or missing else " Every one read back intact.")
        except _Stop:
            message = f"Stopped after checking {checked:,} files."
        except Exception as exc:                            # noqa: BLE001
            log.exception("the check of the second copy stopped")
            self._update(error=f"The check stopped: {exc}")
        finally:
            self._update(running=False, current="", waiting="", message=message, job="")
            if message:
                log.info("second copy: %s", message)

    def _bump_copied(self) -> None:
        with self._lock:
            self._state["copied_this_run"] += 1

    def _restore(self, folder: str = "") -> None:
        """What the copy holds and the library has lost, put back."""
        restored = skipped = failed = 0
        touched: set[str] = set()
        message = ""
        prefix = "/".join(p for p in str(folder or "").replace("\\", "/").split("/")
                          if p and p not in (".", ".."))
        try:
            conn = self._db()
            target = self.folder
            assert target is not None
            self._know_the_disk(conn, target)
            sql = "SELECT root, rel_path, sha256, mtime FROM mirror_copies"
            args: list[Any] = []
            if prefix:
                # The folder's own name escaped, not stripped: "my_trip" with
                # its underscore taken out matched nothing at all.
                like = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                sql += " WHERE rel_path = ? OR rel_path LIKE ? ESCAPE '\\'"
                args = [prefix, like + "/%"]
            for row in conn.execute(sql + " ORDER BY root, rel_path", args).fetchall():
                self._pause_for_the_household()
                root = row["root"]
                home = Path(root) / row["rel_path"]
                if not Path(root).is_dir() or home.exists():
                    continue
                source = self._on_disk(target, root, row["rel_path"])
                self._update(current=row["rel_path"])
                try:
                    if not source.is_file():
                        skipped += 1
                        continue
                    home.parent.mkdir(parents=True, exist_ok=True)
                    partial = home.with_name(home.name + ".ninaivu-part")
                    digest = hashlib.sha256()
                    with open(source, "rb") as src, open(partial, "wb") as out:
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
                        os.link(partial, home)
                        partial.unlink()
                    except FileExistsError:
                        partial.unlink(missing_ok=True)
                        continue
                    except OSError:
                        if home.exists():
                            partial.unlink(missing_ok=True)
                            continue
                        os.replace(partial, home)
                    restored += 1
                    touched.add(root)
                    self._bump_copied()
                except _Stop:
                    raise
                except OSError as exc:
                    failed += 1
                    log.warning("could not restore %s from the second copy: %s", row["rel_path"], exc)
            message = f"Put back {restored:,} files from the second copy."
            if failed:
                message += f" {failed:,} did not match their fingerprint or could not be read."
            if not restored and not failed:
                message = "Nothing on the second copy is missing from the library."
        except _Stop:
            message = f"Stopped after putting back {restored:,} files."
        except Exception as exc:                            # noqa: BLE001
            log.exception("the restore from the second copy stopped")
            self._update(error=f"The restore stopped: {exc}")
        finally:
            self._update(running=False, current="", waiting="", message=message, job="")
            if touched and self.scanner is not None:
                self.scanner.start(sorted(touched))

    def _copy_one(self, target: Path, root: str, rel: str) -> tuple[int, str]:
        source = Path(root) / rel
        before = source.stat()
        dest = target / self._library_folder(root) / Path(*rel.replace("\\", "/").split("/"))
        if not Path(os.path.abspath(dest)).is_relative_to(os.path.abspath(target)):
            raise OSError("refusing a path outside the second copy")
        if shutil.disk_usage(target).free < before.st_size + RESERVE:
            raise OSError(28, "no room left on the second copy's disk")
        dest.parent.mkdir(parents=True, exist_ok=True)
        partial = dest.with_name(dest.name + ".ninaivu-part")
        digest = hashlib.sha256()
        written = 0
        try:
            with open(source, "rb") as src, open(partial, "wb") as out:
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
            if dest.is_file() and dest.stat().st_size == written \
                    and _hash(dest) == digest.hexdigest():
                # The same bytes are there already; only the file's time moved
                # (a library copied between disks without keeping times).
                partial.unlink()
                os.utime(dest, (before.st_atime, before.st_mtime))
                return written, digest.hexdigest()
            if dest.exists():
                # Never overwritten: the version that was there is set aside,
                # named for when, in case the new one is the damaged one.
                stamp = time.strftime("%Y-%m-%d %H%M%S", time.localtime(dest.stat().st_mtime))
                kept = dest.with_name(f"{dest.stem} (before {stamp}){dest.suffix}")
                n = 1
                while kept.exists():
                    n += 1
                    kept = dest.with_name(f"{dest.stem} (before {stamp} {n}){dest.suffix}")
                os.rename(dest, kept)
            os.replace(partial, dest)
        except BaseException:
            try:
                partial.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return written, digest.hexdigest()


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()
