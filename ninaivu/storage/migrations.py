"""Numbered migrations: each change to a database's shape, once, in order.

Until 4.0 a database was brought up to date by running its whole schema
again at every start (``CREATE TABLE IF NOT EXISTS``), adding the columns
that arrived later (:data:`db.LATE_COLUMNS`) and a few hand-written steps
under one number (:data:`db.SCHEMA_VERSION`). That stays: it is what makes a
3.x index open as it is. What it cannot do well is a change that must happen
exactly once and in order — a table filled from another, a column renamed,
rows moved — because a schema script runs every time and cannot tell a first
run from the hundredth.

Those changes are written here, as numbered steps per database:

* **Migration 1 is "what 3.7 has".** It changes nothing: the schema script
  has already run when it is applied, so a 3.7 index is up to date the moment
  this version opens it.
* **Each later step runs once**, inside one transaction with the record of
  it, so a step is either wholly done and recorded or not done at all. A step
  that fails rolls back, is logged, and stops the start: the steps after it
  may depend on it.
* **The record is ``PRAGMA user_version``**, the number SQLite keeps in the
  file's header for exactly this. No 3.x version set it, so a 3.x database
  reads 0. A repair (storage/index_check.py) carries it across.
* **Never lowered.** A database a newer Ninaivu opened records a step this
  version does not know. Nothing is applied and nothing is changed: the newer
  steps only add, and *Go back* restores the copy made before the update when
  an older version must have the database as it was.

A step is a function of the connection. It may call ``executescript`` only
with statements that do not commit (no ``BEGIN``/``COMMIT``/``VACUUM``):
:func:`run` holds the transaction.
"""
from __future__ import annotations

import hashlib
import inspect
import logging
import sqlite3
from dataclasses import dataclass
from typing import Callable, Sequence

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Migration:
    """One step: its number, a few words on what it does, and the step."""

    number: int
    name: str
    apply: Callable[[sqlite3.Connection], None]


class MigrationFailed(RuntimeError):
    """A step raised; nothing of it was kept, and the start should stop."""


def _nothing(conn: sqlite3.Connection) -> None:
    """Migration 1: the database as 3.7 leaves it, which the schema script made."""


# -- 2: where each photograph is ---------------------------------------------
#
# A photograph has been known by its place, the ``(root, rel_path)`` of its
# row in ``assets``. Proposal 3 of 4.0 turns that round: what the file *is*,
# its fingerprint, is the photograph, and a path is only where it is now.
# ``locations`` is that second half, one row per place a file is known to be:
# for now only its place in the library, so exactly one row for each row of
# ``assets``.
#
# ``assets.root`` and ``rel_path`` stay as they are, because 3.7 reads them
# and every writer writes them. The triggers below keep ``locations`` in step
# with them, and with ``content_fingerprints``, inside the statement that made
# the change, so no writer in 4.0 or in 3.7 (after Go back) has to know the
# table is there. They live in the file, made once by this step: the schema
# scripts init_db runs at every start create only what is missing and drop
# only their own triggers by name, so these are left alone.
#
# The fingerprint is written ``algo:digest`` ("blake3:9f…"), so one made the
# slower way without BLAKE3 can never be taken for one made with it. It is
# the fingerprint of the file *as it is now*: one taken at another size or
# modified time describes an earlier version, and is NULL here until the
# file is fingerprinted again, the test ``fingerprints.UP_TO_DATE`` makes.

#: The fingerprint the row of ``locations`` being written should carry.
_FINGERPRINT_OF_ROW = (
    "(SELECT f.algo || ':' || f.digest FROM content_fingerprints f "
    "JOIN assets a ON a.id = f.asset_id "
    "WHERE f.asset_id = locations.asset_id AND f.size = a.size AND f.mtime = a.mtime)")

#: The same, for a row of ``assets`` just written (``NEW`` in its triggers).
_FINGERPRINT_OF_NEW = (
    "(SELECT f.algo || ':' || f.digest FROM content_fingerprints f "
    "WHERE f.asset_id = NEW.id AND f.size = NEW.size AND f.mtime = NEW.mtime)")

#: Now, in seconds, as ``time.time()`` gives it.
_NOW = "((julianday('now') - 2440587.5) * 86400.0)"

#: Migration 2, statement by statement: ``executescript`` would commit the
#: transaction :func:`run` holds before it began.
LOCATIONS = (
    """CREATE TABLE IF NOT EXISTS locations (
    id          INTEGER PRIMARY KEY,
    asset_id    INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    fingerprint TEXT,
    root        TEXT NOT NULL,
    rel_path    TEXT NOT NULL,
    place       TEXT NOT NULL DEFAULT 'library',
    seen_at     REAL NOT NULL DEFAULT 0,
    UNIQUE (root, rel_path)
)""",
    "CREATE INDEX IF NOT EXISTS idx_locations_fingerprint ON locations(fingerprint)",
    # The triggers find a photograph's place by its id, and deleting a row of
    # assets looks here for rows that point at it (the foreign key).
    "CREATE INDEX IF NOT EXISTS idx_locations_asset ON locations(asset_id, place)",
    # A new row of assets: its place. Anything left at that path, or under
    # that id, by a writer that went round the triggers is the past.
    f"""CREATE TRIGGER IF NOT EXISTS locations_follow_insert AFTER INSERT ON assets
BEGIN
    DELETE FROM locations WHERE root = NEW.root AND rel_path = NEW.rel_path;
    DELETE FROM locations WHERE asset_id = NEW.id AND place = 'library';
    INSERT INTO locations(asset_id, fingerprint, root, rel_path, place, seen_at)
    VALUES (NEW.id, {_FINGERPRINT_OF_NEW}, NEW.root, NEW.rel_path, 'library',
            COALESCE(NEW.added_at, NULLIF(NEW.indexed_at, 0), {_NOW}));
END""",
    # Moved or renamed (respell_asset, a library folder put somewhere else),
    # or the file changed under it, which leaves its fingerprint out of date.
    # A rescan rewrites these columns with the values they had, so only a
    # real change does anything.
    f"""CREATE TRIGGER IF NOT EXISTS locations_follow_update
AFTER UPDATE OF root, rel_path, size, mtime ON assets
WHEN OLD.root IS NOT NEW.root OR OLD.rel_path IS NOT NEW.rel_path
  OR OLD.size IS NOT NEW.size OR OLD.mtime IS NOT NEW.mtime
BEGIN
    DELETE FROM locations
     WHERE root = NEW.root AND rel_path = NEW.rel_path AND asset_id <> NEW.id;
    UPDATE locations SET root = NEW.root, rel_path = NEW.rel_path,
           fingerprint = {_FINGERPRINT_OF_NEW},
           seen_at = CASE WHEN root IS NOT NEW.root OR rel_path IS NOT NEW.rel_path
                          THEN {_NOW} ELSE seen_at END
     WHERE asset_id = NEW.id AND place = 'library';
    INSERT INTO locations(asset_id, fingerprint, root, rel_path, place, seen_at)
    SELECT NEW.id, {_FINGERPRINT_OF_NEW}, NEW.root, NEW.rel_path, 'library', {_NOW}
     WHERE NOT EXISTS (SELECT 1 FROM locations
                        WHERE asset_id = NEW.id AND place = 'library');
END""",
    # The foreign key does this too, but only on a connection that has
    # foreign keys switched on, and a tool's connection may not.
    """CREATE TRIGGER IF NOT EXISTS locations_follow_delete AFTER DELETE ON assets
BEGIN
    DELETE FROM locations WHERE asset_id = OLD.id;
END""",
    # A fingerprint arriving, taken again, or gone with its photograph.
    f"""CREATE TRIGGER IF NOT EXISTS locations_fingerprint_insert
AFTER INSERT ON content_fingerprints
BEGIN
    UPDATE locations SET fingerprint = {_FINGERPRINT_OF_ROW}
     WHERE asset_id = NEW.asset_id AND place = 'library';
END""",
    f"""CREATE TRIGGER IF NOT EXISTS locations_fingerprint_update
AFTER UPDATE ON content_fingerprints
BEGIN
    UPDATE locations SET fingerprint = {_FINGERPRINT_OF_ROW}
     WHERE asset_id IN (OLD.asset_id, NEW.asset_id) AND place = 'library';
END""",
    """CREATE TRIGGER IF NOT EXISTS locations_fingerprint_delete
AFTER DELETE ON content_fingerprints
BEGIN
    UPDATE locations SET fingerprint = NULL
     WHERE asset_id = OLD.asset_id AND place = 'library';
END""",
)

#: Every photograph already in the index, at its place, with the fingerprint
#: it has if that still describes the file.
_FILL_LOCATIONS = (
    "INSERT INTO locations(asset_id, fingerprint, root, rel_path, place, seen_at) "
    "SELECT a.id, CASE WHEN f.asset_id IS NULL THEN NULL "
    "ELSE f.algo || ':' || f.digest END, a.root, a.rel_path, 'library', "
    "COALESCE(a.added_at, NULLIF(a.indexed_at, 0), 0) "
    "FROM assets a LEFT JOIN content_fingerprints f "
    "ON f.asset_id = a.id AND f.size = a.size AND f.mtime = a.mtime "
    "ORDER BY a.id")


def _locations(conn: sqlite3.Connection) -> None:
    """Migration 2: the ``locations`` table, filled, and the triggers that keep it.

    Only adds: no table 3.7 reads is changed, and no photograph's id. Files
    without a fingerprint yet get one from the fingerprint pass of the next
    scan, and the triggers carry it here.
    """
    for statement in LOCATIONS:
        conn.execute(statement)
    # Empty unless the record of this step was lost (a copy of the index
    # with user_version cleared): filled afresh rather than twice.
    conn.execute("DELETE FROM locations WHERE place = 'library'")
    filled = conn.execute(_FILL_LOCATIONS).rowcount
    log.info("index: %s photographs given their place in locations", f"{filled:,}")


def _copies_ledger(conn: sqlite3.Connection) -> None:
    """Migration 3: the ledger of which photograph is kept where (ninaivu/copies/).

    A new table only: nothing 3.7 reads is changed, so *Go back* finds the
    index as it left it, with one table it does not know. Filled from what the
    copies already record (the second disk, the off-site copy, Google Drive,
    the Secondary), read only: nothing is sent again and no engine is told
    anything.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS copies ("
        " fingerprint TEXT NOT NULL,"
        " destination TEXT NOT NULL,"
        " state       TEXT NOT NULL,"
        " verified_at REAL NOT NULL DEFAULT 0,"
        " size        INTEGER NOT NULL DEFAULT 0,"
        " PRIMARY KEY (fingerprint, destination)"
        ") WITHOUT ROWID")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_copies_destination "
                 "ON copies(destination, state)")
    from ..copies import ledger                            # noqa: PLC0415
    ledger.fill(conn)


#: The index (``index.db``).
INDEX: tuple[Migration, ...] = (
    Migration(1, "what 3.7 has", _nothing),
    Migration(2, "where each photograph is", _locations),
    Migration(3, "the copies ledger", _copies_ledger),
)

#: The archive's record of every file it copied (``archive.db``).
ARCHIVE: tuple[Migration, ...] = (
    Migration(1, "what 3.7 has", _nothing),
)

#: Every list of steps, by the database it belongs to.
DATABASES: dict[str, tuple[Migration, ...]] = {
    "index": INDEX,
    "archive": ARCHIVE,
}


def check(steps: Sequence[Migration]) -> None:
    """Refuse a list whose numbers do not run 1, 2, 3 … without a gap.

    A gap or a repeat is a mistake made while writing a step, and it would
    otherwise show itself only on a household's computer.
    """
    numbers = [m.number for m in steps]
    if numbers != list(range(1, len(numbers) + 1)):
        raise ValueError(f"migrations must be numbered 1 to {len(numbers)} in order: {numbers}")


def recorded(conn: sqlite3.Connection) -> int:
    """The last step this database records, 0 for one no step has touched."""
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row else 0


def latest(steps: Sequence[Migration]) -> int:
    """The number of the last step this version knows."""
    return steps[-1].number if steps else 0


def run(conn: sqlite3.Connection, steps: Sequence[Migration], *,
        database: str = "index") -> list[int]:
    """Apply the steps this database has not had, in order. Returns their numbers.

    The caller holds whatever lock keeps its own writers out (the index's
    write lock); ``BEGIN IMMEDIATE`` keeps other processes out too.
    """
    check(steps)
    have = recorded(conn)
    known = latest(steps)
    if have > known:
        log.warning("%s: records migration %d, newer than this version knows (%d); "
                    "left as it is", database, have, known)
        return []
    applied: list[int] = []
    for step in steps:
        if step.number <= have:
            continue
        if conn.in_transaction:
            conn.commit()
        try:
            conn.execute("BEGIN IMMEDIATE")
            step.apply(conn)
            conn.execute(f"PRAGMA user_version = {int(step.number)}")
            conn.commit()
        except Exception as exc:
            if conn.in_transaction:
                conn.rollback()
            log.error("%s: migration %d (%s) failed; nothing of it was kept",
                      database, step.number, step.name, exc_info=True)
            raise MigrationFailed(
                f"{database}: migration {step.number} ({step.name}) failed: {exc}") from exc
        log.info("%s: migration %d applied (%s)", database, step.number, step.name)
        applied.append(step.number)
    return applied


def shape() -> str:
    """What the steps are, for :func:`storage.upgrade.index_shape`.

    A new step changes it, so an update that brings one makes the copy of the
    index before it starts, as a change to the schema script always has.
    """
    parts: list[str] = []
    for database, steps in sorted(DATABASES.items()):
        for step in steps:
            try:
                source = inspect.getsource(step.apply).replace("\r\n", "\n")
            except (OSError, TypeError):
                source = step.apply.__qualname__
            parts.append(f"{database}:{step.number}:{step.name}:{source}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]
