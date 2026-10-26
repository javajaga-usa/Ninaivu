"""Sent for screening: family members asking an administrator to look at items.

A family member who comes across a photograph that should not be in the
family's gallery — a screenshot of a bank page, a document, something not
meant for the children — cannot hide it themselves. They send it here, and
it waits in the console's Screening queue until an administrator decides:
ignore the request, hide the item (documents and screenshots), mark it
explicit (adult content only) or move it to the recycle bin.

Sending takes the item out of the family's view at once, everywhere: the
gallery, albums, search, films, share links, anything that honours
visibility. It is *held*: its visibility is set to admin-only with the source
``screening``, and what it was is kept in ``screening_holds``. Administrators
still see it. The decision then settles it: Ignore puts back what it was,
Hide and Mark explicit apply on top of that, Delete moves it to the bin. A
hold is not a hide: the AI's reading of the item is kept (db.forget_ai_reading
passes held items by), so an item that comes back has lost nothing.

Two tables. ``screening_requests``: one row per item and person who sent it,
only ever open; the administrator's decision is written to the audit log and
the rows are removed. ``screening_holds``: one row per held item (a live
photo's clip is held with its still), with the visibility to put back. An
item moved to the bin takes its rows with it (the foreign keys), so neither
can outlive its photograph.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Sequence

__all__ = ["SCHEMA", "NOTE_MAX", "ACTIONS", "send", "withdraw", "sent_by", "sent_any",
           "open_count", "queue", "settle", "hold", "release", "HOLD_SOURCE"]

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

CREATE TABLE IF NOT EXISTS screening_holds (
    asset_id       INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
    was_visibility INTEGER,
    was_source     TEXT
);
"""

#: ``assets.vis_source`` while an item is held for screening.
HOLD_SOURCE = "screening"

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
    """An administrator has decided: these items leave the queue, and what
    was held for them is put back as it was (the decision is applied after)."""
    ids = [int(i) for i in asset_ids]
    if not ids:
        return 0
    release(conn, ids)
    from .db import _write_lock                                   # noqa: PLC0415
    with _write_lock:
        cursor = conn.execute(
            f"DELETE FROM screening_requests WHERE asset_id IN ({_marks(ids)})", ids)
        conn.commit()
    return cursor.rowcount


def hold(conn: sqlite3.Connection, asset_ids: Sequence[int]) -> int:
    """Take these items out of the family's view until an admin decides.

    Only what the family can see now is held: an item already admin-only, or
    kept "Only to me", is out of their view already and is left as it is.
    Returns how many rows were held, live clips included.
    """
    from .db import _write_lock, with_live_clips                  # noqa: PLC0415
    ids = with_live_clips(conn, [int(i) for i in asset_ids])
    held = 0
    with _write_lock:
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            marks = _marks(chunk)
            conn.execute(
                f"INSERT OR IGNORE INTO screening_holds (asset_id, was_visibility, was_source) "
                f"SELECT id, visibility, vis_source FROM assets "
                f"WHERE id IN ({marks}) AND COALESCE(visibility, 1) < 2 "
                f"AND private_to IS NULL", chunk)
            cursor = conn.execute(
                f"UPDATE assets SET visibility=2, vis_source=? "
                f"WHERE id IN (SELECT asset_id FROM screening_holds WHERE asset_id IN ({marks})) "
                f"AND COALESCE(visibility, 1) < 2", (HOLD_SOURCE, *chunk))
            held += max(0, cursor.rowcount)
        conn.commit()
    return held


def release(conn: sqlite3.Connection, asset_ids: Sequence[int]) -> int:
    """Put held items back to what they were before they were sent.

    An item whose visibility was changed again while it was held (an admin
    hid its folder, say) keeps that later decision. Returns how many items
    were put back.
    """
    from .db import _write_lock, with_live_clips                  # noqa: PLC0415
    ids = with_live_clips(conn, [int(i) for i in asset_ids])
    restored = 0
    with _write_lock:
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            marks = _marks(chunk)
            cursor = conn.execute(
                f"UPDATE assets SET "
                f"visibility = COALESCE((SELECT was_visibility FROM screening_holds h "
                f"  WHERE h.asset_id = assets.id), 1), "
                f"vis_source = COALESCE((SELECT was_source FROM screening_holds h "
                f"  WHERE h.asset_id = assets.id), 'default') "
                f"WHERE id IN ({marks}) AND vis_source = ? "
                f"AND id IN (SELECT asset_id FROM screening_holds)", (*chunk, HOLD_SOURCE))
            restored += max(0, cursor.rowcount)
            conn.execute(f"DELETE FROM screening_holds WHERE asset_id IN ({marks})", chunk)
        conn.commit()
    return restored
