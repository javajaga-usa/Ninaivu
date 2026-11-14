"""The week, for whoever looks after the library: the weekly health digest.

"Is everything safe?" (server/safety.py) answers for this minute. What it
cannot say is what happened while nobody was looking: the storage check that
found two photographs changed on Tuesday, the faces that could not be read on
Thursday, the disk that lost 40 GB since last Sunday, the archive drive that
was unplugged all week. Each of those is on a page of its own, and the
administrator who opens the console once a week had to visit every one to
find out.

This gathers the last seven days into four parts, each with one of the
safety page's words (``ok``, ``attention``, ``problem``):

* **damaged** — files the storage check found changed or unreadable this
  week, files that could not be opened when they were added, and errors the
  library scans met;
* **space** — the free room on each disk the library and the index are on,
  what it was a week ago, and how long the library's own rate leaves;
* **jobs** — work that failed this week: photographs the job engine set
  aside, archive runs, test restores and scans that did not finish well,
  and background work that is failing now;
* **drives** — library folders that are not connected, and drives logging
  errors (where the computer records them).

It does no work of its own and changes nothing but its own small file
(``health-week.json`` in the state folder): a reading of the free space every
few hours, so a week from now there is something to compare with, and the
digest of each finished week, kept for :data:`KEEP_WEEKS` weeks. When a week
turns, the digest of the week just ended is kept and, if the household set a
notification address, sent there in counts only: never a photograph, a path
or a name, the notifications' promise (utils/notify.py).

Shown on the console's Overview and, to administrators only, at the top of
the family app (``GET /api/health/week``).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from ..words import filled, said

log = logging.getLogger(__name__)

OK, ATTENTION, PROBLEM = "ok", "attention", "problem"
_ORDER = {PROBLEM: 0, ATTENTION: 1, OK: 2}

DAY = 86400.0
WEEK = 7 * DAY

#: The file beside the index that holds the readings and the kept weeks.
FILE_NAME = "health-week.json"
#: How many finished weeks are kept.
KEEP_WEEKS = 12
#: Readings of the free space older than this are dropped.
KEEP_READINGS = 60 * DAY
#: Hours between two readings of the free space (the keeper's round).
READ_EVERY = 6 * 3600.0
#: A reading counts as "a week ago" when it is within this of seven days.
WEEK_AGO_SLACK = 2 * DAY
#: How long an answer is kept for the pages that ask again.
KEEP = 300.0

#: Free space below either of these is worth acting on now; the drive
#: watch's own thresholds (storage/disk_health.py LOW_BYTES, LOW_FRACTION).
LOW_BYTES = 20 * 1024 ** 3
LOW_FRACTION = 0.05
#: Below this share the space is worth a look.
TIGHT_FRACTION = 0.10

#: The job engine's work, as a person calls it (engine/derivations.py).
ENGINE_KINDS = {
    "text": said("Reading words in photographs"),
    "faces": said("Finding faces"),
    "covers": said("Pictures for videos"),
    "keyframes": said("Frames from videos"),
    "fingerprints": said("Fingerprints of the originals"),
}

#: The notification event a finished week is sent as (utils/notify.py).
EVENT = "weekly_health"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _line(key: str, **params: Any) -> dict[str, Any]:
    """One sentence, sent as the console translates it: a ``said`` template
    and its numbers, and filled in for anything that reads it as it is."""
    return {"key": key, "params": params, "text": filled(key, params)}


def _tb(n: float) -> str:
    """Bytes in the largest unit that keeps the number short."""
    n = float(n)
    for unit in ("bytes", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{int(n)} bytes" if unit == "bytes" else f"{n:.1f} {unit}" if abs(n) < 100 \
                else f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _count(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    try:
        row = conn.execute(sql, params).fetchone()
    except sqlite3.Error:
        return 0
    return int((row[0] if row else 0) or 0)


def _folder_name(path: str) -> str:
    """What a person calls a folder: its last part, not the whole path."""
    name = Path(str(path)).name
    return name or str(path)


def _part(part_id: str, title: str, status: str, lines: list[dict[str, Any]], page: str,
          numbers: dict[str, Any], items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"id": part_id, "title": title, "status": status, "lines": lines,
            "summary": " ".join(line["text"] for line in lines), "page": page,
            "numbers": numbers, "items": items or []}


def week_start(now: float) -> float:
    """Midnight at the start of the Monday of *now*'s week, this computer's time."""
    day = datetime.fromtimestamp(now)
    monday = (day - timedelta(days=day.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return monday.timestamp()


# ---------------------------------------------------------------------------
# The four parts
# ---------------------------------------------------------------------------

def damaged(services, conn: sqlite3.Connection, since: float, now: float) -> dict[str, Any]:
    """Files found damaged: by the storage check, when they were added, or by a scan."""
    from ..storage import db                                        # noqa: PLC0415

    found_now = _count(
        conn, "SELECT COUNT(DISTINCT asset_id) FROM bitrot_records WHERE asset_id IS NOT NULL "
              "AND checked_at >= ? AND status IN ('corrupt', 'unreadable')", (since,))
    try:
        summary = db.get_bitrot_summary(conn)
    except sqlite3.Error:
        summary = {}
    still = int(summary.get("corrupt", 0) or 0) + int(summary.get("unreadable", 0) or 0)
    missing = int(summary.get("missing", 0) or 0)
    # A photograph or a video that could not be opened when it was added has
    # no picture made from it (media/scanner.py _retry_unread).
    unopened_where = ("trashed=0 AND kind IN ('picture', 'video') AND thumb IS NULL")
    unopened_week = _count(
        conn, f"SELECT COUNT(*) FROM assets WHERE {unopened_where} "
              "AND COALESCE(added_at, indexed_at) >= ?", (since,))
    unopened = _count(conn, f"SELECT COUNT(*) FROM assets WHERE {unopened_where}")
    scan_errors = _count(conn, "SELECT SUM(errors) FROM scan_runs WHERE started_at >= ?",
                         (since,))

    lines = []
    if found_now:
        lines.append(_line(said("The storage check found {count} files changed or unreadable this week."),
                           count=f"{found_now:,}") if found_now != 1 else
                     _line(said("The storage check found 1 file changed or unreadable this week.")))
    if unopened_week:
        lines.append(_line(said("{count} files added this week could not be opened."),
                           count=f"{unopened_week:,}") if unopened_week != 1 else
                     _line(said("1 file added this week could not be opened.")))
    if scan_errors:
        lines.append(_line(said("Library scans met {count} errors this week."),
                           count=f"{scan_errors:,}") if scan_errors != 1 else
                     _line(said("Library scans met 1 error this week.")))
    if not lines:
        lines.append(_line(said("Nothing new was found damaged this week.")))
    if still and still != found_now:
        lines.append(_line(said("{count} damaged files are still waiting to be put back."),
                           count=f"{still:,}"))
    if missing:
        lines.append(_line(said("{count} files were missing at the last storage check."),
                           count=f"{missing:,}"))

    if found_now:
        status = PROBLEM
    elif still or unopened_week or scan_errors or missing:
        status = ATTENTION
    else:
        status = OK
    return _part("damaged", said("Damaged files"), status, lines, "health", {
        "found_this_week": found_now, "still_damaged": still, "missing": missing,
        "unopened_this_week": unopened_week, "unopened": unopened,
        "scan_errors": scan_errors})


def space(services, conn: sqlite3.Connection, since: float, now: float,
          week_ago: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """The free room on each disk the library or the index is on, and its week."""
    disks = disks_now(services, conn, now)
    week_ago = week_ago or {}
    worst = OK
    lines = []
    items = []
    for disk in disks:
        free, total = disk["free_bytes"], disk["total_bytes"]
        before = week_ago.get(disk["key"])
        change = (free - int(before["free_bytes"])) if before else None
        share = (free / total) if total else 1.0
        months = disk.get("months_left")
        if free < LOW_BYTES or share < LOW_FRACTION or (months is not None and months < 1):
            state = PROBLEM
        elif share < TIGHT_FRACTION or (months is not None and months < 6):
            state = ATTENTION
        else:
            state = OK
        worst = min(worst, state, key=lambda s: _ORDER[s])
        items.append({**disk, "status": state, "change_bytes": change})
        params = {"name": disk["name"], "free": _tb(free), "total": _tb(total)}
        if change is None:
            lines.append(_line(said("{name}: {free} free of {total}."), **params))
        elif change < 0:
            lines.append(_line(said("{name}: {free} free of {total}, {change} less than a week ago."),
                               **params, change=_tb(-change)))
        elif change > 0:
            lines.append(_line(said("{name}: {free} free of {total}, {change} more than a week ago."),
                               **params, change=_tb(change)))
        else:
            lines.append(_line(said("{name}: {free} free of {total}, the same as a week ago."),
                               **params))
        if months is not None and months <= 120:
            lines.append(_line(said("At this rate it is full in about {months} months."),
                               months=f"{months:.0f}"))
    if not disks:
        lines.append(_line(said("No library disk is connected to measure.")))
        worst = ATTENTION if services.cfg.library_roots else OK
    return _part("space", said("Free space"), worst, lines, "health",
                 {"disks": len(disks)}, items)


def disks_now(services, conn: sqlite3.Connection, now: float) -> list[dict[str, Any]]:
    """Every disk the library folders and the index are on, once each."""
    import shutil                                                   # noqa: PLC0415

    from ..storage import roots as roots_kit, space as space_kit   # noqa: PLC0415

    cfg = services.cfg
    roots = [str(r) for r in cfg.library_roots]
    here = [r for r in roots if r not in set(roots_kit.unavailable(roots))]
    found: dict[Any, dict[str, Any]] = {}
    try:
        forecast = space_kit.forecast(conn, here, now=now)["disks"] if here else []
    except (sqlite3.Error, OSError):
        forecast = []
    for disk in forecast:
        try:
            device = os.stat(disk["roots"][0]).st_dev
        except OSError:
            continue
        found[device] = {"key": disk["roots"][0], "name": _folder_name(disk["roots"][0]),
                         "for": "library", "free_bytes": int(disk["free_bytes"]),
                         "total_bytes": int(disk["total_bytes"]),
                         "months_left": disk.get("months_left")}
    for root in here:
        # A disk the forecast left out (it had no history yet).
        try:
            device = os.stat(root).st_dev
            if device in found:
                continue
            usage = shutil.disk_usage(root)
        except OSError:
            continue
        found[device] = {"key": root, "name": _folder_name(root), "for": "library",
                         "free_bytes": int(usage.free), "total_bytes": int(usage.total),
                         "months_left": None}
    state_dir = str(getattr(cfg, "state_dir", "") or "")
    if state_dir:
        try:
            device = os.stat(state_dir).st_dev
            if device not in found:
                usage = shutil.disk_usage(state_dir)
                found[device] = {"key": state_dir, "name": said("Ninaivu's own records"),
                                 "for": "index", "free_bytes": int(usage.free),
                                 "total_bytes": int(usage.total), "months_left": None}
        except OSError:
            pass
    return list(found.values())


def _engine_failed(cfg: Any, since: float) -> dict[str, int]:
    """Photographs the job engine set aside this week, by kind. Read from
    engine.db directly, as server/job_status.py does."""
    from ..engine.engine import database_path                      # noqa: PLC0415

    path = database_path(cfg)
    if not path.is_file():
        return {}
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2) as conn:
            return {str(kind): int(n) for kind, n in conn.execute(
                "SELECT kind, COUNT(*) FROM tasks WHERE state='failed' AND updated_at >= ? "
                "GROUP BY kind", (since,))}
    except sqlite3.Error:
        return {}


def _archive_runs(since: float) -> list[dict[str, Any]]:
    """Archive runs that ended this week, other than dry runs."""
    try:
        from ..archive import database as archive_db                # noqa: PLC0415
        rows = archive_db.get_db().execute(
            "SELECT * FROM jobs WHERE COALESCE(ended_at, started_at) >= ? "
            "ORDER BY id DESC", (since,)).fetchall()
    except Exception:                                               # noqa: BLE001 - not set up here
        return []
    return [dict(r) for r in rows if (dict(r).get("mode") or "") != "dry-run"]


def jobs(services, conn: sqlite3.Connection, since: float, now: float) -> dict[str, Any]:
    """Work that failed this week, and background work failing now."""
    cfg = services.cfg
    items: list[dict[str, Any]] = []
    status = OK

    engine = _engine_failed(cfg, since)
    for kind, n in sorted(engine.items(), key=lambda kv: -kv[1]):
        items.append({"kind": "engine", "title": ENGINE_KINDS.get(kind, kind), "count": n})

    archive_bad = archive_incomplete = 0
    for run in _archive_runs(since):
        message = str(run.get("message") or "")
        if message.startswith("INCOMPLETE"):
            archive_incomplete += 1
        elif str(run.get("state") or "") in ("failed", "interrupted"):
            archive_bad += 1
    if archive_incomplete:
        status = PROBLEM
        items.append({"kind": "archive", "title": said("Archive runs that could not read part of their source"),
                      "count": archive_incomplete})
    if archive_bad:
        items.append({"kind": "archive", "title": said("Archive runs that failed or were interrupted"),
                      "count": archive_bad})

    tester = getattr(services, "restore_tests", None)
    restores_failed = 0
    if tester is not None:
        try:
            restores_failed = sum(1 for t in tester.history(20)
                                  if float(t.get("started_at") or 0) >= since
                                  and t.get("status") == "failed")
        except Exception:                                           # noqa: BLE001
            log.debug("could not read the test restores", exc_info=True)
    if restores_failed:
        status = PROBLEM
        items.append({"kind": "restore", "title": said("Test restores that did not come back intact"),
                      "count": restores_failed})

    scans_failed = _count(conn, "SELECT COUNT(*) FROM scan_runs WHERE started_at >= ? "
                                "AND status='error'", (since,))
    if scans_failed:
        items.append({"kind": "scan", "title": said("Library scans that stopped with an error"),
                      "count": scans_failed})

    supervisor = getattr(services, "supervisor", None)
    failing_now = []
    if supervisor is not None:
        from .supervisor import RETRYING                            # noqa: PLC0415
        try:
            failing_now = [row for row in supervisor.activity() if row["state"] == RETRYING]
        except Exception:                                           # noqa: BLE001
            log.debug("could not ask the supervisor", exc_info=True)
    if failing_now:
        items.append({"kind": "background", "title": said("Background work failing right now"),
                      "count": len(failing_now),
                      "names": [row["name"] for row in failing_now[:5]]})

    failed = sum(int(item["count"]) for item in items)
    if not items:
        lines = [_line(said("Nothing failed this week."))]
    else:
        status = status if status == PROBLEM else ATTENTION
        lines = [_line(said("{title}: {count}."), title=item["title"], count=f"{item['count']:,}")
                 for item in items]
    return _part("jobs", said("Failed jobs"), status, lines, "activity", {
        "failed": failed, "engine": engine, "archive_failed": archive_bad,
        "archive_incomplete": archive_incomplete, "restores_failed": restores_failed,
        "scans_failed": scans_failed, "failing_now": len(failing_now)}, items)


def drives(services, conn: sqlite3.Connection, since: float, now: float) -> dict[str, Any]:
    """Library folders not connected, and drives logging errors."""
    from ..storage import roots as roots_kit                       # noqa: PLC0415

    roots = [str(r) for r in services.cfg.library_roots]
    away = roots_kit.unavailable(roots)
    items: list[dict[str, Any]] = [{"name": _folder_name(r), "state": "away"} for r in away]
    lines = []
    status = OK
    if away:
        status = ATTENTION
        lines.append(_line(said("{count} of {total} library folders are not connected: {names}."),
                           count=len(away), total=len(roots),
                           names=", ".join(_folder_name(r) for r in away[:4])))
    elif roots:
        lines.append(_line(said("All {count} library folders are connected."), count=len(roots))
                     if len(roots) != 1 else _line(said("The library folder is connected.")))

    watch = getattr(services, "disks", None)
    report: dict[str, Any] = {}
    if watch is not None:
        try:
            report = watch.report()
        except Exception:                                           # noqa: BLE001
            log.debug("could not read the drive watch", exc_info=True)
    failing = [d for d in report.get("drives", []) if d.get("status") == "critical"]
    watching = [d for d in report.get("drives", []) if d.get("status") == "warning"]
    for drive in failing:
        items.append({"name": drive["name"], "state": "failing"})
    for drive in watching:
        items.append({"name": drive["name"], "state": "warning"})
    if failing:
        status = PROBLEM
        lines.append(_line(said("{count} drives may be failing: {names}."), count=len(failing),
                           names=", ".join(d["name"] for d in failing[:3])))
    if watching:
        status = status if status == PROBLEM else ATTENTION
        lines.append(_line(said("{count} drives logged errors worth watching: {names}."),
                           count=len(watching), names=", ".join(d["name"] for d in watching[:3])))
    if report.get("supported") and not failing and not watching:
        lines.append(_line(said("No drive logged an error this week.")))
    if not lines:
        lines.append(_line(said("There is no library folder yet.")))
    return _part("drives", said("Drives"), status, lines, "health", {
        "library_folders": len(roots), "away": len(away), "failing": len(failing),
        "warning": len(watching), "watched": bool(report.get("supported"))}, items)


PARTS: list[Callable[..., dict[str, Any]]] = [damaged, space, jobs, drives]
_TITLES = {"damaged": said("Damaged files"), "space": said("Free space"),
           "jobs": said("Failed jobs"), "drives": said("Drives")}


# ---------------------------------------------------------------------------
# The digest, and the keeper of its file
# ---------------------------------------------------------------------------

def build(services, *, since: float, now: float,
          week_ago: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """The digest of *since* to *now*. One part that cannot answer says so
    rather than taking the rest with it."""
    from ..storage import db                                        # noqa: PLC0415

    conn = db.connect(services.cfg.db_path)
    parts = []
    for part in PARTS:
        try:
            if part is space:
                parts.append(space(services, conn, since, now, week_ago))
            else:
                parts.append(part(services, conn, since, now))
        except Exception as exc:                                    # noqa: BLE001
            log.debug("weekly health part %s failed", part.__name__, exc_info=True)
            parts.append(_part(part.__name__, _TITLES.get(part.__name__, part.__name__),
                               ATTENTION, [_line(said("Could not tell."))], "health",
                               {"error": str(exc)[:200]}))
    problems = sum(p["status"] == PROBLEM for p in parts)
    attention = sum(p["status"] == ATTENTION for p in parts)
    if problems:
        verdict = PROBLEM
        key = (said("A week with 1 thing to act on.") if problems == 1
               else said("A week with {problems} things to act on."))
    elif attention:
        verdict = ATTENTION
        key = (said("A good week, with 1 thing to look at.") if attention == 1
               else said("A good week, with {attention} things to look at."))
    else:
        verdict = OK
        key = said("A quiet week: nothing damaged, nothing failed.")
    params = {"problems": problems, "attention": attention}
    return {"since": since, "until": now, "verdict": verdict,
            "headline": filled(key, params), "headline_key": key, "headline_params": params,
            "parts": parts}


def message(digest: dict[str, Any]) -> tuple[str, str]:
    """The notification for a finished week: counts only, never a path or a
    name (utils/notify.py promises that)."""
    numbers = {p["id"]: p["numbers"] for p in digest["parts"]}
    damaged_n = numbers.get("damaged", {})
    jobs_n = numbers.get("jobs", {})
    drives_n = numbers.get("drives", {})
    space_part = next((p for p in digest["parts"] if p["id"] == "space"), {"items": []})
    tight = sum(1 for d in space_part.get("items", []) if d.get("status") != OK)
    detail = "\n".join([
        f"Damaged files found: {int(damaged_n.get('found_this_week', 0)) + int(damaged_n.get('unopened_this_week', 0)):,}",
        f"Failed jobs: {int(jobs_n.get('failed', 0)):,}",
        f"Disks short of space: {tight}",
        f"Library folders not connected: {int(drives_n.get('away', 0))}; "
        f"drives logging errors: {int(drives_n.get('failing', 0)) + int(drives_n.get('warning', 0))}",
        "The console's Overview has the whole week.",
    ])
    return f"Ninaivu's week: {digest['headline']}", detail


class HealthWeek:
    """Keeps the readings and the finished weeks, and answers the pages."""

    JOB = "health-week"

    def __init__(self, services, *, clock: Callable[[], float] = time.time,
                 notify: Callable[[str, str, str], None] | None = None) -> None:
        self.services = services
        self._clock = clock
        self._notify = notify
        self._lock = threading.Lock()
        self._kept: tuple[float, dict[str, Any]] | None = None
        #: The supervisor its round runs on; Ninaivu gives it its own.
        self.supervisor = None

    # -- the file ------------------------------------------------------------
    @property
    def path(self) -> Path:
        return Path(self.services.cfg.state_dir) / FILE_NAME

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"readings": [], "weeks": []}
        if not isinstance(data, dict):
            return {"readings": [], "weeks": []}
        data.setdefault("readings", [])
        data.setdefault("weeks", [])
        return data

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        spare = self.path.with_name(FILE_NAME + ".tmp")
        spare.write_text(json.dumps(data), encoding="utf-8")
        os.replace(spare, self.path)

    @staticmethod
    def _week_ago(data: dict[str, Any], now: float) -> dict[str, dict[str, Any]]:
        """The reading nearest to a week before *now*, per disk."""
        target = now - WEEK
        best = None
        for reading in data.get("readings", []):
            gap = abs(float(reading.get("at", 0)) - target)
            if gap <= WEEK_AGO_SLACK and (best is None or gap < best[0]):
                best = (gap, reading)
        return dict(best[1].get("disks", {})) if best else {}

    # -- the round -------------------------------------------------------------
    def tick(self) -> dict[str, Any] | None:
        """One round: note the free space, and keep (and send) the week just
        ended if it has not been. Returns that week's digest if it was kept."""
        from ..storage import db                                    # noqa: PLC0415

        with self._lock:
            now = self._clock()
            data = self._load()
            conn = db.connect(self.services.cfg.db_path)
            disks = {d["key"]: {"free_bytes": d["free_bytes"], "total_bytes": d["total_bytes"]}
                     for d in disks_now(self.services, conn, now)}
            data["readings"] = [r for r in data["readings"]
                                if now - float(r.get("at", 0)) <= KEEP_READINGS]
            data["readings"].append({"at": now, "disks": disks})
            kept = None
            start = week_start(now)
            last = max((float(w.get("until", 0)) for w in data["weeks"]), default=0.0)
            if last < start - DAY:
                # The week that ended at the start of this one: kept once.
                kept = build(self.services, since=start - WEEK, now=start,
                             week_ago=self._week_ago(data, start))
                kept["kept_at"] = now
                data["weeks"] = ([kept] + data["weeks"])[:KEEP_WEEKS]
            self._save(data)
            self._kept = None
        if kept is not None and self._notify is not None:
            try:
                summary, detail = message(kept)
                self._notify(EVENT, summary, detail)
            except Exception:                                       # noqa: BLE001
                log.debug("could not send the week", exc_info=True)
        return kept

    def start(self) -> None:
        from .supervisor import Job, every, supervisor_of           # noqa: PLC0415
        supervisor_of(self).add(Job(
            self.JOB, self.tick, every(READ_EVERY, first=120.0), log=log,
            failure="could not note the week's health"))

    def stop(self, timeout: float = 5.0) -> None:
        from .supervisor import supervisor_of                       # noqa: PLC0415
        supervisor_of(self).stop_job(self.JOB, timeout)

    # -- the answer --------------------------------------------------------------
    def report(self, fresh: bool = False) -> dict[str, Any]:
        """The last seven days, and the weeks kept before them."""
        with self._lock:
            now = self._clock()
            if not fresh and self._kept and now - self._kept[0] < KEEP:
                return self._kept[1]
            data = self._load()
            current = build(self.services, since=now - WEEK, now=now,
                            week_ago=self._week_ago(data, now))
            weeks = [{"since": w.get("since"), "until": w.get("until"),
                      "verdict": w.get("verdict"), "headline": w.get("headline"),
                      "headline_key": w.get("headline_key"),
                      "headline_params": w.get("headline_params") or {},
                      "parts": w.get("parts") or []}
                     for w in data["weeks"]]
            answer = {"current": current, "weeks": weeks, "at": now}
            self._kept = (now, answer)
            return answer
