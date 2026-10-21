"""Moments inside videos: where in a long clip each scene is, as search vectors.

A clip is described for search by one vector (media/scanner.py reads it at
five moments and keeps the middle one), which is enough to find "the beach
video" and never enough to find the candle being blown out at 14:32 of a
twenty-minute birthday. This keeps, for every clip long enough to hold more
than one scene, a vector for each scene and the second it starts at, made by
the same picture-search model the gallery uses (media/video_moments.py), so a
search's words can be matched against every scene of every clip and answered
with a time to open the clip at.

Two tables:

* ``video_moments``: one row per scene, ``(asset_id, at)``, its vector as
  half-precision floats (``vector``), unit length.
* ``video_moments_done``: one row per clip looked at, with the model that
  looked, the :data:`VERSION` of the pass, the file's ``mtime`` then (a clip
  replaced on disk is looked at again), how many scenes it found, and the
  failures for a clip that could not be read.

Only what AI may read is kept (``db.AI_MAY_READ``): an item an administrator
hides loses its moments (:func:`forget`, from db.forget_ai_reading), and one
shown again is looked at again.

Searched by :func:`search`, over every vector at once held in memory as one
half-precision matrix (:func:`_matrix`), read again only when a write has
changed what is stored.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any, Iterable, Sequence

try:
    import numpy as np
except ImportError:                                          # pragma: no cover
    np = None  # type: ignore[assignment]

#: Bumped when the way moments are found changes enough to find them again.
VERSION = 1
#: Clips shorter than this are left to the clip's own vector: a few seconds
#: is one scene, and five moments already describe it.
MIN_SECONDS = 20.0
#: Attempts at a clip before it is set aside until a newer model or pass.
MAX_FAILURES = 2
#: The most clips one search answers with.
MAX_RESULTS = 24
#: And the most moments of one clip.
PER_CLIP = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS video_moments (
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    at       REAL NOT NULL,
    vector   BLOB NOT NULL,
    PRIMARY KEY (asset_id, at)
);

CREATE TABLE IF NOT EXISTS video_moments_done (
    asset_id INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
    model    TEXT NOT NULL DEFAULT '',
    version  INTEGER NOT NULL DEFAULT 0,
    mtime    REAL NOT NULL DEFAULT 0,
    moments  INTEGER NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0,
    error    TEXT NOT NULL DEFAULT '',
    made_at  REAL NOT NULL DEFAULT 0
);

CREATE TRIGGER IF NOT EXISTS video_moments_follow_asset AFTER DELETE ON assets
BEGIN
    DELETE FROM video_moments WHERE asset_id = OLD.id;
    DELETE FROM video_moments_done WHERE asset_id = OLD.id;
END;
"""

#: The clips worth looking into: long enough, in the bin or hidden from AI
#: never, and not the moving half of a Live Photo.
_CLIPS = ("a.trashed = 0 AND a.kind = 'video' AND a.live_clip = 0 AND a.visibility < 2 "
          "AND COALESCE(a.duration, 0) >= ?")

#: Held between searches; see :func:`_matrix`.
_held: dict[str, Any] = {"key": None, "ids": None, "at": None, "matrix": None}
_held_lock = threading.Lock()
_generation = 0


def create(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _changed() -> None:
    global _generation
    with _held_lock:
        _generation += 1


def _owed_where(model: str) -> tuple[str, list[Any]]:
    return (f"{_CLIPS} AND (d.asset_id IS NULL OR "
            "(d.failures < ? AND d.version = 0) OR "
            "(d.version > 0 AND (d.version < ? OR d.model != ? OR d.mtime != COALESCE(a.mtime, 0))))",
            [MIN_SECONDS, MAX_FAILURES, VERSION, str(model)])


def owed(conn: sqlite3.Connection, model: str, limit: int = 50,
         after: int = 0) -> list[sqlite3.Row]:
    """Clips not yet looked into by *model* in this :data:`VERSION`, or
    changed on disk since; newest first by id, from below *after* when it is
    given (0: from the top). Rows of ``id``, ``root``, ``rel_path``,
    ``duration`` and ``mtime``."""
    where, params = _owed_where(model)
    if after:
        where += " AND a.id < ?"
        params.append(int(after))
    try:
        return conn.execute(
            "SELECT a.id, a.root, a.rel_path, a.duration, COALESCE(a.mtime, 0) AS mtime "
            "FROM assets a LEFT JOIN video_moments_done d ON d.asset_id = a.id "
            f"WHERE {where} ORDER BY a.id DESC LIMIT ?", [*params, int(limit)]).fetchall()
    except sqlite3.OperationalError:
        return []


def count_owed(conn: sqlite3.Connection, model: str) -> int:
    where, params = _owed_where(model)
    try:
        return int(conn.execute(
            "SELECT COUNT(*) FROM assets a LEFT JOIN video_moments_done d ON d.asset_id = a.id "
            f"WHERE {where}", params).fetchone()[0])
    except sqlite3.OperationalError:
        return 0


def _blob(vector: Any) -> bytes:
    row = np.asarray(vector, dtype="float32").ravel()
    norm = float(np.linalg.norm(row))
    if norm > 0:
        row = row / norm
    return row.astype("float16").tobytes()


def store(conn: sqlite3.Connection, asset_id: int, moments: Sequence[tuple[float, Any]], *,
          model: str, mtime: float) -> bool:
    """Keep a clip's moments, ``(seconds, vector)`` each, in place of any it
    had. False when the clip went, or was hidden from AI, meanwhile."""
    from . import db                                            # noqa: PLC0415

    rows = [(int(asset_id), round(float(at), 2), _blob(vector)) for at, vector in moments]
    with db._write_lock:                                        # noqa: SLF001
        if not conn.execute(f"SELECT 1 FROM assets WHERE id=? AND {db.AI_MAY_READ}",
                            (int(asset_id),)).fetchone():
            return False
        conn.execute("DELETE FROM video_moments WHERE asset_id=?", (int(asset_id),))
        conn.executemany("INSERT OR REPLACE INTO video_moments(asset_id, at, vector) "
                         "VALUES (?,?,?)", rows)
        conn.execute(
            "INSERT INTO video_moments_done(asset_id, model, version, mtime, moments, failures, "
            "error, made_at) VALUES (?,?,?,?,?,0,'',?) ON CONFLICT(asset_id) DO UPDATE SET "
            "model=excluded.model, version=excluded.version, mtime=excluded.mtime, "
            "moments=excluded.moments, failures=0, error='', made_at=excluded.made_at",
            (int(asset_id), str(model)[:80], VERSION, float(mtime or 0), len(rows), time.time()))
        conn.commit()
    _changed()
    return True


def fail(conn: sqlite3.Connection, asset_id: int, error: str, *, model: str,
         mtime: float) -> bool:
    """Count one failed look at a clip. True when it is now set aside (until
    a newer model or pass, or the file changes)."""
    from . import db                                            # noqa: PLC0415

    with db._write_lock:                                        # noqa: SLF001
        if not conn.execute("SELECT 1 FROM assets WHERE id=?", (int(asset_id),)).fetchone():
            return False
        row = conn.execute("SELECT failures, version FROM video_moments_done WHERE asset_id=?",
                           (int(asset_id),)).fetchone()
        failures = (int(row[0]) if row and int(row[1]) == 0 else 0) + 1
        set_aside = failures >= MAX_FAILURES
        conn.execute(
            "INSERT INTO video_moments_done(asset_id, model, version, mtime, moments, failures, "
            "error, made_at) VALUES (?,?,?,?,0,?,?,?) ON CONFLICT(asset_id) DO UPDATE SET "
            "model=excluded.model, version=excluded.version, mtime=excluded.mtime, moments=0, "
            "failures=excluded.failures, error=excluded.error, made_at=excluded.made_at",
            (int(asset_id), str(model)[:80], VERSION if set_aside else 0, float(mtime or 0),
             failures, str(error)[:300], time.time()))
        conn.commit()
    return set_aside


def forget(conn: sqlite3.Connection, ids: Iterable[int]) -> int:
    """Take back the moments of these items (hidden from AI). The caller
    holds ``db._write_lock`` and commits."""
    ids = [int(i) for i in ids]
    gone = 0
    try:
        for at in range(0, len(ids), 500):
            part = ids[at:at + 500]
            marks = ",".join("?" * len(part))
            gone += conn.execute(f"DELETE FROM video_moments WHERE asset_id IN ({marks})",
                                 part).rowcount
            conn.execute(f"DELETE FROM video_moments_done WHERE asset_id IN ({marks})", part)
    except sqlite3.OperationalError:
        return 0                                    # an index from before moments
    if gone:
        _changed()
    return gone


def of_clip(conn: sqlite3.Connection, asset_id: int) -> list[float]:
    """The seconds a clip's moments start at, in order."""
    try:
        return [float(r[0]) for r in conn.execute(
            "SELECT at FROM video_moments WHERE asset_id=? ORDER BY at", (int(asset_id),))]
    except sqlite3.OperationalError:
        return []


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    """How many clips have been looked into, and how many moments they hold."""
    try:
        row = conn.execute("SELECT COUNT(*), COALESCE(SUM(moments), 0) FROM video_moments_done "
                           "WHERE version > 0").fetchone()
        return {"clips": int(row[0]), "moments": int(row[1])}
    except sqlite3.OperationalError:
        return {"clips": 0, "moments": 0}


def _key(conn: sqlite3.Connection, model: str) -> tuple:
    path = conn.execute("PRAGMA database_list").fetchone()[2] or ""
    row = conn.execute("SELECT COUNT(*), COALESCE(MAX(rowid), 0) FROM video_moments").fetchone()
    return (path, str(model), int(row[0]), int(row[1]), _generation)


def _matrix(conn: sqlite3.Connection, model: str):
    """Every moment *model* made: (asset ids, seconds, half-precision matrix),
    held until something is stored or forgotten."""
    key = _key(conn, model)
    with _held_lock:
        if _held["key"] == key:
            return _held["ids"], _held["at"], _held["matrix"]
    rows = conn.execute(
        "SELECT m.asset_id, m.at, m.vector FROM video_moments m "
        "JOIN video_moments_done d ON d.asset_id = m.asset_id "
        "WHERE d.model = ? AND d.version > 0 ORDER BY m.asset_id, m.at", (str(model),)).fetchall()
    widths = {len(r[2]) for r in rows}
    if not rows or len(widths) != 1:
        ids = np.zeros(0, dtype=np.int64)
        at = np.zeros(0, dtype=np.float32)
        matrix = np.zeros((0, 0), dtype=np.float16)
    else:
        ids = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
        at = np.fromiter((r[1] for r in rows), dtype=np.float32, count=len(rows))
        matrix = np.frombuffer(b"".join(r[2] for r in rows), dtype=np.float16
                               ).reshape(len(rows), widths.pop() // 2)
    with _held_lock:
        _held.update(key=key, ids=ids, at=at, matrix=matrix)
    return ids, at, matrix


#: Rows scored at a time: the half-precision matrix is widened a slice at a
#: time, never all at once.
_SLICE = 32768


def search(conn: sqlite3.Connection, query_vector: Any, model: str, *,
           min_score: float, limit: int = MAX_RESULTS * 4,
           per_clip: int = PER_CLIP) -> list[dict[str, Any]]:
    """The clips whose moments best match *query_vector*, best first:
    ``{"id", "score", "moments": [{"at", "score"}, ...]}``, at most *limit*
    clips and *per_clip* moments each (not two moments of one scene: they are
    at least ten seconds apart). Only candidates: the caller narrows them to
    what the viewer may see."""
    if np is None or query_vector is None:
        return []
    ids, at, matrix = _matrix(conn, model)
    if not len(ids):
        return []
    query = np.asarray(query_vector, dtype="float32").ravel()
    if query.shape[0] != matrix.shape[1]:
        return []
    scores = np.empty(len(ids), dtype=np.float32)
    for start in range(0, len(ids), _SLICE):
        scores[start:start + _SLICE] = matrix[start:start + _SLICE].astype(np.float32) @ query
    good = np.nonzero(scores >= float(min_score))[0]
    if not good.size:
        return []
    good = good[np.argsort(-scores[good], kind="stable")]
    clips: dict[int, dict[str, Any]] = {}
    for row in good:
        clip = int(ids[row])
        found = clips.get(clip)
        if found is None:
            if len(clips) >= limit:
                continue
            found = clips[clip] = {"id": clip, "score": float(scores[row]), "moments": []}
        if len(found["moments"]) >= per_clip:
            continue
        second = float(at[row])
        if any(abs(second - m["at"]) < 10 for m in found["moments"]):
            continue
        found["moments"].append({"at": round(second, 2), "score": round(float(scores[row]), 4)})
    for clip in clips.values():
        clip["score"] = round(clip["score"], 4)
    return list(clips.values())
