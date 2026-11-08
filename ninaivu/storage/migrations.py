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


def _reserved_locations(conn: sqlite3.Connection) -> None:
    """Migration 2: kept for the ``locations`` table of the fingerprints work.

    Written as a step that does nothing so that the ledger below can be number
    3 on a branch of its own; the two are put together before any release, and
    this placeholder is replaced by the real step then.
    """


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
    Migration(2, "reserved: locations (fingerprints workstream)", _reserved_locations),
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
