"""The year each person was born, for Then and now (media/then_and_now.py).

"Amma at six, beside Arun at six" needs the year each of them was born, and
nothing else in the library knows it: a face has a name, never a birthday.
So the family says it once, on the Then and now screen, and it is kept here,
one row per named person. Only the year: the month would be guessed at for
most old photographs anyway, and an age read from two years is right to
within one.

The row goes with the person (``ON DELETE CASCADE``) and moves with them when
two people are merged (:func:`carry_over`, from ``db.merge_people``).
"""
from __future__ import annotations

import sqlite3
import time
from typing import Iterable

__all__ = ["SCHEMA", "create", "born", "set_born", "carry_over", "EARLIEST"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS person_born (
    person_id  INTEGER PRIMARY KEY REFERENCES people_clusters(id) ON DELETE CASCADE,
    year       INTEGER NOT NULL,
    set_by     INTEGER,            -- a profile id
    set_at     REAL NOT NULL
);
"""

#: The earliest year anybody in a family photograph was born.
EARLIEST = 1850


def create(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def born(conn: sqlite3.Connection, people: Iterable[int] | None = None) -> dict[int, int]:
    """The year each of *people* (everybody, for None) was born, where known."""
    try:
        rows = conn.execute("SELECT person_id, year FROM person_born").fetchall()
    except sqlite3.OperationalError:
        return {}
    wanted = None if people is None else {int(p) for p in people}
    return {int(r[0]): int(r[1]) for r in rows if wanted is None or int(r[0]) in wanted}


def set_born(conn: sqlite3.Connection, person_id: int, year: int | None,
             set_by: int | None = None) -> None:
    """Keep the year *person_id* was born, or forget it (*year* None)."""
    from . import db                                            # noqa: PLC0415
    with db._write_lock:                                        # noqa: SLF001
        create(conn)
        if year is None:
            conn.execute("DELETE FROM person_born WHERE person_id=?", (int(person_id),))
        else:
            conn.execute(
                "INSERT INTO person_born(person_id, year, set_by, set_at) VALUES (?,?,?,?) "
                "ON CONFLICT(person_id) DO UPDATE SET year=excluded.year, "
                "set_by=excluded.set_by, set_at=excluded.set_at",
                (int(person_id), int(year), set_by, time.time()))
        conn.commit()


def carry_over(conn: sqlite3.Connection, source: int, target: int) -> None:
    """Keep *source*'s year for *target* in a merge, unless *target* has one.

    Called inside ``db.merge_people``'s lock and transaction: it neither
    takes the lock nor commits."""
    try:
        conn.execute(
            "INSERT OR IGNORE INTO person_born(person_id, year, set_by, set_at) "
            "SELECT ?, year, set_by, set_at FROM person_born WHERE person_id=?",
            (int(target), int(source)))
    except sqlite3.OperationalError:
        return
