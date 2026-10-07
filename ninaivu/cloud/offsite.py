"""A third copy, somewhere else, that nobody there can read.

The library is one copy; Google Drive is a second; the second disk (mirror)
is a third, but it sits in the same house. The old rule — three copies, on
two kinds of storage, one of them away from home — wants one more thing: a
copy at a *different* provider or a different address, so that one account
locked, or one house flooded, loses nothing.

This keeps it, one way, like the Drive backup:

* **Where** — any service that speaks the S3 object API (Backblaze B2,
  Wasabi, Cloudflare R2, AWS, a MinIO or Synology box at a friend's), or a
  folder: a network share, or a disk at somebody else's house that is
  mounted here.
* **Encrypted before it leaves**, with the same key encrypted Drive backups
  use (ninaivu/cloud/keyring.py; made once on the Mugil page). Not only the
  bytes: the *names* too. Each file is stored under a keyed hash of where it
  lives, so the provider learns neither what the photographs are nor what
  they are called. An encrypted manifest (``manifest.ninaivu``) says which
  object is which file; without the key it is noise.
* **Only ever added to.** A photograph deleted here stays there. A changed
  one is stored as a new object beside the old — each version's name is a
  keyed hash of its path *and its contents* — so a file damaged or
  encrypted by ransomware here cannot replace the good copy there; the
  manifest keeps every version, and a restore takes the newest unless asked
  for all. Nothing Ninaivu does removes or overwrites a file in the copy.
* **Known by a marker, not by its address.** The destination holds an
  identity file (``ninaivu-offsite-id.json``); a different disk at the same
  path, an emptied bucket or a changed address is noticed, and what the
  record says was sent is not believed there. A folder whose disk is not
  mounted is refused rather than filled on the system disk.
* **Never written over by another key or a fresh index.** The manifest
  already there is read first and merged with; one made with a different
  key is not replaced (bring that key back from its recovery file), and the
  one before each run is kept as ``manifest.prev.ninaivu``.
* **Restorable without this computer** — the recovery file, or the
  passphrase with the key settings kept beside the manifest
  (``ninaivu-encryption.json``), is enough: ``ninaivu offsite-restore``
  (ninaivu/cli/offsite_restore.py) reads the manifest and puts every file back under its own name.

Files are encrypted one at a time into Ninaivu's state folder, sent, and the
temporary copy removed; the household comes first (the workload's say), as
with every other backup.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import random
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..utils.files import same_bytes, sha256_file, stays_inside
from .tempfiles import create_new

log = logging.getLogger(__name__)

__all__ = ["Offsite", "FolderTarget", "S3Target", "object_name", "SCHEMA", "MANIFEST", "MANIFEST_PREV",
           "MARKER", "PARAMS", "Refused", "latest", "read_manifest"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS offsite_copies (
    root         TEXT NOT NULL,
    rel_path     TEXT NOT NULL,
    size         INTEGER NOT NULL DEFAULT 0,
    mtime        REAL NOT NULL DEFAULT 0,
    object       TEXT NOT NULL,
    stored_size  INTEGER NOT NULL DEFAULT 0,
    uploaded_at  REAL NOT NULL DEFAULT 0,
    digest       TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (root, rel_path)
);
CREATE TABLE IF NOT EXISTS offsite_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '');
"""

MANIFEST = "manifest.ninaivu"
#: The manifest as it was before the latest run, kept in case that one is lost.
MANIFEST_PREV = "manifest.prev.ninaivu"
#: Which destination this is (see :meth:`Offsite._know_destination`).
MARKER = "ninaivu-offsite-id.json"
#: The key's salt and scrypt settings, not the key: with the passphrase,
#: enough to make it again (keyring.public_params).
PARAMS = "ninaivu-encryption.json"
README = "README.txt"
_README_TEXT = """This folder is an encrypted off-site copy of a Ninaivu photo library.

Every file in files/ is one photograph or video, encrypted (AES-256-GCM) with the
household's Ninaivu backup key, and named by a keyed hash so nothing about it can be
read here. manifest.ninaivu, encrypted with the same key, says which is which; a file
that changed is kept in every version it was sent in, and the newest is restored.

To get the photographs back, with the recovery file Ninaivu's console offers
(Mugil, "Download the recovery file"), and Ninaivu installed on any computer:

    ninaivu offsite-restore --recovery ninaivu-recovery.json <this folder> <where to put them>

or with the passphrase, which uses ninaivu-encryption.json from this folder:

    ninaivu offsite-restore <this folder> <where to put them>

Without Ninaivu installed, Python with only the "cryptography" package is enough,
from a copy of Ninaivu's source: python ninaivu/cli/offsite_restore.py (same arguments).

Without the recovery file or the passphrase, nobody can read these files — Ninaivu included.
"""
SECRET_FILE = "offsite-secret.json"


def object_name(key: bytes, root: str, rel_path: str, sha256: str = "") -> str:
    """Where one version of a file is stored: a keyed hash of where it lives
    and of its contents' SHA-256, so the name says nothing, the same bytes
    always land in the same place, and changed bytes never replace them.

    Without *sha256*, the name copies made before versions were kept used
    (one per path); those are still read.
    """
    text = f"{root}\0{rel_path}" + (f"\0{sha256}" if sha256 else "")
    digest = hmac.new(key, text.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"files/{digest[:2]}/{digest[:40]}.ninaivu"


def _absent(exc: BaseException) -> bool:
    """Whether a read failed because the thing is not there at all."""
    return isinstance(exc, FileNotFoundError) or getattr(exc, "status", None) == 404


class Refused(ValueError):
    """The run will not go on, for a reason the administrator has to act on."""


# -- where it goes --------------------------------------------------------------

class FolderTarget:
    """A folder: a network share, or somebody else's disk mounted here."""

    def __init__(self, folder: Path | str):
        self.base = Path(folder).expanduser() / "ninaivu-offsite"

    def describe(self) -> str:
        return str(self.base)

    def ready(self) -> str | None:
        parent = self.base.parent
        if not parent.is_dir():
            return f"{parent} is not there — is the disk or share connected?"
        return None

    def _path(self, name: str) -> Path:
        path = self.base.joinpath(*name.split("/"))
        if not stays_inside(self.base, path):
            raise OSError("refusing a name outside the off-site folder")
        return path

    def gone(self) -> str | None:
        """Why the off-site folder cannot be written now, or None."""
        if not self.base.is_dir():
            return (f"{self.base} is not there any more — has the disk or share "
                    "been disconnected?")
        return None

    def _folder_for(self, target: Path, name: str) -> None:
        """Make the folder *target* goes in — never the off-site folder itself,
        except for the marker that starts a new one.

        Every write used to create the whole path. A disk that dropped out in
        the middle of a run leaves an empty mount point behind, and the copy
        went on into a new ninaivu-offsite folder on the system disk until it
        was full (a Raspberry Pi's SD card), and the next run, finding no
        marker there, sent everything again.
        """
        if name == MARKER and self.ready() is None:
            self.base.mkdir(exist_ok=True)
        if not self.base.is_dir():
            raise OSError(f"{self.base} is not there any more — has the disk or "
                          "share been disconnected?")
        target.parent.mkdir(parents=True, exist_ok=True)

    def put_file(self, name: str, source: Path, on_bytes=None, stop=None) -> None:
        target = self._path(name)
        self._folder_for(target, name)
        partial = target.with_name(target.name + ".part")
        # Made new, never opened through a link left at the predictable name.
        stopped = False
        with open(source, "rb") as reading, create_new(partial) as handle:
            while chunk := reading.read(4 * 1024 * 1024):
                if stop is not None and stop():
                    # Stop means now, not at the end of a large video.
                    stopped = True
                    break
                handle.write(chunk)
            if not stopped:
                handle.flush()
                os.fsync(handle.fileno())
        if stopped:
            partial.unlink(missing_ok=True)
            raise OSError("stopped")
        os.replace(partial, target)
        if on_bytes:
            on_bytes(target.stat().st_size)

    def put_bytes(self, name: str, data: bytes) -> None:
        target = self._path(name)
        self._folder_for(target, name)
        partial = target.with_name(target.name + ".part")
        with create_new(partial) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(partial, target)

    def exists(self, name: str) -> int | None:
        path = self._path(name)
        return path.stat().st_size if path.is_file() else None

    def get_file(self, name: str, dest: Path) -> None:
        with open(self._path(name), "rb") as reading, create_new(dest) as handle:
            shutil.copyfileobj(reading, handle, 4 * 1024 * 1024)

    def get_bytes(self, name: str) -> bytes:
        return self._path(name).read_bytes()


class S3Target:
    """A bucket at any S3-compatible service, under a prefix."""

    def __init__(self, client, prefix: str = "ninaivu"):
        self.client = client
        self.prefix = prefix.strip("/")

    def describe(self) -> str:
        return f"{self.client.endpoint.rstrip('/')}/{self.client.bucket}/{self.prefix}"

    def ready(self) -> str | None:
        if not (self.client.endpoint and self.client.bucket and self.client.access_key
                and self.client.secret_key):
            return "Fill in the service's address, the bucket and the key."
        return None

    def _key(self, name: str) -> str:
        return f"{self.prefix}/{name}" if self.prefix else name

    def put_file(self, name: str, source: Path, on_bytes=None, stop=None) -> None:
        self.client.put_file(self._key(name), source, on_bytes, stop)

    def put_bytes(self, name: str, data: bytes) -> None:
        self.client.put(self._key(name), data)

    def exists(self, name: str) -> int | None:
        return self.client.exists(self._key(name))

    def get_file(self, name: str, dest: Path) -> None:
        self.client.get_file(self._key(name), dest)

    def get_bytes(self, name: str) -> bytes:
        return self.client.get(self._key(name))


class _Stop(Exception):
    pass


class Offsite:
    """Keeps the off-site copy up to date, once a day while it is turned on."""

    def __init__(self, cfg, connect_db: Callable[[], sqlite3.Connection], *,
                 hold: Callable[[], Any] | None = None, clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self._connect = connect_db
        self._hold = hold
        self._clock = clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        #: The schedule's own stop. Apart from the run's: stopping one run
        #: from the console used to end the schedule too, until a restart.
        self._asleep = threading.Event()
        self._thread: threading.Thread | None = None
        self._timer: threading.Thread | None = None
        self._state: dict[str, Any] = {"running": False, "job": "", "current": "", "sent": 0,
                                       "sent_bytes": 0, "failed": 0, "message": "", "error": "",
                                       "waiting": "", "problems": []}
        #: The manifest found at the destination when this run began, merged
        #: into every one written. None until it has been read: nothing is
        #: written over a manifest that was not read first.
        self._remote: list[dict[str, Any]] | None = None

    # -- settings ----------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "offsite_enabled", False))

    @property
    def roots(self) -> list[str]:
        return self.cfg.library_roots

    def secret(self) -> str:
        try:
            data = json.loads((Path(self.cfg.state_dir) / SECRET_FILE).read_text(encoding="utf-8"))
            return str(data.get("secret_key") or "")
        except (OSError, ValueError):
            return ""

    def save_secret(self, secret: str) -> None:
        target = Path(self.cfg.state_dir) / SECRET_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"secret_key": secret}, handle)
        os.replace(temporary, target)

    def target(self):
        kind = str(getattr(self.cfg, "offsite_kind", "folder") or "folder")
        if kind == "s3":
            from .s3 import S3                              # noqa: PLC0415
            client = S3(endpoint=str(getattr(self.cfg, "offsite_endpoint", "") or ""),
                        bucket=str(getattr(self.cfg, "offsite_bucket", "") or ""),
                        access_key=str(getattr(self.cfg, "offsite_access_key", "") or ""),
                        secret_key=self.secret(),
                        region=str(getattr(self.cfg, "offsite_region", "") or "us-east-1"))
            return S3Target(client, str(getattr(self.cfg, "offsite_prefix", "ninaivu") or "ninaivu"))
        folder = str(getattr(self.cfg, "offsite_folder", "") or "")
        return FolderTarget(folder) if folder else None

    def _where(self) -> str:
        """The destination's settings, as one value: a change in any of them
        is a different destination, and what was sent is not there."""
        kind = str(getattr(self.cfg, "offsite_kind", "folder") or "folder")
        if kind == "s3":
            return json.dumps(["s3", str(getattr(self.cfg, "offsite_endpoint", "") or "")
                               .strip().rstrip("/").lower(),
                               str(getattr(self.cfg, "offsite_bucket", "") or "").strip(),
                               str(getattr(self.cfg, "offsite_prefix", "ninaivu") or "ninaivu")
                               .strip("/")])
        folder = str(getattr(self.cfg, "offsite_folder", "") or "")
        return json.dumps(["folder", os.path.normcase(os.path.abspath(os.path.expanduser(folder)))
                           if folder else ""])

    def _sent_here(self, conn) -> int:
        """How many files the record says are at the destination now chosen."""
        known = self._meta(conn, "dest_where")
        if known and known != self._where():
            return 0
        return int(conn.execute("SELECT COUNT(*) FROM offsite_copies").fetchone()[0])

    def problem(self) -> str | None:
        from . import keyring                               # noqa: PLC0415
        if keyring.load(self.cfg.state_dir) is None:
            return ("Make the backup key first (Mugil: encrypt backups). The off-site copy "
                    "is always encrypted, with that key.")
        target = self.target()
        if target is None:
            return "Choose where the off-site copy goes."
        if isinstance(target, FolderTarget):
            chosen = Path(target.base).parent.resolve()
            for root in self.roots:
                if chosen.is_relative_to(Path(root).resolve()):
                    return "The off-site folder cannot be inside the library."
            if chosen.is_relative_to(Path(self.cfg.state_dir).resolve()):
                return "The off-site folder cannot be inside Ninaivu's own folder."
        problem = target.ready()
        if problem is None and isinstance(target, FolderTarget) and not target.base.is_dir():
            # A disk that is not mounted usually leaves its mount point behind,
            # an empty folder on the system disk. Starting a copy there would
            # fill the system disk (often a Pi's SD card) and call it the
            # off-site copy.
            sent = self._sent_here(self._db())
            if sent:
                return (f"{target.base.parent} has no off-site copy in it, though {sent:,} "
                        f"file{'' if sent == 1 else 's'} were sent there. Is the disk "
                        "connected and mounted? If that copy is gone on purpose, "
                        "start the off-site copy over.")
        return problem

    def _key(self) -> tuple[bytes, bytes]:
        from . import keyring                               # noqa: PLC0415
        record = keyring.load(self.cfg.state_dir)
        if record is None:
            raise ValueError("there is no backup key")
        return keyring.key_material(record)

    def _db(self) -> sqlite3.Connection:
        conn = self._connect()
        conn.executescript(SCHEMA)
        # Copies made before the manifest carried each file's SHA-256.
        columns = {r[1] for r in conn.execute("PRAGMA table_info(offsite_copies)")}
        if "digest" not in columns:
            conn.execute("ALTER TABLE offsite_copies ADD COLUMN digest TEXT NOT NULL DEFAULT ''")
            conn.commit()
        return conn

    @staticmethod
    def _meta(conn, key: str, default: str = "") -> str:
        row = conn.execute("SELECT value FROM offsite_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    @staticmethod
    def _set_meta(conn, key: str, value: Any) -> None:
        conn.execute("INSERT INTO offsite_meta(key, value) VALUES(?, ?) ON CONFLICT(key) "
                     "DO UPDATE SET value=excluded.value", (key, str(value)))
        conn.commit()

    # -- asking ---------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        with self._lock:
            snap = dict(self._state, problems=list(self._state["problems"]))
        conn = self._db()
        marks = ",".join("?" * len(self.roots)) or "''"
        total = conn.execute(f"SELECT COUNT(*), COALESCE(SUM(size), 0) FROM assets WHERE trashed=0 "
                             f"AND root IN ({marks})", self.roots).fetchone()
        kept = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(o.size), 0) FROM offsite_copies o JOIN assets a "
            "ON a.root = o.root AND a.rel_path = o.rel_path AND a.size = o.size "
            f"AND ABS(a.mtime - o.mtime) < 0.001 WHERE a.trashed=0 AND a.root IN ({marks})",
            self.roots).fetchone()
        known = self._meta(conn, "dest_where")
        if known and known != self._where():
            kept = (0, 0)                   # sent somewhere else; nothing is there yet
        target = self.target()
        snap.update(
            enabled=self.enabled, problem=self.problem(),
            where=target.describe() if target else "",
            kind=str(getattr(self.cfg, "offsite_kind", "folder") or "folder"),
            files=int(total[0]), bytes=int(total[1]), kept=int(kept[0]), kept_bytes=int(kept[1]),
            last_run=float(self._meta(conn, "last_run", "0") or 0),
            secret_saved=bool(self.secret()),
            form={k: getattr(self.cfg, f"offsite_{k}", "") for k in
                  ("kind", "folder", "endpoint", "region", "bucket", "prefix", "access_key",
                   "every_hours")},
        )
        return snap

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self, start_over: bool = False) -> dict[str, Any]:
        """Bring the copy up to date. *start_over* forgets what was sent to the
        destination now chosen and sends everything — for a copy removed on
        purpose, which is otherwise refused as a disk that is not mounted."""
        if start_over:
            with self._lock:
                if self._thread and self._thread.is_alive():
                    raise ValueError("The off-site copy is already busy.")
            self._forget(self._db())
            self._set_meta(self._db(), "dest_id", "")
        return self._begin(self._run, "backup", "Starting…")

    def restore(self, folder: str) -> dict[str, Any]:
        destination = Path(folder).expanduser()
        if not str(folder).strip() or not destination.parent.exists():
            raise ValueError("Choose a folder to put the files in.")
        return self._begin(lambda: self._restore(destination), "restore", "Reading the manifest…")

    def _begin(self, work, job: str, message: str) -> dict[str, Any]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise ValueError("The off-site copy is already busy.")
            problem = self.problem()
            if problem:
                raise ValueError(problem)
            self._stop.clear()
            self._state.update(running=True, job=job, current="", sent=0, sent_bytes=0, failed=0,
                               message=message, error="", waiting="", problems=[])
            self._thread = threading.Thread(target=work, name=f"ninaivu-offsite-{job}", daemon=True)
            self._thread.start()
        return self.status()

    def stop(self, join: bool = False) -> None:
        """Stop the run in progress; with *join*, at shutdown, the schedule too."""
        self._stop.set()
        if join:
            self._asleep.set()
            for thread in (self._thread, self._timer):
                if thread is not None and thread is not threading.current_thread():
                    thread.join(15)

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def keep(self) -> None:
        if self._timer is not None and self._timer.is_alive():
            return
        self._asleep.clear()

        def loop() -> None:
            while not self._asleep.wait(600):
                try:
                    if self.due() and not self.running:
                        self.start()
                except ValueError:
                    pass
                except Exception:                           # noqa: BLE001
                    log.exception("the off-site copy's schedule")

        self._timer = threading.Thread(target=loop, name="ninaivu-offsite-schedule", daemon=True)
        self._timer.start()

    def due(self) -> bool:
        if not self.enabled or self.problem():
            return False
        every = max(1.0, float(getattr(self.cfg, "offsite_every_hours", 24) or 24)) * 3600
        last = float(self._meta(self._db(), "last_run", "0") or 0)
        return self._clock() - last >= every

    def test(self) -> dict[str, Any]:
        """Write, read back and check a small file at the destination."""
        problem = self.problem()
        if problem:
            raise ValueError(problem)
        target = self.target()
        probe = f"probe-{int(self._clock())}.txt".encode()
        target.put_bytes("ninaivu-probe.txt", probe)
        if target.get_bytes("ninaivu-probe.txt") != probe:
            raise OSError("what was read back was not what was written")
        return {"ok": True, "where": target.describe()}

    # -- the work -------------------------------------------------------------

    def _update(self, **fields: Any) -> None:
        with self._lock:
            self._state.update(fields)

    def _pause(self) -> None:
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

    def _problem(self, rel: str, why: str) -> None:
        with self._lock:
            self._state["failed"] += 1
            if len(self._state["problems"]) < 50:
                self._state["problems"].append({"file": rel, "why": why})

    def _run(self) -> None:
        from . import crypto                                # noqa: PLC0415
        message = ""
        sent = 0
        self._remote = None
        try:
            key, key_id = self._key()
            target = self.target()
            conn = self._db()
            staging = Path(self.cfg.state_dir) / "offsite-staging"
            staging.mkdir(parents=True, exist_ok=True)
            self._update(message="Checking the destination…")
            self._know_destination(conn, target, key, key_id, staging)
            marks = ",".join("?" * len(self.roots)) or "''"
            owed = conn.execute(
                "SELECT a.root, a.rel_path, a.size, a.mtime, o.digest AS was, o.object AS had "
                "FROM assets a LEFT JOIN offsite_copies o "
                "ON o.root = a.root AND o.rel_path = a.rel_path "
                f"WHERE a.trashed = 0 AND a.root IN ({marks}) AND (o.root IS NULL OR o.size != a.size "
                "OR ABS(o.mtime - a.mtime) > 0.001) ORDER BY a.id", self.roots).fetchall()
            for row in owed:
                self._pause()
                rel = row["rel_path"]
                source = Path(row["root"]).joinpath(*rel.replace("\\", "/").split("/"))
                self._update(current=rel)
                if not source.is_file():
                    continue
                try:
                    before = source.stat()
                    # The original's SHA-256 names this version and goes in
                    # the (encrypted) manifest, so a restore can tell a file
                    # already on disk is intact without fetching its copy.
                    digest = sha256_file(source)
                    if row["had"] and row["was"] == digest:
                        # Only its time changed: the copy there is these bytes.
                        name, stored, sending = row["had"], 0, False
                    else:
                        name = object_name(key, row["root"], rel, digest)
                        stored = before.st_size + crypto.V2_OVERHEAD
                        # Already there — from another disk's run, or one cut
                        # short before it was recorded — is not sent again.
                        sending = target.exists(name) != stored
                    if sending:
                        with tempfile.NamedTemporaryFile(dir=staging, suffix=".ninaivu",
                                                         delete=False) as tmp:
                            encrypted = Path(tmp.name)
                        try:
                            stored = crypto.encrypt_file_v2(source, encrypted, key, key_id)
                            after = source.stat()
                            if after.st_mtime != before.st_mtime or after.st_size != before.st_size:
                                raise OSError("the file changed while it was being read")
                            target.put_file(name, encrypted, stop=self._stop.is_set)
                        finally:
                            encrypted.unlink(missing_ok=True)
                        # Asked back, not taken on trust: a service can answer
                        # an upload with success and still not have the object.
                        there = target.exists(name)
                        if there != stored:
                            raise OSError(f"the destination holds {there or 0:,} bytes of it, "
                                          f"not the {stored:,} sent")
                except (OSError, ValueError) as exc:
                    if self._stop.is_set():
                        break
                    gone = getattr(target, "gone", None)
                    reason = gone() if gone else None
                    if reason:
                        # The disk went away mid-run: one problem, and the run
                        # ends, rather than one failure for every file left.
                        raise Refused(reason) from exc
                    self._problem(rel, str(exc))
                    continue
                conn.execute(
                    "INSERT INTO offsite_copies(root, rel_path, size, mtime, object, stored_size, "
                    "uploaded_at, digest) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(root, rel_path) DO UPDATE SET size=excluded.size, "
                    "mtime=excluded.mtime, object=excluded.object, "
                    "stored_size=CASE WHEN excluded.stored_size > 0 THEN excluded.stored_size "
                    "ELSE offsite_copies.stored_size END, "
                    "uploaded_at=CASE WHEN excluded.uploaded_at > 0 THEN excluded.uploaded_at "
                    "ELSE offsite_copies.uploaded_at END, digest=excluded.digest",
                    (row["root"], rel, before.st_size, before.st_mtime, name, stored,
                     self._clock() if stored else 0, digest))
                conn.commit()
                if not stored:
                    continue
                sent += 1
                with self._lock:
                    self._state["sent"] = sent
                    self._state["sent_bytes"] += before.st_size
                if sent % 200 == 0:
                    self._manifest(conn, target, key, key_id)
            self._manifest(conn, target, key, key_id)
            self._set_meta(conn, "last_run", self._clock())
            snap = self.status()
            message = f"Sent {sent:,} file{'' if sent == 1 else 's'}."
            if snap["failed"]:
                message += f" {snap['failed']:,} could not be sent."
        except _Stop:
            message = f"Stopped after sending {sent:,} files. Starting again carries on."
            try:
                key, key_id = self._key()
                self._manifest(self._db(), self.target(), key, key_id)
            except Exception:                               # noqa: BLE001
                pass
        except Refused as exc:
            log.warning("the off-site copy did not run: %s", exc)
            self._update(error=str(exc))
            try:
                # Asked again on the usual schedule, not every ten minutes.
                self._set_meta(self._db(), "last_run", self._clock())
            except sqlite3.Error:
                pass
        except Exception as exc:                            # noqa: BLE001
            log.exception("the off-site copy stopped")
            self._update(error=f"It stopped: {exc}")
        finally:
            self._update(running=False, current="", waiting="", message=message, job="")

    # -- the destination -------------------------------------------------------

    def _forget(self, conn) -> None:
        """Forget what was sent: it is not where the copy now goes."""
        conn.execute("DELETE FROM offsite_copies")
        conn.commit()

    def _marker(self, target) -> str:
        """The destination's identity, or "" when it has none."""
        try:
            found = json.loads(target.get_bytes(MARKER).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return ""
        except OSError as exc:
            if _absent(exc):
                return ""
            raise
        return str(found.get("id") or "") if isinstance(found, dict) else ""

    def _know_destination(self, conn, target, key: bytes, key_id: bytes, staging: Path) -> None:
        """Make sure the record is about the destination that is actually there,
        and read the manifest already there, before anything is sent.

        What is owed is worked out from the record of what was sent. A new
        folder, bucket or prefix, a different disk mounted at the same path,
        a bucket a lifecycle rule emptied: each has none of it, and believing
        the record reported "kept N of N" over an empty destination. So the
        record is forgotten when the settings or the key changed, and when the
        destination's marker is not the one the record was made against. A
        manifest found there (the other of two rotated disks, or the copy a
        fresh index or a rebuilt machine finds) is taken back into the record,
        so only what changed is sent; a few recorded objects are looked for.
        """
        where, key_hex = self._where(), key_id.hex()
        known_where = self._meta(conn, "dest_where")
        known_key = self._meta(conn, "key_id")
        known_id = self._meta(conn, "dest_id")
        if (known_where and known_where != where) or (known_key and known_key != key_hex):
            log.warning("the off-site copy's destination or key changed; sending it all "
                        "to %s", target.describe())
            self._forget(conn)
            known_id = ""
        marker = self._marker(target)
        self._read_remote(target, key, key_id, staging)
        if known_id and marker != known_id:
            log.warning("the off-site copy at %s is not the one the record was made "
                        "against; what is not there will be sent", target.describe())
            self._forget(conn)
        elif not known_id and not self._remote:
            # Never marked (made before there were markers, or new): with no
            # manifest there either, nothing there says the record is true.
            self._forget(conn)
        if self._remote and not self._sent_here(conn):
            self._adopt(conn, self._remote)
        self._spot_check(conn, target)
        if not marker:
            marker = secrets.token_hex(12)
            target.put_bytes(MARKER, json.dumps({
                "id": marker, "made": time.strftime("%Y-%m-%d %H:%M:%S"),
                "note": "Ninaivu's off-site copy. Leave this file where it is: it is how "
                        "Ninaivu knows this is the copy it sent to."}, indent=2).encode())
        self._set_meta(conn, "dest_where", where)
        self._set_meta(conn, "key_id", key_hex)
        self._set_meta(conn, "dest_id", marker)

    def _read_remote(self, target, key: bytes, key_id: bytes, staging: Path) -> None:
        """Read the manifest at the destination into ``self._remote`` ([] when
        there is none), and keep it there as ``manifest.prev.ninaivu``.

        Refused — and nothing written — when it is there and cannot be read
        with this key. Replacing it would leave every object it names
        impossible to map back to a file: the copy unrestorable.
        """
        problem: Exception | None = None
        for name in (MANIFEST, MANIFEST_PREV):
            sealed = staging / f"remote-{name}"
            try:
                target.get_file(name, sealed)
            except OSError as exc:
                sealed.unlink(missing_ok=True)
                if not _absent(exc):
                    raise
                if name == MANIFEST:
                    self._remote = []
                    return
                continue
            try:
                found = _key_id_of(sealed)
                if found is not None and found != key_id:
                    raise Refused(
                        f"The off-site copy at {target.describe()} was made with another "
                        f"backup key ({found.hex()}), not this computer's ({key_id.hex()}). "
                        "Ninaivu will not write over its list of files: that would leave the "
                        "copy there impossible to restore. Bring that key back (Cloud, "
                        "Encryption: import its recovery file), or choose another place "
                        "for the off-site copy.")
                try:
                    entries = _open_manifest(sealed, key, staging)
                except (OSError, ValueError) as exc:
                    problem = problem or exc
                    continue
                if name == MANIFEST:
                    target.put_file(MANIFEST_PREV, sealed)
                self._remote = entries
                return
            finally:
                sealed.unlink(missing_ok=True)
        raise Refused(
            f"The list of files in the off-site copy at {target.describe()} "
            f"(manifest.ninaivu) cannot be read with this computer's backup key: {problem}. "
            "Ninaivu will not write over it. Bring back the key it was made with "
            "(import its recovery file), or choose another place for the off-site copy.")

    def _adopt(self, conn, entries: list[dict[str, Any]]) -> None:
        """Take a manifest found at the destination as the record of what is there."""
        for entry in latest(entries):
            try:
                conn.execute(
                    "INSERT INTO offsite_copies(root, rel_path, size, mtime, object, stored_size, "
                    "uploaded_at, digest) VALUES(?, ?, ?, ?, ?, 0, ?, ?) "
                    "ON CONFLICT(root, rel_path) DO NOTHING",
                    (str(entry["root"]), str(entry["path"]), int(entry["size"]),
                     float(entry.get("mtime") or 0), str(entry["o"]),
                     float(entry.get("at") or 0), str(entry.get("sha256") or "")))
            except (KeyError, TypeError, ValueError):
                continue
        conn.commit()

    #: How many recorded objects are looked for at the start of each run.
    SPOT_CHECK = 8

    def _spot_check(self, conn, target) -> None:
        """Look for a few recorded objects; one missing is sent again."""
        rows = conn.execute("SELECT root, rel_path, object FROM offsite_copies").fetchall() \
            if self.SPOT_CHECK else []
        sample = random.sample(rows, min(self.SPOT_CHECK, len(rows)))
        missing = [r for r in sample if target.exists(r["object"]) is None]
        if not missing:
            return
        log.warning("%d of %d files looked for in the off-site copy are not there; "
                    "sending again", len(missing), len(sample))
        if len(missing) == len(sample) and len(sample) >= 3:
            self._forget(conn)
            return
        conn.executemany("DELETE FROM offsite_copies WHERE root=? AND rel_path=?",
                         [(r["root"], r["rel_path"]) for r in missing])
        conn.commit()

    def _manifest(self, conn, target, key: bytes, key_id: bytes) -> None:
        """Which object is which file — encrypted, and written last so it never
        names an object that is not there.

        Everything the manifest already there listed is kept: older versions
        of changed files, files this index no longer has (a fresh index, a
        library folder taken out of the settings). Only ever added to.
        """
        from . import crypto, keyring                       # noqa: PLC0415
        if self._remote is None:
            return                          # never written over unread
        merged: dict[str, dict[str, Any]] = {}
        for entry in self._remote:
            if isinstance(entry, dict) and entry.get("o"):
                merged[str(entry["o"])] = entry
        for r in conn.execute("SELECT * FROM offsite_copies ORDER BY root, rel_path"):
            merged[r["object"]] = {
                "o": r["object"], "library": Path(r["root"]).name or "library",
                "root": r["root"], "path": r["rel_path"], "size": r["size"], "mtime": r["mtime"],
                "at": r["uploaded_at"], **({"sha256": r["digest"]} if r["digest"] else {})}
        entries = list(merged.values())
        document = json.dumps({"format": "ninaivu-offsite", "version": 1, "key_id": key_id.hex(),
                               "made": self._clock(), "files": entries}, ensure_ascii=False).encode()
        staging = Path(self.cfg.state_dir) / "offsite-staging"
        staging.mkdir(parents=True, exist_ok=True)
        plain = staging / "manifest.json"
        sealed = staging / MANIFEST
        try:
            with create_new(plain) as handle:
                handle.write(document)
            crypto.encrypt_file_v2(plain, sealed, key, key_id)
            target.put_file(MANIFEST, sealed)
            self._remote = entries
            target.put_bytes(README, _README_TEXT.encode())
            record = keyring.load(self.cfg.state_dir)
            if record is not None and keyring.key_material(record)[1] == key_id:
                # The passphrase is then enough, without Drive: the settings
                # go beside the manifest, named for the key, and under the
                # plain name the first time.
                params = json.dumps(keyring.public_params(record), indent=2).encode()
                target.put_bytes(keyring.params_name(record), params)
                if target.exists(PARAMS) is None:
                    target.put_bytes(PARAMS, params)
        finally:
            plain.unlink(missing_ok=True)
            sealed.unlink(missing_ok=True)

    def _restore(self, destination: Path) -> None:
        message = ""
        restored = 0
        try:
            key, _ = self._key()
            target = self.target()
            entries = latest(read_manifest(target, key, Path(self.cfg.state_dir) / "offsite-staging"))
            libraries = {e["library"] for e in entries}
            already = beside = failed = 0
            for entry in entries:
                self._pause()
                rel = entry["path"]
                self._update(current=rel)
                try:
                    out = restore_path(destination, entry, libraries)
                    # A file already there is checked against the copy, not
                    # taken on its size: a damaged photograph is usually the
                    # size it was, and it is the one most in need of this.
                    outcome = fetch_one(target, entry, key, out, base=destination)
                except (OSError, ValueError) as exc:
                    self._problem(rel, str(exc))
                    failed += 1
                    continue
                if outcome == ALREADY:
                    already += 1
                    continue
                if outcome == BESIDE:
                    beside += 1
                restored += 1
                self._update(sent=restored)
            message = f"Put back {restored:,} file{'' if restored == 1 else 's'} in {destination}."
            if already:
                message += f" {already:,} {'was' if already == 1 else 'were'} already there."
            if beside:
                message += (f" {beside:,} went beside a different file of the same name, "
                            "marked (restored).")
            if failed:
                message += f" {failed:,} could not be put back."
        except _Stop:
            message = f"Stopped after {restored:,} files."
        except Exception as exc:                            # noqa: BLE001
            log.exception("the off-site restore stopped")
            self._update(error=f"The restore stopped: {exc}")
        finally:
            self._update(running=False, current="", waiting="", message=message, job="")


def _key_id_of(sealed: Path) -> bytes | None:
    from . import crypto                                    # noqa: PLC0415
    return crypto.read_key_id(sealed)


def _open_manifest(sealed: Path, key: bytes, staging: Path) -> list[dict[str, Any]]:
    """The entries of a downloaded manifest; ValueError when it does not open."""
    from . import crypto                                    # noqa: PLC0415
    plain = staging / "manifest.out"
    try:
        crypto.decrypt_file_v2(sealed, plain, key)
        document = json.loads(plain.read_text(encoding="utf-8"))
    finally:
        plain.unlink(missing_ok=True)
    if not isinstance(document, dict) or document.get("format") != "ninaivu-offsite":
        raise ValueError("that is not a Ninaivu off-site copy")
    return [e for e in document.get("files") or [] if isinstance(e, dict)]


def read_manifest(target, key: bytes, staging: Path) -> list[dict[str, Any]]:
    """The list of files in an off-site copy, decrypted with *key*.

    Every version of every file; :func:`latest` picks the newest of each.
    When the manifest will not open, the one kept from before the last run
    (``manifest.prev.ninaivu``) is tried before giving up.
    """
    staging.mkdir(parents=True, exist_ok=True)
    sealed = staging / "manifest.in"
    first: Exception | None = None
    for name in (MANIFEST, MANIFEST_PREV):
        try:
            target.get_file(name, sealed)
            return _open_manifest(sealed, key, staging)
        except (OSError, ValueError) as exc:
            if first is None:
                first = exc
        finally:
            sealed.unlink(missing_ok=True)
    assert first is not None
    raise first


def latest(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The newest version of each file in a manifest's list, in its order.

    A file that changed is kept in every version it was sent in. Entries from
    before versions were kept have no ``at``, and there is one of them a file.
    """
    newest: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in entries:
        which = (str(entry.get("root") or entry.get("library") or ""), str(entry.get("path")))
        when = (float(entry.get("at") or 0), float(entry.get("mtime") or 0))
        kept = newest.get(which)
        if kept is None or when >= (float(kept.get("at") or 0), float(kept.get("mtime") or 0)):
            newest[which] = entry
    chosen = {id(e) for e in newest.values()}
    return [e for e in entries if id(e) in chosen]


def by_age(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every version, newest of each file first: older ones then go beside it."""
    return sorted(entries, key=lambda e: (-float(e.get("at") or 0), -float(e.get("mtime") or 0)))


#: What :func:`fetch_one` did.
RESTORED, ALREADY, BESIDE = "restored", "already", "beside"


def restore_path(destination: Path, entry: dict[str, Any], libraries: set[str]) -> Path:
    """Where one manifest entry goes under *destination*; ValueError when it
    would land outside it, a linked folder included."""
    parts = ([entry["library"]] if len(libraries) > 1 else []) + \
        str(entry["path"]).replace("\\", "/").split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError("a path outside the destination")
    out = destination.joinpath(*parts)
    if not stays_inside(destination, out):
        raise ValueError("a path outside the destination")
    return out


def fetch_one(target, entry: dict[str, Any], key: bytes, out: Path,
              base: Path | None = None) -> str:
    """One file back from the copy, decrypted, with its time put back.

    Nothing is replaced. A file already at *out* (or beside it from an earlier
    restore) holding the same bytes means it is already there: ``ALREADY``. A
    different one, a damaged copy included, is left alone and this one goes
    beside it as ``name (restored).ext``: ``BESIDE``. Otherwise ``RESTORED``.
    With *base*, the folders made on the way are checked to stay inside it.
    """
    from . import crypto                                    # noqa: PLC0415
    from .restore import _beside, _earlier_beside, _publish  # noqa: PLC0415
    out.parent.mkdir(parents=True, exist_ok=True)
    if base is not None and not stays_inside(base, out):
        raise ValueError("a path outside the destination")
    # A manifest that carries the original's SHA-256 lets a file already
    # there be checked without fetching its copy. An older one does not, and
    # the copy is fetched and compared below.
    want = str(entry.get("sha256") or "")
    if want and (out.exists() or out.is_symlink()):
        for earlier in (out, *_earlier_beside(out)):
            if (earlier.is_file() and not earlier.is_symlink()
                    and earlier.stat().st_size == int(entry["size"])
                    and sha256_file(earlier) == want):
                return ALREADY
    sealed = out.with_name(f".{out.name}.ninaivu-part")
    plain = out.with_name(f".{out.name}.plain")
    try:
        target.get_file(entry["o"], sealed)
        crypto.decrypt_file_v2(sealed, plain, key)
        if plain.stat().st_size != int(entry["size"]):
            raise ValueError("it came back a different size")
        # The size alone let a same-size swap, or an older version put in its
        # place, through: the manifest's SHA-256 of the original is checked.
        if want and sha256_file(plain) != want:
            raise ValueError("it came back different from what was sent (SHA-256 differs)")
        outcome = RESTORED
        if out.exists() or out.is_symlink():
            for earlier in (out, *_earlier_beside(out)):
                if (earlier.is_file() and not earlier.is_symlink()
                        and same_bytes(plain, earlier)):
                    return ALREADY
            out = _beside(out)
            outcome = BESIDE
        if entry.get("mtime"):
            os.utime(plain, (float(entry["mtime"]), float(entry["mtime"])))
        _publish(plain, out)
        return outcome
    finally:
        sealed.unlink(missing_ok=True)
        plain.unlink(missing_ok=True)
