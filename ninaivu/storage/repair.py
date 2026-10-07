"""Putting a damaged photograph back from one of its other copies.

The storage check (``admin_api._run_scrubber``) has always been able to say
that a file rotted: different bytes, same size, same modification time —
the one thing no editor produces. What it could not do was anything about
it, and a warning that arrives with no remedy is one a household learns to
ignore. Meanwhile Ninaivu keeps two other copies of every photograph: the
second copy on another disk and the backup in Google Drive.

So this closes the loop, file by file:

1. **Only a copy that is provably the good one.** The storage check recorded
   the SHA-256 the file had before it went wrong. A copy is used only if its
   bytes hash to exactly that; a copy made after the damage (and so damaged
   too) is refused, not trusted.
2. **The second disk first**, because reading it costs nothing; **then Drive**,
   downloaded (and decrypted, with this machine's key) into Ninaivu's state
   folder and checked before it goes anywhere near the library.
3. **Nothing is thrown away.** The damaged file is moved into
   ``<state>/repair/damaged/<date>/`` — out of the library, so it is not
   indexed as a second photograph, but kept in case the "good" copy turns out
   not to be what somebody wanted.
4. **The file keeps its time.** The copy is given the modification time the
   index knows, so the index, the second copy and the backup all still see
   the same file and nothing is uploaded or copied again for it.

A missing file is put back the same way, but only while its library folder
is there: a disk that is not plugged in is not a disk whose files are gone.
"There" is :func:`roots.root_present`, not merely a folder at the path: an
unplugged drive under a ``nofail`` mount leaves an empty folder, and the
repair used to copy the library from the second copy onto the system disk.
For the same reason a library folder most of whose files the check found
missing is not repaired at all: that is a disk that went away, not damage.

This also runs the storage check on a schedule — once every
``scrub_every_days``, started in the small hours — and repairs what it
finds straight afterwards when ``scrub_repair`` is on.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..utils.files import CHUNK, create_new, sha256_file, stays_inside, sync_folder
from . import roots as roots_kit

log = logging.getLogger(__name__)

__all__ = ["Repairer", "candidates"]

#: Hours of the night a scheduled storage check may start in (local time).
NIGHT = range(1, 6)
#: How often the schedule looks at the clock.
TICK = 15 * 60
#: The most files one repair takes on. The rest wait for the next.
MAX_FILES = 5000
#: A library folder with more than this share of its files missing is not
#: repaired: that is a drive that is away, or the wrong one mounted, and
#: "putting back" its files would write a whole library somewhere it is not.
MOST_MISSING = 0.5
#: When the storage check last read the library. A repair writes a row too,
#: and that is not a check of anything else.
LAST_PASS = "SELECT MAX(checked_at) FROM bitrot_records WHERE status != 'repaired'"



def candidates(conn: sqlite3.Connection, asset_ids: list[int] | None = None,
               limit: int = MAX_FILES) -> list[dict[str, Any]]:
    """Files whose latest check says damaged or missing, with the hash they
    should have — which is the only thing a copy can be judged against."""
    sql = ("SELECT b.asset_id, b.status, b.expected_hash, b.file_mtime, b.file_size, "
           "a.root, a.rel_path, a.mtime, a.size, a.filename FROM bitrot_records b "
           "JOIN assets a ON a.id = b.asset_id "
           "WHERE b.id = (SELECT MAX(b2.id) FROM bitrot_records b2 WHERE b2.asset_id = b.asset_id) "
           "AND b.status IN ('corrupt', 'missing') AND COALESCE(b.expected_hash, '') != '' "
           "AND a.trashed = 0")
    params: list[Any] = []
    if asset_ids:
        sql += f" AND b.asset_id IN ({','.join('?' * len(asset_ids))})"
        params += [int(i) for i in asset_ids]
    sql += " ORDER BY b.asset_id LIMIT ?"
    params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params)]


class Repairer:
    """Repairs from the second copy or Drive, and runs the check on a schedule."""

    def __init__(self, cfg, connect_db: Callable[[], sqlite3.Connection], *,
                 mirror=None, cloud=None, scanner=None,
                 start_check: Callable[..., bool] | None = None,
                 check_running: Callable[[], bool] | None = None,
                 notify: Callable[[str, str, str], None] | None = None,
                 clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self._connect = connect_db
        self.mirror = mirror
        self.cloud = cloud
        self.scanner = scanner
        self._start_check = start_check
        self._check_running = check_running or (lambda: False)
        self._notify = notify
        self._clock = clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        #: The Drive download a repair is waiting on, so Stop can stop it too.
        self._fetching = None
        #: The schedule's own stop, apart from a repair's: stopping a repair
        #: must not end the nightly check until the next restart.
        self._asleep = threading.Event()
        self._thread: threading.Thread | None = None
        self._timer: threading.Thread | None = None
        self._state: dict[str, Any] = {
            "running": False, "total": 0, "done": 0, "repaired": 0, "failed": 0,
            "current": "", "message": "", "problems": [], "finished_at": 0.0,
        }

    # -- settings ---------------------------------------------------------

    @property
    def every(self) -> float:
        return max(0.0, float(getattr(self.cfg, "scrub_every_days", 30) or 0)) * 86400

    @property
    def automatic(self) -> bool:
        return bool(getattr(self.cfg, "scrub_repair", True))

    def damaged_folder(self) -> Path:
        return Path(self.cfg.state_dir) / "repair" / "damaged"

    # -- asking -----------------------------------------------------------

    def status(self) -> dict[str, Any]:
        with self._lock:
            snap = dict(self._state, problems=list(self._state["problems"]))
        conn = self._connect()           # this thread's own, kept open (db.connect)
        try:
            snap["waiting"] = len(candidates(conn))
            last = conn.execute(LAST_PASS).fetchone()[0]
        except sqlite3.OperationalError:
            snap["waiting"], last = 0, None
        snap.update(
            every_days=int(getattr(self.cfg, "scrub_every_days", 30) or 0),
            automatic=self.automatic,
            last_checked_at=last,
            next_check_at=(float(last) + self.every) if (last and self.every) else None,
            sources=self._sources(),
        )
        return snap

    def _sources(self) -> list[str]:
        found = []
        folder = getattr(self.mirror, "folder", None) if self.mirror is not None else None
        if folder is not None and Path(folder).is_dir():
            found.append("the second copy")
        creds = getattr(self.cloud, "creds", None)
        if creds is not None and getattr(creds, "connected", False):
            found.append("Google Drive")
        return found

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # -- repairing --------------------------------------------------------

    def start(self, asset_ids: list[int] | None = None) -> dict[str, Any]:
        with self._lock:
            if self.running:
                raise ValueError("A repair is already running.")
            self._state.update(running=True, total=0, done=0, repaired=0, failed=0,
                               current="", message="Starting…", problems=[])
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, args=(asset_ids,),
                                            name="ninaivu-repair", daemon=True)
            self._thread.start()
        return self.status()

    def stop(self, join: bool = False) -> None:
        """Stop the repair in progress; with *join*, at shutdown, the schedule too."""
        self._stop.set()
        fetching = getattr(self, "_fetching", None)
        if fetching is not None:
            fetching.stop()
        if join:
            self._asleep.set()
            for thread in (self._thread, self._timer):
                if thread is not None and thread is not threading.current_thread():
                    thread.join(10)

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def _update(self, **fields: Any) -> None:
        with self._lock:
            self._state.update(fields)

    def _problem(self, name: str, why: str) -> None:
        with self._lock:
            self._state["failed"] += 1
            if len(self._state["problems"]) < 50:
                self._state["problems"].append({"file": name, "why": why})

    def _run(self, asset_ids: list[int] | None) -> None:
        repaired = 0
        message = ""
        try:
            conn = self._connect()
            wanted = candidates(conn, asset_ids)
            self._update(total=len(wanted))
            refused = self._roots_not_to_touch(conn, {str(r["root"]) for r in wanted})
            for row in wanted:
                if self._stop.is_set():
                    message = "Stopped."
                    break
                self._update(current=row["rel_path"])
                why = refused.get(str(row["root"]))
                if why:
                    self._problem(row["rel_path"], why)
                    with self._lock:
                        self._state["done"] += 1
                    continue
                try:
                    source = self._repair_one(conn, row)
                except (OSError, ValueError, RuntimeError) as exc:
                    log.warning("could not repair %s: %s", row["rel_path"], exc)
                    self._problem(row["rel_path"], str(exc))
                else:
                    repaired += 1
                    self._update(repaired=repaired)
                    log.info("repaired %s from %s", row["rel_path"], source)
                with self._lock:
                    self._state["done"] += 1
            snap = self.status()
            if not message:
                message = (f"Repaired {repaired:,} file{'' if repaired == 1 else 's'}"
                           + (f"; {snap['failed']:,} could not be" if snap["failed"] else "")
                           + "." if snap["total"] else "Nothing needed repairing.")
            if repaired and self._notify is not None:
                self._notify("integrity", f"{repaired} damaged file(s) repaired",
                             f"{repaired} file(s) the storage check found damaged or missing "
                             "were put back from a copy that matched them exactly. The damaged "
                             f"ones are kept in {self.damaged_folder()}.")
        except Exception as exc:                            # noqa: BLE001
            log.exception("the repair stopped")
            message = f"The repair stopped: {exc}"
        finally:
            self._update(running=False, current="", message=message,
                         finished_at=self._clock())

    def _roots_not_to_touch(self, conn: sqlite3.Connection, roots: set[str]) -> dict[str, str]:
        """Library folders this repair must not write into, with why."""
        refused: dict[str, str] = {}
        for root in roots:
            if not roots_kit.root_present(root):
                refused[root] = "its library folder is not there — is the disk plugged in?"
                continue
            total, missing = conn.execute(
                "SELECT COUNT(*), SUM(CASE WHEN (SELECT b.status FROM bitrot_records b "
                "WHERE b.asset_id = a.id ORDER BY b.id DESC LIMIT 1) = 'missing' "
                "THEN 1 ELSE 0 END) FROM assets a WHERE a.root = ? AND a.trashed = 0",
                (root,)).fetchone()
            if total and (missing or 0) > total * MOST_MISSING:
                refused[root] = (f"{missing:,} of the {total:,} files in {root} are missing, "
                                 "which looks like a disk that is away rather than damage; "
                                 "nothing was written there")
        return refused

    def _repair_one(self, conn: sqlite3.Connection, row: dict[str, Any]) -> str:
        root, rel = str(row["root"]), str(row["rel_path"])
        target = Path(root).joinpath(*rel.replace("\\", "/").split("/"))
        if not roots_kit.root_present(root):
            raise ValueError("its library folder is not there — is the disk plugged in?")
        if not stays_inside(root, target):
            raise ValueError("refusing a path outside the library")
        good = str(row["expected_hash"])
        if row["status"] == "missing" and target.exists():
            raise ValueError("it is back where it was; the next check will see it")
        if target.is_file() and sha256_file(target) == good:
            # Put right by somebody else since the check.
            self._record(conn, row, target, good)
            return "the file itself"

        source, where, cleanup = self._find_copy(row, good)
        try:
            self._put_back(source, target, row, good)
        finally:
            if cleanup is not None:
                cleanup()
        self._record(conn, row, target, good)
        return where

    def _find_copy(self, row: dict[str, Any], good: str) -> tuple[Path, str, Callable[[], None] | None]:
        tried = []
        folder = getattr(self.mirror, "folder", None) if self.mirror is not None else None
        if folder is not None and Path(folder).is_dir():
            path = self.mirror.copy_path(row["root"], row["rel_path"])
            if path.is_file():
                if sha256_file(path) == good:
                    return path, "the second copy", None
                tried.append("the second copy's is different too")
            else:
                tried.append("the second copy does not have it")
        found = self._from_drive(row, good, tried)
        if found is not None:
            return found
        raise ValueError("no copy matches what it was: " + "; ".join(tried or ["no other copy is set up"]))

    def _from_drive(self, row: dict[str, Any], good: str,
                    tried: list[str]) -> tuple[Path, str, Callable[[], None]] | None:
        cloud = self.cloud
        creds = getattr(cloud, "creds", None)
        if cloud is None or creds is None or not getattr(creds, "connected", False):
            return None
        from ..cloud import restore                         # noqa: PLC0415
        items = restore.from_record(self._connect(), roots=[row["root"]],
                                    asset_ids=[int(row["asset_id"])])
        if not items:
            tried.append("it is not in the Drive backup")
            return None
        try:
            key = cloud.restore_key(items)
        except ValueError as exc:
            tried.append(f"Drive: {exc}")
            return None
        staging = Path(self.cfg.state_dir) / "repair" / f"from-drive-{int(self._clock() * 1000)}"
        staging.mkdir(parents=True, exist_ok=True)
        item = items[0]
        item.rel_path = "file" + Path(item.rel_path).suffix
        item.mtime = 0.0
        job = restore.RestoreJob(connect=cloud.client, items=[item], destination=staging, key=key)
        self._fetching = job
        try:
            job.start()
            # In slices, so Stop is heard: waiting out the download whole kept
            # a stopped repair "running" for up to an hour, and a new one
            # could not start until it ended.
            deadline = self._clock() + 3600
            while job.running and self._clock() < deadline:
                if self._stop.is_set():
                    job.stop(join=True)
                    break
                job.join(0.5)
        finally:
            self._fetching = None
        done = staging / item.rel_path

        def cleanup() -> None:
            shutil.rmtree(staging, ignore_errors=True)

        if not done.is_file():
            snap = job.state.snapshot()
            why = (snap.get("problems") or [{}])[0].get("why") or snap.get("message") or "it did not arrive"
            tried.append(f"Drive: {why}")
            cleanup()
            return None
        if sha256_file(done) != good:
            tried.append("the Drive copy is different too")
            cleanup()
            return None
        return done, "Google Drive", cleanup

    def _put_back(self, source: Path, target: Path, row: dict[str, Any], good: str) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f".{target.name}.ninaivu-repair")
        digest = hashlib.sha256()
        keep_temp = False
        try:
            with open(source, "rb") as src, create_new(temp) as out:
                while chunk := src.read(CHUNK):
                    out.write(chunk)
                    digest.update(chunk)
                out.flush()
                os.fsync(out.fileno())
            if digest.hexdigest() != good:
                raise ValueError("the copy changed while it was being read")
            when = float(row.get("file_mtime") or row.get("mtime") or 0)
            if when:
                os.utime(temp, (when, when))
            kept = None
            if target.exists():
                kept = self._set_aside(target, row)
                log.info("the damaged %s is kept at %s", target, kept)
            try:
                os.replace(temp, target)
            except OSError:
                # The damaged file has already been moved out. Deleting the
                # good copy as well left the library with neither: put the
                # damaged one back, and if even that fails keep the good one
                # under its temporary name rather than lose both.
                if kept is not None:
                    try:
                        shutil.move(str(kept), str(target))
                    except OSError:
                        keep_temp = True
                        log.error("could not put %s back; the good copy is kept at %s "
                                  "and the damaged one at %s", target, temp, kept)
                raise
            sync_folder(target.parent)
        finally:
            if not keep_temp:
                temp.unlink(missing_ok=True)

    def _set_aside(self, target: Path, row: dict[str, Any]) -> Path:
        day = time.strftime("%Y-%m-%d", time.localtime(self._clock()))
        base = self.damaged_folder() / day / (Path(row["root"]).name or "library")
        dest = base.joinpath(*str(row["rel_path"]).replace("\\", "/").split("/"))
        n = 1
        while dest.exists():
            n += 1
            dest = dest.with_name(f"{dest.stem} ({n}){dest.suffix}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(target, dest)
        except OSError:
            # Another disk than the state folder's: copy, then remove.
            shutil.copy2(target, dest)
            target.unlink()
        return dest

    def _record(self, conn: sqlite3.Connection, row: dict[str, Any], target: Path, good: str) -> None:
        from . import db                                     # noqa: PLC0415
        stat = target.stat()
        db.record_bitrot_check(conn, int(row["asset_id"]), row["root"], row["rel_path"],
                               good, good, "repaired", stat.st_mtime, stat.st_size)

    # -- the schedule -----------------------------------------------------

    def keep(self) -> None:
        """Watch the clock for the next scheduled storage check."""
        if self._timer is not None and self._timer.is_alive():
            return
        self._asleep.clear()
        self._timer = threading.Thread(target=self._loop, name="ninaivu-check-schedule",
                                       daemon=True)
        self._timer.start()

    def _loop(self) -> None:
        while not self._asleep.wait(TICK):
            try:
                self.tick()
            except Exception:                               # noqa: BLE001
                log.exception("the storage check schedule")

    def check_due(self) -> bool:
        if not self.every or self._start_check is None or self._check_running():
            return False
        if time.localtime(self._clock()).tm_hour not in NIGHT:
            return False
        try:
            last = self._connect().execute(LAST_PASS).fetchone()[0]
        except sqlite3.OperationalError:
            last = None
        return last is None or self._clock() - float(last) >= self.every

    def tick(self) -> bool:
        """Start the check if it is due. True when one was started."""
        if not self.check_due():
            return False
        assert self._start_check is not None
        return bool(self._start_check(on_done=self.after_check))

    def after_check(self) -> None:
        """What a finished check found damaged, repaired, when that is wanted."""
        if not self.automatic or self.running:
            return
        owed = candidates(self._connect(), limit=1)
        if owed and self._sources():
            try:
                self.start()
            except ValueError:
                pass
