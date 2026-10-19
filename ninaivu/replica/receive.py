"""Taking up the Mac's hand-over, on the Pi.

The Pi reads the hand-over folder (:mod:`.publish`) and serves from its own
copy of the index, in its own state folder: ``index-<generation>.db``. Each
generation is a file of its own, never written over the one before, because
an SQLite file replaced under a connection that has it open is a corrupt
file, and the write-ahead log beside it would be applied to the wrong
database. The server is pointed at the new file (``Config.index_file``) and
every web thread lets go of the old one at its next request
(:func:`ninaivu.storage.db.retire`).

A snapshot is taken up only when it checks out: the format is one this
version reads, its schema is not newer than this version's, and its size and
SHA-256 match ``replica.json``. A hand-over that does not is left where it is
and the Pi carries on with what it had, saying why in its log and its status.

What the Pi does write is its own: the copy of the index, and in it the
sign-in sessions of the family members using it, carried from one generation
to the next so that a new hand-over does not sign everybody out. The
thumbnails, face crops, profile pictures and stories are read where the Mac
put them: the state folder holds a link to each.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from ..storage import db, roots as roots_kit
from . import FOLDERS, FORMAT, MANIFEST

log = logging.getLogger(__name__)

__all__ = ["Replica", "ReplicaError", "APPLIED", "KEEP", "LOOK_EVERY"]

#: In the Pi's state folder: the generation it serves, and when it took it up.
APPLIED = "replica-applied.json"

#: Generations of the index kept in the Pi's state folder: the one served and
#: the one before, which a web thread may still be reading.
KEEP = 2

#: How often the Pi looks for a newer hand-over.
LOOK_EVERY = 60.0

#: Tables the Pi writes for itself, carried from one generation to the next.
CARRIED = ("sessions", "auth_limits")


class ReplicaError(Exception):
    """A hand-over that cannot be taken up, in words for the household."""


def _read_manifest(source: Path) -> dict[str, Any]:
    try:
        data = json.loads((source / MANIFEST).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ReplicaError(f"No hand-over from the Mac in {source} yet "
                           f"(there is no {MANIFEST}).") from None
    except (OSError, ValueError) as exc:
        raise ReplicaError(f"The hand-over in {source} could not be read: {exc}") from None
    if not isinstance(data, dict):
        raise ReplicaError(f"{MANIFEST} in {source} is not a hand-over.")
    return data


def _check(manifest: dict[str, Any]) -> None:
    if manifest.get("format") != FORMAT:
        raise ReplicaError(f"The hand-over is format {manifest.get('format')!r}; this "
                           f"Ninaivu reads format {FORMAT}. Run the same version on both.")
    try:
        schema = int(manifest.get("schema_version") or 0)
    except (TypeError, ValueError):
        schema = 0
    if schema > db.SCHEMA_VERSION:
        raise ReplicaError(f"The Mac's index is newer (schema {schema}) than this Ninaivu "
                           f"reads ({db.SCHEMA_VERSION}). Update Ninaivu on the Pi.")
    index = manifest.get("index")
    if not isinstance(index, dict) or not str(index.get("file", "")).startswith("index-") \
            or "/" in str(index.get("file")) or "\\" in str(index.get("file")):
        raise ReplicaError("The hand-over names no index snapshot.")


def _copy_checked(source: Path, target: Path, size: int, digest: str) -> None:
    """Copy *source* to *target*, whole or not at all, checking it on the way."""
    temporary = target.with_name(f".{target.name}.part")
    hasher = hashlib.sha256()
    written = 0
    try:
        with open(source, "rb") as src, open(temporary, "wb") as dst:
            while chunk := src.read(1024 * 1024):
                hasher.update(chunk)
                dst.write(chunk)
                written += len(chunk)
            dst.flush()
            os.fsync(dst.fileno())
    except FileNotFoundError:
        temporary.unlink(missing_ok=True)
        raise ReplicaError(f"{source.name} named in the hand-over is not there; the Mac "
                           f"may be writing a newer one.") from None
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    if written != size or hasher.hexdigest() != digest:
        temporary.unlink(missing_ok=True)
        raise ReplicaError(f"{source.name} does not match the hand-over (size or "
                           f"checksum); it is not taken up.")
    os.replace(temporary, target)


def _columns(conn: sqlite3.Connection, schema: str, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA {schema}.table_info({table})")]


def carry_sessions(old: Path, new: Path) -> int:
    """Copy the Pi's own sign-ins from *old* into *new*. Returns how many.

    Only for accounts *new* still has, and only sessions not yet expired: an
    account the Mac removed since signs out here too.
    """
    if not old.is_file():
        return 0
    carried = 0
    with closing(sqlite3.connect(str(new), timeout=30.0)) as conn:
        conn.execute("ATTACH DATABASE ? AS old", (str(old),))
        try:
            for table in CARRIED:
                here = _columns(conn, "main", table)
                there = set(_columns(conn, "old", table))
                shared = [c for c in here if c in there]
                if not shared:
                    continue
                names = ", ".join(shared)
                where = ""
                if table == "sessions":
                    where = (" WHERE user_id IN (SELECT id FROM main.users) "
                             f"AND expires_at > {time.time()!r}")
                cursor = conn.execute(f"INSERT OR IGNORE INTO main.{table} ({names}) "
                                      f"SELECT {names} FROM old.{table}{where}")
                carried += max(0, cursor.rowcount)
            conn.commit()
        finally:
            conn.execute("DETACH DATABASE old")
    return carried


class Replica:
    """The Pi's side: which generation it serves, and taking up newer ones."""

    def __init__(self, cfg, source: Path | str) -> None:
        self.cfg = cfg
        self.source = Path(source).expanduser()
        self.state = Path(cfg.state_dir)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._keeper: threading.Thread | None = None
        self.error = ""
        self.applied: dict[str, Any] = self._load_applied()

    # -- what is served ----------------------------------------------------
    def _load_applied(self) -> dict[str, Any]:
        try:
            data = json.loads((self.state / APPLIED).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict) or not (self.state / str(data.get("file", ""))).is_file():
            return {}
        return data

    @property
    def generation(self) -> int:
        return int(self.applied.get("generation") or 0)

    @property
    def as_of(self) -> float | None:
        """When the Mac made the copy being served."""
        made = self.applied.get("made_at")
        return float(made) if made else None

    def serving(self) -> bool:
        return bool(self.applied)

    def resume(self) -> bool:
        """Serve what was taken up last time, before looking for anything newer."""
        if not self.applied:
            return False
        self._point_at(self.state / self.applied["file"], self.applied)
        return True

    # -- taking up a hand-over ---------------------------------------------
    def take_newest(self) -> bool:
        """Take up the newest hand-over if it is newer than what is served and
        checks out. Returns whether anything changed; raises
        :class:`ReplicaError` when there is a hand-over that cannot be used."""
        with self._lock:
            manifest = _read_manifest(self.source)
            _check(manifest)
            generation = int(manifest.get("generation") or 0)
            if generation and generation == self.generation:
                return False
            if generation < self.generation:
                raise ReplicaError("The hand-over is older than the copy already served; "
                                   "kept the newer one.")
            index = manifest["index"]
            target = self.state / str(index["file"])
            if not target.is_file():
                _copy_checked(self.source / str(index["file"]), target,
                              int(index.get("size") or -1), str(index.get("sha256") or ""))
            # Brought up to this version's schema in place (only ever this
            # copy), with the Pi's own tables, before anybody reads it.
            from ..server import auth                      # noqa: PLC0415
            conn = db.init_db(target)
            auth.init_auth_schema(conn)
            db.close(target)
            previous = Path(self.cfg.db_path) if self.applied else None
            if previous is not None:
                carry_sessions(previous, target)
            self._write_library_ids(manifest)
            record = {"generation": generation, "file": target.name,
                      "made_at": manifest.get("made_at"), "taken_at": time.time(),
                      "version": manifest.get("version")}
            self._point_at(target, record, manifest)
            tmp = self.state / f".{APPLIED}.part"
            tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
            os.replace(tmp, self.state / APPLIED)
            self.applied = record
            self.error = ""
            self._prune()
            log.info("serving the Mac's copy of %s (generation %s)",
                     time.strftime("%Y-%m-%d %H:%M", time.localtime(record["made_at"] or 0)),
                     generation)
            return True

    def _write_library_ids(self, manifest: dict[str, Any]) -> None:
        ids = manifest.get("libraries")
        if isinstance(ids, dict):
            tmp = self.state / f".{roots_kit.KNOWN}.part"
            tmp.write_text(json.dumps(ids, indent=2), encoding="utf-8")
            os.replace(tmp, self.state / roots_kit.KNOWN)
            roots_kit.remember_state_dir(self.state)

    def _point_at(self, index: Path, record: dict[str, Any],
                  manifest: dict[str, Any] | None = None) -> None:
        if manifest is None:
            try:
                manifest = _read_manifest(self.source)
                if int(manifest.get("generation") or 0) != int(record.get("generation") or 0):
                    manifest = None
            except ReplicaError:
                manifest = None
        if manifest is not None:
            self._take_settings(manifest.get("settings"))
        self._link_folders()
        roots_kit.remember_state_dir(self.state)
        old = str(self.cfg.db_path)
        self.cfg.index_file = str(index)
        if old != str(index):
            db.retire(old)

    def _take_settings(self, settings: Any) -> None:
        """Apply the Mac's family settings to this process, in memory only."""
        if not isinstance(settings, dict):
            return
        from .publish import SETTINGS                       # noqa: PLC0415
        for key in SETTINGS:
            if key not in settings:
                continue
            value = settings[key]
            if key == "ignore_dirs":
                value = set(value or ())
            elif key == "thumb_sizes":
                value = tuple(value or ())
            setattr(self.cfg, key, value)

    def _link_folders(self) -> None:
        """Point the state folder's thumbs, faces, avatars, stories and films at the
        hand-over's, so they are read where the Mac put them."""
        for name in FOLDERS:
            link, target = self.state / name, self.source / name
            if link.is_symlink():
                if os.readlink(link) == str(target):
                    continue
                link.unlink()
            elif link.is_dir():
                try:
                    link.rmdir()                       # an empty folder made at start
                except OSError:
                    log.warning("%s is a folder with files in it; leaving it instead of "
                                "reading the Mac's %s", link, name)
                    continue
            try:
                target.mkdir(parents=True, exist_ok=True)
                link.symlink_to(target, target_is_directory=True)
            except OSError as exc:
                log.warning("could not link %s to %s: %s", link, target, exc)

    def _prune(self) -> None:
        keep = {self.applied.get("file")}
        older = sorted((p for p in self.state.glob("index-*.db") if p.name not in keep),
                       key=lambda p: p.name, reverse=True)
        for path in older[KEEP - 1:]:
            for suffix in ("", "-wal", "-shm"):
                Path(f"{path}{suffix}").unlink(missing_ok=True)

    # -- keeping up --------------------------------------------------------
    def look(self) -> bool:
        """Take up a newer hand-over if there is one; never raises."""
        try:
            return self.take_newest()
        except ReplicaError as exc:
            if str(exc) != self.error:
                log.warning("%s", exc)
            self.error = str(exc)
        except Exception as exc:                            # noqa: BLE001
            log.exception("could not take up the Mac's hand-over")
            self.error = f"The hand-over could not be taken up: {exc}"
        return False

    def keep(self) -> None:
        if self._keeper is not None and self._keeper.is_alive():
            return
        self._stop.clear()
        self._keeper = threading.Thread(target=self._loop, name="ninaivu-replica-receive",
                                        daemon=True)
        self._keeper.start()

    def _loop(self) -> None:
        while not self._stop.wait(LOOK_EVERY):
            self.look()

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict[str, Any]:
        return {"replica": True, "generation": self.generation, "as_of": self.as_of,
                "taken_at": self.applied.get("taken_at"),
                "mac_version": self.applied.get("version"), "error": self.error}
