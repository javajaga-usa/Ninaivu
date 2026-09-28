"""Proving the cloud backup restores, before the day it has to.

A backup nobody has restored from is a hope. This restores a few photographs
from Google Drive every week, on its own, into a temporary folder: through
the same checks a real restore makes (the SHA-256 recorded when the file was
sent, the key named inside an encrypted file, AES-GCM's authentication, the
size), and then once more against the original still in the library, byte
for byte. Then it deletes the copies and writes down what happened.

What it catches that nothing else does:

* a backup that uploads happily but cannot be read back — a Drive account
  whose permission no longer covers the files, a file damaged in Drive;
* an **encrypted backup whose key is no longer on this machine** — the case
  where everything looks fine until the day it is needed;
* a library copy that has silently changed since it was backed up.

It never writes anywhere but its own temporary folder, never touches the
library, and takes a handful of files, not the library: enough to know the
whole chain works, cheap enough to run every week on a home connection.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import keyring, restore, store

log = logging.getLogger(__name__)

__all__ = ["RestoreTester", "SCHEMA"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS restore_tests (
    id          INTEGER PRIMARY KEY,
    started_at  REAL NOT NULL,
    ended_at    REAL NOT NULL DEFAULT 0,
    status      TEXT NOT NULL,
    files       INTEGER NOT NULL DEFAULT 0,
    bytes       INTEGER NOT NULL DEFAULT 0,
    restored    INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    matched     INTEGER NOT NULL DEFAULT 0,
    summary     TEXT NOT NULL DEFAULT '',
    detail      TEXT NOT NULL DEFAULT '[]',
    scheduled   INTEGER NOT NULL DEFAULT 0
);
"""

PASSED, FAILED, SKIPPED = "passed", "failed", "skipped"

#: How long one test may take before it is given up as failed.
TIME_LIMIT = 3600.0

#: How often the keeper looks to see whether one is due.
LOOK_EVERY = 3600.0


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def pick(conn: sqlite3.Connection, count: int, max_bytes: int) -> list[restore.RestoreItem]:
    """A handful of uploaded files, at random, none larger than *max_bytes*.

    One video among them when there is one small enough: videos are most of
    the bytes in a family backup and take the multi-piece download path, which
    photographs rarely exercise.
    """
    store.init_schema(conn)
    base = ("SELECT root, rel_path, size, digest, remote_id, encrypted, source_mtime "
            "FROM cloud_uploads WHERE state=? AND remote_id != '' AND size > 0 "
            "AND size <= ?")
    chosen: list[sqlite3.Row] = []
    if count > 1:
        chosen += conn.execute(base + " AND kind=? ORDER BY RANDOM() LIMIT 1",
                               (store.DONE, int(max_bytes), store.VIDEO)).fetchall()
    taken = {(r["root"], r["rel_path"]) for r in chosen}
    for row in conn.execute(base + " ORDER BY RANDOM() LIMIT ?",
                            (store.DONE, int(max_bytes), int(count) + len(chosen))):
        if len(chosen) >= count:
            break
        if (row["root"], row["rel_path"]) not in taken:
            chosen.append(row)
    items = []
    for row in chosen:
        rel = restore._clean_rel(row["rel_path"])               # noqa: SLF001
        if rel is None:
            continue
        items.append(restore.RestoreItem(
            remote_id=row["remote_id"], rel_path=rel, root=row["root"],
            size=int(row["size"] or 0), sha256=row["digest"] or "",
            encrypted=bool(row["encrypted"]), mtime=float(row["source_mtime"] or 0)))
    return items


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


class RestoreTester:
    """Runs a test restore now, or on a schedule. One at a time."""

    def __init__(self, cfg, service, *, connect_db: Callable[[], sqlite3.Connection],
                 notify: Callable[[str, str, str], None] | None = None,
                 hold: Callable[[], str | None] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self.service = service
        self._connect = connect_db
        self._notify = notify
        self._hold = hold
        self._clock = clock
        self._running = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._worker: threading.Thread | None = None
        self.current: restore.RestoreJob | None = None

    # -- settings ----------------------------------------------------------

    @property
    def every_days(self) -> int:
        return max(0, int(getattr(self.cfg, "restore_test_days", 7) or 0))

    @property
    def files(self) -> int:
        return max(1, int(getattr(self.cfg, "restore_test_files", 5) or 5))

    @property
    def max_bytes(self) -> int:
        return max(1, int(getattr(self.cfg, "restore_test_max_mb", 1024) or 1024)) * 1024 * 1024

    @property
    def running(self) -> bool:
        return self._running.locked()

    # -- schedule ---------------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ninaivu-restore-test",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        job = self.current
        if job is not None:
            job.stop()
        for thread in (self._thread, self._worker):
            if thread and thread.is_alive():
                thread.join(timeout)

    def _loop(self) -> None:
        while not self._stop.wait(LOOK_EVERY):
            try:
                if self.due():
                    self.run(scheduled=True)
            except Exception:                                   # noqa: BLE001
                log.exception("the scheduled test restore failed to run")

    def due(self) -> bool:
        """Is a scheduled test owed, and is now a fair time for it?"""
        if not self.every_days or self.running:
            return False
        if not self.service.creds.connected:
            return False
        restoring = getattr(self.service, "_restore", None)
        if restoring is not None and restoring.running:
            return False
        if self._hold is not None and self._hold():
            return False                  # the household comes first
        last = self.last()
        if last and self._clock() - float(last["started_at"]) < self.every_days * 86400:
            return False
        conn = self._connect()
        store.init_schema(conn)
        return conn.execute("SELECT 1 FROM cloud_uploads WHERE state=? AND "
                            "remote_id != '' LIMIT 1", (store.DONE,)).fetchone() is not None

    def run_in_background(self) -> bool:
        """Start one now for somebody who asked. False if one is running."""
        if self.running:
            return False
        self._worker = threading.Thread(target=self.run, name="ninaivu-restore-test-now",
                                        daemon=True)
        self._worker.start()
        return True

    # -- one test -------------------------------------------------------------------

    def run(self, *, scheduled: bool = False) -> dict[str, Any]:
        if not self._running.acquire(blocking=False):
            return {"status": SKIPPED, "summary": "A test restore is already running."}
        started = self._clock()
        try:
            result = self._run()
        except Exception as exc:                                # noqa: BLE001
            log.exception("the test restore stopped")
            result = {"status": FAILED, "summary": f"The test stopped: {exc}",
                      "files": 0, "bytes": 0, "restored": 0, "failed": 0,
                      "matched": 0, "detail": []}
        finally:
            self.current = None
            self._running.release()
        result["started_at"] = started
        result["ended_at"] = self._clock()
        self._record(result, scheduled)
        self._tell(result)
        return result

    def _run(self) -> dict[str, Any]:
        empty = {"files": 0, "bytes": 0, "restored": 0, "failed": 0, "matched": 0,
                 "detail": []}
        if not self.service.creds.connected:
            return {**empty, "status": SKIPPED,
                    "summary": "No Google account is connected, so there is nothing to test."}
        conn = self._connect()
        items = pick(conn, self.files, self.max_bytes)
        if not items:
            return {**empty, "status": SKIPPED,
                    "summary": "Nothing has been uploaded yet, so there is nothing to test."}
        size = sum(item.size for item in items)
        key = None
        if any(item.encrypted for item in items):
            record = keyring.load(self.cfg.state_dir)
            if record is None:
                return {**empty, "files": len(items), "bytes": size, "status": FAILED,
                        "failed": len(items),
                        "summary": "These backups are encrypted and this computer no "
                                   "longer has the key. Only the recovery file or the "
                                   "passphrase can read them now — make sure one of "
                                   "them is somewhere safe."}
            key = keyring.key_material(record)[0]

        folder = Path(self.cfg.state_dir) / "restore-test" / time.strftime(
            "%Y%m%d-%H%M%S", time.localtime(self._clock()))
        try:
            job = restore.RestoreJob(connect=self.service.client, items=items,
                                     destination=folder, key=key)
            self.current = job
            job.start()
            job.join(TIME_LIMIT)
            if job.running:
                job.stop(join=True)
                return {**empty, "files": len(items), "bytes": size, "status": FAILED,
                        "failed": len(items),
                        "summary": "The test restore took more than an hour and was stopped."}
            state = job.state.snapshot()
            detail = [{"file": p["file"], "why": p["why"]} for p in state["problems"]]
            matched = 0
            for item in items:
                copy = folder.joinpath(*item.rel_path.split("/"))
                if not copy.is_file():
                    continue
                original = Path(item.root).joinpath(*item.rel_path.split("/"))
                try:
                    if original.is_file() and original.stat().st_size == item.size:
                        if _sha256(original) == _sha256(copy):
                            matched += 1
                        else:
                            detail.append({"file": item.rel_path,
                                           "why": "the backup is intact, but the copy in "
                                                  "the library no longer matches it",
                                           "warning": True})
                except OSError:
                    continue
        finally:
            shutil.rmtree(folder, ignore_errors=True)

        restored = int(state["restored"])
        failed = int(state["failed"])
        passed = failed == 0 and restored == len(items)
        if passed:
            summary = (f"All {len(items)} files came back intact"
                       + (f", and {matched} matched the library copy byte for byte."
                          if matched else "."))
        else:
            summary = (f"{failed or len(items) - restored} of {len(items)} files did not "
                       f"come back intact.")
        return {"status": PASSED if passed else FAILED, "files": len(items),
                "bytes": size, "restored": restored, "failed": failed,
                "matched": matched, "summary": summary, "detail": detail}

    # -- the record ---------------------------------------------------------------

    def _record(self, result: dict[str, Any], scheduled: bool) -> None:
        from ..storage import db                                    # noqa: PLC0415
        conn = self._connect()
        init_schema(conn)
        with db._write_lock:                                        # noqa: SLF001
            self._insert(conn, result, scheduled)
        log.info("test restore from Google Drive: %s — %s", result["status"],
                 result["summary"])
        if result["status"] == FAILED:
            log.warning("a test restore from Google Drive failed: %s", result["summary"])

    @staticmethod
    def _insert(conn, result: dict[str, Any], scheduled: bool) -> None:
        conn.execute(
            "INSERT INTO restore_tests(started_at, ended_at, status, files, bytes, "
            "restored, failed, matched, summary, detail, scheduled) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (result["started_at"], result["ended_at"], result["status"],
             result.get("files", 0), result.get("bytes", 0), result.get("restored", 0),
             result.get("failed", 0), result.get("matched", 0), result["summary"],
             json.dumps(result.get("detail", [])[:50]), int(scheduled)))
        conn.commit()

    def _tell(self, result: dict[str, Any]) -> None:
        if result["status"] != FAILED or self._notify is None:
            return
        reasons = "; ".join(f"{d['file']}: {d['why']}"
                            for d in result.get("detail", []) if not d.get("warning"))[:600]
        try:
            self._notify("restore_test", "A test restore from Google Drive failed",
                         f"{result['summary']} {reasons}".strip())
        except Exception:                                       # noqa: BLE001
            log.debug("could not report the failed test restore", exc_info=True)

    def history(self, limit: int = 10) -> list[dict[str, Any]]:
        conn = self._connect()
        init_schema(conn)
        rows = conn.execute("SELECT * FROM restore_tests ORDER BY id DESC LIMIT ?",
                            (int(limit),)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["detail"] = json.loads(item["detail"] or "[]")
            except ValueError:
                item["detail"] = []
            out.append(item)
        return out

    def last(self) -> dict[str, Any] | None:
        found = self.history(1)
        return found[0] if found else None

    def status(self) -> dict[str, Any]:
        last = self.last()
        due_at = (float(last["started_at"]) + self.every_days * 86400
                  if last and self.every_days else None)
        job = self.current
        return {
            "every_days": self.every_days,
            "files": self.files,
            "running": self.running,
            "progress": job.state.snapshot() if job is not None else None,
            "last": last,
            "next_due": due_at,
            "history": self.history(10),
        }
