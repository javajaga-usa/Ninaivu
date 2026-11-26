"""Photographs somebody removed on purpose, and the one question about them.

A photograph deleted in Ninaivu leaves a record: ``recycled`` while it is in
the bin, ``erased`` once the bin lets it go. One removed in Finder, or by any
other program, left nothing. The storage check then read it as "missing",
the automatic repair put it back from the second copy or Drive, and once a
scan had forgotten it the Secondary could bring it back again: a photograph
the household threw away kept coming home.

Ninaivu cannot tell a file removed on purpose from one that was lost, so it
asks. The Storage check lists what it could not find apart from what is
damaged, and the person says which it is: :func:`mark_removed` records the
ones removed on purpose in ``erased``, the same record emptying the bin
leaves, and forgets them; the others can be put back from a copy
(storage/repair.py, with ``missing=True``). A scan that finds a file gone and
forgets it records it the same way (db_assets.delete_missing), since that is
the moment the library stops describing it.

:func:`deleted_on_purpose` is the one question everything that could bring a
photograph back asks first: the Secondary's bring-in, the two-computer
check, the repair.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

__all__ = ["deleted_on_purpose", "record_erased", "gone", "gone_count", "mark_removed"]

#: Files whose latest storage check found them not on the disk: a library
#: photograph, not trashed, not let go to the other computer of two.
_GONE = ("FROM assets a JOIN bitrot_records b ON b.id = (SELECT MAX(b2.id) "
         "FROM bitrot_records b2 WHERE b2.asset_id = a.id) "
         "WHERE b.status = 'missing' AND a.trashed = 0 "
         "AND NOT EXISTS (SELECT 1 FROM mugil_let_go g "
         "WHERE g.root = a.root AND g.rel_path = a.rel_path)")


def deleted_on_purpose(conn: sqlite3.Connection | None, root: str, rel_path: str,
                       size: int | None = None) -> bool:
    """Whether somebody here deleted the photograph at *rel_path* of *root*:
    it is in the bin, or the bin let it go, or it was removed outside
    Ninaivu and said to be removed on purpose. With *size*, a record of the
    same place but another size (a different photograph put there since)
    does not count once it left the bin."""
    if conn is None:
        return False
    try:
        if conn.execute("SELECT 1 FROM recycled WHERE root = ? AND rel_path = ? "
                        "AND restored_at IS NULL LIMIT 1", (root, rel_path)).fetchone():
            return True
        if size is None:
            return conn.execute("SELECT 1 FROM erased WHERE root = ? AND rel_path = ? "
                                "LIMIT 1", (root, rel_path)).fetchone() is not None
        return conn.execute("SELECT 1 FROM erased WHERE root = ? AND rel_path = ? "
                            "AND size = ? LIMIT 1",
                            (root, rel_path, int(size))).fetchone() is not None
    except sqlite3.Error:
        return False


def record_erased(conn: sqlite3.Connection, rows: Iterable[Any],
                  when: float | None = None) -> int:
    """Write an ``erased`` record for each asset row (root, rel_path, size).

    Only writes; the caller holds the write lock and commits, in the same
    transaction that forgets the rows. Returns how many were written.
    """
    stamp = time.time() if when is None else float(when)
    values = [(str(r["root"]), str(r["rel_path"]), int(r["size"] or 0), stamp)
              for r in rows]
    if values:
        conn.executemany("INSERT OR REPLACE INTO erased(root, rel_path, size, erased_at) "
                         "VALUES (?, ?, ?, ?)", values)
    return len(values)


def gone(conn: sqlite3.Connection, limit: int = 500) -> list[dict[str, Any]]:
    """What the storage check could not find, newest check first."""
    try:
        return [dict(r) for r in conn.execute(
            "SELECT a.id, a.root, a.rel_path, a.filename, a.size, b.checked_at "
            + _GONE + " ORDER BY b.checked_at DESC, a.id LIMIT ?", (int(limit),))]
    except sqlite3.OperationalError:
        return []                        # an index made before the tables were


def gone_count(conn: sqlite3.Connection) -> int:
    try:
        return int(conn.execute("SELECT COUNT(*) " + _GONE).fetchone()[0])
    except sqlite3.OperationalError:
        return 0


def mark_removed(conn: sqlite3.Connection, asset_ids: Sequence[int] | None = None,
                 roots: Iterable[str] | None = None) -> dict[str, Any]:
    """Say these photographs were removed on purpose, outside Ninaivu.

    *asset_ids* None: everything the storage check could not find. Each must
    be one the check found missing, and is taken only when its library folder
    is there (a drive that is away is not a drive whose files were deleted)
    and the file is still not at its place. Taken ones get an ``erased``
    record and leave the index, in one transaction. *roots*, when given,
    are the library folders the caller may manage.

    Returns ``removed`` (how many), ``kept`` (``{"name", "why"}`` for each
    refused) and ``thumbs`` (thumbnail names for the caller to remove).
    """
    from . import db                                     # noqa: PLC0415
    from . import roots as roots_kit                     # noqa: PLC0415

    rows = gone(conn, limit=1_000_000)
    if asset_ids is not None:
        wanted = {int(i) for i in asset_ids}
        rows = [r for r in rows if int(r["id"]) in wanted]
    allowed = set(roots) if roots is not None else None
    present: dict[str, bool] = {}
    taken: list[dict[str, Any]] = []
    kept: list[dict[str, str]] = []
    for row in rows:
        root = str(row["root"])
        name = str(row["rel_path"])
        if allowed is not None and root not in allowed:
            kept.append({"name": name, "why": "outside the folders you can manage"})
            continue
        if root not in present:
            present[root] = roots_kit.root_present(root)
        if not present[root]:
            kept.append({"name": name,
                         "why": "its library folder is not there — is the disk plugged in?"})
            continue
        if (Path(root) / name).exists():
            kept.append({"name": name,
                         "why": "it is back where it was; the next check will see it"})
            continue
        taken.append(row)
    thumbs: list[str] = []
    if taken:
        ids = [int(r["id"]) for r in taken]
        with db._write_lock:                              # noqa: SLF001 — same package
            for start in range(0, len(ids), 500):
                piece = ids[start:start + 500]
                thumbs += [r[0] for r in conn.execute(
                    f"SELECT thumb FROM assets WHERE id IN ({','.join('?' * len(piece))}) "
                    "AND thumb IS NOT NULL AND thumb != ''", piece)]
            record_erased(conn, taken)
            conn.executemany("DELETE FROM assets WHERE id = ?", [(i,) for i in ids])
            conn.commit()
    return {"removed": len(taken), "kept": kept, "thumbs": thumbs}
