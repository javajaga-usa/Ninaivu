"""Where every long job over the library stands: running, how many left, or complete.

The activity strip (activity.py) answers "what is Ninaivu doing right now?"
and nothing else: a job that has finished leaves it, so a household that
waited a week for 180,000 photographs to be described had nowhere to see
that it was done. Nor could anyone tell a job that was finished from one
that was off, or waiting for its model, or for the next scan.

This is the other question, "how far has each one got?", asked of the index
itself rather than of a running pass, so the answer holds after a restart
and between passes:

* the on-device AI engine's jobs (describing photographs, writing down
  voice stories) and the moments of long videos,
  each with its own background worker;
* what a scan works out for every photograph: the image model's tags,
  places, words in photos, faces, videos described by their moments and
  fingerprints.

Each row says ``running``, ``waiting`` (on for this library, with work
left for its next pass or scan), ``complete``, ``off``, or ``unavailable``
(on, but its model or reader is not here, and why). Counting reads the
whole index, so an answer is kept for :data:`KEEP` seconds.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from typing import Any, Callable

from ..storage import db
from ..words import said

log = logging.getLogger(__name__)

#: How long an answer is kept: a page asking again within it is not worth
#: counting 200,000 rows for again.
KEEP = 20.0

RUNNING, WAITING, COMPLETE, OFF, UNAVAILABLE = (
    "running", "waiting", "complete", "off", "unavailable")

#: The scan phase (scanner progress ``status``) that is each scan job at work.
_SCAN_PHASE = {"tags": "tagging", "places": "naming", "text": "reading",
               "faces": "faces", "videos": "videos", "fingerprints": "fingerprinting"}

_cache: dict[str, Any] = {"at": 0.0, "rows": None}
_cache_lock = threading.Lock()


def _row(id_: str, title: str, state: str, *, left: int | None = None,
         total: int | None = None, failed: int = 0, why: str = "",
         finished_at: float = 0.0, page: str = "") -> dict[str, Any]:
    done = None if total is None or left is None else max(0, total - left)
    return {"id": id_, "title": title, "state": state, "left": left, "done": done,
            "total": total, "failed": int(failed), "why": why,
            "finished_at": float(finished_at or 0.0) or None, "page": page}


def _settled(id_: str, title: str, *, on: bool, why_not: str | None, running: bool,
             left: int | None, total: int | None = None, failed: int = 0,
             message: str = "", finished_at: float = 0.0, page: str = "") -> dict[str, Any]:
    """One row's state from what is known about it."""
    if not on:
        state, why = OFF, ""
    elif why_not:
        state, why = UNAVAILABLE, why_not
    elif running:
        state, why = RUNNING, message
    elif left:
        state, why = WAITING, message
    else:
        state, why = COMPLETE, ""
    return _row(id_, title, state, left=left if on and not why_not else None,
                total=total if on and not why_not else None, failed=failed, why=why,
                finished_at=finished_at, page=page)


def _count(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    try:
        return int(conn.execute(sql, params).fetchone()[0])
    except sqlite3.OperationalError:
        return 0


# ---------------------------------------------------------------------------
# The on-device AI engine's workers. Each counts what it owes now, with the
# same version its next pass would use.
# ---------------------------------------------------------------------------

def _descriptions(services: Any, cfg: Any, conn: sqlite3.Connection) -> dict[str, Any]:
    from ..runtime import florence                          # noqa: PLC0415
    from ..storage import descriptions                      # noqa: PLC0415

    describer = getattr(services, "describer", None)
    state = describer.status() if describer is not None else {}
    on = bool(getattr(cfg, "ai_runtime", False)) and bool(getattr(cfg, "describe_photos", True))
    why_not = florence.ready() if on else None
    version = florence.version()
    left = descriptions.count_owed(conn, version) if on and not why_not else None
    # Set aside after failing twice: not owed, and not described either.
    failed = _count(conn, "SELECT COUNT(*) FROM descriptions WHERE text='' AND version>=?",
                    (version,)) if version else 0
    return _settled("descriptions", said("Describing photographs"), on=on, why_not=why_not,
                    running=bool(state.get("running")), left=left,
                    total=descriptions.count_describable(conn), failed=failed,
                    message=str(state.get("message") or ""),
                    finished_at=float(state.get("finished_at") or 0), page="ai-models")


def _story_words(services: Any, cfg: Any, conn: sqlite3.Connection) -> dict[str, Any]:
    from ..runtime import speech, transcriber as transcriber_mod  # noqa: PLC0415
    from ..storage import story_words                       # noqa: PLC0415

    worker = getattr(services, "transcriber", None)
    state = worker.status() if worker is not None else {}
    on = bool(getattr(cfg, "ai_runtime", False))
    why_not = transcriber_mod.ready() if on else None
    left = (story_words.count_owed(conn, speech.version_of(speech.chosen_model()))
            if on and not why_not else None)
    return _settled("story-words", said("Writing down voice stories"), on=on,
                    why_not=why_not, running=bool(state.get("running")), left=left,
                    message=str(state.get("message") or ""),
                    finished_at=float(state.get("finished_at") or 0), page="ai-models")



def _video_moments(services: Any, cfg: Any, conn: sqlite3.Connection) -> dict[str, Any] | None:
    from ..storage import video_moments                     # noqa: PLC0415

    worker = getattr(services, "moment_finder", None)
    state = worker.status() if worker is not None else {}
    on = worker.enabled if worker is not None else False
    if not on:
        # Off by default since the household asked for it off: no row at all.
        return None
    why_not = worker.ready() if on else None
    left = None
    if on and not why_not:
        engine = worker._engine()                           # noqa: SLF001 - ready() just did
        left = video_moments.count_owed(conn, str(getattr(engine, "model_id", "") or ""))
    return _settled("video-moments", said("Finding moments in videos"), on=on,
                    why_not=why_not, running=bool(state.get("running")), left=left,
                    message=str(state.get("message") or ""),
                    finished_at=float(state.get("finished_at") or 0), page="library")


# ---------------------------------------------------------------------------
# What a scan works out for every photograph, over every library folder.
# ---------------------------------------------------------------------------

def _engine_failed(cfg: Any) -> dict[str, int]:
    """Photographs the job engine set aside after failing, by kind. Read
    from engine.db directly: opening the scanner's engine to ask would put
    back tasks a run is holding."""
    from ..engine.engine import database_path               # noqa: PLC0415

    path = database_path(cfg)
    if not path.is_file():
        return {}
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2) as conn:
            return {str(kind): int(n) for kind, n in conn.execute(
                "SELECT kind, COUNT(*) FROM tasks WHERE state='failed' GROUP BY kind")}
    except sqlite3.Error:
        return {}


def _scan_jobs(services: Any, cfg: Any, conn: sqlite3.Connection) -> list[dict[str, Any]]:
    from ..engine import derivations                        # noqa: PLC0415
    from ..media import scanner as scanner_mod             # noqa: PLC0415
    from ..storage import fingerprints                      # noqa: PLC0415

    roots = cfg.library_roots
    scanner = getattr(services, "scanner", None)
    scan = scanner.progress.snapshot() if scanner is not None else {}
    phase = str(scan.get("status") or "") if scan.get("running") else ""
    failed = _engine_failed(cfg)
    may_read = db.AI_MAY_READ

    def over_roots(where: str, params: tuple) -> int:
        return sum(_count(conn, f"SELECT COUNT(*) FROM assets WHERE root=? AND trashed=0 "
                                f"AND {where}", (root, *params)) for root in roots)

    def pass_owed(kind: str) -> tuple[int, int]:
        """(left, total) of one of the scan's passes, by its own owed query.
        The total is the same query at a version no photograph has reached:
        every photograph the pass covers."""
        where = scanner_mod._OWED[kind]                     # noqa: SLF001
        version = scanner_mod._owed_version(kind)           # noqa: SLF001
        never = "\x00never" if isinstance(version, str) else 2 ** 62
        return over_roots(where, (version,)), over_roots(where, (never,))

    rows = []

    tags_where = f"thumb IS NOT NULL AND kind != 'audio' AND {may_read}"
    on = bool(getattr(cfg, "ai_enabled", True))
    rows.append(_settled(
        "tags", said("Analysing with AI"), on=on, why_not=None,
        running=phase == _SCAN_PHASE["tags"],
        left=over_roots(f"ai_version < ? AND {tags_where}", (scanner_mod.AI_VERSION,)),
        total=over_roots(tags_where, ()), page="ai-models"))

    left, total = pass_owed("places")
    rows.append(_settled("places", said("Naming places"), on=True, why_not=None,
                         running=phase == _SCAN_PHASE["places"], left=left, total=total,
                         page="library"))

    on = bool(getattr(cfg, "ocr_enabled", False))
    left, total = pass_owed("text") if on else (None, None)
    rows.append(_settled("text", said("Reading text in photos"), on=on, why_not=None,
                         running=phase == _SCAN_PHASE["text"], left=left, total=total,
                         failed=failed.get("text", 0), page="library"))

    on = bool(getattr(cfg, "faces_enabled", False))
    left, total = pass_owed("faces") if on else (None, None)
    # Text and faces run as one pass, shown as "reading" while text is in it.
    rows.append(_settled("faces", said("Finding faces"), on=on, why_not=None,
                         running=phase in (_SCAN_PHASE["faces"], _SCAN_PHASE["text"]) and bool(left),
                         left=left, total=total, failed=failed.get("faces", 0), page="faces"))

    on = bool(getattr(cfg, "ai_enabled", True)) and \
        int(getattr(cfg, "video_keyframes", 0) or 0) >= 2
    clips = derivations._CLIPS                              # noqa: SLF001
    version = derivations._keyframes_version()              # noqa: SLF001
    rows.append(_settled(
        "videos", said("Describing videos"), on=on, why_not=None,
        running=phase == _SCAN_PHASE["videos"],
        left=sum(_count(conn, f"SELECT COUNT(*) FROM assets WHERE root=? AND {clips}",
                        (root, version)) for root in roots) if on else None,
        total=sum(_count(conn, f"SELECT COUNT(*) FROM assets WHERE root=? AND {clips}",
                         (root, 2 ** 62)) for root in roots) if on else None,
        failed=failed.get("keyframes", 0), page="library"))

    on = fingerprints.enabled(cfg)
    left, total = pass_owed("fingerprints") if on else (None, None)
    rows.append(_settled("fingerprints", said("Fingerprinting files"), on=on, why_not=None,
                         running=phase == _SCAN_PHASE["fingerprints"], left=left, total=total,
                         failed=failed.get("fingerprints", 0), page="library"))
    return rows


#: In the order they are shown: the AI engine's jobs, then the scan's.
WORKERS: list[Callable[[Any, Any, sqlite3.Connection], dict[str, Any] | None]] = [
    _descriptions, _story_words, _video_moments,
]


def _gather(services: Any, cfg: Any) -> list[dict[str, Any]]:
    conn = db.connect(cfg.db_path)
    rows: list[dict[str, Any]] = []
    for source in WORKERS:
        try:
            row = source(services, cfg, conn)
            if row is not None:
                rows.append(row)
        except Exception:                                   # noqa: BLE001 - one row, not the page
            log.exception("could not say how far %s has got", source.__name__)
    try:
        rows.extend(_scan_jobs(services, cfg, conn))
    except Exception:                                       # noqa: BLE001
        log.exception("could not say how far the scan's jobs have got")
    return rows


def report(services: Any, cfg: Any, *, fresh: bool = False) -> dict[str, Any]:
    """Every job's row, and when it was counted."""
    with _cache_lock:
        if not fresh and _cache["rows"] is not None and time.time() - _cache["at"] < KEEP:
            return {"jobs": _cache["rows"], "counted_at": _cache["at"]}
    rows = _gather(services, cfg)
    at = time.time()
    with _cache_lock:
        _cache.update(at=at, rows=rows)
    return {"jobs": rows, "counted_at": at}
