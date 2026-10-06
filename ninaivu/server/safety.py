"""Is everything safe? One answer, from every protection Ninaivu has.

Ninaivu now keeps the library safe in seven different ways — a copy in Google
Drive, a weekly test that it restores, a copy of the index there too, local
index backups, a watch on the drives, the archive, and a storage check that
reads every file — and each lives on a page of its own. Answering "is the
household's library safe?" meant visiting all seven and knowing what each
one's numbers meant. This asks all of them and says, for each, one of:

* ``ok`` — nothing to do;
* ``attention`` — worth a look soon: behind, overdue, never done;
* ``problem`` — something a person should act on now;
* ``off`` — not in use here, said plainly rather than hidden.

Each check is on its own: one that cannot answer shows as ``unknown`` rather
than taking the page with it. Nothing here does any work or changes anything;
it reads what the other parts already know, cheaply, and the answer is kept
for a minute so the Overview can ask whenever it opens.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from typing import Any, Callable

from ..words import filled, said

log = logging.getLogger(__name__)

OK, ATTENTION, PROBLEM, OFF, UNKNOWN = "ok", "attention", "problem", "off", "unknown"
_ORDER = {PROBLEM: 0, ATTENTION: 1, UNKNOWN: 2, OK: 3, OFF: 4}

#: How long an answer is kept.
KEEP = 60.0

DAY = 86400.0


def _age(seconds: float) -> str:
    if seconds < 3600:
        return "less than an hour ago"
    if seconds < DAY:
        hours = int(seconds // 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = int(seconds // DAY)
    return f"{days} day{'s' if days != 1 else ''} ago"


def _check(check_id: str, title: str, status: str, summary: str, page: str,
           detail: str = "") -> dict[str, Any]:
    return {"id": check_id, "title": title, "status": status, "summary": summary,
            "detail": detail, "page": page}


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------

def cloud_copy(services, now: float) -> dict[str, Any]:
    from ..cloud import store                                      # noqa: PLC0415
    from ..storage import db                                      # noqa: PLC0415

    title, page = said("A copy outside the house"), "cloud"
    cloud = services.cloud
    cfg = services.cfg
    if not getattr(cfg, "cloud_enabled", False) or not cloud.creds.connected:
        return _check("cloud", title, PROBLEM,
                      said("There is no copy of the library outside this house. A fire, a theft or a burst pipe would take every photograph."), page,
                      said("Connect a Google account and switch cloud backup on."))
    engine = getattr(cloud, "_engine", None)
    state = engine.state.snapshot() if engine is not None else {}
    if state.get("needs_reconnect"):
        return _check("cloud", title, PROBLEM,
                      said("Google has stopped accepting Ninaivu's permission, so nothing new is being backed up."), page, said("Connect the account again."))
    conn = db.connect(cfg.db_path)
    # The schema is applied when the services start; applying it again here
    # ran executescript, and so a COMMIT, on every look at the Overview. A
    # library that has never queued anything reads as nothing done, as before.
    try:
        summary = store.summary(conn)
    except sqlite3.OperationalError:
        summary = {}
    eligible = conn.execute("SELECT COUNT(*) FROM assets WHERE trashed=0 AND "
                            "visibility < 2").fetchone()[0]
    done = int(summary.get("done", 0))
    share = done / eligible if eligible else 1.0
    line = f"{done:,} of {eligible:,} files are in Google Drive ({share:.0%})."
    failed = int(summary.get("failed", 0))
    if failed:
        line += f" {failed:,} gave up after five tries."
    away = state.get("offline_roots") or []
    if away:
        return _check("cloud", title, ATTENTION, line, page,
                      f"{', '.join(away)} is not connected, so its files cannot go up.")
    if share >= 0.99 and not failed:
        return _check("cloud", title, OK, line, page)
    moving = " It is uploading now." if engine is not None and engine.running else \
        " It is not uploading right now."
    return _check("cloud", title, ATTENTION, line + moving, page)


def restore_tests(services, now: float) -> dict[str, Any]:
    title, page = said("The backup restores"), "cloud"
    tester = getattr(services, "restore_tests", None)
    if tester is None or not services.cloud.creds.connected:
        return _check("restore_test", title, OFF, said("Nothing to test until there is a copy in Google Drive."), page)
    last = tester.last()
    if last is None:
        return _check("restore_test", title, ATTENTION,
                      said("The backup has never been test-restored."), page,
                      said("Run a test from the Cloud page; it takes a minute."))
    when = _age(now - float(last["started_at"]))
    if last["status"] == "failed":
        return _check("restore_test", title, PROBLEM,
                      f"The last test restore failed ({when}).", page, last["summary"])
    if last["status"] != "passed":
        return _check("restore_test", title, ATTENTION,
                      f"The last test did not run ({when}).", page, last["summary"])
    every = tester.every_days or 7
    if now - float(last["started_at"]) > 2 * every * DAY:
        return _check("restore_test", title, ATTENTION,
                      f"The last test passed, but that was {when}.", page,
                      said("Tests are overdue — they wait for the household and for the Google account, so check both."))
    return _check("restore_test", title, OK, f"A test restore passed {when}.", page,
                  last["summary"])


def index_in_drive(services, now: float) -> dict[str, Any]:
    title, page = said("The index is in Drive too"), "cloud"
    copier = getattr(services, "index_copy", None)
    if copier is None or not services.cloud.creds.connected:
        return _check("index_copy", title, OFF, said("Nothing to send it to until a Google account is connected."), page)
    if not copier.every_hours:
        return _check("index_copy", title, ATTENTION,
                      said("Sending a copy of the index is switched off. A new computer would get the photographs back but not the faces, albums or folders."), page)
    status = copier.status()
    last = status.get("last")
    if status.get("error"):
        return _check("index_copy", title, PROBLEM,
                      said("The last copy of the index could not be sent."), page,
                      status["error"])
    if not last:
        return _check("index_copy", title, ATTENTION,
                      said("No copy of the index has been sent yet."), page,
                      said("Send one from the Cloud page."))
    age = now - float(last["at"])
    line = f"Sent {_age(age)}{', encrypted' if last.get('encrypted') else ''}."
    if age > 3 * DAY:
        return _check("index_copy", title, ATTENTION, line, page,
                      said("Copies go once a day when something has changed, within the upload hours."))
    return _check("index_copy", title, OK, line, page)


def local_backups(services, now: float) -> dict[str, Any]:
    from ..storage import backup                                   # noqa: PLC0415

    title, page = said("Copies of the index here"), "health"
    keeper = services.backups
    newest = backup.latest(keeper.folder)
    if newest is None:
        return _check("backups", title, PROBLEM,
                      said("There is no backup of the index on this computer."), page,
                      said("Make one from the Health page."))
    line = f"The latest was made {_age(now - float(newest['at']))}."
    checked = keeper.last_verification()
    if checked and not checked.get("ok"):
        return _check("backups", title, PROBLEM, line + " Its restore check failed.",
                      page, str(checked.get("error") or ""))
    shared = keeper.shares_a_drive_with() if hasattr(keeper, "shares_a_drive_with") else []
    if now - float(newest["at"]) > 3 * DAY:
        return _check("backups", title, ATTENTION, line, page,
                      said("They are made daily when anything changed."))
    copier = getattr(services, "index_copy", None)
    sent = float((copier.last() if copier is not None else {}).get("at") or 0)
    if shared and now - sent > 3 * DAY:
        # One drive holding the index and its only copies is one failure from
        # losing both. A recent copy in Drive is the second place.
        return _check("backups", title, ATTENTION, line, page,
                      f"They are on the same drive as {', '.join(shared)}, and there is "
                      "no recent copy of the index in Drive, so one failing drive "
                      "would take both.")
    return _check("backups", title, OK, line, page)


def drives(services, now: float) -> dict[str, Any]:
    title, page = said("The drives are healthy"), "health"
    watch = getattr(services, "disks", None)
    if watch is None:
        return _check("drives", title, OFF, said("Drive health is not being watched."), page)
    report = watch.report()
    if not report.get("supported"):
        return _check("drives", title, OFF, said("Drive health comes from Windows, and this is not Windows."), page)
    if not report.get("checked_at"):
        return _check("drives", title, UNKNOWN, said("Not checked yet — the first check runs a minute after Ninaivu starts."), page)
    failing = [d for d in report["drives"] if d["status"] == "critical"]
    watching = [d for d in report["drives"] if d["status"] == "warning"]
    if failing:
        used = [d for d in failing if d["used_for"]]
        names = ", ".join(d["name"] for d in (used or failing)[:3])
        return _check("drives", title, PROBLEM,
                      f"{len(failing)} drive{'s' if len(failing) != 1 else ''} may be "
                      f"failing: {names}.", page,
                      failing[0]["problems"][0].get("advice", "") if failing[0]["problems"] else "")
    if watching:
        return _check("drives", title, ATTENTION,
                      f"{', '.join(d['name'] for d in watching[:3])} logged errors "
                      "worth watching this week.", page)
    return _check("drives", title, OK, f"{len(report['drives'])} drives, nothing "
                  "logged this week.", page)


def archive(services, now: float) -> dict[str, Any]:
    from ..archive import database as archive_db                   # noqa: PLC0415

    title, page = said("The last archive run finished"), "archive"
    job = archive_db.latest_job()
    if not job or (job.get("mode") or "") == "dry-run":
        return _check("archive", title, OFF, said("No archive runs yet."), page)
    message = str(job.get("message") or "")
    when = _age(now - float(job.get("ended_at") or job.get("started_at") or now))
    state = str(job.get("state") or "")
    if state == "running":
        return _check("archive", title, OK, said("A run is going now."), page)
    if message.startswith("INCOMPLETE"):
        return _check("archive", title, PROBLEM,
                      f"The last run ({when}) could not read part of its source, and "
                      "did not archive what was inside it.", page,
                      said("Run it again: it steps over what is done. Check the Errors tab, and the drive's cable and port."))
    if state in ("failed", "interrupted", "stopped"):
        return _check("archive", title, ATTENTION, f"The last run {state} ({when}).",
                      page, message[:200])
    return _check("archive", title, OK, f"The last run finished {when}.", page)


def storage_check(services, now: float) -> dict[str, Any]:
    from ..storage import db                                       # noqa: PLC0415

    title, page = said("The files read back as they were"), "health"
    conn = db.connect(services.cfg.db_path)
    if conn.execute("SELECT 1 FROM bitrot_records LIMIT 1").fetchone() is None:
        return _check("storage", title, ATTENTION,
                      said("The library has never had a storage check."), page,
                      said("It reads every file and remembers its fingerprint, so a later check can tell when one changes without being edited."))
    summary = db.get_bitrot_summary(conn)
    when = _age(now - float(summary.get("last_checked_at") or now))
    corrupt, missing = int(summary.get("corrupt", 0)), int(summary.get("missing", 0))
    unreadable = int(summary.get("unreadable", 0))
    if corrupt or unreadable:
        return _check("storage", title, PROBLEM,
                      f"{corrupt + unreadable:,} file{'s' if corrupt + unreadable != 1 else ''} "
                      f"changed or could not be read at the last check ({when}).", page,
                      said("Restore those from the cloud backup; the Health page lists them."))
    if missing:
        return _check("storage", title, ATTENTION,
                      f"{missing:,} files were missing at the last check ({when}).", page)
    if now - float(summary.get("last_checked_at") or 0) > 45 * DAY:
        return _check("storage", title, ATTENTION, f"The last check was {when}.", page)
    return _check("storage", title, OK, f"Every file checked read back intact ({when}).",
                  page)


def copies(services, now: float) -> dict[str, Any]:
    """Every photograph in more than one place (ninaivu/storage/copies.py)."""
    from ..storage import copies as copies_mod, db              # noqa: PLC0415

    title, page = said("Every photograph in more than one place"), "cloud"
    cfg = services.cfg
    roots = cfg.library_roots
    if not roots:
        return _check("copies", title, OFF, said("There is no library yet."), page)
    report = copies_mod.summary(db.connect(cfg.db_path), roots)
    if not report["files"]:
        return _check("copies", title, OFF, said("The library is empty."), page)
    one = report["copies"]["1"]["files"]
    more = report["files"] - one
    line = f"{more:,} files are in two places or more, and {one:,} only in the library."
    if one == 0:
        return _check("copies", title, OK, line, page)
    share = one / report["files"]
    reasons = sorted(report["single_reasons"].values(), key=lambda r: -r["files"])
    detail = "Mostly: " + ", ".join(f"{r['files']:,} {r['label']}" for r in reasons[:3]) + "."
    return _check("copies", title, PROBLEM if share > 0.5 else ATTENTION, line, page, detail)


CHECKS: list[Callable[[Any, float], dict[str, Any]]] = [
    copies, cloud_copy, restore_tests, index_in_drive, local_backups, drives, archive,
    storage_check,
]

_TITLES = {
    "cloud_copy": said("A copy outside the house"), "restore_tests": said("The backup restores"),
    "index_in_drive": said("The index is in Drive too"), "local_backups": said("Copies of the index here"),
    "drives": said("The drives are healthy"), "archive": said("The last archive run finished"),
    "storage_check": said("The files read back as they were"),
    "copies": said("Every photograph in more than one place"),
}


# ---------------------------------------------------------------------------
# The answer
# ---------------------------------------------------------------------------

class Safety:
    """Asks every check, and keeps the answer for a minute."""

    def __init__(self, services, clock: Callable[[], float] = time.time) -> None:
        self.services = services
        self._clock = clock
        self._lock = threading.Lock()
        self._kept: tuple[float, dict[str, Any]] | None = None

    def report(self, fresh: bool = False) -> dict[str, Any]:
        # Worked out under the lock, so that the several pages which open on
        # the same minute's answer wait for one computation instead of each
        # running every check against the index at once.
        with self._lock:
            now = self._clock()
            if not fresh and self._kept and now - self._kept[0] < KEEP:
                return self._kept[1]
            answer = self._compute(now)
            self._kept = (now, answer)
            return answer

    def _compute(self, now: float) -> dict[str, Any]:
        checks = []
        for check in CHECKS:
            try:
                checks.append(check(self.services, now))
            except Exception as exc:                               # noqa: BLE001
                log.debug("safety check %s failed", check.__name__, exc_info=True)
                checks.append(_check(check.__name__, _TITLES.get(check.__name__, check.__name__),
                                     UNKNOWN, said("Could not tell."), "health", str(exc)[:200]))
        worst = min((_ORDER[c["status"]] for c in checks), default=_ORDER[OK])
        problems = sum(c["status"] == PROBLEM for c in checks)
        attention = sum(c["status"] == ATTENTION for c in checks)
        # A check that could not run is not a check that passed. It used to
        # be left out of the headline, so a backup or a drive that could not
        # be looked at still read "Everything is safe."
        unknown = sum(c["status"] == UNKNOWN for c in checks)
        # Whole sentences with the numbers left as {names}, so the console can
        # put the same sentence into another language: see words.filled().
        if problems:
            verdict = PROBLEM
            if attention:
                key = (said("1 thing to act on, and {attention} to look at.") if problems == 1
                       else said("{problems} things to act on, and {attention} to look at."))
            else:
                key = said("1 thing to act on.") if problems == 1 else said("{problems} things to act on.")
        elif attention:
            verdict = ATTENTION
            key = (said("Safe, with 1 thing to look at.") if attention == 1
                   else said("Safe, with {attention} things to look at."))
        elif unknown:
            verdict = UNKNOWN
            key = (said("Nothing wrong found, but 1 thing could not be checked.") if unknown == 1
                   else said("Nothing wrong found, but {unknown} things could not be checked."))
        else:
            verdict = OK
            key = said("Everything is safe.")
        params = {"problems": problems, "attention": attention, "unknown": unknown}
        headline = filled(key, params)
        checks.sort(key=lambda c: _ORDER[c["status"]])
        return {"verdict": verdict, "headline": headline, "headline_key": key,
                "headline_params": params, "checks": checks, "at": now, "worst": worst}
