"""Keeping a copy of what Ninaivu knows, without being asked.

Archive and Cloud protect the photographs. This protects the other half: the
index — every named face, album, visibility rule, share link, corrected capture
date and edit. Lose the photographs and you have lost the photographs; lose the
index and the photographs are all still there, unnamed, unsorted and unshared,
and the household's work on top of them is gone.

``tools/backup_restore.py`` could already make a bundle, and it is a fine piece
of work: a live SQLite snapshot through the online backup API, the certificates
and avatars beside it, a manifest with a checksum for every file. It was also a
command-line script, which means it ran when somebody remembered — and nobody
remembers on a Tuesday.

So the making of a bundle lives here, where the app can do it on a schedule,
and the tool calls this rather than keeping a second copy of it. Restoring
stays in the tool on purpose: putting a backup back over a running library is a
deliberate act, done once, by somebody who has stopped and read what they are
about to do.
"""

from __future__ import annotations

from .. import __version__
from ..utils.files import sha256_file

import json
import logging
import os
import shutil
import sqlite3
import tarfile
import tempfile
import threading
import time
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

log = logging.getLogger(__name__)

__all__ = ["snapshot", "bundles", "latest", "prune", "BackupKeeper", "PREFIX"]

PREFIX = "ninaivu_backup_"

#: What goes in, beyond the databases. Anything missing is simply skipped: a
#: household with no HTTPS certificate should still get a backup.
#:
#: ``tls`` is where Ninaivu's own certificate authority lives (utils/tls.py);
#: the top-level ``ninaivu-ca.*`` names are kept for state written before that
#: move. Losing the CA means every phone told to trust it warns again. The
#: cloud encryption key and the Google sign-in go too: without the key, an
#: encrypted cloud copy is readable only by whoever kept the recovery file.
#: A bundle therefore holds secrets and is written readable by this account
#: alone.
EXTRAS = ("config.json", "library-path", "ninaivu-ca.crt", "ninaivu-ca.key",
          "ninaivu.crt", "ninaivu.key", "tls", "cloud-encryption.json",
          "google.json", "avatars", "archive-logs", "pending-uploads",
          "library-ids.json")

#: Extras a restore replaces only when the bundle actually carries them. An
#: older bundle made before these were collected must not wipe the ones this
#: machine has now.
REPLACED_ONLY_IF_PRESENT = ("tls", "cloud-encryption.json", "google.json",
                            "library-ids.json")

#: Entries that are folders, and may hold files of their own.
_FOLDERS = ("avatars", "archive-logs", "pending-uploads", "tls")

DATABASES = ("index.db", "archive.db")


def hot_copy(source: Path, target: Path) -> bool:
    """Snapshot a live SQLite database through its own backup API.

    Copying the file would catch it mid-write; this does not. The library can
    stay open and in use the whole time.
    """
    if not source.is_file():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with closing(sqlite3.connect(str(source), timeout=30.0)) as src:
            with closing(sqlite3.connect(str(target))) as dst:
                with dst:
                    # One step, not many. A stepped backup starts again from
                    # the first page whenever another connection writes, and
                    # the cloud upload commits after every file — a 100-page
                    # step on a library index was restarted for over an hour.
                    # In WAL mode one step is one read transaction: writers
                    # carry on while it runs.
                    src.backup(dst, pages=-1)
        return True
    except Exception as exc:                            # noqa: BLE001
        log.warning("could not snapshot %s: %s", source.name, exc)
        return False


def snapshot(state_dir: Path | str, out_dir: Path | str) -> Path | None:
    return _snapshot(state_dir, out_dir)


def _stage(state_dir: Path, staged: Path) -> bool:
    """Copy the databases and extras into *staged*. False if any could not be."""
    for name in DATABASES:
        source = state_dir / name
        if source.is_file() and not hot_copy(source, staged / name):
            log.error("backup abandoned: %s could not be snapshotted", name)
            return False

    for name in EXTRAS:
        source, target = state_dir / name, staged / name
        try:
            if source.is_file():
                shutil.copy2(source, target)
            elif source.is_dir():
                # Linked, not copied, where the disk allows: the pending
                # uploads can be gigabytes of video, and this runs holding
                # the write lock every scan and approval waits on.
                shutil.copytree(source, target, copy_function=_link_or_copy)
            elif source.exists() or source.is_symlink():
                raise OSError("unsupported or broken state entry")
        except OSError as exc:
            log.error("backup abandoned: could not copy %s: %s", name, exc)
            return False
    return True


def _link_or_copy(source: str, target: str) -> None:
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


#: The staging folder's name, inside the state folder: on the same disk as what
#: it stages, so files can be linked rather than copied, and not in ``/tmp``,
#: which on a Pi is often memory.
STAGING_PREFIX = ".backup-staging-"


def _clear_old_staging(state_dir: Path, older_than: float = 86400) -> None:
    """Staging left by a backup that was killed part-way."""
    now = time.time()
    for leftover in state_dir.glob(f"{STAGING_PREFIX}*"):
        try:
            if now - leftover.stat().st_mtime > older_than:
                shutil.rmtree(leftover, ignore_errors=True)
        except OSError:
            continue


def _snapshot(state_dir: Path | str, out_dir: Path | str) -> Path | None:
    """Write one verified bundle into *out_dir*. Returns its path, or None."""
    state_dir = Path(state_dir)
    out_dir = Path(out_dir)
    if not state_dir.is_dir():
        log.warning("nothing to back up: %s is not there", state_dir)
        return None
    if not (state_dir / "index.db").is_file():
        log.error("backup abandoned: the library index is missing")
        return None

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive = out_dir / f"{PREFIX}{stamp}_{uuid.uuid4().hex}.tar.gz"

    _clear_old_staging(state_dir)
    with tempfile.TemporaryDirectory(prefix=STAGING_PREFIX, dir=state_dir) as tmp:
        staged = Path(tmp) / "state"
        staged.mkdir()

        # Approval moves staged bytes and updates SQLite under this same lock,
        # so the copy is taken under it. Only the copy: hashing and gzipping
        # the bundle takes minutes, and every scan pass that writes waits on
        # this lock — held for the whole backup, an occasions rebuild queued
        # behind it for 84 minutes.
        from . import db
        with db._write_lock:
            if not _stage(state_dir, staged):
                return None

        manifest: dict[str, Any] = {
            # Which Ninaivu made it — read by nobody yet, but a person
            # looking at a bundle years on deserves the true answer.
            "version": __version__,
            "created_at": datetime.now().isoformat(),
            "source_state_dir": str(state_dir),
            "files": {},
        }
        for path in staged.rglob("*"):
            if path.is_file():
                manifest["files"][path.relative_to(staged).as_posix()] = {
                    "size": path.stat().st_size, "sha256": sha256_file(path)}
        (staged / "backup_manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8")
        try:
            verify_state(staged)
        except (ValueError, OSError, sqlite3.Error) as exc:
            log.error("backup abandoned: staged state is incomplete: %s", exc)
            return None

        pending = None
        try:
            # Build beside the destination so publication is one atomic rename.
            # Listings and retention must never see an unfinished bundle.
            with tempfile.NamedTemporaryFile(
                    dir=out_dir, prefix=".ninaivu_backup_", suffix=".tmp",
                    delete=False) as output:
                pending = Path(output.name)
                with tarfile.open(fileobj=output, mode="w:gz") as tar:
                    tar.add(staged, arcname="state")
                output.flush()
                os.fsync(output.fileno())
            os.replace(pending, archive)
        except OSError as exc:
            log.error("could not write the backup bundle: %s", exc)
            return None
        finally:
            if pending is not None:
                pending.unlink(missing_ok=True)

    log.info("backup written: %s (%.1f MB)", archive.name,
             archive.stat().st_size / 1048576)
    return archive


# ---------------------------------------------------------------------------
# Checking a bundle can be restored
# ---------------------------------------------------------------------------
#
# The same checks tools/backup_restore.py makes before it replaces anything, so
# "this backup verified" means exactly "a restore of it would get past every
# check". They live here so Ninaivu can run them on its own backups, on a
# schedule, without anyone typing a command.

#: Where Ninaivu notes the result of the last check, beside the bundles.
VERIFIED_FILE = "last-verification.json"


def valid_state_path(name: str) -> bool:
    """Portable, relative names containing only the state a bundle carries."""
    if not isinstance(name, str) or not name or "\\" in name or ":" in name:
        return False
    parts = name.split("/")
    if any(part in ("", ".", "..") or part.endswith((".", " ")) for part in parts):
        return False
    if PurePosixPath(name).is_absolute():
        return False
    allowed = (*DATABASES, *EXTRAS, "backup_manifest.json")
    return parts[0] in allowed and (
        len(parts) == 1 or parts[0] in _FOLDERS)


def safe_members(tar: tarfile.TarFile, dest: Path):
    """Split an archive's members into what is safe to extract and what is not.

    Belt-and-braces alongside ``filter="data"``: that filter is not on every
    Python this might run under, and a backup is not always read from a
    fully-trusted place. A member whose name resolves outside ``dest``, that is
    not a plain file or directory, or that repeats a name, is refused — Ninaivu's
    own bundles never contain one.
    """
    root = dest.resolve()
    safe, rejected, seen = [], [], set()
    for member in tar.getmembers():
        target = (dest / member.name).resolve()
        name = member.name.rstrip("/") if member.isdir() else member.name
        portable = (name == "state" and member.isdir()) or (
            name.startswith("state/") and valid_state_path(name[6:]))
        key = name.casefold()
        ok = portable and key not in seen and (
            member.isfile() or member.isdir()) and root in target.parents
        seen.add(key)
        (safe if ok else rejected).append(member)
    return safe, rejected


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate manifest key: {key}")
        result[key] = value
    return result


def verify_state(state: Path) -> None:
    """ValueError unless *state* is exactly what its manifest says, and sound."""
    manifest = json.loads((state / "backup_manifest.json").read_text(encoding="utf-8"),
                          object_pairs_hook=_unique_object)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict):
        raise ValueError("Manifest must contain a files object")
    files = manifest["files"]
    if "index.db" not in files:
        raise ValueError("Backup is missing its library index")
    actual = {p.relative_to(state).as_posix() for p in state.rglob("*")
              if p.is_file() and p != state / "backup_manifest.json"}
    if set(files) != actual:
        raise ValueError("Manifest does not exactly match the archived files")
    for name, meta in files.items():
        if not valid_state_path(name) or name == "backup_manifest.json":
            raise ValueError(f"Invalid manifest path: {name}")
        if not isinstance(meta, dict) or type(meta.get("size")) is not int:
            raise ValueError(f"Invalid file metadata: {name}")
        path = state / name
        if path.stat().st_size != meta["size"] or sha256_file(path) != meta.get("sha256"):
            raise ValueError(f"Checksum or size mismatch: {name}")
    for name in DATABASES:
        path = state / name
        if path.is_file():
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as conn:
                if conn.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise ValueError(f"Database integrity check failed: {name}")
                if name == "index.db" and conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='pending_uploads'").fetchone():
                    for key, filename in conn.execute(
                            "SELECT storage_key, filename FROM pending_uploads WHERE status='pending'"):
                        expected = f"pending-uploads/{key}/{filename}"
                        if expected not in files or not valid_state_path(expected):
                            raise ValueError("Backup is missing pending upload bytes")


def extract(archive: Path, dest: Path) -> Path:
    """Safely unpack *archive* into *dest*; returns the extracted state folder."""
    with tarfile.open(archive, "r:gz") as tar:
        members, rejected = safe_members(tar, dest)
        if rejected:
            raise ValueError(f"The archive contains {len(rejected)} unsafe or unexpected "
                             "entries (outside the restore directory, or not a plain file)")
        if hasattr(tarfile, "data_filter"):
            tar.extractall(dest, members=members, filter="data")
        else:
            tar.extractall(dest, members=members)
    return dest / "state"


def verify_bundle(archive: Path | str) -> dict[str, Any]:
    """Rehearse restoring *archive* without touching anything live.

    Unpacks it into a temporary folder, makes every check the restore tool
    makes, then opens the restored index the way Ninaivu would and counts what
    is in it — a bundle whose index cannot be read as a library is not a backup
    of one, whatever its checksums say.
    """
    archive = Path(archive)
    result: dict[str, Any] = {"bundle": archive.name, "at": time.time(), "ok": False,
                              "error": None, "assets": None, "profiles": None}
    try:
        with tempfile.TemporaryDirectory(prefix="ninaivu_verify_") as tmp:
            state = extract(archive, Path(tmp))
            verify_state(state)
            index = state / "index.db"
            with closing(sqlite3.connect(index.as_uri() + "?mode=ro", uri=True)) as conn:
                tables = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                if "assets" not in tables:
                    raise ValueError("The restored index has no media table")
                result["assets"] = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
                if "users" in tables:
                    result["profiles"] = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        result["ok"] = True
    except (OSError, ValueError, tarfile.TarError, sqlite3.Error) as exc:
        result["error"] = str(exc)
    return result


def bundles(out_dir: Path | str) -> list[dict[str, Any]]:
    """Every bundle in *out_dir*, newest first."""
    out_dir = Path(out_dir)
    if not out_dir.is_dir():
        return []
    found = []
    for path in out_dir.glob(f"{PREFIX}*.tar.gz"):
        try:
            stat = path.stat()
        except OSError:
            continue
        found.append({"name": path.name, "path": str(path),
                      "at": stat.st_mtime, "bytes": stat.st_size})
    return sorted(found, key=lambda b: b["at"], reverse=True)


def latest(out_dir: Path | str) -> dict[str, Any] | None:
    found = bundles(out_dir)
    return found[0] if found else None


def _device(path: Path) -> int | None:
    """The storage device holding *path*, or None if that cannot be told.

    A backup folder may not exist until the first copy is written, so the
    nearest existing parent answers for it. Device 0 is what some network
    and virtual filesystems report for everything; treating it as an answer
    would call every share "the same drive" as every other.
    """
    try:
        candidate = path.resolve()
    except OSError:
        return None
    for folder in (candidate, *candidate.parents):
        try:
            device = os.stat(folder).st_dev
        except OSError:
            continue
        return device or None
    return None


def prune(out_dir: Path | str, keep: int) -> list[str]:
    """Keep the newest *keep* bundles and remove the rest.

    Deleting anything is done narrowly and on purpose: only files this module
    named, only in the folder it was told to use.
    """
    removed = []
    if keep <= 0:
        return removed
    for old in bundles(out_dir)[keep:]:
        try:
            Path(old["path"]).unlink()
            removed.append(old["name"])
        except OSError as exc:                          # noqa: PERF203
            log.warning("could not remove the old backup %s: %s", old["name"], exc)
    return removed


class BackupKeeper:
    """Runs :func:`snapshot` on a schedule, quietly.

    It skips a run when the databases and backed-up settings have not changed
    since the last bundle, and it never runs two at once.
    """

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = threading.Lock()
        self.last_error: str | None = None

    # -- where and how often ----------------------------------------------
    @property
    def folder(self) -> Path:
        chosen = getattr(self.cfg, "backup_dir", "") or ""
        return Path(chosen) if chosen else Path(self.cfg.state_dir) / "backups"

    @property
    def every(self) -> float:
        return float(getattr(self.cfg, "backup_every_hours", 24) or 0) * 3600

    @property
    def keep(self) -> int:
        return int(getattr(self.cfg, "backup_keep", 7) or 0)

    @property
    def busy(self) -> bool:
        """Is a bundle being made right now? Asked by the activity strip.

        Bundling copies every database and then restores the copy to check it,
        which on a large library is real disk work — and it used to happen
        with nothing on screen to say so.
        """
        return self._running.locked()

    # -- the loop ----------------------------------------------------------
    def start(self) -> None:
        if self.every <= 0 or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ninaivu-backup",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)

    def _loop(self) -> None:
        # Not at start-up: the first minutes after a restart are the busiest
        # the machine gets, and a backup is the least urgent thing in them.
        # Then every quarter of an hour, asking `due()` — which is what keeps
        # it to one bundle per interval. Sleeping the whole interval after a
        # look that found nothing due meant a server restarted every day never
        # looked a second time, and a day's backup slid to two or more.
        #
        # The interval is read afresh each round, and the console can set it to
        # 0 (off) while this runs: a wait of 0 then returned at once, forever,
        # and held a CPU core until the next restart. Off still looks every
        # quarter-hour, in case it is switched back on.
        while not self._stop.wait(min(self.every, 900) if self.every > 0 else 900):
            try:
                if self.due():
                    self.run()
            except Exception as exc:                    # noqa: BLE001
                self.last_error = str(exc)
                log.exception("the scheduled backup failed")

    def due(self) -> bool:
        """Is a bundle worth making now?"""
        if self.every <= 0:
            return False
        last = latest(self.folder)
        if not last:
            return True
        if time.time() - last["at"] < self.every:
            return False
        state_dir = Path(self.cfg.state_dir)
        # SQLite WAL writes need not touch the main database. Settings,
        # avatars and archive progress are also part of the backup contract.
        paths = [state_dir / name for name in DATABASES]
        paths += [state_dir / f"{name}-wal" for name in DATABASES]
        paths += [state_dir / name for name in EXTRAS]
        try:
            for path in paths:
                if not path.exists():
                    continue
                if path.stat().st_mtime > last["at"]:
                    return True
                if path.is_dir() and any(
                        child.stat().st_mtime > last["at"]
                        for child in path.rglob("*")):
                    return True
            return not (state_dir / "index.db").exists()
        except OSError:
            return True

    def run(self) -> Path | None:
        """Make one now, then check it restores. Safe from a route or the loop."""
        if not self._running.acquire(blocking=False):
            return None
        try:
            self.last_error = None
            made = snapshot(self.cfg.state_dir, self.folder)
            if made:
                # Verify before pruning. The other way round, the oldest
                # *known-good* bundle went to make room for one not yet shown
                # to restore — with keep=1, the only good copy. A bundle that
                # fails its check is removed; the older ones stay.
                result = self._verify(made)
                if result["ok"]:
                    prune(self.folder, self.keep)
                else:
                    try:
                        made.unlink()
                    except OSError as exc:
                        log.warning("could not remove the failed backup %s: %s",
                                    made.name, exc)
                    self.last_error = f"the new backup did not verify: {result['error']}"
                    prune(self.folder, self.keep)
            else:
                self.last_error = "the snapshot could not be written"
            return made
        finally:
            self._running.release()

    def verify_latest(self) -> dict[str, Any] | None:
        """Rehearse a restore of the newest bundle now. None if one is being written."""
        last = latest(self.folder)
        if last is None:
            return {"ok": False, "error": "There is no backup to check yet.", "at": time.time(),
                    "bundle": None, "assets": None, "profiles": None}
        if not self._running.acquire(blocking=False):
            return None
        try:
            return self._verify(self.folder / last["name"])
        finally:
            self._running.release()

    def _verify(self, archive: Path) -> dict[str, Any]:
        result = verify_bundle(archive)
        if not result["ok"]:
            log.error("backup %s did not verify: %s", archive.name, result["error"])
        try:
            (self.folder / VERIFIED_FILE).write_text(json.dumps(result), encoding="utf-8")
        except OSError as exc:
            log.warning("could not record the backup check: %s", exc)
        return result

    def last_verification(self) -> dict[str, Any] | None:
        try:
            data = json.loads((self.folder / VERIFIED_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def shares_a_drive_with(self) -> list[str]:
        """What else lives on the drive the backups are written to.

        A copy on the same disk as the thing it copies survives a mistake but
        not the disk failing, and the default folder sits inside the state
        directory — so out of the box it is always on the same drive as the
        index. Saying so is the only way most households find out.
        """
        here = _device(self.folder)
        if here is None:
            return []
        shared = []
        if _device(Path(self.cfg.state_dir)) == here:
            shared.append("the library index")
        libraries = list(getattr(self.cfg, "libraries", None) or []) \
            or list(getattr(self.cfg, "roots", None) or [])
        if any(_device(Path(root)) == here for root in libraries if root):
            shared.append("the photo library")
        return shared

    def state(self) -> dict[str, Any]:
        """What the console shows."""
        last = latest(self.folder)
        return {
            "verified": self.last_verification(),
            "same_drive_as": self.shares_a_drive_with(),
            "folder": str(self.folder),
            "every_hours": self.every / 3600 if self.every else 0,
            "keep": self.keep,
            "running": self._running.locked(),
            "last": last,
            "count": len(bundles(self.folder)),
            "error": self.last_error,
        }
