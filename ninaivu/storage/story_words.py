"""What was said in each voice story, as the on-device AI engine wrote it down.

One row per story in ``story_words`` (made with the stories, in
storage/stories.py): the language it was told in, the words in that language,
an English translation when the story was not in English and the model can
translate, and the pieces with their start and end, for subtitles.

Shaped as one of the job engine's derivations (2.0, ``ninaivu/engine``), so
it can move onto that engine without changing: a **version** stamped on every
row (the model's, from runtime/speech.py), what is **owed** (a story with no
row, or a row older than the model now downloaded), how an answer is
**stored**, and what is written when a story **fails**. A story whose
recording cannot be written down is tried :data:`MAX_FAILURES` times in all;
after that it is stamped with the version it failed at, so it is not tried
again until a newer model makes it owed, as the job engine sets aside a file
that crashed its worker twice.

Who may read the words is decided exactly as who may hear the story: the
route reaches them through the story's item (api/api_stories.py).
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Iterable

#: Attempts at a story before it is set aside until a newer model.
MAX_FAILURES = 2
#: The most characters kept of what a story says, in each language. Six hours
#: of talking, the longest story believed, is well under this.
MAX_WORDS = 200_000


def owed(conn: sqlite3.Connection, version: int, limit: int = 500) -> list[int]:
    """Stories whose words are missing or older than *version*, newest first:
    the story somebody has just told is the one they will look for."""
    if version <= 0:
        return []
    try:
        rows = conn.execute(
            "SELECT s.id FROM stories s LEFT JOIN story_words w ON w.story_id = s.id "
            "WHERE w.story_id IS NULL OR w.version < ? "
            "ORDER BY s.created_at DESC, s.id DESC LIMIT ?", (int(version), int(limit)))
        return [int(r[0]) for r in rows]
    except sqlite3.OperationalError:
        return []                                   # an index from before stories


def count_owed(conn: sqlite3.Connection, version: int) -> int:
    if version <= 0:
        return 0
    try:
        return int(conn.execute(
            "SELECT COUNT(*) FROM stories s LEFT JOIN story_words w ON w.story_id = s.id "
            "WHERE w.story_id IS NULL OR w.version < ?", (int(version),)).fetchone()[0])
    except sqlite3.OperationalError:
        return 0


def _cut(text: Any) -> str:
    return str(text or "")[:MAX_WORDS]


def store(conn: sqlite3.Connection, story_id: int, result: dict[str, Any]) -> bool:
    """Keep what the engine wrote down for one story. False when the story
    went while it was being listened to."""
    from . import db                                            # noqa: PLC0415

    segments = [{"start": float(s.get("start") or 0), "end": float(s.get("end") or 0),
                 "text": _cut(s.get("text")), "english": _cut(s.get("english"))}
                for s in result.get("segments") or []]
    with db._write_lock:                                        # noqa: SLF001
        if not conn.execute("SELECT 1 FROM stories WHERE id=?", (int(story_id),)).fetchone():
            return False
        conn.execute(
            "INSERT OR REPLACE INTO story_words(story_id, language, words, english, segments, "
            "model, version, failures, error, made_at) VALUES(?,?,?,?,?,?,?,0,'',?)",
            (int(story_id), str(result.get("language") or "")[:8], _cut(result.get("text")),
             _cut(result.get("english")), json.dumps(segments, ensure_ascii=False),
             str(result.get("model") or "")[:40], int(result.get("version") or 0), time.time()))
        conn.commit()
    return True


def fail(conn: sqlite3.Connection, story_id: int, version: int, error: str) -> bool:
    """Count one failed attempt. True when the story is now set aside.

    Before the last attempt the row keeps version 0, so the story stays owed
    and is tried again on the next pass; the last one stamps *version*, so
    it is not tried again until a newer model is downloaded.
    """
    from . import db                                            # noqa: PLC0415

    with db._write_lock:                                        # noqa: SLF001
        if not conn.execute("SELECT 1 FROM stories WHERE id=?", (int(story_id),)).fetchone():
            return False
        row = conn.execute("SELECT failures, tried FROM story_words WHERE story_id=?",
                           (int(story_id),)).fetchone()
        # Counted per model: a story set aside by one is tried afresh by the next.
        failures = (int(row[0]) if row and int(row[1]) == int(version) else 0) + 1
        set_aside = failures >= MAX_FAILURES
        conn.execute(
            "INSERT OR REPLACE INTO story_words(story_id, version, failures, tried, error, "
            "made_at) VALUES(?,?,?,?,?,?)",
            (int(story_id), int(version) if set_aside else 0, failures, int(version),
             str(error)[:300], time.time()))
        conn.commit()
    return set_aside


def _public(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """A row as the API gives it. Rows are read through ``db.connect``'s
    connections, which hand back ``sqlite3.Row``."""
    if row is None or (not row["words"] and not row["english"] and row["error"]):
        return None                                  # failed: nothing to say yet
    try:
        segments = json.loads(row["segments"] or "[]")
    except (TypeError, ValueError):
        segments = []
    return {"language": row["language"], "words": row["words"], "english": row["english"],
            "segments": segments, "model": row["model"]}


def get(conn: sqlite3.Connection, story_id: int) -> dict[str, Any] | None:
    """The words of one story, or None when it has none (yet)."""
    try:
        row = conn.execute("SELECT * FROM story_words WHERE story_id=?",
                           (int(story_id),)).fetchone()
    except sqlite3.OperationalError:
        return None
    return _public(row)


def for_stories(conn: sqlite3.Connection, story_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    """The words of each of these stories that has some."""
    ids = [int(i) for i in story_ids]
    found: dict[int, dict[str, Any]] = {}
    for start in range(0, len(ids), 500):
        piece = ids[start:start + 500]
        try:
            rows = conn.execute(
                f"SELECT * FROM story_words WHERE story_id IN ({','.join('?' * len(piece))})",
                piece).fetchall()
        except sqlite3.OperationalError:
            return {}
        for row in rows:
            public = _public(row)
            if public is not None:
                found[int(row["story_id"])] = public
    return found
