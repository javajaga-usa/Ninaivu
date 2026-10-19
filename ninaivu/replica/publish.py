"""Writing the hand-over for the Pi, on the Mac.

A hand-over is a folder:

* ``index-<generation>.db``: the index, snapshotted through SQLite's backup
  API (the library stays open and in use) and slimmed of what must not leave
  this computer or what the Pi has no use for;
* ``thumbs/``, ``faces/``, ``avatars/``, ``stories/``: copied as they are,
  and only what changed since the last hand-over to the same folder;
* ``replica.json``, written last and renamed into place: the generation, the
  snapshot's size and SHA-256, the library folders and their marker ids, and
  the settings that decide what the family sees.

The index snapshot of the generation before is kept beside the new one, so a
Pi that started copying it a moment before the new ``replica.json`` landed
still finishes with a whole file. Older ones are removed.

What the copied files were is remembered in ``replica-sent.db`` beside the
index, so a hand-over of an unchanged library reads the local thumbnail
folder and nothing on the Pi's disk: asking a network share about 366,000
thumbnails one by one would take longer than copying the changes. The
destination is known by an id in ``.replica-id`` inside it; a different
folder, or the same one emptied, is copied to from the beginning.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any, Callable

from .. import __version__
from ..storage import backup, roots as roots_kit
from ..utils.files import sha256_file
from . import FOLDERS, FORMAT, MANIFEST

log = logging.getLogger(__name__)

__all__ = ["publish", "slim", "inside_what_it_copies", "family_settings", "Publisher", "SETTINGS",
           "RECORD", "SENT", "DEST_ID", "KEEP_SNAPSHOTS"]

#: The settings the Pi takes from the Mac: what the family sees and how, and
#: nothing about this machine (its ports, its disks, its backups, its models)
#: and no secret. A list of what goes rather than of what stays, so a setting
#: added later stays on the Mac until somebody decides it belongs here.
SETTINGS = (
    "roots", "active_root", "lock_roots", "house_name", "open_browsing",
    "lock_after_minutes", "thumb_sizes", "thumb_format", "thumb_quality",
    "video_thumbs", "page_size", "max_page_size", "nsfw_filter", "hide_screens",
    "strip_location", "home_lat", "home_lon", "home_radius_m", "index_hidden",
    "southern_hemisphere", "date_phrase_search", "phrase_search", "map_tiles",
    "place_names", "faces_enabled", "ocr_enabled", "min_media_bytes",
    "ignore_dirs", "follow_symlinks",
)

#: Beside the index: when the last hand-over was written, and how it went.
RECORD = "replica-publish.json"

#: Beside the index: which files were copied to which destination.
SENT = "replica-sent.db"

#: Inside the destination: the id the record above knows it by.
DEST_ID = ".replica-id"

#: Index snapshots kept in the destination: the new one and the one before.
KEEP_SNAPSHOTS = 2

#: How often the keeper looks whether a hand-over is due.
LOOK_EVERY = 300.0


class Stopped(Exception):
    """The hand-over was asked to stop part-way; what was copied stays."""


# ---------------------------------------------------------------------------
# The index
# ---------------------------------------------------------------------------

def slim(index: Path) -> None:
    """Take out of a snapshot what must not leave the Mac, and what the Pi has
    no use for.

    Kept: everything the family app shows — photographs, faces and who they
    are, albums, visibility, dates, captions, stories — and the accounts, so
    the family signs in on the Pi with the passwords and PINs they use on the
    mini. Taken out: sign-in sessions (keys to the mini), the sign-in record
    and counters (keyed by whatever was typed, a whole share link included),
    share and "who is this?" tokens (each one *is* its link), the handover
    code's hash, upload resume addresses, and the image-search vectors, which
    are half the index and need a model the Pi does not run.
    """
    with closing(sqlite3.connect(str(index))) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("sessions", "audit", "auth_limits", "shares", "embeddings",
                      "ask_answers", "ask_faces", "ask_questions"):
            if table in tables:
                conn.execute(f"DELETE FROM {table}")
        if "handover_plan" in tables:
            conn.execute("UPDATE handover_plan SET code_hash=''")
        if "cloud_uploads" in tables:
            conn.execute("UPDATE cloud_uploads SET resume_url=''")
        if "pending_uploads" in tables:
            # Their bytes are on the Mac, waiting to be looked at; not in the
            # library, so not in the mirror the Pi serves from.
            conn.execute("DELETE FROM pending_uploads WHERE status='pending'")
        conn.commit()
        conn.execute("VACUUM")


def _schema_version(index: Path) -> int:
    with closing(sqlite3.connect(str(index))) as conn:
        try:
            row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        except sqlite3.Error:
            return 0
    try:
        return int(row[0]) if row else 0
    except (TypeError, ValueError):
        return 0


def family_settings(cfg) -> dict[str, Any]:
    """The settings in :data:`SETTINGS`, as JSON holds them."""
    out: dict[str, Any] = {}
    for key in SETTINGS:
        value = getattr(cfg, key, None)
        if isinstance(value, (set, frozenset)):
            value = sorted(value)
        elif isinstance(value, tuple):
            value = list(value)
        out[key] = value
    return out


def _library_ids(cfg) -> dict[str, str]:
    """The marker id of each library folder, from ``library-ids.json``."""
    try:
        known = json.loads((Path(cfg.state_dir) / roots_kit.KNOWN).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(known, dict):
        return {}
    return {root: known[root] for root in cfg.library_roots
            if isinstance(known.get(root), str)}


# ---------------------------------------------------------------------------
# Writing files that are whole or absent
# ---------------------------------------------------------------------------

def _copy_whole(source: Path, target: Path, *, sync: bool = False) -> None:
    """Copy under a temporary name in the target's folder, then rename."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex[:8]}.part")
    try:
        with open(source, "rb") as src, open(temporary, "wb") as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
            if sync:
                dst.flush()
                os.fsync(dst.fileno())
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _write_json(target: Path, data: dict[str, Any]) -> None:
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex[:8]}.part")
    try:
        with open(temporary, "w", encoding="utf-8") as out:
            json.dump(data, out, indent=2)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# The folders beside the index
# ---------------------------------------------------------------------------

def _destination_id(dest: Path) -> str:
    marker = dest / DEST_ID
    try:
        value = marker.read_text(encoding="utf-8").strip()
        if value:
            return value
    except OSError:
        pass
    value = uuid.uuid4().hex
    marker.write_text(value, encoding="utf-8")
    return value


def _open_sent(state_dir: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(state_dir / SENT), timeout=30.0)
    conn.execute("""CREATE TABLE IF NOT EXISTS sent (
        dest TEXT NOT NULL, folder TEXT NOT NULL, path TEXT NOT NULL,
        size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
        PRIMARY KEY (dest, folder, path))""")
    return conn


def _listing(folder: Path) -> dict[str, tuple[int, int]]:
    """Every file under *folder*: relative path -> (size, mtime in ns)."""
    found: dict[str, tuple[int, int]] = {}
    if not folder.is_dir():
        return found
    stack = [folder]
    while stack:
        here = stack.pop()
        try:
            entries = list(os.scandir(here))
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith(".") and entry.name.endswith(".part"):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    st = entry.stat(follow_symlinks=False)
                    rel = Path(entry.path).relative_to(folder).as_posix()
                    found[rel] = (st.st_size, st.st_mtime_ns)
            except OSError:
                continue
    return found


def sync_folder(source: Path, target: Path, sent: sqlite3.Connection, dest_id: str,
                name: str, *, stop: threading.Event | None = None,
                pause: Callable[[], None] | None = None) -> dict[str, int]:
    """Bring *target* in line with *source*, copying only what changed since
    the record says it was last copied there. Returns the counts."""
    here = _listing(source)
    before = {row[0]: (row[1], row[2]) for row in sent.execute(
        "SELECT path, size, mtime_ns FROM sent WHERE dest=? AND folder=?", (dest_id, name))}
    copied = removed = 0
    batch = 0
    for rel, stamp in here.items():
        if before.get(rel) == stamp:
            continue
        if stop is not None and stop.is_set():
            sent.commit()
            raise Stopped()
        if pause is not None:
            pause()
        try:
            _copy_whole(source / rel, target / rel)
        except FileNotFoundError:
            continue                      # replaced or removed while we looked
        sent.execute("INSERT OR REPLACE INTO sent(dest, folder, path, size, mtime_ns) "
                     "VALUES (?,?,?,?,?)", (dest_id, name, rel, stamp[0], stamp[1]))
        copied += 1
        batch += 1
        if batch >= 500:
            sent.commit()
            batch = 0
    for rel in before.keys() - here.keys():
        try:
            (target / rel).unlink(missing_ok=True)
        except OSError:
            continue
        sent.execute("DELETE FROM sent WHERE dest=? AND folder=? AND path=?",
                     (dest_id, name, rel))
        removed += 1
    sent.commit()
    return {"copied": copied, "removed": removed, "files": len(here)}


# ---------------------------------------------------------------------------
# One hand-over
# ---------------------------------------------------------------------------

def _previous(dest: Path) -> dict[str, Any]:
    try:
        data = json.loads((dest / MANIFEST).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _prune(dest: Path, keep: set[str]) -> None:
    snapshots = sorted(dest.glob("index-*.db"), key=lambda p: p.name)
    for path in snapshots:
        if path.name not in keep:
            path.unlink(missing_ok=True)
    for leftover in dest.glob(".index-*.part"):
        # A hand-over that was cut off. Only ours ever start with this name.
        if time.time() - leftover.stat().st_mtime > 3600:
            leftover.unlink(missing_ok=True)


def inside_what_it_copies(cfg, dest: Path | str) -> bool:
    """Whether *dest* is in a library folder or the state folder (or is one)."""
    here = Path(dest).expanduser().resolve()
    for folder in (cfg.state_dir, *cfg.library_roots):
        there = Path(folder).expanduser().resolve()
        if here == there or there in here.parents:
            return True
    return False


def publish(cfg, dest: Path | str, *, stop: threading.Event | None = None,
            pause: Callable[[], None] | None = None) -> dict[str, Any]:
    """Write a hand-over of this library into *dest*. Returns its manifest."""
    dest = Path(dest).expanduser()
    state = Path(cfg.state_dir)
    if not Path(cfg.db_path).is_file():
        raise FileNotFoundError("the library index is missing")
    if not dest.is_dir():
        raise FileNotFoundError(f"{dest} is not there; is the Pi's share connected?")
    if inside_what_it_copies(cfg, dest):
        # Written into the library it would be scanned as photographs; into
        # the state folder, it would copy itself.
        raise OSError(f"{dest} is inside the library or Ninaivu's own folder; "
                      f"choose a folder outside both")
    dest_id = _destination_id(dest)
    previous = _previous(dest)
    generation = time.time_ns()
    name = f"index-{generation}.db"

    with tempfile.TemporaryDirectory(prefix="ninaivu_replica_", dir=state) as tmp:
        staged = Path(tmp) / "index.db"
        if not backup.hot_copy(Path(cfg.db_path), staged):
            raise OSError("the index could not be snapshotted")
        slim(staged)
        digest = sha256_file(staged)
        size = staged.stat().st_size
        schema = _schema_version(staged)
        _copy_whole(staged, dest / name, sync=True)

    counts: dict[str, dict[str, int]] = {}
    with closing(_open_sent(state)) as sent:
        for folder in FOLDERS:
            counts[folder] = sync_folder(state / folder, dest / folder, sent, dest_id,
                                         folder, stop=stop, pause=pause)

    manifest = {
        "format": FORMAT,
        "generation": generation,
        "made_at": time.time(),
        "version": __version__,
        "schema_version": schema,
        "index": {"file": name, "size": size, "sha256": digest},
        "libraries": _library_ids(cfg),
        "settings": family_settings(cfg),
        "folders": counts,
    }
    _write_json(dest / MANIFEST, manifest)
    keep = {name}
    if isinstance(previous.get("index"), dict) and previous["index"].get("file"):
        keep.add(str(previous["index"]["file"]))
    _prune(dest, keep)
    log.info("handed the library over to %s (%d bytes of index, %s)", dest, size,
             ", ".join(f"{k} +{v['copied']}/-{v['removed']}" for k, v in counts.items()))
    return manifest


# ---------------------------------------------------------------------------
# Keeping it up to date
# ---------------------------------------------------------------------------

class Publisher:
    """Hands the library over to the Pi by itself, when the index has changed
    and ``replica_every_minutes`` have passed since the last time."""

    def __init__(self, cfg, *, hold: Callable[[], str | None] | None = None) -> None:
        self.cfg = cfg
        self._hold = hold
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._keeper: threading.Thread | None = None
        self._lock = threading.Lock()
        self._live: dict[str, Any] = {}

    # -- settings ----------------------------------------------------------
    @property
    def folder(self) -> Path | None:
        raw = str(getattr(self.cfg, "replica_dir", "") or "").strip()
        return Path(raw).expanduser() if raw else None

    @property
    def every(self) -> float:
        return max(0.0, float(getattr(self.cfg, "replica_every_minutes", 0) or 0)) * 60

    # -- record ------------------------------------------------------------
    def _record_path(self) -> Path:
        return Path(self.cfg.state_dir) / RECORD

    def last(self) -> dict[str, Any]:
        try:
            data = json.loads(self._record_path().read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _remember(self, **fields: Any) -> None:
        record = {**self.last(), **fields}
        try:
            _write_json(self._record_path(), record)
        except OSError:
            log.warning("could not note the hand-over in %s", self._record_path())

    def _index_stamp(self) -> list[int]:
        """What changes whenever the index does, read without opening it."""
        out = []
        for suffix in ("", "-wal"):
            try:
                st = os.stat(f"{self.cfg.db_path}{suffix}")
                out += [st.st_mtime_ns, st.st_size]
            except OSError:
                out += [0, 0]
        return out

    # -- running -----------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def due(self) -> bool:
        if self.folder is None or self.every <= 0 or self.running:
            return False
        last = self.last()
        if last.get("folder") != str(self.folder):
            return True
        if time.time() - float(last.get("at") or 0) < self.every:
            return False
        return last.get("stamp") != self._index_stamp()

    def start(self) -> dict[str, Any]:
        """Start a hand-over now, on its own thread."""
        with self._lock:
            if self.folder is None:
                return {"started": False, "error": "No folder for the Pi is set (replica_dir)."}
            if self.running:
                return {"started": False, "error": "A hand-over is already running."}
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="ninaivu-replica-publish",
                                            daemon=True)
            self._thread.start()
            return {"started": True}

    def _pause(self) -> None:
        while self._hold is not None:
            reason = self._hold()
            if not reason:
                break
            self._live["waiting"] = reason
            if self._stop.wait(5):
                raise Stopped()
        self._live["waiting"] = ""

    def _run(self) -> None:
        folder = self.folder
        stamp = self._index_stamp()
        self._live = {"started_at": time.time(), "waiting": ""}
        try:
            manifest = publish(self.cfg, folder, stop=self._stop, pause=self._pause)
        except Stopped:
            self._remember(error="", stopped_at=time.time())
        except Exception as exc:                            # noqa: BLE001
            log.warning("could not hand the library over to the Pi: %s", exc)
            self._remember(error=str(exc), failed_at=time.time())
        else:
            self._remember(at=time.time(), folder=str(folder), stamp=stamp, error="",
                           generation=manifest["generation"],
                           index_bytes=manifest["index"]["size"])
        finally:
            self._live = {}

    def keep(self) -> None:
        """Look every few minutes whether a hand-over is due, and start it."""
        if self._keeper is not None and self._keeper.is_alive():
            return
        self._keeper = threading.Thread(target=self._loop, name="ninaivu-replica-keeper",
                                        daemon=True)
        self._keeper.start()

    def _loop(self) -> None:
        while not self._stop.wait(LOOK_EVERY):
            try:
                if self.due():
                    self.start()
            except Exception:                               # noqa: BLE001
                log.exception("the hand-over to the Pi could not start by itself")

    def stop(self, join: bool = False) -> None:
        self._stop.set()
        if join and self._thread is not None:
            self._thread.join(30)

    def status(self) -> dict[str, Any]:
        last = self.last()
        return {
            "folder": str(self.folder) if self.folder else "",
            "every_minutes": self.every / 60,
            "running": self.running,
            "waiting": self._live.get("waiting", ""),
            "last_at": last.get("at"),
            "generation": last.get("generation"),
            "index_bytes": last.get("index_bytes"),
            "error": last.get("error", ""),
        }
