"""The words of a letter, and the reading of them kept (``letters_aloud``).

One row per photograph that has been given its words: what they are, the
language they are read in, whether the text reader found them or somebody
typed them, and who did it last. The reading the voice made of exactly
those words is kept beside them (``reading``: its key, length and when each
sentence starts), so playing a letter a second time is instant and a
memory film can be narrated by it; the sound itself is a file in the state
folder (api/api_letters.py), named by the same key. Changing the words
changes the key, so a reading of old words is never played for new ones.

The row goes with the photograph (``ON DELETE CASCADE``), and its reading's
file with it the next time the readings are swept (:func:`sweep_readings`).
Words the text reader read off a photograph that is hidden afterwards go
as every other AI reading of it does (``db.forget_ai_reading``); words
somebody typed are theirs and stay, as a caption does.
"""
from __future__ import annotations

import contextlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Sequence

__all__ = ["SCHEMA", "create", "get", "put", "keep_reading", "remove", "forget_read",
           "sweep_readings", "SOURCES"]

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
    from . import db                                            # noqa: PLC0415
    source = source if source in SOURCES else "typed"
    with db._write_lock:                                        # noqa: SLF001
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
    from . import db                                            # noqa: PLC0415
    with db._write_lock:                                        # noqa: SLF001
        conn.execute("UPDATE letter_words SET reading=? WHERE asset_id=?",
                     (json.dumps(reading, ensure_ascii=False), int(asset_id)))
        conn.commit()


def remove(conn: sqlite3.Connection, asset_id: int) -> None:
    from . import db                                            # noqa: PLC0415
    with db._write_lock:                                        # noqa: SLF001
        conn.execute("DELETE FROM letter_words WHERE asset_id=?", (int(asset_id),))
        conn.commit()


def forget_read(conn: sqlite3.Connection, asset_ids: Sequence[int]) -> int:
    """Let go of the words the text reader read off these photographs.

    For ``db.forget_ai_reading``, which calls it holding the write lock and
    commits: the photographs were hidden, and what AI read off a hidden one
    is not kept. Typed or corrected words stay. Returns how many went.
    """
    gone = 0
    ids = [int(i) for i in asset_ids]
    for start in range(0, len(ids), 500):
        part = ids[start:start + 500]
        try:
            gone += max(0, conn.execute(
                f"DELETE FROM letter_words WHERE source='read' "
                f"AND asset_id IN ({','.join('?' * len(part))})", part).rowcount)
        except sqlite3.OperationalError:
            return gone
    return gone


#: A reading's file name: the photograph's id and the key of its words.
_READING = re.compile(r"(\d+)-([0-9a-f]+)\.wav")


def sweep_readings(folder: str | Path, conn: sqlite3.Connection) -> int:
    """Remove every reading in *folder* that is not of a photograph's words
    as they are now: its photograph erased, its words removed, changed, or
    forgotten when it was hidden. A reading is the letter said aloud, so it
    does not outlive the words. Returns how many files went."""
    folder = Path(folder)
    # A reading made in the last few minutes may be one whose words are
    # being noted this moment (api/api_letters.py moves the file first).
    settled = time.time() - 600
    try:
        names = [entry.name for entry in folder.iterdir()
                 if entry.is_file() and entry.stat().st_mtime < settled]
    except OSError:
        return 0
    kept: dict[int, str] = {}
    try:
        for asset_id, reading in conn.execute(
                "SELECT asset_id, reading FROM letter_words WHERE reading != ''"):
            with contextlib.suppress(ValueError, TypeError, AttributeError):
                kept[int(asset_id)] = str(json.loads(reading).get("key") or "")
    except sqlite3.OperationalError:
        return 0
    removed = 0
    for name in names:
        found = _READING.fullmatch(name)
        if found is None or kept.get(int(found.group(1))) == found.group(2):
            continue                    # not a reading (a part being written), or wanted
        with contextlib.suppress(OSError):
            (folder / name).unlink()
            removed += 1
    return removed
