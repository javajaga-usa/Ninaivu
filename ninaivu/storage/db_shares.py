"""Share links: made, read, counted and removed. Part of :mod:`.db`, which
re-exports every name.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from . import db as _db


# ---------------------------------------------------------------------------
# Tokenized Shares
# ---------------------------------------------------------------------------

def create_share(
    conn: sqlite3.Connection,
    token: str,
    scope: str,
    target_id: int,
    created_by: int | None = None,
    expires_at: float | None = None,
    password: str | None = None,
) -> dict[str, Any]:
    now = time.time()
    with _db._write_lock:
        conn.execute(
            "INSERT INTO shares(token, scope, target_id, created_at, created_by, expires_at, password, view_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
            (token, scope, target_id, now, created_by, expires_at, password),
        )
        conn.commit()
    return get_share(conn, token) or {}


def get_share(conn: sqlite3.Connection, token: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM shares WHERE token=?", (token,)).fetchone()
    if not row:
        return None
    d = dict(row)
    if d.get("expires_at") and time.time() > float(d["expires_at"]):
        d["expired"] = True
    else:
        d["expired"] = False
    return d


def increment_share_views(conn: sqlite3.Connection, token: str) -> None:
    with _db._write_lock:
        conn.execute("UPDATE shares SET view_count = view_count + 1 WHERE token=?", (token,))
        conn.commit()


def delete_share(conn: sqlite3.Connection, token: str) -> None:
    with _db._write_lock:
        conn.execute("DELETE FROM shares WHERE token=?", (token,))
        conn.commit()


def list_shares(conn: sqlite3.Connection, created_by: int | None = None) -> list[dict[str, Any]]:
    if created_by is not None:
        rows = conn.execute("SELECT * FROM shares WHERE created_by=? ORDER BY created_at DESC", (created_by,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM shares ORDER BY created_at DESC").fetchall()
    now = time.time()
    res = []
    for r in rows:
        d = dict(r)
        d["expired"] = bool(d.get("expires_at") and now > float(d["expires_at"]))
        d.pop("password", None)
        res.append(d)
    return res
