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
* **Only ever added to.** A photograph deleted here stays there. Nothing
  Ninaivu does removes a file from the off-site copy.
* **Restorable without this computer** — the recovery file (or the
  passphrase and the saved settings) is enough: ``ninaivu offsite-restore``
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
import shutil
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

__all__ = ["Offsite", "FolderTarget", "S3Target", "object_name", "SCHEMA", "MANIFEST"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS offsite_copies (
    root         TEXT NOT NULL,
    rel_path     TEXT NOT NULL,
    size         INTEGER NOT NULL DEFAULT 0,
    mtime        REAL NOT NULL DEFAULT 0,
    object       TEXT NOT NULL,
    stored_size  INTEGER NOT NULL DEFAULT 0,
    uploaded_at  REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (root, rel_path)
);
CREATE TABLE IF NOT EXISTS offsite_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '');
"""

MANIFEST = "manifest.ninaivu"
README = "README.txt"
_README_TEXT = """This folder is an encrypted off-site copy of a Ninaivu photo library.

Every file in files/ is one photograph or video, encrypted (AES-256-GCM) with the
household's Ninaivu backup key, and named by a keyed hash so nothing about it can be
read here. manifest.ninaivu, encrypted with the same key, says which is which.

To get the photographs back, with the recovery file Ninaivu's console offers
(Mugil, "Download the recovery file"), and Ninaivu installed on any computer:

    ninaivu offsite-restore --recovery ninaivu-recovery.json <this folder> <where to put them>

Without the recovery file or the passphrase, nobody can read these files — Ninaivu included.
"""
SECRET_FILE = "offsite-secret.json"


def object_name(key: bytes, root: str, rel_path: str) -> str:
    """Where one file is stored: a keyed hash of where it lives, so the name
    says nothing — and the same file always lands in the same place."""
    digest = hmac.new(key, f"{root}\0{rel_path}".encode("utf-8"), hashlib.sha256).hexdigest()
    return f"files/{digest[:2]}/{digest[:40]}.ninaivu"


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
        if not Path(os.path.abspath(path)).is_relative_to(os.path.abspath(self.base)):
            raise OSError("refusing a name outside the off-site folder")
        return path

    def put_file(self, name: str, source: Path, on_bytes=None, stop=None) -> None:
        target = self._path(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".part")
        shutil.copyfile(source, partial)
        with open(partial, "rb+") as handle:
            os.fsync(handle.fileno())
        os.replace(partial, target)
        if on_bytes:
            on_bytes(target.stat().st_size)

    def put_bytes(self, name: str, data: bytes) -> None:
        target = self._path(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".part")
        partial.write_bytes(data)
        os.replace(partial, target)

    def exists(self, name: str) -> int | None:
        path = self._path(name)
        return path.stat().st_size if path.is_file() else None

    def get_file(self, name: str, dest: Path) -> None:
        shutil.copyfile(self._path(name), dest)

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

    # -- settings ----------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "offsite_enabled", False))

    @property
    def roots(self) -> list[str]:
        return list(self.cfg.roots or ([self.cfg.active_root] if self.cfg.active_root else []))

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
        return target.ready()

    def _key(self) -> tuple[bytes, bytes]:
        from . import keyring                               # noqa: PLC0415
        record = keyring.load(self.cfg.state_dir)
        if record is None:
            raise ValueError("there is no backup key")
        return keyring.key_material(record)

    def _db(self) -> sqlite3.Connection:
        conn = self._connect()
        conn.executescript(SCHEMA)
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

    def start(self) -> dict[str, Any]:
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
        try:
            key, key_id = self._key()
            target = self.target()
            conn = self._db()
            marks = ",".join("?" * len(self.roots)) or "''"
            owed = conn.execute(
                "SELECT a.root, a.rel_path, a.size, a.mtime FROM assets a LEFT JOIN offsite_copies o "
                "ON o.root = a.root AND o.rel_path = a.rel_path "
                f"WHERE a.trashed = 0 AND a.root IN ({marks}) AND (o.root IS NULL OR o.size != a.size "
                "OR ABS(o.mtime - a.mtime) > 0.001) ORDER BY a.id", self.roots).fetchall()
            staging = Path(self.cfg.state_dir) / "offsite-staging"
            staging.mkdir(parents=True, exist_ok=True)
            for row in owed:
                self._pause()
                rel = row["rel_path"]
                source = Path(row["root"]).joinpath(*rel.replace("\\", "/").split("/"))
                self._update(current=rel)
                if not source.is_file():
                    continue
                name = object_name(key, row["root"], rel)
                try:
                    before = source.stat()
                    with tempfile.NamedTemporaryFile(dir=staging, suffix=".ninaivu", delete=False) as tmp:
                        encrypted = Path(tmp.name)
                    try:
                        stored = crypto.encrypt_file_v2(source, encrypted, key, key_id)
                        after = source.stat()
                        if after.st_mtime != before.st_mtime or after.st_size != before.st_size:
                            raise OSError("the file changed while it was being read")
                        target.put_file(name, encrypted, stop=self._stop.is_set)
                    finally:
                        encrypted.unlink(missing_ok=True)
                except (OSError, ValueError) as exc:
                    self._problem(rel, str(exc))
                    continue
                conn.execute(
                    "INSERT INTO offsite_copies(root, rel_path, size, mtime, object, stored_size, uploaded_at) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?) ON CONFLICT(root, rel_path) DO UPDATE SET size=excluded.size, "
                    "mtime=excluded.mtime, object=excluded.object, stored_size=excluded.stored_size, "
                    "uploaded_at=excluded.uploaded_at",
                    (row["root"], rel, before.st_size, before.st_mtime, name, stored, self._clock()))
                conn.commit()
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
        except Exception as exc:                            # noqa: BLE001
            log.exception("the off-site copy stopped")
            self._update(error=f"It stopped: {exc}")
        finally:
            self._update(running=False, current="", waiting="", message=message, job="")

    def _manifest(self, conn, target, key: bytes, key_id: bytes) -> None:
        """Which object is which file — encrypted, and written last so it never
        names an object that is not there."""
        from . import crypto                                # noqa: PLC0415
        entries = [{"o": r["object"], "library": Path(r["root"]).name or "library",
                    "root": r["root"], "path": r["rel_path"], "size": r["size"], "mtime": r["mtime"]}
                   for r in conn.execute("SELECT * FROM offsite_copies ORDER BY root, rel_path")]
        document = json.dumps({"format": "ninaivu-offsite", "version": 1, "key_id": key_id.hex(),
                               "made": self._clock(), "files": entries}, ensure_ascii=False).encode()
        staging = Path(self.cfg.state_dir) / "offsite-staging"
        staging.mkdir(parents=True, exist_ok=True)
        plain = staging / "manifest.json"
        sealed = staging / MANIFEST
        try:
            plain.write_bytes(document)
            crypto.encrypt_file_v2(plain, sealed, key, key_id)
            target.put_file(MANIFEST, sealed)
            target.put_bytes(README, _README_TEXT.encode())
        finally:
            plain.unlink(missing_ok=True)
            sealed.unlink(missing_ok=True)

    def _restore(self, destination: Path) -> None:
        message = ""
        restored = 0
        try:
            key, _ = self._key()
            target = self.target()
            entries = read_manifest(target, key, Path(self.cfg.state_dir) / "offsite-staging")
            libraries = {e["library"] for e in entries}
            for entry in entries:
                self._pause()
                rel = entry["path"]
                parts = ([entry["library"]] if len(libraries) > 1 else []) + rel.replace("\\", "/").split("/")
                out = destination.joinpath(*parts)
                if not Path(os.path.abspath(out)).is_relative_to(os.path.abspath(destination)):
                    self._problem(rel, "a path outside the destination")
                    continue
                self._update(current=rel)
                if out.is_file() and out.stat().st_size == entry["size"]:
                    continue
                try:
                    fetch_one(target, entry, key, out)
                except (OSError, ValueError) as exc:
                    self._problem(rel, str(exc))
                    continue
                restored += 1
                self._update(sent=restored)
            message = f"Put back {restored:,} file{'' if restored == 1 else 's'} in {destination}."
        except _Stop:
            message = f"Stopped after {restored:,} files."
        except Exception as exc:                            # noqa: BLE001
            log.exception("the off-site restore stopped")
            self._update(error=f"The restore stopped: {exc}")
        finally:
            self._update(running=False, current="", waiting="", message=message, job="")


def read_manifest(target, key: bytes, staging: Path) -> list[dict[str, Any]]:
    """The list of files in an off-site copy, decrypted with *key*."""
    from . import crypto                                    # noqa: PLC0415
    staging.mkdir(parents=True, exist_ok=True)
    sealed, plain = staging / "manifest.in", staging / "manifest.out"
    try:
        target.get_file(MANIFEST, sealed)
        crypto.decrypt_file_v2(sealed, plain, key)
        document = json.loads(plain.read_text(encoding="utf-8"))
    finally:
        sealed.unlink(missing_ok=True)
        plain.unlink(missing_ok=True)
    if document.get("format") != "ninaivu-offsite":
        raise ValueError("that is not a Ninaivu off-site copy")
    return list(document.get("files") or [])


def fetch_one(target, entry: dict[str, Any], key: bytes, out: Path) -> None:
    """One file back from the copy, decrypted, with its time put back."""
    from . import crypto                                    # noqa: PLC0415
    out.parent.mkdir(parents=True, exist_ok=True)
    sealed = out.with_name(f".{out.name}.ninaivu-part")
    plain = out.with_name(f".{out.name}.plain")
    try:
        target.get_file(entry["o"], sealed)
        crypto.decrypt_file_v2(sealed, plain, key)
        if plain.stat().st_size != int(entry["size"]):
            raise ValueError("it came back a different size")
        if entry.get("mtime"):
            os.utime(plain, (float(entry["mtime"]), float(entry["mtime"])))
        if out.exists():
            raise FileExistsError(f"{out.name} is already there, and different")
        os.replace(plain, out)
    finally:
        sealed.unlink(missing_ok=True)
        plain.unlink(missing_ok=True)
