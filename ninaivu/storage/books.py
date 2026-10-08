"""Photo books: the PDFs a person has had Ninaivu make, and who they belong to.

The book itself is a file under the state folder (``books/``), written by
ninaivu/media/books.py; this table is what says whose it is, what it was made
from and whether it is finished. A book belongs to whoever made it. It is
listed for them alone, and only they — or an administrator — may download or
delete it: the photographs in it were chosen through *their* limits, so
handing the file to somebody else would hand them whatever those limits let
in.

Nothing here decides what is in a book. The ids kept are a record of what was
chosen, so a book can be described later; they are never used to serve a
photograph.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

__all__ = ["SCHEMA", "STATES", "create", "get", "listed", "set_state", "delete",
           "books_dir", "file_of"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS photo_books (
    id           INTEGER PRIMARY KEY,
    owner_id     INTEGER NOT NULL,
    title        TEXT NOT NULL DEFAULT '',
    subtitle     TEXT NOT NULL DEFAULT '',
    source_kind  TEXT NOT NULL DEFAULT '',
    source_id    INTEGER,
    template     TEXT NOT NULL DEFAULT 'plain',
    size         TEXT NOT NULL DEFAULT 'a4-portrait',
    bleed        INTEGER NOT NULL DEFAULT 0,
    captions     INTEGER NOT NULL DEFAULT 0,
    item_ids     TEXT NOT NULL DEFAULT '[]',
    pages        INTEGER NOT NULL DEFAULT 0,
    file         TEXT NOT NULL DEFAULT '',
    bytes        INTEGER NOT NULL DEFAULT 0,
    state        TEXT NOT NULL DEFAULT 'building',
    error        TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL,
    finished_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_photo_books_owner ON photo_books(owner_id, created_at DESC);
"""

#: building → done | error | cancelled. Nothing goes back to building: a book
#: that failed is made again as a new one, so a half-written file is never
#: mistaken for the finished one.
STATES = ("building", "done", "error", "cancelled")


def init(conn: sqlite3.Connection) -> None:
    """Made with the index (``db.init_db``); here for a database made before."""
    conn.executescript(SCHEMA)


def books_dir(state_dir: Path | str) -> Path:
    return Path(state_dir) / "books"


def file_of(state_dir: Path | str, book: dict[str, Any]) -> Path | None:
    """The PDF of a finished book, or None. The name is ours, never a request's."""
    name = book.get("file") or ""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    return books_dir(state_dir) / name


def _row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    try:
        data["item_ids"] = json.loads(data.get("item_ids") or "[]")
    except ValueError:
        data["item_ids"] = []
    data["bleed"] = bool(data.get("bleed"))
    data["captions"] = bool(data.get("captions"))
    return data


def create(conn: sqlite3.Connection, *, owner_id: int, title: str, subtitle: str,
           source_kind: str, source_id: int | None, template: str, size: str,
           bleed: bool, captions: bool, item_ids: list[int]) -> int:
    init(conn)
    cur = conn.execute(
        "INSERT INTO photo_books(owner_id, title, subtitle, source_kind, source_id, template, "
        "size, bleed, captions, item_ids, state, created_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'building', ?)",
        (int(owner_id), title, subtitle, source_kind, source_id, template, size,
         1 if bleed else 0, 1 if captions else 0, json.dumps([int(i) for i in item_ids]),
         time.time()))
    conn.commit()
    return int(cur.lastrowid)


def get(conn: sqlite3.Connection, book_id: int) -> dict[str, Any] | None:
    init(conn)
    row = conn.execute("SELECT * FROM photo_books WHERE id=?", (int(book_id),)).fetchone()
    return _row(row) if row else None


def listed(conn: sqlite3.Connection, owner_id: int) -> list[dict[str, Any]]:
    """This person's books, newest first. An administrator's list is their
    own too: the books of everybody in the house in one list would be a list
    of what each of them has been looking at."""
    init(conn)
    rows = conn.execute("SELECT * FROM photo_books WHERE owner_id=? "
                        "ORDER BY created_at DESC, id DESC", (int(owner_id),)).fetchall()
    return [_row(r) for r in rows]


def may_open(book: dict[str, Any] | None, user_id: int, is_admin: bool) -> bool:
    return bool(book) and (book["owner_id"] == user_id or is_admin)


def set_state(conn: sqlite3.Connection, book_id: int, state: str, *, error: str = "",
              file: str | None = None, size_bytes: int | None = None,
              pages: int | None = None) -> None:
    if state not in STATES:
        raise ValueError(state)
    fields = ["state=?", "error=?"]
    values: list[Any] = [state, error[:500]]
    if state != "building":
        fields.append("finished_at=?")
        values.append(time.time())
    for column, value in (("file", file), ("bytes", size_bytes), ("pages", pages)):
        if value is not None:
            fields.append(f"{column}=?")
            values.append(value)
    conn.execute(f"UPDATE photo_books SET {', '.join(fields)} WHERE id=?", (*values, int(book_id)))
    conn.commit()


def delete(conn: sqlite3.Connection, state_dir: Path | str, book: dict[str, Any]) -> None:
    """The row and its file. A file already gone is not an error."""
    path = file_of(state_dir, book)
    if path is not None:
        path.unlink(missing_ok=True)
        path.with_name(path.name + ".part").unlink(missing_ok=True)
    conn.execute("DELETE FROM photo_books WHERE id=?", (int(book["id"]),))
    conn.commit()
