"""The Archive tab's endpoints — consolidating drives into the library.

This is the web layer for :mod:`ninaivu.archive`, and it replaces the tool's
former standalone Flask app entirely. Three things changed in the move, all of
them in Ninaivu's favour:

**It lives on the console alone.** Every route is registered on the admin app
only, so on the family port they do not exist — a 404, not a 403. Consolidating
drives writes gigabytes and can enumerate every disk on the machine; that is
administrator work by definition.

**Ninaivu's login replaces the old guard.** Standing alone, the tool defended
itself by pinning the ``Host`` header and refusing cross-origin writes, because
any web page in the same browser can POST to ``127.0.0.1``. Behind Ninaivu those
requests need an administrator session, and the app refuses any write whose
``Sec-Fetch-Site``/``Origin`` says it came from another page (see
``_refuse_cross_origin_writes`` in ``ninaivu/__init__.py``) — the browser would
attach the admin's cookie to a forged request, so the cookie alone is no guard.

**Paths are namespaced.** Everything sits under ``/api/archive/`` so nothing
collides with the library's own ``/api/scan``, ``/api/settings`` or ``/api/stats``.
"""

from __future__ import annotations

import csv
import io
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, current_app, jsonify, request

from .. import archive
from ..server import auth
from ..storage import db
from ..server.config import is_forbidden_root
from ..archive import database as adb
from ..archive.safety import (check_free_space, is_within, job_notices, long_path,
                              translatable, validate_job)
from ..archive.scanner import (MODE_COPY, MODE_DRY_RUN, MODE_VERIFY, ArchiveJob,
                              is_scan_paused, is_scanning, job_progress,
                              pause_scan, remember_estimate, remember_pause,
                              resolve_archive_destination, resume_scan,
                              start_scan, stop_scan)
from ..server.auth import current_user, require_admin
from ._body import json_object

archive_bp = Blueprint("archive", __name__)

VALID_MEDIA_TYPES = {"image", "video", "audio"}

#: A consolidation can stream for hours; a handful of watching tabs is normal,
#: an unbounded number is a slow leak of threads and SQLite readers.
MAX_STREAMS = 8
_stream_count = 0
_stream_lock = threading.Lock()

# ---------------------------------------------------------------------------
# What the estimate is doing, while it does it
# ---------------------------------------------------------------------------
#
# Adding a drive as a source starts a walk of every folder on it, and that can
# run for minutes. The console used to show one static word for the whole of
# it, which makes a walk working steadily through four hundred thousand files
# indistinguishable from one that has hit a spun-down disk and hung.
#
# So the walk publishes itself here: a small record per estimate, keyed by a
# token the browser makes up, holding the running count, the bytes so far, the
# folder it is in at this moment and when it started. It is read by polling
# rather than streamed, because an estimate is seconds-to-minutes work watched
# by exactly one tab — a second SSE stream for that would cost more than it
# saves.
#
# The same record carries the cancel flag. Changing the sources mid-walk should
# call the old walk off rather than leave it grinding a disk nobody is waiting
# on, and — just as important — stop a stale answer from landing on top of a
# newer one.

#: One record per estimate, and the console starts one per edit. Old ones are
#: dropped oldest-first rather than kept for a session that may last days.
MAX_ESTIMATE_RECORDS = 24

#: Updating the record on every single file would put a lock acquisition in the
#: middle of the tightest loop in the walk. Once every this-many files is
#: plenty for something a person is reading.
ESTIMATE_REPORT_EVERY = 25

_estimates: dict[str, dict[str, Any]] = {}
_estimate_lock = threading.Lock()


class EstimateCancelled(Exception):
    """Raised inside the walk when the browser has moved on."""


def _estimate_begin(token: str) -> None:
    if not token:
        return
    with _estimate_lock:
        _estimates[token] = {
            "running": True, "finished": False, "cancelled": False,
            "files": 0, "bytes": 0, "folder": "", "started_at": time.time(),
        }
        # Oldest first, so the record the browser is polling right now is the
        # last thing that would ever be dropped.
        while len(_estimates) > MAX_ESTIMATE_RECORDS:
            oldest = min(_estimates, key=lambda k: _estimates[k]["started_at"])
            if oldest == token:
                break
            del _estimates[oldest]


def _estimate_update(token: str, *, files: int, size: int, folder: str) -> None:
    """Publish where the walk is, and raise if it has been called off."""
    if not token:
        return
    with _estimate_lock:
        record = _estimates.get(token)
        if record is None:
            return
        if record["cancelled"]:
            raise EstimateCancelled
        record["files"] = files
        record["bytes"] = size
        record["folder"] = folder


def _estimate_end(token: str, *, files: int, size: int,
                  cancelled: bool = False) -> None:
    if not token:
        return
    with _estimate_lock:
        record = _estimates.get(token)
        if record is None:
            return
        record.update(running=False, finished=not cancelled, files=files,
                      bytes=size, cancelled=cancelled, folder="",
                      ended_at=time.time())


def _short_folder(path: str, keep: int = 3) -> str:
    r"""The tail of a folder path, for a one-line "now in…" report.

    A walk fifteen levels down a drive produces paths far too long to sit in a
    status line, and the useful part is always the end. ``\\?\`` is stripped
    first so what is shown is what the user would type.
    """
    from ..archive.safety import short_path

    plain = short_path(str(path)).rstrip(os.sep)
    parts = [p for p in plain.replace("\\", "/").split("/") if p]
    if len(parts) <= keep:
        return plain
    return "…/" + "/".join(parts[-keep:])


def _estimate_snapshot(token: str) -> dict[str, Any]:
    with _estimate_lock:
        record = _estimates.get(token)
        if record is None:
            # The browser polls before the POST has been picked up, and again
            # after a record has aged out. Neither is a fault worth an error.
            return {"running": False, "finished": False, "cancelled": False,
                    "known": False, "files": 0, "bytes": 0, "folder": "",
                    "elapsed": 0.0}
        snapshot = dict(record)
    started = snapshot.pop("started_at", None)
    ended = snapshot.pop("ended_at", None)
    snapshot["known"] = True
    snapshot["elapsed"] = round((ended or time.time()) - started, 1) if started else 0.0
    return snapshot


def _cfg():
    return current_app.config["MV_CONFIG"]


def _scanner():
    return current_app.config["MV_SCANNER"]


def _power():
    return current_app.config.get("MV_POWER")


def _guardian():
    return current_app.config.get("MV_GUARDIAN")


def _conn():
    return db.connect(_cfg().db_path)


# ---------------------------------------------------------------------------
# Sharing the disk with the library indexer
# ---------------------------------------------------------------------------

#: How long the archive has to be idle before the indexer gets the disk back.
#: Asymmetric on purpose: slow to hand over, instant to take back. The pacer
#: re-decides every two seconds and its thresholds have no hysteresis, so a
#: machine hovering at 89-91 degrees would otherwise stop and restart the
#: indexer every few seconds, and each restart re-walks the library.
IDLE_RELEASE_AFTER = 25.0


def _archive_idle_state():
    """Why the archive is not moving bytes right now, or ``None`` if it is.

    Three different things stop an archive job, and ``is_scanning()`` is true
    for all of them, which is why simply watching that flag left the indexer
    stood down and the CPU pinned at full speed while nothing was happening.
    """
    try:
        if is_scan_paused():
            return "paused"
        progress = job_progress() or {}
        if progress.get("waiting_for_drives"):
            return "waiting"
        if (progress.get("pacing") or {}).get("mode") == "paused":
            return "thermal"
    except Exception:  # noqa: BLE001 - a status read must never stop the watcher
        return None
    return None


class _DiskPolicy:
    """Decides who gets the disk and how fast the CPU runs.

    Kept as a plain state machine with the clock injected so the whole policy
    can be tested without threads, sleeps or a real archive job.

    Two rules that are easy to get backwards:

    *Power follows the work, immediately.* If the archive is not moving bytes,
    there is nothing to finish sooner, so the efficient policy is correct even
    during a thermal park - especially then.

    *The indexer does not follow the power.* Releasing it during a thermal park
    would hand a machine that just stopped at 90 degrees a decode-and-hash
    workload, which is hotter than the job it paused to escape. So indexing
    resumes for a human pause or a missing drive, and never for heat.
    """

    RELEASES_THE_INDEXER = ("paused", "waiting")

    def __init__(self, clock=time.monotonic, release_after: float = IDLE_RELEASE_AFTER):
        self._clock = clock
        self._release_after = release_after
        self.performance = True      # _yield_the_disk asked for it before we started
        self.indexer_deferred = True
        self._idle_since = None

    def step(self, state: str | None) -> dict[str, bool]:
        """Fold one observation in. Returns the actions the caller should take."""
        actions = {"to_performance": False, "to_efficient": False,
                   "release_indexer": False, "defer_indexer": False}

        want_performance = state is None
        if want_performance != self.performance:
            actions["to_performance" if want_performance else "to_efficient"] = True
            self.performance = want_performance

        if state in self.RELEASES_THE_INDEXER:
            if self._idle_since is None:
                self._idle_since = self._clock()
            idle_for = self._clock() - self._idle_since
            if self.indexer_deferred and idle_for >= self._release_after:
                actions["release_indexer"] = True
                self.indexer_deferred = False
        else:
            self._idle_since = None
            if not self.indexer_deferred:
                actions["defer_indexer"] = True
                self.indexer_deferred = True

        return actions


#: What the indexer is told it is standing down for, per kind of archive job.
YIELD_LABELS = {MODE_COPY: "a consolidation is running",
                MODE_DRY_RUN: "a dry run is running",
                MODE_VERIFY: "an archive audit is running"}


def _yield_the_disk(scanner, label: str, power=None) -> None:
    """Give the archive the disk and full speed, then restore normal serving.

    The watcher thread exists because the archive engine owns its own thread and
    knows nothing about Ninaivu. Rather than reach into the engine to add a
    callback — the one part of this module that must stay exactly as verified —
    we watch its public status and restore the indexer the moment the job stops,
    however it stopped: finished, cancelled or crashed.

    It also watches for the job going idle without ending. An archive waiting on
    an unplugged drive can sit there overnight, and holding the library indexer
    down for all of it buys nobody anything.
    """
    if power is not None:
        power.archive_started()
    try:
        scanner.defer(label)
    except Exception:
        if power is not None:
            power.archive_finished()
        raise

    def watch() -> None:
        policy = _DiskPolicy()
        try:
            # start_scan() starts the thread before it returns. If a tiny job
            # has already ended, restoring immediately is the right answer;
            # waiting for a start that already happened wastes ten seconds.
            while is_scanning():
                actions = policy.step(_archive_idle_state())
                if power is not None:
                    if actions["to_efficient"]:
                        power.archive_finished()
                    elif actions["to_performance"]:
                        power.archive_started()
                if actions["release_indexer"]:
                    scanner.resume(label)
                elif actions["defer_indexer"]:
                    scanner.defer(label)
                time.sleep(0.25)
        finally:
            # Balance whatever the loop left behind. archive_finished() clamps
            # at zero, so an extra call here cannot strand the policy.
            if power is not None:
                power.archive_finished()
            scanner.resume(label)

    try:
        threading.Thread(target=watch, name="archive-yield", daemon=True).start()
    except Exception:
        if power is not None:
            power.archive_finished()
        scanner.resume(label)
        raise


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def _same_folder(a: str, b: str) -> bool:
    """Windows is case-insensitive and both separators reach the same folder."""
    def key(p: str) -> str:
        return os.path.normcase(os.path.abspath(os.path.expanduser(p))).rstrip("\\/")
    return bool(a) and bool(b) and key(a) == key(b)


def _already_a_library(path: str, libraries: list[str]) -> bool:
    """*path* is a library folder, or inside one, so the library shows it
    already (the default archive sits inside the library)."""
    return bool(path) and any(
        _same_folder(path, existing) or is_within(path, existing)
        for existing in libraries if existing)


#: The archive's folder when the administrator has not chosen one.
ARCHIVE_NAME = "Ninaivu Archive"


def default_destination(libraries: list[str]) -> str:
    """Where an import goes unless the administrator chooses otherwise, from
    Ninaivu Lite: inside the first library folder, so what comes in from old
    drives, cards and phone backups is indexed and shown to the family as
    it lands (the walk steps around it, so the library can still be a
    source). Without a library yet, under Pictures."""
    for library in libraries:
        if library:
            return os.path.join(str(library), ARCHIVE_NAME)
    home = os.path.expanduser("~")
    pictures = os.path.join(home, "Pictures")
    return os.path.join(pictures if os.path.isdir(pictures) else home, ARCHIVE_NAME)


def _handoff(cfg) -> dict[str, Any]:
    """Whether there is a finished archive worth offering to the library.

    Deliberately an *offer*, never an action: a dry run, an experiment or a
    half-finished migration must not silently become a folder Ninaivu indexes
    and shows the whole household.
    """
    destination = adb.load_settings().get("destination_dir") or ""
    verified = int(adb.get_stats().get("verified") or 0)
    return {
        "destination": destination,
        "verified": verified,
        # Only worth offering once something real has landed there.
        "available": bool(destination and verified and not is_scanning()
                          and os.path.isdir(destination)),
        "in_library": _already_a_library(destination, list(cfg.libraries)),
    }


def _status_payload(cfg, scanner, power=None, guardian=None) -> dict[str, Any]:
    """The whole Archive tab in one object.

    Takes its services explicitly rather than reading them off
    ``current_app``, because the SSE generator below runs *after* the request
    context has been torn down — ``current_app`` is gone by then, and the
    stream would die on its first tick.
    """
    payload = adb.get_stats()
    payload.update(job_progress())
    payload["is_scanning"] = is_scanning()
    payload["is_paused"] = is_scan_paused()
    payload["handoff"] = _handoff(cfg)
    payload["indexer"] = {
        "deferred": scanner.deferred,
        "status": scanner.progress.snapshot().get("status"),
    }
    payload["power"] = (power.snapshot() if power is not None else {
        "mode": "unmanaged", "managed": False, "archive_active": False,
    })
    payload["guardian"] = guardian.snapshot() if guardian is not None else {}
    # Said before a job starts, not only once it is running and already slow:
    # on battery the archive deliberately throttles itself, and a large job
    # started unplugged takes many times longer without saying why.
    from ..archive.pacing import read_battery
    on_battery, percent = read_battery()
    payload["battery"] = {"on_battery": on_battery, "percent": percent}
    return payload


@archive_bp.get("/api/archive/status")
@require_admin
def status():
    return jsonify(_status_payload(_cfg(), _scanner(), _power(), _guardian()))


@archive_bp.get("/api/archive/version")
@require_admin
def version():
    return jsonify({
        "version": archive.VERSION,
        "build": archive.BUILD,
        "hosted_by": current_app.config.get("APP_NAME", "Ninaivu"),
        "state": archive.state_paths(),
    })


@archive_bp.get("/api/archive/settings")
@require_admin
def settings():
    """The last job, so a restart does not mean retyping four drive paths.

    Before the first job the destination is the suggested one (see
    ``default_destination``), marked so; it is saved only when a job starts.
    """
    saved = adb.load_settings()
    saved["destination_is_default"] = not saved.get("destination_dir")
    if saved["destination_is_default"]:
        cfg = _cfg()
        saved["destination_dir"] = default_destination(
            list(cfg.libraries) or ([cfg.active_root] if cfg.active_root else []))
    return jsonify(saved)


@archive_bp.get("/api/archive/takeout-albums")
@require_admin
def takeout_albums():
    """The Google Photos albums found under the last import's sources, so
    the page can offer to make them in the library."""
    from ..archive import takeout                        # noqa: PLC0415
    sources = [s["path"] for s in adb.load_settings().get("source_dirs", [])]
    found = takeout.albums_in(sources)
    return jsonify(albums=[{"title": a["title"], "files": len(a["files"])} for a in found])


@archive_bp.post("/api/archive/takeout-albums")
@require_admin
def takeout_albums_recreate():
    """Make those albums in the library, from what the import archived.

    Nothing is read from the export a second time and nothing is copied: the
    archive's own record says where each member went, and the library's
    index says what it is called now. Members the library has not indexed
    yet are counted, and a second run after the scan picks them up.
    """
    from ..archive import takeout                        # noqa: PLC0415
    cfg = current_app.config["MV_CONFIG"]
    sources = [s["path"] for s in adb.load_settings().get("source_dirs", [])]
    roots = cfg.libraries or ([cfg.active_root] if cfg.active_root else [])
    if not roots:
        return jsonify(error="No library folder has been set up yet."), 409
    result = takeout.recreate(db.connect(cfg.db_path), roots, sources,
                              created_by=current_user().id)
    auth.audit(db.connect(cfg.db_path), current_user().id, "takeout_albums",
               f"{len(result['albums'])} albums, {result['unmatched']} members not yet indexed")
    return jsonify(result)


@archive_bp.get("/api/archive/stream")
@require_admin
def stream():
    """Server-sent events: one message per actual change, plus a heartbeat.

    Writes only when something changed, so an idle console is not polling
    SQLite once a second per open tab, and exits on disconnect.
    """
    global _stream_count
    with _stream_lock:
        if _stream_count >= MAX_STREAMS:
            return jsonify({"error": "Too many open status streams."}), 429
        _stream_count += 1

    # Bound now, while the request context still exists.
    cfg, scanner, power, guardian = _cfg(), _scanner(), _power(), _guardian()

    def events():
        global _stream_count
        last = None
        beat = 0.0
        try:
            while True:
                try:
                    payload = _status_payload(cfg, scanner, power, guardian)
                except Exception as exc:  # noqa: BLE001
                    yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"
                    return
                blob = json.dumps(payload, sort_keys=True)
                now = time.time()
                if blob != last:
                    last = blob
                    yield f"data: {blob}\n\n"
                elif now - beat > 15:
                    beat = now
                    yield ": keep-alive\n\n"
                time.sleep(1.0)
        finally:
            adb.close_db()
            with _stream_lock:
                _stream_count -= 1

    # Deliberately no "Connection: keep-alive". It is a hop-by-hop header,
    # which PEP 3333 forbids a WSGI application from setting — the connection
    # belongs to the server, not to the application. Werkzeug let it through;
    # waitress asserts on it, which killed every event stream at the first
    # byte. Nothing is lost: HTTP/1.1 keeps the connection alive by default,
    # and the server decides either way.
    return Response(events(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
    })


# ---------------------------------------------------------------------------
# Planning a job
# ---------------------------------------------------------------------------

@archive_bp.post("/api/archive/validate")
@require_admin
def validate():
    data = json_object()
    sources = data.get("source_dirs") or []
    # Correct a destination chosen from inside an existing archive *before*
    # validating, so the user sees the real target while still editing.
    resolution = resolve_archive_destination(data.get("destination_dir") or "")
    cfg = _cfg()
    problems = validate_job(sources, resolution["destination"],
                            protected=[cfg.state_dir])
    notices = job_notices(sources, resolution["destination"],
                          libraries=list(cfg.libraries))
    return jsonify({"ok": not problems, "problems": problems,
                    "problem_keys": translatable(problems), "notices": notices,
                    "notice_keys": translatable(notices), "resolution": resolution})


@archive_bp.post("/api/archive/capacity")
@require_admin
def capacity():
    """Estimate what a job would move and whether it fits, before committing.

    Cheap by design: file stats only, no hashing.
    """
    data = json_object()
    sources = data.get("source_dirs") or []
    token = str(data.get("progress_token") or "")[:64]
    resolution = resolve_archive_destination(data.get("destination_dir") or "")
    destination = resolution["destination"]
    requested = data["media_types"] if "media_types" in data else VALID_MEDIA_TYPES
    types = set(requested or ()) & VALID_MEDIA_TYPES

    problems = validate_job(sources, destination, protected=[_cfg().state_dir])
    if problems:
        return jsonify({"ok": False, "problems": problems,
                        "problem_keys": translatable(problems),
                        "resolution": resolution})

    probe = ArchiveJob(sources, destination, media_types=types,
                       deep_scan=bool(data.get("deep_scan", True)), vision=_vision())
    # The estimate is where held-back course material is reviewed, so it says
    # how much each folder is; a run does not need to measure them.
    probe.measure_course_folders = True
    count = total = 0
    probe.course_progress = lambda folder: _estimate_update(
        token, files=count, size=total, folder=_short_folder(folder))
    bytes_by_kind: dict[str, int] = {}
    truncated = False
    _estimate_begin(token)
    try:
        for root, _name in probe._walk():
            count += 1
            size = probe.last_size          # read with the folder; no second lookup
            total += size
            # The walk has already decided this file's kind; ask it rather than
            # classifying a second time, which on a deep scan means re-reading
            # the first bytes of every extensionless file on the drive.
            kind = probe.last_kind
            if kind:
                bytes_by_kind[kind] = bytes_by_kind.get(kind, 0) + size
            # Say where we are, often enough to read and rarely enough not to
            # put a lock in the hot loop. This is also the cancellation check,
            # so the two share a cadence: a walk can only be called off as
            # often as it reports, which on a real drive is several times a
            # second. The first few files report unconditionally, so a slow
            # disk shows a moving number straight away rather than a zero.
            if count <= 3 or count % ESTIMATE_REPORT_EVERY == 0:
                _estimate_update(token, files=count, size=total,
                                 folder=_short_folder(root))
            if count >= 200_000:                 # keep the preview responsive
                truncated = True
                break
    except EstimateCancelled:
        # The browser has moved on — a source was removed, a type switched, or
        # the tab was closed. Returning the half-count would race a newer
        # estimate onto the screen, so say plainly that this one was dropped.
        _estimate_end(token, files=count, size=total, cancelled=True)
        return jsonify({"ok": False, "cancelled": True, "files": count,
                        "resolution": resolution})

    _estimate_end(token, files=count, size=total)
    if not truncated:
        # Start can use this count instead of walking every source again first.
        remember_estimate(probe, count, total)
    already = adb.bytes_already_archived()
    needed = max(0, total - already)
    fits, free, required = check_free_space(destination, needed)
    return jsonify({"ok": True, "files": count, "bytes": total,
                    "needed": required, "free": free, "fits": fits,
                    # Per-kind, so a selection that is not what the user
                    # believes they chose is visible in the estimate itself.
                    "by_kind": dict(probe.included_by_kind),
                    "bytes_by_kind": bytes_by_kind,
                    "excluded": probe.filtered_out,
                    "excluded_by_kind": dict(probe.filtered_by_kind),
                    "truncated": truncated,
                    "course_folders": [_course_folder_payload(c)
                                       for c in probe.course_folders],
                    "entertainment": [_entertainment_payload(c)
                                      for c in probe.entertainment],
                    "sources": [{"path": e["path"], "types": sorted(e["types"])}
                                for e in probe.source_entries],
                    "resolution": resolution})


def _vision():
    """The loaded image model, if it can judge pictures; None otherwise.

    Only ever the model Ninaivu already has in memory for search: an archive
    estimate never loads one of its own.
    """
    from .. import ai
    engine = ai.get_engine()
    return engine if callable(getattr(engine, "embed_images", None)) else None


def _course_folder_payload(folder: dict[str, Any]) -> dict[str, Any]:
    return {key: folder.get(key) for key in
            ("path", "state", "score", "reasons", "camera_photos", "files", "bytes",
             "camera_check_incomplete", "measurement_truncated")}


def _entertainment_payload(entry: dict[str, Any]) -> dict[str, Any]:
    return {key: entry.get(key) for key in
            ("path", "category", "state", "score", "reasons", "files", "bytes", "examples",
             "spared", "spared_reasons", "spared_examples")}


@archive_bp.post("/api/archive/course-folders/decision")
@require_admin
def decide_course_folders():
    """Remember what to do with folders the archive set aside.

    ``{"decisions": [{"path": ..., "decision": "exclude" | "include" | null,
    "category": "course" | "film" | "music"}]}``. ``category`` defaults to
    ``course``; for films and music the choice covers the files of that kind the
    walk held back in that folder, never the ones it spared as personal.
    ``null`` forgets a decision, so the folder is held back and asked about
    again. The key a decision is kept under is worked out here from the path and
    the drive it is on, never taken from the request, so it always matches the
    one the walk will look up -- and so a decision about a folder follows the
    drive rather than its letter.
    """
    from ..archive import course_material
    from ..archive.scanner import _volume_identity

    data = json_object()
    decisions = data.get("decisions")
    if not isinstance(decisions, list) or not decisions or len(decisions) > 5000:
        return jsonify({"error": "Send a list of folder decisions."}), 400
    cleaned = []
    for item in decisions:
        if not isinstance(item, dict):
            return jsonify({"error": "Each decision needs a folder and a choice."}), 400
        path, decision = item.get("path"), item.get("decision")
        if not isinstance(path, str) or not path.strip():
            return jsonify({"error": "Each decision needs a folder."}), 400
        if decision not in ("exclude", "include", None):
            return jsonify({"error": "Choose exclude, include, or clear."}), 400
        category = item.get("category", "course")
        if category not in ("course", "film", "music"):
            return jsonify({"error": "Choose course, film or music."}), 400
        if not os.path.isdir(long_path(path)):
            return jsonify({"error": f"That folder is not available: {path}"}), 400
        cleaned.append((path, decision, category))
    for path, decision, category in cleaned:
        key = course_material.decision_key(path, _volume_identity(path))
        if category != "course":
            key = f"{key}#{category}"
        adb.set_folder_decision(key, path, decision)
    cleaned = [(path, decision) for path, decision, _category in cleaned]
    counts = {choice or "cleared": sum(1 for _p, d in cleaned if d == choice)
              for choice in ("exclude", "include", None)}
    auth.audit(_conn(), current_user().id, "archive_course_folders",
               ", ".join(f"{n} {k}" for k, n in counts.items() if n))
    return jsonify({"ok": True, "saved": len(cleaned), **counts})


@archive_bp.get("/api/archive/capacity/progress")
@require_admin
def capacity_progress():
    """Where the estimate has got to. Polled by the console while it waits."""
    return jsonify(_estimate_snapshot(request.args.get("token", "")))


@archive_bp.post("/api/archive/capacity/cancel")
@require_admin
def capacity_cancel():
    """Call off a walk nobody is waiting for any more.

    Always 200, including for a token that finished a moment ago or never
    existed: the browser fires this on every edit and cannot know which.
    """
    data = request.get_json(silent=True)
    token = str((data if isinstance(data, dict) else {}).get("token") or "")
    with _estimate_lock:
        record = _estimates.get(token)
        if record is not None and record["running"]:
            record["cancelled"] = True
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Running a job
# ---------------------------------------------------------------------------

@archive_bp.post("/api/archive/start")
@require_admin
def start():
    data = json_object()
    sources = data.get("source_dirs") or []
    destination = (data.get("destination_dir") or "").strip()
    mode = data.get("mode") or MODE_COPY

    guardian = _guardian()
    if guardian is not None and guardian.snapshot().get("running"):
        return jsonify({"error": "Wait for the archive health check to finish."}), 409

    # `or` would be wrong: an explicitly empty list is a request to archive
    # nothing, which must be refused rather than silently widened to
    # "everything". This is only the fallback for sources given as bare paths —
    # a source sent in the per-folder form carries its own selection.
    requested = data["media_types"] if "media_types" in data else VALID_MEDIA_TYPES
    types = set(requested or ()) & VALID_MEDIA_TYPES
    if not types and not any(isinstance(s, dict) and s.get("types") for s in sources):
        message = "Select at least one kind of file to archive."
        return jsonify({"error": message, "problems": [message]}), 409

    if mode != MODE_VERIFY:
        # Ninaivu's own data folder is refused here, where the configuration
        # is known; start_scan below checks everything else.
        early = resolve_archive_destination(destination)
        refused = [problem for problem in validate_job(
            sources, early["destination"], protected=[_cfg().state_dir])
            if "Ninaivu's own data folder" in problem]
        if refused:
            return jsonify({"error": refused[0], "problems": refused,
                            "problem_keys": translatable(refused),
                            "resolution": early}), 409

    ok, problems, resolution = start_scan(
        sources, destination, mode=mode, media_types=types,
        deep_scan=bool(data.get("deep_scan", True)), vision=_vision())
    if not ok:
        return jsonify({"error": problems[0], "problems": problems,
                        "problem_keys": translatable(problems),
                        "resolution": resolution}), 409

    destination = resolution["destination"]
    adb.save_settings(sources, destination, types,
                      deep_scan=bool(data.get("deep_scan", True)))
    remember_pause(False)

    _yield_the_disk(_scanner(), YIELD_LABELS.get(mode, "an archive job is running"),
                    _power())

    messages = {
        MODE_COPY: "Started. Start also resumes an interrupted run.",
        MODE_DRY_RUN: "Dry run started. Nothing will be written.",
        MODE_VERIFY: "Audit started. Re-reading every archived file.",
    }
    message = messages.get(mode, "Started.")
    if resolution["corrected"]:
        message = f'Using the archive root "{destination}". ' + message

    auth.audit(_conn(), current_user().id,
               f"archive_{mode.replace('-', '_')}",
               f"{len(sources)} source(s) → {destination}")
    return jsonify({"message": message, "resolution": resolution,
                    "destination": destination})


@archive_bp.post("/api/archive/stop")
@require_admin
def stop():
    stop_scan()
    remember_pause(False)
    auth.audit(_conn(), current_user().id, "archive_stop", "")
    return jsonify({"message": "Stop requested. In-flight copies are discarded, "
                               "not left half-written."})


@archive_bp.post("/api/archive/pause")
@require_admin
def pause():
    pause_scan()
    # So a restart while paused brings the job back paused, not running.
    remember_pause(True)
    return jsonify({"message": "Paused"})


@archive_bp.post("/api/archive/resume")
@require_admin
def resume():
    resume_scan()
    remember_pause(False)
    return jsonify({"message": "Resumed"})


@archive_bp.post("/api/archive/guardian/run")
@require_admin
def guardian_run():
    guardian = _guardian()
    if guardian is None:
        return jsonify({"error": "Archive Guardian is unavailable."}), 503
    if not guardian.request_check():
        return jsonify({"error": "A health check or archive run is already active."}), 409
    return jsonify({"message": "Archive health check started."}), 202


# ---------------------------------------------------------------------------
# Handing the finished archive to the library
# ---------------------------------------------------------------------------

@archive_bp.post("/api/archive/adopt")
@require_admin
def adopt():
    """Add the archive that was just built to Ninaivu's library, on request.

    This is the join between the two halves of the app: the Archive tab builds
    a folder, the library shows it. It stays a button rather than an automatic
    step, because a dry run or a trial migration must never quietly become
    something the whole household can browse.
    """
    data = json_object()
    cfg = _cfg()
    destination = (data.get("path")
                   or adb.load_settings().get("destination_dir") or "").strip()

    if not destination:
        return jsonify({"error": "There is no finished archive to add yet."}), 400

    path = Path(destination).expanduser()
    # Refused as typed, before existence is looked at: a system folder is
    # never adopted, and on Windows "/etc" only resolves to an absent C:\etc,
    # which would otherwise be reported as "not there" rather than refused.
    if is_forbidden_root(path):
        return jsonify({
            "error": f"“{destination}” is a system folder, so Ninaivu won't index it."
        }), 403

    if not path.is_dir():
        return jsonify({
            "error": f"“{destination}” is not there any more. "
                     f"Reconnect the drive and try again."
        }), 400

    resolved = str(path.resolve())
    # Before the library picker's gate, which refuses a folder inside a
    # library: an archive there (the default one) is shown already.
    if _already_a_library(resolved, list(cfg.libraries)):
        return jsonify({
            "ok": True, "already": True, "path": resolved,
            "message": "That archive is already in your library.",
        })

    # The same gate the library picker uses, so --lock-roots confines this
    # door too; it used to be the one way round it.
    from .api_library import _can_be_library, _root_refusal   # noqa: PLC0415
    if not _can_be_library(path.resolve(), cfg):
        return jsonify({"error": _root_refusal(path, cfg)}), 403

    cfg.add_library(resolved)
    cfg.save()

    conn = _conn()
    auth.audit(conn, current_user().id, "archive_adopt", resolved)

    # The indexer may still be standing down from the run that just finished;
    # ask for the scan either way — a deferred Scanner queues it.
    scanner = _scanner()
    scanner.start(resolved)

    return jsonify({
        "ok": True, "already": False, "path": resolved,
        "libraries": cfg.libraries,
        "message": f"“{path.name or resolved}” was added to your library. "
                   f"Ninaivu is indexing it now.",
    })


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

@archive_bp.get("/api/archive/recent")
@require_admin
def recent():
    try:
        limit = max(1, min(500, int(request.args.get("limit", 50))))
    except (TypeError, ValueError):
        limit = 50
    return jsonify(adb.get_recent_files(limit, request.args.get("status")))


@archive_bp.get("/api/archive/years")
@require_admin
def years():
    """Distribution across capture years.

    If everything lands in the current year, dates were not read properly —
    the exact failure this tool exists to avoid, and one that is invisible in
    a flat file count.
    """
    return jsonify(adb.year_breakdown())


@archive_bp.get("/api/archive/logs")
@require_admin
def logs():
    """The plain-text run logs, newest first."""
    from ..archive.scanner import LOG_DIR

    if not os.path.isdir(LOG_DIR):
        return jsonify([])
    out = []
    for name in sorted(os.listdir(LOG_DIR)):
        if not name.endswith(".log"):
            continue
        try:
            st = os.stat(os.path.join(LOG_DIR, name))
        except OSError:
            continue
        out.append({"name": name, "size": st.st_size, "modified": st.st_mtime})
    out.sort(key=lambda r: r["modified"], reverse=True)
    return jsonify(out)


@archive_bp.get("/api/archive/manifest.csv")
@require_admin
def manifest():
    """One row per file, with both hashes.

    The record has to be exportable to something that outlives this app, so an
    archive can be audited against its sources long after Ninaivu is gone.
    """
    def rows():
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(["source_path", "filename", "size", "source_sha256",
                         "archived_sha256", "capture_date", "date_source",
                         "status", "destination_path", "duplicate_of", "error"])
        yield buf.getvalue()
        buf.seek(0), buf.truncate(0)

        for row in adb.iter_all_files():
            writer.writerow([row[k] if row[k] is not None else "" for k in
                             ("source_path", "filename", "size", "file_hash",
                              "dest_hash", "exif_date", "date_source", "status",
                              "destination_path", "duplicate_of", "error")])
            yield buf.getvalue()
            buf.seek(0), buf.truncate(0)
        adb.close_db()

    stamp = time.strftime("%Y%m%d-%H%M%S")
    return Response(rows(), mimetype="text/csv", headers={
        "Content-Disposition":
            f'attachment; filename="archive-manifest-{stamp}.csv"'})


@archive_bp.get("/api/archive/recovery.json")
@require_admin
def recovery_report():
    """Portable checksums consumable without Ninaivu or its SQLite database."""
    destination = (adb.load_settings().get("destination_dir") or "").strip()

    def document():
        generated = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        header = {
            "format": "ninaivu-recovery-v1",
            "generated_at": generated,
            "archive_root": os.path.basename(destination.rstrip("\\/")) if destination else "",
        }
        prefix = json.dumps(header, separators=(",", ":"))[:-1]
        yield prefix + ',"files":['
        first = True
        try:
            for row in adb.iter_recovery_files(destination):
                path = row["destination_path"]
                if not destination:
                    continue
                try:
                    relative = os.path.relpath(path, destination)
                except ValueError:
                    continue
                if relative == os.pardir or relative.startswith(os.pardir + os.sep):
                    continue
                item = {
                    "path": relative.replace("\\", "/"),
                    "size": row["size"],
                    "sha256": row["file_hash"],
                }
                yield ("" if first else ",") + json.dumps(
                    item, separators=(",", ":"))
                first = False
        finally:
            adb.close_db()
        yield "]}"

    stamp = time.strftime("%Y%m%d-%H%M%S")
    return Response(document(), mimetype="application/json", headers={
        "Content-Disposition":
            f'attachment; filename="ninaivu-recovery-{stamp}.json"'})


@archive_bp.post("/api/archive/retry-errors")
@require_admin
def retry_errors():
    if is_scanning():
        return jsonify({"error": "Stop the current run first."}), 409
    n = adb.reset_errors()
    return jsonify({"message": f"{n} failed files requeued. Press Start to retry them.",
                    "requeued": n})


@archive_bp.post("/api/archive/reset")
@require_admin
def reset():
    if is_scanning():
        return jsonify({"error": "Cannot reset while a run is going."}), 409
    adb.clear_db()
    auth.audit(_conn(), current_user().id, "archive_reset", "")
    return jsonify({"message": "Report cleared. No archived files were touched."})
