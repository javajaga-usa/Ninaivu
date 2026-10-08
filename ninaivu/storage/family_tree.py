"""The family tree: who is whose parent, and who is married to whom.

Two kinds of relation between named people, and nothing more: ``parent``
(``person_a`` is a parent of ``person_b``) and ``spouse`` (kept with the
smaller id first, so a marriage is one row however it was entered). Children,
grandparents and cousins are read from those two; storing them as well would
be a second copy of the same facts, free to disagree with the first.

Nobody may be their own parent, or their own ancestor by any route: the tree
is drawn a generation to a row, and a loop has no row to go in. The check
walks the whole table, hidden people included, because a loop through
somebody the viewer cannot see is still a loop.

What a viewer is shown is narrowed elsewhere (api_family_tree.py) to the
people they may see on the People page; a relation reaches them only when
both its ends do.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Iterable

__all__ = ["SCHEMA", "KINDS", "add", "remove", "get", "relations", "generations",
           "carry_over"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS person_relations (
    id          INTEGER PRIMARY KEY,
    person_a    INTEGER NOT NULL REFERENCES people_clusters(id) ON DELETE CASCADE,
    person_b    INTEGER NOT NULL REFERENCES people_clusters(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('parent', 'spouse')),
    created_by  INTEGER,            -- a profile id; see ask_family.SCHEMA
    created_at  REAL NOT NULL,
    CHECK (person_a <> person_b),
    UNIQUE (person_a, person_b, kind)
);
CREATE INDEX IF NOT EXISTS idx_person_relations_b ON person_relations(person_b);
"""

KINDS = ("parent", "spouse")


def init(conn: sqlite3.Connection) -> None:
    """Made with the index (``db.init_db``); here for a database made before."""
    conn.executescript(SCHEMA)


def relations(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT id, person_a, person_b, kind FROM person_relations ORDER BY id")]


def get(conn: sqlite3.Connection, relation_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT id, person_a, person_b, kind FROM person_relations "
                       "WHERE id=?", (int(relation_id),)).fetchone()
    return dict(row) if row else None


def _is_ancestor(conn: sqlite3.Connection, ancestor: int, person: int) -> bool:
    """Is *ancestor* above *person* by any line of parents?"""
    seen: set[int] = set()
    todo = [int(person)]
    while todo:
        current = todo.pop()
        for row in conn.execute("SELECT person_a FROM person_relations "
                                "WHERE kind='parent' AND person_b=?", (current,)):
            parent = int(row[0])
            if parent == int(ancestor):
                return True
            if parent not in seen:
                seen.add(parent)
                todo.append(parent)
    return False


def _check(conn: sqlite3.Connection, a: int, b: int, kind: str) -> tuple[int, int]:
    """The row to store for this relation, or ValueError saying why not."""
    if kind not in KINDS:
        raise ValueError("A relation is a parent or a spouse")
    if a == b:
        raise ValueError("Nobody is related to themselves that way")
    if kind == "spouse":
        a, b = min(a, b), max(a, b)
        if conn.execute("SELECT 1 FROM person_relations WHERE kind='parent' AND "
                        "((person_a=? AND person_b=?) OR (person_a=? AND person_b=?))",
                        (a, b, b, a)).fetchone():
            raise ValueError("A parent and their child cannot also be married")
        return a, b
    # a is to be b's parent: refused if b is already above a, or they are married.
    if _is_ancestor(conn, b, a):
        raise ValueError("That would make somebody their own ancestor")
    if conn.execute("SELECT 1 FROM person_relations WHERE kind='spouse' AND "
                    "person_a=? AND person_b=?", (min(a, b), max(a, b))).fetchone():
        raise ValueError("A parent and their child cannot also be married")
    return a, b


def add(conn: sqlite3.Connection, a: int, b: int, kind: str,
        created_by: int | None) -> dict[str, Any]:
    """Record that *a* is *b*'s parent, or that the two are married.

    Adding what is already there answers with the row that is.
    """
    from .db import _write_lock                                   # noqa: PLC0415

    with _write_lock:
        a, b = _check(conn, int(a), int(b), kind)
        conn.execute(
            "INSERT OR IGNORE INTO person_relations(person_a, person_b, kind, created_by, "
            "created_at) VALUES (?,?,?,?,?)", (a, b, kind, created_by, time.time()))
        conn.commit()
        row = conn.execute("SELECT id, person_a, person_b, kind FROM person_relations "
                           "WHERE person_a=? AND person_b=? AND kind=?", (a, b, kind)).fetchone()
    return dict(row)


def remove(conn: sqlite3.Connection, relation_id: int) -> bool:
    from .db import _write_lock                                   # noqa: PLC0415

    with _write_lock:
        cur = conn.execute("DELETE FROM person_relations WHERE id=?", (int(relation_id),))
        conn.commit()
    return bool(cur.rowcount)


def carry_over(conn: sqlite3.Connection, source: int, target: int) -> None:
    """Move *source*'s relations to *target*, for a merge of the two people.

    Called by ``db.merge_people`` inside its own lock and transaction, so it
    neither takes the lock nor commits. A relation that would now be a person
    with themselves, a duplicate or a loop is dropped rather than moved: the
    merge is the decision being made, and it must not fail over the tree.
    """
    rows = [dict(r) for r in conn.execute(
        "SELECT person_a, person_b, kind, created_by, created_at FROM person_relations "
        "WHERE person_a=? OR person_b=?", (int(source), int(source)))]
    conn.execute("DELETE FROM person_relations WHERE person_a=? OR person_b=?",
                 (int(source), int(source)))
    for row in rows:
        a = int(target) if int(row["person_a"]) == int(source) else int(row["person_a"])
        b = int(target) if int(row["person_b"]) == int(source) else int(row["person_b"])
        try:
            a, b = _check(conn, a, b, row["kind"])
        except ValueError:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO person_relations(person_a, person_b, kind, created_by, "
            "created_at) VALUES (?,?,?,?,?)",
            (a, b, row["kind"], row["created_by"], row["created_at"]))


def generations(people: Iterable[int],
                links: Iterable[dict[str, Any]]) -> dict[int, int | None]:
    """A row for everybody in the tree: 0 for the eldest, one more for each
    generation down. Married couples share a row. People with no relation
    among *people* get None — they are not in the tree yet.

    *links* must already be only those between *people*. A spouse pulled down
    to their partner's row pushes their own children down with them, so this
    repeats until nothing moves; the count is bounded so a tangle the checks
    above did not foresee still finishes.
    """
    ids = [int(p) for p in people]
    gen = {p: 0 for p in ids}
    placed: set[int] = set()
    parents, spouses = [], []
    for link in links:
        a, b = int(link["person_a"]), int(link["person_b"])
        if a not in gen or b not in gen:
            continue
        placed.update((a, b))
        (parents if link["kind"] == "parent" else spouses).append((a, b))
    for _ in range(len(ids) + 1):
        moved = False
        for a, b in parents:
            if gen[b] < gen[a] + 1:
                gen[b] = gen[a] + 1
                moved = True
        for a, b in spouses:
            row = max(gen[a], gen[b])
            if gen[a] != row or gen[b] != row:
                gen[a] = gen[b] = row
                moved = True
        if not moved:
            break
    return {p: (gen[p] if p in placed else None) for p in ids}
