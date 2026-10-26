"""Sent for screening: family members asking an administrator to look at items.

A family member who comes across a photograph that should not be in the
family's gallery — a screenshot of a bank page, a document, something not
meant for the children — cannot hide it themselves. They send it here, and
it waits in the console's Screening queue until an administrator decides:
ignore the request, hide the item (documents and screenshots), mark it
explicit (adult content only) or move it to the recycle bin.

The item stays exactly where it was until then. Sending is a question, not a
decision, so nothing about who can see the item changes when it is sent.

One table, ``screening_requests``: one row per item and person who sent it.
A row is only ever open; the administrator's decision is written to the
audit log and the rows are removed. An item moved to the bin takes its rows
with it (the foreign key), so a request can never outlive its photograph.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Sequence

__all__ = ["SCHEMA", "NOTE_MAX", "ACTIONS", "send", "withdraw", "sent_by", "sent_any",
           "open_count", "queue", "settle"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS screening_requests (
    asset_id   INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    -- A profile id, without a foreign key, as for ask_questions: the index is
    -- also opened where there is no users table. auth.delete_user clears it,
    -- and the request stays: what was asked about is still worth a look.
    user_id    INTEGER,
    note       TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_screening_asset_user
    ON screening_requests(asset_id, user_id);
CREATE INDEX IF NOT EXISTS idx_screening_created
    ON screening_requests(created_at);
"""

#: The most a note may say: "this is my bank statement", not a letter.
NOTE_MAX = 200

#: What an administrator may decide, in the order the console offers them.
ACTIONS = ("ignore", "hide", "explicit", "delete")


def _marks(values: Sequence[Any]) -> str:
    return ",".join("?" * len(values))


def send(conn: sqlite3.Connection, asset_ids: Sequence[int], user_id: int,
         note: str = "") -> tuple[int, int]:
    """Ask for these items to be screened. Returns ``(sent, already)``.

    Sending one again is not an error: the earlier request stands, and a new
    note replaces the old one only when one is given.
    """
    from .db import _write_lock                                   # noqa: PLC0415
    note = (note or "").strip()[:NOTE_MAX]
    now = time.time()
    sent = already = 0
    with _write_lock:
        for asset_id in dict.fromkeys(int(i) for i in asset_ids):
            cursor = conn.execute(
                "INSERT OR IGNORE INTO screening_requests "
                "(asset_id, user_id, note, created_at) VALUES (?, ?, ?, ?)",
                (asset_id, user_id, note, now))
            if cursor.rowcount:
                sent += 1
            else:
                already += 1
                if note:
                    conn.execute("UPDATE screening_requests SET note=? "
                                 "WHERE asset_id=? AND user_id=?", (note, asset_id, user_id))
        conn.commit()
    return sent, already


def withdraw(conn: sqlite3.Connection, asset_ids: Sequence[int], user_id: int) -> int:
    """Take back this person's own requests; somebody else's stay."""
    ids = [int(i) for i in asset_ids]
    if not ids:
        return 0
    from .db import _write_lock                                   # noqa: PLC0415
    with _write_lock:
        cursor = conn.execute(
            f"DELETE FROM screening_requests WHERE user_id=? AND asset_id IN ({_marks(ids)})",
            (user_id, *ids))
        conn.commit()
    return cursor.rowcount


def sent_by(conn: sqlite3.Connection, asset_ids: Sequence[int], user_id: int) -> set[int]:
    """Which of these items this person has sent and is still waiting on."""
    ids = [int(i) for i in asset_ids]
    if not ids or not user_id:
        return set()
    rows = conn.execute(
        f"SELECT asset_id FROM screening_requests WHERE user_id=? "
        f"AND asset_id IN ({_marks(ids)})", (user_id, *ids)).fetchall()
    return {int(r[0]) for r in rows}


def sent_any(conn: sqlite3.Connection, asset_ids: Sequence[int]) -> set[int]:
    """Which of these items anybody has sent and is still waiting on."""
    ids = [int(i) for i in asset_ids]
    if not ids:
        return set()
    rows = conn.execute(
        f"SELECT DISTINCT asset_id FROM screening_requests "
        f"WHERE asset_id IN ({_marks(ids)})", ids).fetchall()
    return {int(r[0]) for r in rows}


def open_count(conn: sqlite3.Connection) -> int:
    """How many items are waiting, each counted once however many sent it."""
    return int(conn.execute(
        "SELECT COUNT(DISTINCT asset_id) FROM screening_requests").fetchone()[0])


def queue(conn: sqlite3.Connection, limit: int = 500) -> list[dict[str, Any]]:
    """The items waiting, oldest request first, each with who sent it.

    ``requests`` is a list of ``{user_id, note, created_at}``; names are the
    caller's to look up, since the users table may not be in this file.
    """
    rows = conn.execute(
        "SELECT asset_id, user_id, note, created_at FROM screening_requests "
        "ORDER BY created_at, asset_id").fetchall()
    items: dict[int, dict[str, Any]] = {}
    for row in rows:
        entry = items.setdefault(int(row["asset_id"]), {
            "asset_id": int(row["asset_id"]), "first_at": row["created_at"],
            "requests": []})
        entry["requests"].append({"user_id": row["user_id"], "note": row["note"],
                                  "created_at": row["created_at"]})
    return list(items.values())[:limit]


def settle(conn: sqlite3.Connection, asset_ids: Sequence[int]) -> int:
    """An administrator has decided: these items leave the queue."""
    ids = [int(i) for i in asset_ids]
    if not ids:
        return 0
    from .db import _write_lock                                   # noqa: PLC0415
    with _write_lock:
        cursor = conn.execute(
            f"DELETE FROM screening_requests WHERE asset_id IN ({_marks(ids)})", ids)
        conn.commit()
    return cursor.rowcount
