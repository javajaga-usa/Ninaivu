"""Time capsules: photographs and a letter, sealed for somebody until a date.

"For Arun, to open on his eighteenth birthday": a parent chooses the
photographs of his first year, writes him a letter, and seals it. Until that
morning the capsule is a closed envelope in the family app, saying who it is
from, who it is for and when it opens, and nothing else. On the day it opens
with a small ceremony (static/js/capsules.js).

Sealed means sealed. Before the date nobody is shown the letter or the
photographs through Ninaivu: not whoever it is for, not whoever sealed it (they
may take it back, by deleting it, but not peek), not an administrator. The
date is read on this computer's calendar, so moving a phone's clock does not
open one early.

Who it is for. One person in the household, or everybody in it (``for_user``
NULL): family members and administrators, never guests, since a capsule is
the family's own words.

What is inside is never more than the reader may see. The photographs are
chosen from what whoever seals it may see, and when it opens they are shown
through the reader's own limits (``api_capsules``), the check every gallery
listing makes; a photograph hidden since, or kept from that reader, is simply
not there, and the capsule says how many it holds that cannot be shown.

Nothing here leaves the house: the copy of the index kept in Drive leaves both
tables out (cloud/index_copy.py), like the voice stories.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import date
from typing import Any, Sequence

__all__ = ["SCHEMA", "MAX_ITEMS", "MAX_TITLE", "MAX_LETTER", "MAX_YEARS", "create", "get",
           "listed_for", "items_of", "is_reader", "is_sealed", "mark_opened", "delete",
           "parse_date"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS capsules (
    id          INTEGER PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '',
    letter      TEXT NOT NULL DEFAULT '',
    -- The local day it opens, YYYY-MM-DD.
    opens_on    TEXT NOT NULL,
    -- Who it is for; NULL is everybody in the family.
    for_user    INTEGER,
    created_by  INTEGER,
    -- The name it was sealed under, as the household reads it.
    from_name   TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    -- When whoever it is for first opened it (for everybody: the first of
    -- them). Until then the family app says it is waiting to be opened.
    opened_at   REAL
);
CREATE INDEX IF NOT EXISTS idx_capsules_for ON capsules(for_user, opens_on);

CREATE TABLE IF NOT EXISTS capsule_items (
    capsule_id  INTEGER NOT NULL REFERENCES capsules(id) ON DELETE CASCADE,
    asset_id    INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (capsule_id, asset_id)
);

-- The foreign keys already do this where a connection turned them on.
CREATE TRIGGER IF NOT EXISTS capsule_items_go_with_capsule AFTER DELETE ON capsules
BEGIN DELETE FROM capsule_items WHERE capsule_id = OLD.id; END;
CREATE TRIGGER IF NOT EXISTS capsule_items_go_with_asset AFTER DELETE ON assets
BEGIN DELETE FROM capsule_items WHERE asset_id = OLD.id; END;
"""

#: Photographs in one capsule. A year of somebody's life, chosen.
MAX_ITEMS = 300
#: Characters in a title.
MAX_TITLE = 120
#: Characters in the letter: several pages.
MAX_LETTER = 20_000
#: How far ahead a capsule may be sealed for. Long enough for a newborn's
#: wedding; short enough that a mistyped year is caught.
MAX_YEARS = 60


def init(conn: sqlite3.Connection) -> None:
    """Made with the index (``db.init_db``); here for a database made before."""
    conn.executescript(SCHEMA)


def parse_date(value: Any, today: date | None = None) -> str:
    """*value* as the YYYY-MM-DD a capsule opens on: after today and within
    :data:`MAX_YEARS`. ValueError with what is wrong otherwise."""
    today = today or date.today()
    if not isinstance(value, str):
        raise ValueError("Choose the day it opens.")
    try:
        when = date.fromisoformat(value.strip()[:10])
    except ValueError as exc:
        raise ValueError("Choose the day it opens.") from exc
    if when <= today:
        raise ValueError("A time capsule opens on a day after today.")
    if when.year - today.year > MAX_YEARS:
        raise ValueError("A time capsule opens within sixty years.")
    return when.isoformat()


def create(conn: sqlite3.Connection, *, title: str, letter: str, opens_on: str,
           for_user: int | None, created_by: int | None, from_name: str,
           asset_ids: Sequence[int]) -> int:
    """Seal one capsule. The caller has checked the date, the reader and that
    whoever seals it may see every photograph."""
    from . import db                                            # noqa: PLC0415
    ids = list(dict.fromkeys(int(i) for i in asset_ids))[:MAX_ITEMS]
    with db._write_lock:                                        # noqa: SLF001
        try:
            cursor = conn.execute(
                "INSERT INTO capsules(title, letter, opens_on, for_user, created_by, from_name, "
                "created_at) VALUES(?,?,?,?,?,?,?)",
                (title, letter, opens_on, for_user, created_by, from_name, time.time()))
            capsule_id = int(cursor.lastrowid)
            conn.executemany(
                "INSERT OR IGNORE INTO capsule_items(capsule_id, asset_id, position) VALUES(?,?,?)",
                [(capsule_id, asset_id, n) for n, asset_id in enumerate(ids)])
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return capsule_id


def get(conn: sqlite3.Connection, capsule_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM capsules WHERE id=?", (int(capsule_id),)).fetchone()
    return dict(row) if row else None


def is_sealed(capsule: dict[str, Any], today: date | None = None) -> bool:
    return (today or date.today()).isoformat() < str(capsule["opens_on"])


def is_reader(capsule: dict[str, Any], user_id: int, *, guest: bool) -> bool:
    """Whether *user_id* is who the capsule is for."""
    if not user_id or guest:
        return False
    return capsule.get("for_user") is None or int(capsule["for_user"]) == int(user_id)


def listed_for(conn: sqlite3.Connection, user_id: int, *, guest: bool) -> list[dict[str, Any]]:
    """Every capsule this person is shown: the ones for them, and the ones
    they sealed for somebody else. Soonest to open first, opened ones last."""
    if not user_id or guest:
        return []
    rows = conn.execute(
        "SELECT c.id, c.title, c.opens_on, c.for_user, c.created_by, c.from_name, c.created_at, "
        "       c.opened_at, (SELECT COUNT(*) FROM capsule_items i WHERE i.capsule_id = c.id) "
        "       AS items "
        "FROM capsules c WHERE c.for_user IS NULL OR c.for_user = ? OR c.created_by = ? "
        "ORDER BY (c.opened_at IS NOT NULL), c.opens_on, c.id",
        (int(user_id), int(user_id))).fetchall()
    return [dict(r) for r in rows]


def items_of(conn: sqlite3.Connection, capsule_id: int) -> list[int]:
    return [int(r[0]) for r in conn.execute(
        "SELECT asset_id FROM capsule_items WHERE capsule_id=? ORDER BY position, asset_id",
        (int(capsule_id),))]


def mark_opened(conn: sqlite3.Connection, capsule_id: int) -> None:
    from . import db                                            # noqa: PLC0415
    with db._write_lock:                                        # noqa: SLF001
        conn.execute("UPDATE capsules SET opened_at=? WHERE id=? AND opened_at IS NULL",
                     (time.time(), int(capsule_id)))
        conn.commit()


def delete(conn: sqlite3.Connection, capsule_id: int) -> bool:
    from . import db                                            # noqa: PLC0415
    with db._write_lock:                                        # noqa: SLF001
        gone = conn.execute("DELETE FROM capsules WHERE id=?", (int(capsule_id),)).rowcount
        conn.commit()
    return bool(gone)
