"""Where each photograph is: rebuilding and checking ``locations`` (4.0, proposal 3).

``locations`` (storage/migrations.py, migration 2) holds one row per place a
file is known to be, for now its place in the library, and six triggers keep
it in step with ``assets`` and ``content_fingerprints`` as they are written.
What the triggers cannot see is a table put together again from what could be
read: a repair of a damaged index (storage/index_check.py) salvages each
table on its own, and ``locations`` can come out with rows the other two lost,
or without rows they kept. Nothing then sets it right, since every trigger
acts only on a write.

:func:`check` says what differs from what the two tables say, changing
nothing; :func:`rebuild` fills it afresh from them, by the rules of
migration 2, in one transaction. Both read nothing but the index, so they are
cheap: one pass over ``assets`` with its fingerprints.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

from . import migrations

log = logging.getLogger(__name__)

#: Examples kept of each kind of difference, for the log and the console.
SAMPLE = 20

#: The fingerprint a photograph's place should carry: of the file as it is
#: now, the same size and modified time (migration 2's rule).
_EXPECTED = ("(SELECT f.algo || ':' || f.digest FROM content_fingerprints f "
             "WHERE f.asset_id = a.id AND f.size = a.size AND f.mtime = a.mtime)")

#: A photograph whose place in the library is where its row says.
_AT_ITS_PLACE = ("l.asset_id = a.id AND l.place = 'library' "
                 "AND l.root = a.root AND l.rel_path = a.rel_path")


def present(conn: sqlite3.Connection) -> bool:
    """Whether this index has ``locations`` (migration 2 has been applied)."""
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='locations'").fetchone() is not None


def check(conn: sqlite3.Connection) -> dict[str, Any]:
    """How ``locations`` differs from ``assets`` and ``content_fingerprints``.

    ``missing``: photographs with no place where their row says they are;
    ``extra``: places of no photograph, or not where its row says;
    ``wrong_fingerprint``: a place whose fingerprint is not the file's as it
    is now. Each a count, with up to :data:`SAMPLE` ids (the photograph's for
    missing and wrong, the place's for extra). ``agrees`` when all are none.
    Changes nothing.
    """
    if not present(conn):
        return {"present": False, "agrees": False, "missing": 0, "extra": 0,
                "wrong_fingerprint": 0, "examples": {}}
    found = {
        "missing": [r[0] for r in conn.execute(
            "SELECT a.id FROM assets a WHERE NOT EXISTS "
            f"(SELECT 1 FROM locations l WHERE {_AT_ITS_PLACE}) ORDER BY a.id")],
        "extra": [r[0] for r in conn.execute(
            "SELECT l.id FROM locations l LEFT JOIN assets a ON a.id = l.asset_id "
            "WHERE a.id IS NULL OR (l.place = 'library' AND "
            "(l.root IS NOT a.root OR l.rel_path IS NOT a.rel_path)) ORDER BY l.id")],
        "wrong_fingerprint": [r[0] for r in conn.execute(
            f"SELECT a.id FROM assets a JOIN locations l ON {_AT_ITS_PLACE} "
            f"WHERE l.fingerprint IS NOT {_EXPECTED} ORDER BY a.id")],
    }
    out: dict[str, Any] = {"present": True, "agrees": not any(found.values())}
    out.update({kind: len(ids) for kind, ids in found.items()})
    out["examples"] = {kind: ids[:SAMPLE] for kind, ids in found.items() if ids}
    return out


def rebuild(conn: sqlite3.Connection) -> int:
    """Fill ``locations`` afresh from ``assets`` and ``content_fingerprints``.

    One transaction: every place in the library is written again, by the
    rules of migration 2, and the triggers are made again if a repair lost
    them. A place that was already right keeps the time it was first seen
    there. Places of photographs no longer in ``assets`` go. Returns the
    places written; 0, changing nothing, for an index migration 2 has not
    reached yet (it fills the table when it does).
    """
    from . import db                                     # noqa: PLC0415
    if not present(conn):
        return 0
    with db._write_lock:                                  # noqa: SLF001
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in migrations.LOCATIONS:
                conn.execute(statement)
            conn.execute("DROP TABLE IF EXISTS temp.locations_seen")
            conn.execute("CREATE TEMP TABLE locations_seen AS SELECT asset_id, root, "
                         "rel_path, seen_at FROM locations WHERE place = 'library'")
            conn.execute("DELETE FROM locations WHERE place = 'library' "
                         "OR asset_id NOT IN (SELECT id FROM assets)")
            written = conn.execute(
                "INSERT INTO locations(asset_id, fingerprint, root, rel_path, place, seen_at) "
                f"SELECT a.id, {_EXPECTED}, a.root, a.rel_path, 'library', "
                "COALESCE(s.seen_at, a.added_at, NULLIF(a.indexed_at, 0), 0) "
                "FROM assets a LEFT JOIN temp.locations_seen s ON s.asset_id = a.id "
                "AND s.root = a.root AND s.rel_path = a.rel_path ORDER BY a.id").rowcount
            conn.execute("DROP TABLE temp.locations_seen")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    log.info("index: locations rebuilt, %s photographs given their place", f"{written:,}")
    return written


def health(conn: sqlite3.Connection) -> dict[str, Any]:
    """For the console: photographs (not in the bin), how many have a
    fingerprint of the file as it is now, how many wait for one, and whether
    ``locations`` agrees with ``assets`` (:func:`check`, the bin included)."""
    total, current = (int(n or 0) for n in conn.execute(
        f"SELECT COUNT(*), SUM({_EXPECTED} IS NOT NULL) FROM assets a "
        "WHERE a.trashed = 0").fetchone())
    return {"photographs": total, "fingerprinted": current, "waiting": total - current,
            "locations": check(conn)}


def let_go_paths(conn: sqlite3.Connection, root: str) -> set[str]:
    """The files of *root* let go to free space (mugil/free.py), kept on the
    Secondary and not missing here, by ``rel_path``.

    ``mugil_let_go`` is keyed by path, and every writer that moves a row
    carries it (storage/reroot.py, nested_roots.py). Should a row move
    without it, the let-go record is left at a path no photograph is at, and
    the photograph is known by its fingerprint instead: the one row of
    *root* whose place carries it. Only then, so an exact copy elsewhere in
    the library is never taken for the let-go one while that one's row is
    where it was. The answer only ever keeps a row from being taken for
    missing; it lets nothing go and brings nothing back.
    """
    try:
        paths = {r[0] for r in conn.execute(
            "SELECT rel_path FROM mugil_let_go WHERE root=?", (str(root),))}
    except sqlite3.OperationalError:
        return set()                     # an index made before the table was
    if not paths or not present(conn):
        return paths
    paths.update(r[0] for r in conn.execute(
        "SELECT MIN(l.rel_path) FROM mugil_let_go g "
        "JOIN locations l ON l.fingerprint = g.algo || ':' || g.digest "
        "AND l.root = g.root AND l.place = 'library' "
        "WHERE g.root = ? AND NOT EXISTS (SELECT 1 FROM assets a "
        "WHERE a.root = g.root AND a.rel_path = g.rel_path) "
        "GROUP BY g.root, g.rel_path HAVING COUNT(*) = 1", (str(root),)))
    return paths
