"""Smart albums: a search, kept, that fills itself.

"Maya and Arjun, videos, since 2023" is a question the gallery can already
answer — typed into the search box, it is read into people, a kind and a date
range (ninaivu/utils/phrase.py, ninaivu/utils/query.py). A smart album is that
answer given a name and kept in the sidebar, so next month's videos of the
two of them are in it without anybody adding them.

What is kept is the search *as it was understood*, not the words: the people
by who they are, the place, the kind, the dates, and whatever words were left
for the picture search. So renaming Maya does not break it, and "since 2023"
still means 2023 next year.

Nothing is stored about which photographs are in one. Each time it is opened
it is run as a search by whoever opened it, through their own limits — a
family member sees the photographs of it they may see, a guest cannot open
one at all, and a person or place they cannot see simply matches nothing for
them.

Albums a person makes are theirs; ``shared`` ones appear for the rest of the
family too (and, like a hand-made album, not for anybody who would see none
of it).
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from typing import Any

__all__ = ["SCHEMA", "RULE_KEYS", "clean_rules", "create", "get", "listed", "delete", "rename"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS smart_albums (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    rules       TEXT NOT NULL DEFAULT '{}',
    shared      INTEGER NOT NULL DEFAULT 1,
    created_by  INTEGER,
    created_at  REAL NOT NULL
);
"""

#: What a rule may say, and nothing else.
RULE_KEYS = ("text", "people", "place", "kinds", "favorites", "date_from", "date_to",
             "camera", "tag", "folder")
_KINDS = {"picture", "video", "audio"}
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def init(conn: sqlite3.Connection) -> None:
    """Made with the index (``db.init_db``); here for a database made before."""
    conn.executescript(SCHEMA)


def clean_rules(raw: dict[str, Any]) -> dict[str, Any]:
    """Only the keys a rule may have, each the type it must be. ValueError otherwise."""
    if not isinstance(raw, dict):
        raise ValueError("rules must be an object")
    rules: dict[str, Any] = {}
    text = raw.get("text") or ""
    if not isinstance(text, str) or len(text) > 200:
        raise ValueError("text must be up to 200 characters")
    if text.strip():
        rules["text"] = text.strip()
    people = raw.get("people") or []
    if not isinstance(people, list) or not all(isinstance(p, int) and not isinstance(p, bool)
                                               for p in people) or len(people) > 10:
        raise ValueError("people must be a list of up to 10 ids")
    if people:
        rules["people"] = sorted(set(people))
    for key, limit in (("place", 120), ("camera", 120), ("tag", 120), ("folder", 400)):
        value = raw.get(key) or ""
        if not isinstance(value, str) or len(value) > limit:
            raise ValueError(f"{key} must be text")
        if value.strip():
            rules[key] = value.strip()
    kinds = raw.get("kinds") or []
    if not isinstance(kinds, list) or not set(kinds) <= _KINDS:
        raise ValueError("kinds must be picture, video or audio")
    if kinds:
        rules["kinds"] = sorted(set(kinds))
    if raw.get("favorites") not in (None, False, True):
        raise ValueError("favorites must be true or false")
    if raw.get("favorites"):
        rules["favorites"] = True
    for key in ("date_from", "date_to"):
        value = raw.get(key) or ""
        if value and (not isinstance(value, str) or not _DATE.match(value)):
            raise ValueError(f"{key} must be YYYY-MM-DD")
        if value:
            rules[key] = value
    if not rules:
        raise ValueError("A smart album needs something to look for.")
    return rules


def _row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    try:
        data["rules"] = json.loads(data.get("rules") or "{}")
    except ValueError:
        data["rules"] = {}
    data["shared"] = bool(data.get("shared"))
    return data


def create(conn: sqlite3.Connection, name: str, rules: dict[str, Any], *,
           created_by: int, shared: bool = True) -> int:
    init(conn)
    cur = conn.execute(
        "INSERT INTO smart_albums(name, rules, shared, created_by, created_at) VALUES(?, ?, ?, ?, ?)",
        (name, json.dumps(clean_rules(rules), sort_keys=True), 1 if shared else 0,
         int(created_by), time.time()))
    conn.commit()
    return int(cur.lastrowid)


def get(conn: sqlite3.Connection, album_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM smart_albums WHERE id=?", (int(album_id),)).fetchone()
    return _row(row) if row else None


def may_open(album: dict[str, Any] | None, user_id: int, is_admin: bool) -> bool:
    return bool(album) and (album["shared"] or album["created_by"] == user_id or is_admin)


def may_change(album: dict[str, Any] | None, user_id: int, is_admin: bool) -> bool:
    return bool(album) and (album["created_by"] == user_id or is_admin)


def listed(conn: sqlite3.Connection, user_id: int, is_admin: bool) -> list[dict[str, Any]]:
    """The smart albums this person may open, by name."""
    rows = conn.execute("SELECT * FROM smart_albums ORDER BY name COLLATE NOCASE").fetchall()
    return [a for a in map(_row, rows) if may_open(a, user_id, is_admin)]


def rename(conn: sqlite3.Connection, album_id: int, name: str | None = None,
           shared: bool | None = None) -> None:
    if name is not None:
        conn.execute("UPDATE smart_albums SET name=? WHERE id=?", (name, int(album_id)))
    if shared is not None:
        conn.execute("UPDATE smart_albums SET shared=? WHERE id=?", (1 if shared else 0, int(album_id)))
    conn.commit()


def delete(conn: sqlite3.Connection, album_id: int) -> None:
    conn.execute("DELETE FROM smart_albums WHERE id=?", (int(album_id),))
    conn.commit()
