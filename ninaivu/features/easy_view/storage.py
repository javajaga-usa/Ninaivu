"""Which profiles see the family app in easy view (``easy_view``).

One row per profile that has it, or had it: whether it is on, and who set it
and when. A profile without a row does not have it. The person can turn it on
or off for themselves; an administrator can do it for anyone, which is how a
grandparent who never opens a menu gets it.

The row is not removed with the profile (the profiles live beside the index,
not in it): a row for a profile that is gone is never read.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Iterable

__all__ = ["SCHEMA", "create", "is_on", "set_on", "on_for"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS easy_view (
    user_id  INTEGER PRIMARY KEY,
    on_      INTEGER NOT NULL DEFAULT 0,
    set_by   INTEGER,
    set_at   REAL NOT NULL
);
"""


def create(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def is_on(conn: sqlite3.Connection, user_id: int) -> bool:
    """Whether profile *user_id* sees the family app in easy view."""
    try:
        row = conn.execute("SELECT on_ FROM easy_view WHERE user_id=?", (int(user_id),)).fetchone()
    except sqlite3.OperationalError:
        return False
    return bool(row and row[0])


def on_for(conn: sqlite3.Connection, user_ids: Iterable[int]) -> set[int]:
    """Which of *user_ids* have easy view on."""
    wanted = {int(u) for u in user_ids}
    if not wanted:
        return set()
    try:
        rows = conn.execute("SELECT user_id FROM easy_view WHERE on_=1").fetchall()
    except sqlite3.OperationalError:
        return set()
    return {int(r[0]) for r in rows} & wanted


def set_on(conn: sqlite3.Connection, user_id: int, on: bool, set_by: int | None) -> None:
    """Turn easy view on or off for profile *user_id*."""
    from ...storage import db                                          # noqa: PLC0415
    with db._write_lock:                                        # noqa: SLF001
        _write(conn, user_id, on, set_by)


def _write(conn: sqlite3.Connection, user_id: int, on: bool, set_by: int | None) -> None:
    conn.execute(
        "INSERT INTO easy_view (user_id, on_, set_by, set_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET on_=excluded.on_, set_by=excluded.set_by, "
        "set_at=excluded.set_at",
        (int(user_id), 1 if on else 0, set_by, time.time()))
    conn.commit()
