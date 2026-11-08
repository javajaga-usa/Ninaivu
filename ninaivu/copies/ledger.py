"""The ledger: which photograph is kept where, by what it is.

Each way of keeping a copy has always kept its own record, keyed by where
the file lives in the library: ``mirror_copies`` for the second disk,
``offsite_copies`` for the encrypted copy away from home, ``cloud_uploads``
for Google Drive, and ``mugil-sent.db`` for what the Secondary confirmed.
Four records, four shapes, and no one place that answers the household's
question: *is this photograph kept somewhere else, and where?*

The ledger is that place: one row per photograph per destination, keyed by
the photograph's fingerprint (storage/fingerprints.py) rather than its path,
so a file moved to another folder is still the same photograph kept in the
same places. Its table, ``copies``, is index migration 3
(storage/migrations.py).

In this stage the engines keep writing their own records exactly as before,
and nothing here changes what any of them sends. The ledger is filled *from*
those records (:func:`fill`): read only, mapped from path to fingerprint
through ``content_fingerprints``, and only where the file is still the file
the record was made for (the same size and modified time). A record whose
file has changed since says nothing about the photograph as it is now, and
is left out. Nothing is sent, re-sent or forgotten by filling it.

A row is ``kept`` while the engine's record says the copy is there. One whose
record has gone (the second disk found a bad copy and dropped it, a
different disk was plugged in) becomes ``unconfirmed`` rather than being
deleted: the ledger only ever adds what it learns, as the copies do.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable, Sequence

log = logging.getLogger(__name__)

__all__ = ["KEPT", "UNCONFIRMED", "SOURCES", "fill", "refresh", "headline",
           "counts", "holds", "state_dir_of"]

#: The copy is there, as far as the engine that made it knows.
KEPT = "kept"
#: The engine's record of it has gone: it may be there, but nobody says so.
UNCONFIRMED = "unconfirmed"

#: The file on the Primary where the Secondary's confirmations are kept
#: (mugil/courier.py SENT): named here so filling the ledger does not import
#: the courier, which is not needed to read it.
SENT = "mugil-sent.db"

#: A file in the library, joined to its fingerprint when the fingerprint is
#: of the file as it is now. ``a`` is ``assets``, ``f`` its fingerprint.
_FINGERPRINTED = ("JOIN content_fingerprints f ON f.asset_id = a.id "
                  "AND f.size = a.size AND f.mtime = a.mtime")

#: The fingerprint as the ledger and ``locations`` both write it, ``algo:digest``
#: ("blake3:9f…"), so one made without BLAKE3 can never be taken for one made
#: with it, and the two tables can be joined.
_FP = "(f.algo || ':' || f.digest)"


def _has_table(conn: sqlite3.Connection, name: str, schema: str = "main") -> bool:
    return conn.execute(f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _meta(conn: sqlite3.Connection, table: str, key: str) -> str:
    if not _has_table(conn, table):
        return ""
    row = conn.execute(f"SELECT value FROM {table} WHERE key=?", (key,)).fetchone()
    return str(row[0]) if row else ""


def state_dir_of(conn: sqlite3.Connection) -> Path | None:
    """The folder the index file is in: the state folder, for ``index.db``
    and for a replica's own copies of a hand-over alike. None in memory."""
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main":
            return Path(row[2]).parent if row[2] else None
    return None


# -- what each engine's record says it keeps -------------------------------------
#
# Each source fills ``temp.copies_found`` with (fingerprint, size, verified_at)
# and returns True, or returns False when its record could not be read at all,
# in which case the ledger's rows for it are left as they are.

def _from_mirror(conn: sqlite3.Connection, **_: Any) -> bool:
    """The second disk (storage/mirror.py): a copy recorded at the size and
    modified time the file has now. Verified when it was copied, or at the
    last whole check of the disk, which drops the record of any bad copy."""
    if not _has_table(conn, "mirror_copies"):
        return True
    checked = float(_meta(conn, "mirror_meta", "last_verified") or 0)
    conn.execute(
        "INSERT OR IGNORE INTO temp.copies_found(fingerprint, size, verified_at) "
        "SELECT " + _FP + ", a.size, MAX(MAX(m.copied_at, ?)) FROM mirror_copies m "
        "JOIN assets a ON a.root = m.root AND a.rel_path = m.rel_path AND a.size = m.size "
        f"AND ABS(a.mtime - m.mtime) < 0.001 {_FINGERPRINTED} GROUP BY f.algo, f.digest", (checked,))
    return True


def _from_offsite(conn: sqlite3.Connection, *, offsite_where: str | None = None,
                  **_: Any) -> bool:
    """The encrypted copy away from home (cloud/offsite.py): a file recorded as
    sent at the size and modified time it has now, to the destination now
    chosen (when the caller knows which that is)."""
    if not _has_table(conn, "offsite_copies"):
        return True
    known = _meta(conn, "offsite_meta", "dest_where")
    if offsite_where is not None and known and known != offsite_where:
        return True                          # sent somewhere else: nothing is here yet
    conn.execute(
        "INSERT OR IGNORE INTO temp.copies_found(fingerprint, size, verified_at) "
        "SELECT " + _FP + ", a.size, MAX(o.uploaded_at) FROM offsite_copies o "
        "JOIN assets a ON a.root = o.root AND a.rel_path = o.rel_path AND a.size = o.size "
        f"AND ABS(a.mtime - o.mtime) < 0.001 {_FINGERPRINTED} GROUP BY f.algo, f.digest")
    return True


def _from_drive(conn: sqlite3.Connection, **_: Any) -> bool:
    """Google Drive (cloud/store.py): uploaded, at the size the file has now.
    The record keeps no modified time a 3.x row can be trusted for, so the
    size is the test, as it is for the queue itself."""
    if not _has_table(conn, "cloud_uploads"):
        return True
    since = ""
    if "source_mtime" in _columns(conn, "cloud_uploads"):
        # Where the queue noted the file's time, a different one is a file
        # changed since it went up.
        since = "AND (c.source_mtime = 0 OR ABS(a.mtime - c.source_mtime) < 0.001) "
    conn.execute(
        "INSERT OR IGNORE INTO temp.copies_found(fingerprint, size, verified_at) "
        "SELECT " + _FP + ", a.size, MAX(c.done_at) FROM cloud_uploads c "
        "JOIN assets a ON a.root = c.root AND a.rel_path = c.rel_path AND a.size = c.size "
        f"{since}{_FINGERPRINTED} WHERE c.state = 'done' GROUP BY f.algo, f.digest")
    return True


def _from_secondary(conn: sqlite3.Connection, *, state_dir: Path | str | None = None,
                    **_: Any) -> bool:
    """The Secondary (mugil/courier.py): what it confirmed holding, by path,
    size, modified time and its own fingerprint, kept in ``mugil-sent.db``
    beside the index. Read through a connection of its own, read-only: the
    courier may be writing to it."""
    folder = Path(state_dir) if state_dir else state_dir_of(conn)
    if folder is None or not (folder / SENT).is_file():
        return True
    try:
        with closing(sqlite3.connect(f"{(folder / SENT).as_uri()}?mode=ro", uri=True,
                                     timeout=30)) as sent:
            if not _has_table(sent, "sent"):
                return True
            rows = sent.execute("SELECT root, rel_path, size, mtime, algo, digest, sent_at "
                                "FROM sent").fetchall()
    except sqlite3.Error as exc:
        log.info("the Secondary's record could not be read for the ledger: %s", exc)
        return False
    conn.execute("DROP TABLE IF EXISTS temp.copies_sent")
    conn.execute("CREATE TEMP TABLE copies_sent (root TEXT, rel_path TEXT, size INTEGER, "
                 "mtime REAL, algo TEXT, digest TEXT, sent_at REAL)")
    try:
        conn.executemany("INSERT INTO temp.copies_sent VALUES (?,?,?,?,?,?,?)", rows)
        # Where both fingerprints are of one kind, they must agree as well.
        conn.execute(
            "INSERT OR IGNORE INTO temp.copies_found(fingerprint, size, verified_at) "
            "SELECT " + _FP + ", a.size, MAX(s.sent_at) FROM temp.copies_sent s "
            "JOIN assets a ON a.root = s.root AND a.rel_path = s.rel_path AND a.size = s.size "
            f"AND ABS(a.mtime - s.mtime) < 0.001 {_FINGERPRINTED} "
            "WHERE f.algo != s.algo OR f.digest = s.digest GROUP BY f.algo, f.digest")
    finally:
        conn.execute("DROP TABLE IF EXISTS temp.copies_sent")
    return True


#: Every destination the ledger is filled for, by its name in the ledger.
SOURCES = {
    "mirror": _from_mirror,
    "secondary": _from_secondary,
    "offsite": _from_offsite,
    "drive": _from_drive,
}


def fill(conn: sqlite3.Connection, *, state_dir: Path | str | None = None,
         offsite_where: str | None = None,
         only: Iterable[str] | None = None) -> dict[str, int]:
    """Bring the ledger in line with the engines' records. Returns how many
    photographs each destination keeps.

    Runs inside the caller's transaction (migration 3, or :func:`refresh`)
    and commits nothing itself. Writes only to ``copies`` and to temporary
    tables of this connection.
    """
    kept: dict[str, int] = {}
    if not (_has_table(conn, "assets") and _has_table(conn, "content_fingerprints")):
        return kept
    names = list(only) if only is not None else list(SOURCES)
    for name in names:
        source = SOURCES[name]
        conn.execute("DROP TABLE IF EXISTS temp.copies_found")
        conn.execute("CREATE TEMP TABLE copies_found (fingerprint TEXT PRIMARY KEY, "
                     "size INTEGER NOT NULL, verified_at REAL NOT NULL)")
        try:
            if not source(conn, state_dir=state_dir, offsite_where=offsite_where):
                continue
            conn.execute(
                "UPDATE copies SET state = ? WHERE destination = ? AND state = ? "
                "AND fingerprint NOT IN (SELECT fingerprint FROM temp.copies_found)",
                (UNCONFIRMED, name, KEPT))
            conn.execute(
                "INSERT INTO copies(fingerprint, destination, state, verified_at, size) "
                "SELECT fingerprint, ?, ?, COALESCE(verified_at, 0), size "
                "FROM temp.copies_found WHERE true "
                "ON CONFLICT(fingerprint, destination) DO UPDATE SET state = excluded.state, "
                "verified_at = MAX(copies.verified_at, excluded.verified_at), "
                "size = excluded.size", (name, KEPT))
            kept[name] = int(conn.execute("SELECT COUNT(*) FROM temp.copies_found").fetchone()[0])
        finally:
            conn.execute("DROP TABLE IF EXISTS temp.copies_found")
    return kept


def refresh(conn: sqlite3.Connection, *, state_dir: Path | str | None = None,
            offsite_where: str | None = None) -> dict[str, int]:
    """:func:`fill`, in a transaction of its own under the index's write lock."""
    from ..storage import db                              # noqa: PLC0415
    with db._write_lock:                                  # noqa: SLF001
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            kept = fill(conn, state_dir=state_dir, offsite_where=offsite_where)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return kept


# -- reading it -------------------------------------------------------------------

def _roots_clause(roots: Sequence[str]) -> tuple[str, list[str]]:
    return ",".join("?" * len(roots)) or "''", [str(r) for r in roots]


def headline(conn: sqlite3.Connection, roots: Sequence[str]) -> dict[str, int]:
    """Photographs in at least two places, of all of them: the library is one
    place, and a ``kept`` copy anywhere else is the second.

    A photograph is counted once however many files in the library have its
    bytes. One with no fingerprint yet (``content_fingerprints`` off, or not
    reached) is counted in the total and never as in two places: the ledger
    cannot say where it is kept, and says so (``unfingerprinted``).
    """
    marks, args = _roots_clause(roots)
    if not _has_table(conn, "copies"):
        return {"two_places": 0, "total": 0, "unfingerprinted": 0}
    known = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(EXISTS (SELECT 1 FROM copies c "
        "WHERE c.fingerprint = d.digest AND c.state = ?)), 0) FROM "
        f"(SELECT DISTINCT {_FP} AS digest FROM assets a {_FINGERPRINTED} "
        f"WHERE a.trashed = 0 AND a.root IN ({marks})) d", (KEPT, *args)).fetchone()
    bare = conn.execute(
        f"SELECT COUNT(*) FROM assets a WHERE a.trashed = 0 AND a.root IN ({marks}) "
        "AND NOT EXISTS (SELECT 1 FROM content_fingerprints f WHERE f.asset_id = a.id "
        "AND f.size = a.size AND f.mtime = a.mtime)", args).fetchone()[0]
    return {"two_places": int(known[1]), "total": int(known[0]) + int(bare),
            "unfingerprinted": int(bare)}


def counts(conn: sqlite3.Connection, destination: str) -> dict[str, int]:
    """What the ledger says one destination keeps: photographs and bytes,
    and how many it no longer confirms."""
    out = {"kept": 0, "kept_bytes": 0, "unconfirmed": 0, "verified_at": 0}
    if not _has_table(conn, "copies"):
        return out
    for row in conn.execute("SELECT state, COUNT(*), COALESCE(SUM(size), 0), "
                            "COALESCE(MAX(verified_at), 0) FROM copies WHERE destination = ? "
                            "GROUP BY state", (destination,)):
        if row[0] == KEPT:
            out.update(kept=int(row[1]), kept_bytes=int(row[2]), verified_at=float(row[3]))
        elif row[0] == UNCONFIRMED:
            out["unconfirmed"] = int(row[1])
    return out


def holds(conn: sqlite3.Connection, destination: str, fingerprint: str) -> bool:
    """Whether the ledger says *destination* keeps the photograph *fingerprint*."""
    if not _has_table(conn, "copies"):
        return False
    return conn.execute("SELECT 1 FROM copies WHERE fingerprint = ? AND destination = ? "
                        "AND state = ?", (fingerprint, destination, KEPT)).fetchone() is not None

