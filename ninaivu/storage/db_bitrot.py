"""What the integrity check found, file by file. Part of :mod:`.db`, which
re-exports every name.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from . import db as _db


# ---------------------------------------------------------------------------
# Bitrot Integrity Scrubber
# ---------------------------------------------------------------------------

def record_bitrot_check(
    conn: sqlite3.Connection,
    asset_id: int,
    root: str,
    rel_path: str,
    expected_hash: str | None,
    actual_hash: str | None,
    status: str,
    file_mtime: float | None = None,
    file_size: int | None = None,
) -> None:
    """Write down one check."""
    _db.record_bitrot_checks(conn, [(asset_id, root, rel_path, expected_hash, actual_hash,
                                 status, file_mtime, file_size)])


def record_bitrot_checks(conn: sqlite3.Connection, checks: list[tuple]) -> None:
    """Write down several checks in one transaction, each ``(asset_id, root,
    rel_path, expected_hash, actual_hash, status, file_mtime, file_size)``.

    The storage check gathers about a second of files and writes them here:
    a commit per file was most of the cost of checking a library of small
    photographs. They are written and committed under the one lock, so no
    transaction is left open between files. It used to be (rows written with
    the lock taken and let go each time, the commit later): a scan that took
    the lock meanwhile waited on SQLite for the check's open transaction while
    the check waited for the lock, and after 30 seconds the scan failed with
    "database is locked".
    """
    if not checks:
        return
    now = time.time()
    with _db._write_lock:
        # A scan can remove an asset after the scrubber took its snapshot.
        # Check membership in the INSERT itself so that race cannot abort the
        # rest of the batch (or its alerts) with a foreign-key error.
        conn.executemany(
            "INSERT INTO bitrot_records(asset_id, root, rel_path, expected_hash, "
            "actual_hash, status, file_mtime, file_size, checked_at) "
            "SELECT ?, ?, ?, ?, ?, ?, ?, ?, ? "
            "WHERE EXISTS (SELECT 1 FROM assets WHERE id=?)",
            [(*check, now, check[0]) for check in checks],
        )
        conn.commit()


def last_bitrot_fingerprint(conn: sqlite3.Connection,
                            asset_id: int) -> dict[str, Any] | None:
    """The most recent recorded fingerprint for one asset, if there is one.

    Only rows that actually carry a hash count. A ``missing`` or ``unreadable``
    result records no bytes, so it must not displace the last good fingerprint
    — otherwise unplugging a drive would quietly erase the baseline that a
    later comparison depends on.
    """
    row = conn.execute(
        "SELECT * FROM bitrot_records "
        "WHERE asset_id = ? AND actual_hash IS NOT NULL "
        "ORDER BY id DESC LIMIT 1",
        (int(asset_id),),
    ).fetchone()
    return dict(row) if row else None


#: Every state a scrubber pass can leave a file in.
BITROT_STATES = ("baseline", "verified", "changed", "corrupt", "missing", "unreadable",
                 # Put back from a copy whose bytes matched (storage/repair.py).
                 "repaired")


def current_bitrot_issues(conn: sqlite3.Connection, limit: int = 60) -> list[dict[str, Any]]:
    """Files whose *latest* check found something wrong — not every row that
    ever did, so a file since repaired or put back is not still listed."""
    rows = conn.execute(
        "SELECT b.* FROM bitrot_records b "
        "WHERE b.id = (SELECT MAX(b2.id) FROM bitrot_records b2 WHERE b2.asset_id = b.asset_id) "
        "AND b.status IN ('corrupt', 'missing', 'unreadable') "
        "ORDER BY b.checked_at DESC LIMIT ?", (int(limit),)).fetchall()
    return [dict(r) for r in rows]


def get_bitrot_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """Where the library stands now — one answer per file, not per check.

    Each pass writes a row per file, so counting rows made every total grow
    with the number of runs: three photographs checked three times reported
    nine verified. What anybody actually wants is the current state of each
    file, which is its most recent row.
    """
    total_assets = conn.execute(
        "SELECT COUNT(*) n FROM assets WHERE trashed=0").fetchone()["n"]

    counts = {state: 0 for state in BITROT_STATES}
    # The latest row of each file found in one walk of idx_bitrot_asset, then
    # read by its id. Asking "is this the latest?" of every row instead looked
    # each one up again: 380 ms against 100 ms at three passes of 200,000
    # files, on a figure the Storage check page asks for every two seconds.
    # A row with no asset is never anybody's latest, as before.
    rows = conn.execute(
        "SELECT b.status AS status, COUNT(*) AS n FROM "
        "(SELECT MAX(id) AS id FROM bitrot_records "
        " WHERE asset_id IS NOT NULL GROUP BY asset_id) latest "
        "JOIN bitrot_records b ON b.id = latest.id "
        "GROUP BY b.status"
    ).fetchall()
    for row in rows:
        counts[str(row["status"])] = int(row["n"])

    last_check = conn.execute(
        "SELECT MAX(checked_at) t FROM bitrot_records").fetchone()["t"]
    summary: dict[str, Any] = {
        "total_assets": total_assets,
        "last_checked_at": last_check,
    }
    summary.update(counts)
    # Anything a person should look at, as one number for the console.
    summary["issues"] = counts["corrupt"] + counts["missing"] + counts["unreadable"]
    return summary


def prune_bitrot_records(conn: sqlite3.Connection, chunk: int = 5000) -> int:
    """Drop storage-check rows nothing reads any more. Returns how many went.

    Every check writes a row per file and nothing removed them: a library of
    200,000 photographs checked monthly gained millions of rows a year, all
    carried into every backup. Only each file's latest row, and its latest
    row with a fingerprint (:func:`last_bitrot_fingerprint`), are ever read.
    Called holding ``_write_lock``; commits a piece at a time.
    """
    # The rows to keep are worked out once, then the rest go a range of ids
    # at a time: asking "which to keep" again for every piece read the whole
    # table each time, minutes on a Pi with the index's write lock held.
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _bitrot_keep (id INTEGER PRIMARY KEY)")
    conn.execute("DELETE FROM _bitrot_keep")
    conn.execute(
        "INSERT OR IGNORE INTO _bitrot_keep(id) "
        "SELECT MAX(id) FROM bitrot_records GROUP BY asset_id "
        "UNION SELECT MAX(id) FROM bitrot_records "
        "WHERE actual_hash IS NOT NULL GROUP BY asset_id")
    low, high = conn.execute("SELECT MIN(id), MAX(id) FROM bitrot_records").fetchone()
    removed = 0
    try:
        if low is None:
            return 0
        step = max(1, int(chunk))
        for at in range(int(low), int(high) + 1, step):
            cur = conn.execute(
                "DELETE FROM bitrot_records WHERE id >= ? AND id < ? "
                "AND id NOT IN (SELECT id FROM _bitrot_keep)", (at, at + step))
            conn.commit()
            removed += max(0, cur.rowcount)
        return removed
    finally:
        conn.execute("DELETE FROM _bitrot_keep")
        conn.commit()
