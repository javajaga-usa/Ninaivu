"""Which photographs are colour copies of a black-and-white one, and of which.

A colour copy (``colour_photos``, api/api_colour.py) is an edited copy like
any other: a new file beside its original, saved the way the photo editor
saves one, the original never touched. One row here per copy says which
photograph it came from, so the viewer can lay the two side by side at any
time afterwards ("Before and after"), and so the original can say it has
one. Either photograph leaving the index takes the row with it.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS colour_copies (
    asset_id   INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
    source_id  INTEGER NOT NULL,
    version    INTEGER NOT NULL DEFAULT 0,
    made_by    INTEGER,
    made_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_colour_copies_source ON colour_copies(source_id);

CREATE TRIGGER IF NOT EXISTS colour_copies_follow_asset AFTER DELETE ON assets
BEGIN DELETE FROM colour_copies WHERE asset_id = OLD.id OR source_id = OLD.id; END;
"""


def create(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def record(conn: sqlite3.Connection, copy_id: int, source_id: int, *, version: int = 0,
           made_by: int | None = None) -> None:
    """Note that *copy_id* is the colour copy of *source_id*."""
    conn.execute("INSERT OR REPLACE INTO colour_copies(asset_id, source_id, version, made_by, "
                 "made_at) VALUES (?,?,?,?,?)",
                 (int(copy_id), int(source_id), int(version), made_by, time.time()))
    conn.commit()


def source_of(conn: sqlite3.Connection, copy_id: int) -> int | None:
    """The photograph *copy_id* was coloured from, or None."""
    row = conn.execute("SELECT source_id FROM colour_copies WHERE asset_id=?",
                       (int(copy_id),)).fetchone()
    return int(row[0]) if row else None


def copies_of(conn: sqlite3.Connection, source_id: int) -> list[int]:
    """The colour copies made of *source_id*, newest first."""
    return [int(r[0]) for r in conn.execute(
        "SELECT asset_id FROM colour_copies WHERE source_id=? ORDER BY made_at DESC",
        (int(source_id),))]


def describe(conn: sqlite3.Connection, asset_id: int) -> dict[str, Any]:
    return {"source": source_of(conn, asset_id), "copies": copies_of(conn, asset_id)}
