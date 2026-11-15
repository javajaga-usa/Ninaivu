"""What an administrator decided about each suggested event album.

One row per suggestion somebody answered: made into an album (with the
album's id) or turned down. A suggestion with no row is still waiting. A
turned-down one is never offered again; an album made from one is an
ordinary album from then on, renamed, added to or deleted like any other,
and its suggestion is not offered again either.
"""
from __future__ import annotations

import sqlite3
import time

__all__ = ["SCHEMA", "APPROVED", "DECLINED", "create", "decided", "decide", "forget"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS event_albums (
    key        TEXT PRIMARY KEY,
    kind       TEXT NOT NULL DEFAULT '',
    title      TEXT NOT NULL DEFAULT '',
    state      TEXT NOT NULL,
    album_id   INTEGER,
    decided_by INTEGER,
    decided_at REAL NOT NULL
);
"""

APPROVED, DECLINED = "approved", "declined"


def create(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def decided(conn: sqlite3.Connection) -> dict[str, dict]:
    """Every answered suggestion, by its key."""
    try:
        rows = conn.execute("SELECT * FROM event_albums").fetchall()
    except sqlite3.OperationalError:
        return {}
    return {str(r["key"]): dict(r) for r in rows}


def decide(conn: sqlite3.Connection, key: str, *, kind: str, title: str, state: str,
           album_id: int | None, user_id: int | None) -> None:
    if state not in (APPROVED, DECLINED):
        raise ValueError(state)
    conn.execute(
        "INSERT INTO event_albums(key, kind, title, state, album_id, decided_by, decided_at) "
        "VALUES (?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET kind=excluded.kind, "
        "title=excluded.title, state=excluded.state, album_id=excluded.album_id, "
        "decided_by=excluded.decided_by, decided_at=excluded.decided_at",
        (key, kind, title, state, album_id, user_id, time.time()))
    conn.commit()


def forget(conn: sqlite3.Connection, key: str) -> bool:
    """Offer a turned-down suggestion again. False when there was nothing to forget."""
    cur = conn.execute("DELETE FROM event_albums WHERE key=? AND state=?", (key, DECLINED))
    conn.commit()
    return cur.rowcount > 0
