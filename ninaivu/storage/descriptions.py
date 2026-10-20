"""A description of each photograph, as the on-device AI engine wrote it.

One row per item in ``descriptions``: a sentence or two about what is in the
picture, written on this computer by Florence-2 (runtime/florence.py) from the
gallery's preview. Kept beside the item rather than in its ``caption``, which
belongs to the household and to the tagger: a description never replaces
words somebody wrote, and a re-tag never replaces a description.

Searched with the item's other words: a full-text index of its own
(``descriptions_fts``), asked beside the gallery's (db.query_assets), so
"grandmother lighting a lamp" finds the photograph nobody tagged.

Shaped as one of the job engine's derivations (2.0, ``ninaivu/engine``), like
the words of voice stories (storage/story_words.py): a **version** stamped on
every row, what is **owed** (a picture with no row, or one from an older
model), how an answer is **stored**, and what is written when a picture
**fails**: it is tried :data:`MAX_FAILURES` times, then set aside until a
newer model.

Only what AI may read is described (``db.AI_MAY_READ``): an item an
administrator hides keeps no description (``forget``, from
db.forget_ai_reading), and one shown again is described again.
"""
from __future__ import annotations

import re
import sqlite3
import time
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS descriptions (
    asset_id   INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
    text       TEXT NOT NULL DEFAULT '',
    model      TEXT NOT NULL DEFAULT '',
    version    INTEGER NOT NULL DEFAULT 0,
    failures   INTEGER NOT NULL DEFAULT 0,
    tried      INTEGER NOT NULL DEFAULT 0,
    error      TEXT NOT NULL DEFAULT '',
    made_at    REAL NOT NULL
);

CREATE TRIGGER IF NOT EXISTS descriptions_follow_asset AFTER DELETE ON assets
BEGIN DELETE FROM descriptions WHERE asset_id = OLD.id; END;
"""

#: The words' own index, kept in step with the table by triggers. Made only
#: where SQLite has full-text search; without it a search reads the table.
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS descriptions_fts USING fts5(
    text, content='descriptions', content_rowid='asset_id', tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS descriptions_ai AFTER INSERT ON descriptions BEGIN
    INSERT INTO descriptions_fts(rowid, text) VALUES (new.asset_id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS descriptions_ad AFTER DELETE ON descriptions BEGIN
    INSERT INTO descriptions_fts(descriptions_fts, rowid, text)
    VALUES ('delete', old.asset_id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS descriptions_au AFTER UPDATE OF text ON descriptions BEGIN
    INSERT INTO descriptions_fts(descriptions_fts, rowid, text)
    VALUES ('delete', old.asset_id, old.text);
    INSERT INTO descriptions_fts(rowid, text) VALUES (new.asset_id, new.text);
END;
"""

#: Attempts at a picture before it is set aside until a newer model.
MAX_FAILURES = 2
#: The most characters kept of one description.
MAX_TEXT = 600
#: The most words of a search matched against descriptions.
_MAX_WORDS = 8


def create(conn: sqlite3.Connection) -> None:
    """Make the table, and its full-text index where SQLite has one."""
    conn.executescript(SCHEMA)
    try:
        conn.executescript(FTS_SCHEMA)
    except sqlite3.OperationalError:
        pass                                           # no FTS5 in this SQLite


def _has_fts(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name='descriptions_fts'").fetchone() \
        is not None


#: The items worth describing: pictures and videos with a preview, in the
#: bin or not shown to AI never.
_DESCRIBABLE = ("a.trashed = 0 AND a.thumb IS NOT NULL AND a.kind IN ('picture', 'video') "
                "AND a.visibility < 2")


def owed(conn: sqlite3.Connection, version: int, limit: int = 200,
         before: tuple[str, int] | None = None) -> list[sqlite3.Row]:
    """Items whose description is missing or older than *version*, newest
    photographs first: last week's are the ones being looked for. Rows of
    ``id``, ``thumb`` and ``date_key``.

    *before* is the last row of the previous page (its ``date_key`` and
    ``id``), so a pass over 200,000 photographs reads each once rather than
    walking past every one already described for every page.
    """
    if version <= 0:
        return []
    after, params = "", [int(version)]
    if before is not None:
        after = "AND (COALESCE(a.date_key, '') < ? OR (COALESCE(a.date_key, '') = ? AND a.id < ?)) "
        params += [str(before[0] or ""), str(before[0] or ""), int(before[1])]
    try:
        return conn.execute(
            "SELECT a.id, a.thumb, COALESCE(a.date_key, '') AS date_key FROM assets a "
            "LEFT JOIN descriptions d ON d.asset_id = a.id "
            f"WHERE {_DESCRIBABLE} AND (d.asset_id IS NULL OR d.version < ?) {after}"
            "ORDER BY COALESCE(a.date_key, '') DESC, a.id DESC LIMIT ?",
            [*params, int(limit)]).fetchall()
    except sqlite3.OperationalError:
        return []


def count_owed(conn: sqlite3.Connection, version: int) -> int:
    if version <= 0:
        return 0
    try:
        return int(conn.execute(
            "SELECT COUNT(*) FROM assets a LEFT JOIN descriptions d ON d.asset_id = a.id "
            f"WHERE {_DESCRIBABLE} AND (d.asset_id IS NULL OR d.version < ?)",
            (int(version),)).fetchone()[0])
    except sqlite3.OperationalError:
        return 0


def store(conn: sqlite3.Connection, asset_id: int, text: str, *, model: str,
          version: int) -> bool:
    """Keep one picture's description. False when the item went, or was
    hidden from AI, while it was being described."""
    from . import db                                            # noqa: PLC0415

    with db._write_lock:                                        # noqa: SLF001
        if not conn.execute(f"SELECT 1 FROM assets WHERE id=? AND {db.AI_MAY_READ}",
                            (int(asset_id),)).fetchone():
            return False
        # An upsert, not INSERT OR REPLACE: a replace deletes the old row
        # without firing its delete trigger, and the words index would keep
        # the old description's words.
        conn.execute(
            "INSERT INTO descriptions(asset_id, text, model, version, failures, "
            "tried, error, made_at) VALUES(?,?,?,?,0,?,'',?) "
            "ON CONFLICT(asset_id) DO UPDATE SET text=excluded.text, model=excluded.model, "
            "version=excluded.version, failures=0, tried=excluded.tried, error='', "
            "made_at=excluded.made_at",
            (int(asset_id), str(text or "")[:MAX_TEXT], str(model)[:40], int(version),
             int(version), time.time()))
        conn.commit()
    return True


def fail(conn: sqlite3.Connection, asset_id: int, version: int, error: str) -> bool:
    """Count one failed attempt. True when the picture is now set aside.

    Before the last attempt the row keeps version 0, so the picture stays
    owed and is tried again next pass; the last one stamps *version*, so it
    is not tried again until a newer model.
    """
    from . import db                                            # noqa: PLC0415

    with db._write_lock:                                        # noqa: SLF001
        if not conn.execute("SELECT 1 FROM assets WHERE id=?", (int(asset_id),)).fetchone():
            return False
        row = conn.execute("SELECT failures, tried FROM descriptions WHERE asset_id=?",
                           (int(asset_id),)).fetchone()
        failures = (int(row[0]) if row and int(row[1]) == int(version) else 0) + 1
        set_aside = failures >= MAX_FAILURES
        conn.execute(
            "INSERT INTO descriptions(asset_id, text, version, failures, tried, error, made_at) "
            "VALUES(?,'',?,?,?,?,?) ON CONFLICT(asset_id) DO UPDATE SET text='', "
            "version=excluded.version, failures=excluded.failures, tried=excluded.tried, "
            "error=excluded.error, made_at=excluded.made_at",
            (int(asset_id), int(version) if set_aside else 0, failures, int(version),
             str(error)[:300], time.time()))
        conn.commit()
    return set_aside


def get(conn: sqlite3.Connection, asset_id: int) -> str | None:
    """One item's description, or None when it has none (yet)."""
    try:
        row = conn.execute("SELECT text FROM descriptions WHERE asset_id=? AND text != ''",
                           (int(asset_id),)).fetchone()
    except sqlite3.OperationalError:
        return None
    return str(row[0]) if row else None


def forget(conn: sqlite3.Connection, ids: Iterable[int]) -> int:
    """Take back the descriptions of these items (hidden from AI). The
    caller holds ``db._write_lock`` and commits."""
    ids = [int(i) for i in ids]
    gone = 0
    try:
        for at in range(0, len(ids), 500):
            part = ids[at:at + 500]
            gone += conn.execute(
                f"DELETE FROM descriptions WHERE asset_id IN ({','.join('?' * len(part))})",
                part).rowcount
    except sqlite3.OperationalError:
        return 0                                    # an index from before descriptions
    return gone


def count(conn: sqlite3.Connection) -> int:
    try:
        return int(conn.execute("SELECT COUNT(*) FROM descriptions WHERE text != ''")
                   .fetchone()[0])
    except sqlite3.OperationalError:
        return 0


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def _words(text: str) -> list[str]:
    return [w for w in re.split(r"\W+", str(text).lower()) if w][:_MAX_WORDS]


def words_clause(conn: sqlite3.Connection, text: str) -> tuple[str, list[Any]] | None:
    """SQL choosing the asset ids whose description holds every word of
    *text* (each as a word or the start of one, as the gallery's own index
    matches), or None when no description does, so a search no description
    answers is built exactly as before."""
    words = _words(text)
    if not words:
        return None
    try:
        if _has_fts(conn):
            match = " AND ".join(f'"{w}"*' for w in words)
            clause = "SELECT rowid FROM descriptions_fts WHERE descriptions_fts MATCH ?"
            params: list[Any] = [match]
        else:
            clause = ("SELECT asset_id FROM descriptions WHERE "
                      + " AND ".join(["LOWER(text) LIKE ?"] * len(words)))
            params = [f"%{w}%" for w in words]
        if not conn.execute(f"SELECT EXISTS({clause})", params).fetchone()[0]:
            return None
    except sqlite3.OperationalError:
        return None                                 # no descriptions table
    return clause, params


#: The most items a description search hands to the image search's ranking.
_MAX_HITS = 2000


def matching_asset_ids(conn: sqlite3.Connection, text: str) -> list[int]:
    """The asset ids a word search finds in descriptions, for the image
    search (api._semantic_ids), which ranks them ahead of the rest. Only
    candidates: the caller narrows them to what the viewer may see."""
    clause = words_clause(conn, text)
    if clause is None:
        return []
    return [int(r[0]) for r in conn.execute(f"{clause[0]} LIMIT {_MAX_HITS}", clause[1])]
