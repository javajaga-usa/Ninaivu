"""The words written for a life in photographs (media/life_story.py).

The writing model takes a minute or two to write somebody's chapters, so
what it wrote is kept here and read back the next time the story is opened,
after a restart too. Words are kept under the facts they were written from
(``media.life_story.fingerprint``): one viewer who may see more photographs
than another sees different facts, so neither is ever shown words written
from the other's. When the photographs change, the facts change, and the
words are written afresh; the oldest few are let go.

The rows go with the person (``ON DELETE CASCADE``); a merge simply has new
facts to write from.
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

__all__ = ["SCHEMA", "create", "get", "put", "KEEP"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS life_story_words (
    person_id   INTEGER NOT NULL REFERENCES people_clusters(id) ON DELETE CASCADE,
    language    TEXT NOT NULL,
    facts       TEXT NOT NULL,      -- media.life_story.fingerprint
    written     TEXT NOT NULL,      -- {"opening", "chapters": [{"title", "text"}]}
    model       TEXT NOT NULL DEFAULT '',
    written_at  REAL NOT NULL,
    PRIMARY KEY (person_id, language, facts)
);
"""

#: Words kept for each person and language: the newest few.
KEEP = 4


def create(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def get(conn: sqlite3.Connection, person_id: int, language: str, facts: str) -> dict[str, Any] | None:
    """What was written for exactly these facts, or None."""
    try:
        row = conn.execute(
            "SELECT written, model, written_at FROM life_story_words "
            "WHERE person_id=? AND language=? AND facts=?",
            (int(person_id), language, facts)).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    try:
        written = json.loads(row[0])
    except ValueError:
        return None
    return {**written, "model": row[1], "written_at": row[2]}


def put(conn: sqlite3.Connection, person_id: int, language: str, facts: str,
        written: dict[str, Any], model: str = "") -> None:
    """Keep *written* for these facts, letting the oldest go past :data:`KEEP`."""
    from . import db                                            # noqa: PLC0415
    with db._write_lock:                                        # noqa: SLF001
        create(conn)
        conn.execute(
            "INSERT OR REPLACE INTO life_story_words(person_id, language, facts, written, model, "
            "written_at) VALUES (?,?,?,?,?,?)",
            (int(person_id), language, facts, json.dumps(written, ensure_ascii=False), model,
             time.time()))
        conn.execute(
            "DELETE FROM life_story_words WHERE person_id=? AND language=? AND facts NOT IN ("
            "SELECT facts FROM life_story_words WHERE person_id=? AND language=? "
            "ORDER BY written_at DESC LIMIT ?)",
            (int(person_id), language, int(person_id), language, KEEP))
        conn.commit()
