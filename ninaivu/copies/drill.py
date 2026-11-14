"""The backup drill: one button that proves every copy would come back.

Each copy already checks itself in its own way and on its own clock: the
second disk reads itself back once a month, Google Drive restores a few files
a week, the newest copy of the index is unpacked as soon as it is written.
None of that answers the one question a household asks before the day it
matters — *if the library went now, would our backups bring it back?* — and
the answers sit on four different pages.

The drill asks it of every copy at once, the way a fire drill does: for real,
but small. From each copy that keeps the originals it restores a handful of
photographs chosen at random into a temporary folder in the state folder,
and checks each one against the library's **content fingerprint** (BLAKE3,
storage/fingerprints.py) — not against the copy's own record of what it
sent, which is what each copy's own check already trusts. A copy whose
record and bytes agree with each other but not with the photograph is
caught here. Then the temporary folder is removed.

* **Second disk** — the files read from the disk, copied out and
  fingerprinted.
* **Off-site copy** — fetched, decrypted and fingerprinted
  (``Offsite.restore_sample``).
* **Google Drive** — downloaded through the same checks a real restore
  makes, decrypted when encrypted, and fingerprinted.
* **The Secondary** — read back there end to end over the signed pairing,
  and its fingerprints compared with the ones it confirmed
  (``Courier.read_back``). Nothing crosses the network but the answer.
* **The index** — the newest index backup unpacked and opened
  (``BackupKeeper.verify_latest``).

A file whose library copy has changed since it was backed up cannot be held
to the fingerprint the library has now; it is checked against what the copy
recorded when it was made instead, and said so. A file with no fingerprint
yet is treated the same way.

Nothing in the library or at any destination is written, moved or removed.
The result is kept in ``backup-drill.json`` in the state folder, the last
:data:`KEEP` of them, and shown on the Health page.
"""

from __future__ import annotations

import json
import logging
import random
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..utils.files import atomic_write, sha256_file
from . import ledger

log = logging.getLogger(__name__)

__all__ = ["BackupDrill", "PASSED", "FAILED", "ATTENTION", "NOTHING", "OFF", "RECORD",
           "SAMPLE", "fingerprint_of", "judge"]

#: What a drill, or one copy in it, came to.
PASSED, FAILED, ATTENTION, NOTHING, OFF = "passed", "failed", "attention", "nothing", "off"

#: Photographs taken from each copy. Enough to prove the whole chain, few
#: enough to finish in minutes over a home connection.
SAMPLE = 5

#: Files larger than this are left out of the sample when there are enough
#: smaller ones: three films would turn a drill into an evening.
SMALL_ENOUGH = 256 * 1024 * 1024

#: The record of past drills, in the state folder.
RECORD = "backup-drill.json"

#: Drills kept in the record.
KEEP = 10

#: The order copies are tried in, and shown in.
ORDER = ("mirror", "offsite", "drive", "secondary", "index")

#: A modified time this close is the same modified time (FAT and exFAT keep
#: two seconds).
SAME_TIME = 2.0


def fingerprint_of(conn: sqlite3.Connection, root: str, rel: str) -> dict[str, Any] | None:
    """The library's fingerprint of one file, with the size and modified time
    it was taken at, or None when it has none."""
    if not ledger._has_table(conn, "content_fingerprints"):        # noqa: SLF001
        return None
    row = conn.execute(
        "SELECT f.algo, f.digest, f.size, f.mtime FROM assets a "
        "JOIN content_fingerprints f ON f.asset_id = a.id "
        "WHERE a.root = ? AND a.rel_path = ?", (root, rel)).fetchone()
    if row is None:
        return None
    return {"algo": str(row[0]), "digest": str(row[1]), "size": int(row[2]),
            "mtime": float(row[3])}


def _hash(path: Path, algo: str) -> str | None:
    from ..mugil.hold import hasher                                # noqa: PLC0415
    fresh = hasher(algo)
    if fresh is None:
        return None
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            fresh.update(chunk)
    return fresh.hexdigest()


def judge(conn: sqlite3.Connection, restored: Path, *, root: str, rel: str, size: int,
          mtime: float = 0.0, own_ok: bool | None = None) -> dict[str, Any]:
    """One restored file held against the library's fingerprint.

    *size* and *mtime* are of the file as the copy recorded it. When the
    library's fingerprint is of that same version, the restored bytes must
    match it. When it is not (the file changed since it was copied) or there
    is none, the copy's own check (*own_ok*) is all there is, and the answer
    says so in ``checked``.
    """
    out: dict[str, Any] = {"path": rel}
    try:
        got = restored.stat().st_size
    except OSError:
        return {**out, "ok": False, "why": "it did not come back"}
    if got != int(size):
        return {**out, "ok": False, "why": "it came back a different size"}
    known = fingerprint_of(conn, root, rel)
    same_version = (known is not None and known["size"] == int(size)
                    and (not mtime or abs(known["mtime"] - float(mtime)) <= SAME_TIME))
    if same_version:
        assert known is not None
        digest = _hash(restored, known["algo"])
        if digest is not None:
            same = digest == known["digest"]
            return {**out, "ok": same, "checked": "fingerprint",
                    **({} if same else {"why": "it does not match the library's fingerprint"})}
    checked = "changed since" if known is not None else "no fingerprint"
    if own_ok is None:
        return {**out, "ok": True, "checked": checked}
    return {**out, "ok": bool(own_ok), "checked": checked,
            **({} if own_ok else {"why": "it does not match what was backed up"})}


def _copy_result(name: str, status: str, files: list[dict[str, Any]] | None = None,
                 reason: str = "") -> dict[str, Any]:
    files = list(files or [])
    return {"name": name, "status": status, "checked": len(files),
            "passed": sum(1 for f in files if f.get("ok")),
            "failed": sum(1 for f in files if not f.get("ok")),
            "fingerprinted": sum(1 for f in files if f.get("ok") and f.get("checked") == "fingerprint"),
            "reason": reason, "files": files[:50]}


def _from_files(name: str, files: list[dict[str, Any]], reason: str = "") -> dict[str, Any]:
    if not files:
        return _copy_result(name, NOTHING, reason=reason or "nothing to check yet")
    bad = any(not f.get("ok") for f in files)
    return _copy_result(name, FAILED if bad else PASSED, files, reason)


def overall(copies: list[dict[str, Any]]) -> str:
    """What the whole drill came to, from its copies."""
    said = [c["status"] for c in copies]
    if FAILED in said:
        return FAILED
    if ATTENTION in said:
        return ATTENTION
    if PASSED in said:
        return PASSED
    return NOTHING


class BackupDrill:
    """One drill at a time, on a thread of its own, started from the console."""

    def __init__(self, cfg, *, connect_db: Callable[[], sqlite3.Connection],
                 mirror=None, offsite=None, cloud=None, courier=None, backups=None,
                 restore_tests=None, sample: int = SAMPLE,
                 pick: Callable[[list, int], list] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self._connect = connect_db
        self.mirror = mirror
        self.offsite = offsite
        self.cloud = cloud
        self.courier = courier
        self.backups = backups
        self.restore_tests = restore_tests
        self.sample = max(1, int(sample))
        self._pick = pick or random.sample
        self._clock = clock
        self._running = threading.Lock()
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self._job = None
        self._progress: dict[str, Any] = {}

    # -- where things go ------------------------------------------------------

    @property
    def record_path(self) -> Path:
        return Path(self.cfg.state_dir) / RECORD

    @property
    def scratch(self) -> Path:
        return Path(self.cfg.state_dir) / "backup-drill"

    @property
    def running(self) -> bool:
        return self._running.locked()

    # -- start and stop -------------------------------------------------------

    def start(self) -> bool:
        """Start a drill in the background. False when one is running."""
        if self.running:
            return False
        self._stop.clear()
        self._progress = {"step": "", "done": 0, "total": 0}
        self._worker = threading.Thread(target=self.run, name="ninaivu-backup-drill", daemon=True)
        self._worker.start()
        return True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        job = self._job
        if job is not None:
            job.stop()
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout)

    # -- the drill ------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        if not self._running.acquire(blocking=False):
            return {"status": NOTHING, "copies": [], "busy": True}
        started = self._clock()
        try:
            result = self._run(started)
            # Kept before the lock is let go, so whoever sees the drill
            # finished also sees what it came to.
            self._keep(result)
        finally:
            self._job = None
            self._progress = {}
            self._running.release()
        if result["status"] == FAILED:
            log.warning("the backup drill failed: %s",
                        ", ".join(c["name"] for c in result["copies"] if c["status"] == FAILED))
        else:
            log.info("the backup drill: %s", result["status"])
        return result

    def _run(self, started: float) -> dict[str, Any]:
        copies: list[dict[str, Any]] = []
        steps = [(name, getattr(self, f"_{name}")) for name in ORDER]
        try:
            # Anything here is left over from a drill cut off by a power cut.
            shutil.rmtree(self.scratch, ignore_errors=True)
            for done, (name, step) in enumerate(steps):
                if self._stop.is_set():
                    copies.append(_copy_result(name, ATTENTION, reason="stopped"))
                    continue
                self._progress = {"step": name, "done": done, "total": len(steps)}
                try:
                    copies.append(step())
                except Exception as exc:                            # noqa: BLE001
                    log.exception("the backup drill could not check %s", name)
                    copies.append(_copy_result(name, FAILED, reason=f"the check stopped: {exc}"))
        finally:
            shutil.rmtree(self.scratch, ignore_errors=True)
        return {"started_at": started, "ended_at": self._clock(),
                "status": overall(copies), "copies": copies, "sample": self.sample}

    def _folder(self, name: str) -> Path:
        folder = self.scratch / name
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _choose(self, rows: list, count: int) -> list:
        small = [r for r in rows if int(r["size"] or 0) <= SMALL_ENOUGH]
        pool = small if len(small) >= min(count, len(rows)) else rows
        return list(self._pick(pool, min(count, len(pool))))

    # The second disk: a plain folder, so restoring is reading the file off it.
    def _mirror(self) -> dict[str, Any]:
        engine = self.mirror
        if engine is None or not engine.enabled:
            return _copy_result("mirror", OFF)
        problem = engine.problem()
        if problem:
            return _copy_result("mirror", ATTENTION, reason=problem)
        target = engine.folder
        if target is None or not target.is_dir():
            return _copy_result("mirror", ATTENTION, reason="the disk is not plugged in")
        conn = self._connect()
        if not ledger._has_table(conn, "mirror_copies"):            # noqa: SLF001
            return _copy_result("mirror", NOTHING, reason="nothing has been copied yet")
        rows = conn.execute("SELECT root, rel_path, size, mtime, sha256 FROM mirror_copies "
                            "WHERE size > 0").fetchall()
        if not rows:
            return _copy_result("mirror", NOTHING, reason="nothing has been copied yet")
        out = self._folder("mirror")
        files = []
        for number, row in enumerate(self._choose(rows, self.sample)):
            if self._stop.is_set():
                break
            rel = str(row["rel_path"])
            restored = out / f"{number}{Path(rel).suffix}"
            try:
                shutil.copyfile(engine.copy_path(row["root"], rel), restored)
            except FileNotFoundError:
                files.append({"path": rel, "ok": False, "why": "it is not on the disk"})
                continue
            except OSError as exc:
                files.append({"path": rel, "ok": False, "why": f"it could not be read: {exc}"})
                continue
            own = str(row["sha256"] or "")
            files.append(judge(conn, restored, root=row["root"], rel=rel, size=int(row["size"]),
                               mtime=float(row["mtime"] or 0),
                               own_ok=(sha256_file(restored) == own) if own else None))
            restored.unlink(missing_ok=True)
        return _from_files("mirror", files)

    # The off-site copy restores and fingerprints its own sample.
    def _offsite(self) -> dict[str, Any]:
        engine = self.offsite
        if engine is None or not engine.enabled:
            return _copy_result("offsite", OFF)
        problem = engine.problem()
        if problem:
            return _copy_result("offsite", ATTENTION, reason=problem)
        try:
            done = engine.restore_sample(self.sample)
        except (ValueError, OSError) as exc:
            return _copy_result("offsite", FAILED, reason=str(exc))
        files = [{**f, "checked": "fingerprint" if f.get("checked") not in (None, "sha256")
                  else "no fingerprint"} for f in done.get("files", [])]
        return _from_files("offsite", files, str(done.get("reason") or ""))

    # Google Drive: downloaded the way a real restore downloads.
    def _drive(self) -> dict[str, Any]:
        service = self.cloud
        # Connected is enough: what was uploaded is still there to restore
        # with the upload switched off.
        if service is None or not service.creds.connected:
            return _copy_result("drive", OFF)
        restoring = getattr(service, "_restore", None)
        if (restoring is not None and restoring.running) or \
                (self.restore_tests is not None and self.restore_tests.running):
            return _copy_result("drive", ATTENTION,
                                reason="a restore from Google Drive is already running")
        from ..cloud import keyring, restore                       # noqa: PLC0415
        from ..cloud.restore_test import TIME_LIMIT, pick           # noqa: PLC0415
        conn = self._connect()
        items = pick(conn, self.sample, SMALL_ENOUGH)
        if not items:
            return _copy_result("drive", NOTHING, reason="nothing has been uploaded yet")
        key = None
        if any(item.encrypted for item in items):
            record = keyring.load(self.cfg.state_dir)
            if record is None:
                return _copy_result("drive", FAILED, [
                    {"path": i.rel_path, "ok": False, "why": "the key is not on this computer"}
                    for i in items], reason="These backups are encrypted and this computer no "
                                            "longer has the key.")
            key = keyring.key_material(record)[0]
        out = self._folder("drive")
        job = restore.RestoreJob(connect=service.client, items=items, destination=out, key=key)
        self._job = job
        job.start()
        job.join(TIME_LIMIT)
        if job.running:
            job.stop(join=True)
            job.join(600)
            return _copy_result("drive", FAILED, reason="it took more than an hour and was stopped")
        state = job.state.snapshot()
        problems = {p["file"]: p["why"] for p in state["problems"]}
        files = []
        for item in items:
            restored = out.joinpath(*item.rel_path.split("/"))
            if item.rel_path in problems or not restored.is_file():
                files.append({"path": item.rel_path, "ok": False,
                              "why": problems.get(item.rel_path) or "it did not come back"})
                continue
            # The restore checked it against what was sent (SHA-256, or
            # Drive's MD5) before keeping it: that is its own check.
            files.append(judge(conn, restored, root=item.root, rel=item.rel_path,
                               size=item.size, mtime=item.mtime, own_ok=True))
        if not files or (not state["finished"] and not state["failed"] and state["message"]
                         and not any(f["ok"] for f in files)):
            return _copy_result("drive", ATTENTION, files, reason=str(state["message"] or ""))
        return _from_files("drive", files)

    # The Secondary reads its own copies back and says what it found.
    def _secondary(self) -> dict[str, Any]:
        engine = self.courier
        if engine is None:
            return _copy_result("secondary", OFF)
        from ..mugil import PRIMARY, pairing, role                  # noqa: PLC0415
        if role(self.cfg) != PRIMARY or pairing.load(self.cfg.state_dir) is None:
            return _copy_result("secondary", OFF)
        done = engine.read_back(self.sample)
        if not done.get("started"):
            reason = str(done.get("reason") or "")
            if "confirmed no file" in reason:
                return _copy_result("secondary", NOTHING, reason=reason)
            return _copy_result("secondary", ATTENTION, reason=reason)
        files = [{**f, "checked": "fingerprint"} for f in done.get("files", [])]
        return _from_files("secondary", files)

    # The newest copy of the index, unpacked and opened.
    def _index(self) -> dict[str, Any]:
        keeper = self.backups
        if keeper is None:
            return _copy_result("index", OFF)
        done = keeper.verify_latest()
        if done is None:
            return _copy_result("index", ATTENTION, reason="a backup of the index is being written")
        if done.get("bundle") is None and not done.get("ok"):
            return _copy_result("index", NOTHING, reason=str(done.get("error") or ""))
        files = [{"path": str(done.get("bundle") or ""), "ok": bool(done.get("ok")),
                  "checked": "checksums",
                  **({} if done.get("ok") else {"why": str(done.get("error") or "")})}]
        result = _from_files("index", files)
        result["items"] = done.get("assets")
        return result

    # -- the record -----------------------------------------------------------

    def history(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []

    def last(self) -> dict[str, Any] | None:
        found = self.history()
        return found[0] if found else None

    def _keep(self, result: dict[str, Any]) -> None:
        try:
            atomic_write(self.record_path, json.dumps([result, *self.history()][:KEEP]))
        except OSError as exc:
            log.warning("could not record the backup drill: %s", exc)

    def status(self) -> dict[str, Any]:
        return {"running": self.running, "progress": dict(self._progress) if self.running else None,
                "sample": self.sample, "last": self.last(), "history": self.history()[1:]}
