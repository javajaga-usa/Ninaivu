"""The words of a letter, and the reading of them kept (``letters_aloud``).

One row per photograph that has been given its words: what they are, the
language they are read in, whether the text reader found them or somebody
typed them, and who did it last. The reading the voice made of exactly
those words is kept beside them (``reading``: its key, length and when each
sentence starts), so playing a letter a second time is instant and a
memory film can be narrated by it; the sound itself is a file in the state
folder (api/api_letters.py), named by the same key. Changing the words
changes the key, so a reading of old words is never played for new ones.

The row goes with the photograph (``ON DELETE CASCADE``).
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

__all__ = ["SCHEMA", "create", "get", "put", "keep_reading", "remove", "SOURCES"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS letter_words (
    asset_id    INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
    words       TEXT NOT NULL,
    language    TEXT NOT NULL DEFAULT 'ta',
    source      TEXT NOT NULL DEFAULT 'typed',   -- 'read' by the text reader, or 'typed'
    written_by  INTEGER,
    written_at  REAL NOT NULL,
    reading     TEXT NOT NULL DEFAULT ''         -- {"key", "duration", "segments", "voices"}
);
"""

#: Where the words came from.
SOURCES = ("read", "typed")


def create(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def get(conn: sqlite3.Connection, asset_id: int) -> dict[str, Any] | None:
    """The words of *asset_id*, with the reading kept of them, or None."""
    try:
        row = conn.execute(
            "SELECT words, language, source, written_by, written_at, reading "
            "FROM letter_words WHERE asset_id=?", (int(asset_id),)).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    try:
        reading = json.loads(row[5]) if row[5] else None
    except ValueError:
        reading = None
    return {"words": row[0], "language": row[1], "source": row[2], "written_by": row[3],
            "written_at": row[4], "reading": reading if isinstance(reading, dict) else None}


def put(conn: sqlite3.Connection, asset_id: int, words: str, language: str, source: str,
        written_by: int | None) -> None:
    """Keep *words* for *asset_id*. A reading of other words is let go."""
    source = source if source in SOURCES else "typed"
    old = get(conn, asset_id)
    reading = ""
    if old and old["reading"] and old["words"] == words and old["language"] == language:
        reading = json.dumps(old["reading"], ensure_ascii=False)
    conn.execute(
        "INSERT INTO letter_words(asset_id, words, language, source, written_by, written_at, "
        "reading) VALUES (?,?,?,?,?,?,?) ON CONFLICT(asset_id) DO UPDATE SET "
        "words=excluded.words, language=excluded.language, source=excluded.source, "
        "written_by=excluded.written_by, written_at=excluded.written_at, "
        "reading=excluded.reading",
        (int(asset_id), words, language, source, written_by, time.time(), reading))
    conn.commit()


def keep_reading(conn: sqlite3.Connection, asset_id: int, reading: dict[str, Any]) -> None:
    """Note the reading the voice made of the words *asset_id* has now."""
    conn.execute("UPDATE letter_words SET reading=? WHERE asset_id=?",
                 (json.dumps(reading, ensure_ascii=False), int(asset_id)))
    conn.commit()


def remove(conn: sqlite3.Connection, asset_id: int) -> None:
    conn.execute("DELETE FROM letter_words WHERE asset_id=?", (int(asset_id),))
    conn.commit()
