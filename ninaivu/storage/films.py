"""Memory films: the MP4s a person has had Ninaivu make, and who they belong to.

The same rules as photo books (storage/books.py). The film itself is a file
under the state folder (``films/``), written by ninaivu/media/films.py; this
table says whose it is, what it was made from and whether it is finished. A
film belongs to whoever made it: it is listed for them alone, and only they —
or an administrator — may play, download or delete it, because the
photographs and stories in it were chosen through *their* limits.

The ids kept are a record of what was chosen, so a film can be described and
checked again before it is handed out; they are never used to serve anything.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any

__all__ = ["SCHEMA", "STATES", "create", "get", "listed", "set_state", "delete",
           "films_dir", "file_of", "may_open", "MAX_SONG_BYTES", "MAX_SONGS_EACH", "add_song", "songs_of",
           "song_path", "delete_song", "forget_songs"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_films (
    id           INTEGER PRIMARY KEY,
    owner_id     INTEGER NOT NULL,
    title        TEXT NOT NULL DEFAULT '',
    subtitle     TEXT NOT NULL DEFAULT '',
    source_kind  TEXT NOT NULL DEFAULT '',
    source_id    INTEGER,
    size         TEXT NOT NULL DEFAULT '1080p',
    subtitles    TEXT NOT NULL DEFAULT 'both',
    music_id     INTEGER,
    item_ids     TEXT NOT NULL DEFAULT '[]',
    story_ids    TEXT NOT NULL DEFAULT '[]',
    seconds      REAL NOT NULL DEFAULT 0,
    file         TEXT NOT NULL DEFAULT '',
    bytes        INTEGER NOT NULL DEFAULT 0,
    state        TEXT NOT NULL DEFAULT 'building',
    error        TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL,
    finished_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_memory_films_owner ON memory_films(owner_id, created_at DESC);
"""

#: building → done | error | cancelled, as for books: a film that failed is
#: made again as a new one.
STATES = ("building", "done", "error", "cancelled")


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def films_dir(state_dir: Path | str) -> Path:
    return Path(state_dir) / "films"


def file_of(state_dir: Path | str, film: dict[str, Any]) -> Path | None:
    """The MP4 of a finished film, or None. The name is ours, never a request's."""
    name = film.get("file") or ""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    return films_dir(state_dir) / name


def _ids(text: Any) -> list[int]:
    try:
        return [int(i) for i in json.loads(text or "[]")]
    except (TypeError, ValueError):
        return []


def _row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["item_ids"] = _ids(data.get("item_ids"))
    data["story_ids"] = _ids(data.get("story_ids"))
    return data


def create(conn: sqlite3.Connection, *, owner_id: int, title: str, subtitle: str,
           source_kind: str, source_id: int | None, size: str, subtitles: str,
           music_id: int | None, item_ids: list[int], story_ids: list[int]) -> int:
    init(conn)
    cur = conn.execute(
        "INSERT INTO memory_films(owner_id, title, subtitle, source_kind, source_id, size, "
        "subtitles, music_id, item_ids, story_ids, state, created_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'building', ?)",
        (int(owner_id), title, subtitle, source_kind, source_id, size, subtitles, music_id,
         json.dumps([int(i) for i in item_ids]), json.dumps([int(i) for i in story_ids]),
         time.time()))
    conn.commit()
    return int(cur.lastrowid)


def get(conn: sqlite3.Connection, film_id: int) -> dict[str, Any] | None:
    init(conn)
    row = conn.execute("SELECT * FROM memory_films WHERE id=?", (int(film_id),)).fetchone()
    return _row(row) if row else None


def listed(conn: sqlite3.Connection, owner_id: int) -> list[dict[str, Any]]:
    """This person's films, newest first (an administrator's too: their own)."""
    init(conn)
    rows = conn.execute("SELECT * FROM memory_films WHERE owner_id=? "
                        "ORDER BY created_at DESC, id DESC", (int(owner_id),)).fetchall()
    return [_row(r) for r in rows]


def may_open(film: dict[str, Any] | None, user_id: int, is_admin: bool) -> bool:
    return bool(film) and (film["owner_id"] == user_id or is_admin)


def set_state(conn: sqlite3.Connection, film_id: int, state: str, *, error: str = "",
              file: str | None = None, size_bytes: int | None = None,
              seconds: float | None = None) -> None:
    if state not in STATES:
        raise ValueError(state)
    fields = ["state=?", "error=?"]
    values: list[Any] = [state, error[:500]]
    if state != "building":
        fields.append("finished_at=?")
        values.append(time.time())
    for column, value in (("file", file), ("bytes", size_bytes), ("seconds", seconds)):
        if value is not None:
            fields.append(f"{column}=?")
            values.append(value)
    conn.execute(f"UPDATE memory_films SET {', '.join(fields)} WHERE id=?",
                 (*values, int(film_id)))
    conn.commit()


def delete(conn: sqlite3.Connection, state_dir: Path | str, film: dict[str, Any]) -> None:
    """The row and its file. A file already gone is not an error."""
    path = file_of(state_dir, film)
    if path is not None:
        path.unlink(missing_ok=True)
        path.with_name(path.name + ".part").unlink(missing_ok=True)
        # Where a render writes now: hidden, so a hand-over to the Pi does
        # not copy half a film (media/films.py).
        path.with_name(f".{path.name}.part").unlink(missing_ok=True)
    conn.execute("DELETE FROM memory_films WHERE id=?", (int(film["id"]),))
    conn.commit()


# ---------------------------------------------------------------------------
# Songs a person brought for their films
# ---------------------------------------------------------------------------
#
# Sound files in the library are for administrators alone (the scanner keeps
# them hidden: a library swept up from old phones is full of voice notes), so
# a family member brings the music for a film themselves. A song is kept under
# ``films/music/`` with its owner's id at the front of its name, is listed
# for them alone, and goes when they delete it or their profile goes.

#: The largest song taken.
MAX_SONG_BYTES = 40 * 1024 * 1024
#: The most songs one person keeps: 1.2 GB at the largest (audit C-48).
MAX_SONGS_EACH = 30
_SONG_NAME = re.compile(r"^(\d+)-([0-9a-f]{32})(\.[a-z0-9]{2,5})$")
_TITLE_FILE = ".title"


def _songs_dir(state_dir: Path | str) -> Path:
    return films_dir(state_dir) / "music"


def add_song(state_dir: Path | str, owner_id: int, data: bytes, ext: str, title: str) -> str:
    """Keep a song for *owner_id*; returns the token a film names it by."""
    folder = _songs_dir(state_dir)
    folder.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(16)
    target = folder / f"{int(owner_id)}-{token}{ext}"
    partial = target.with_name(target.name + ".part")
    partial.write_bytes(data)
    os.replace(partial, target)
    target.with_name(target.name + _TITLE_FILE).write_text(title[:120], encoding="utf-8")
    return token


def songs_of(state_dir: Path | str, owner_id: int) -> list[dict[str, Any]]:
    """This person's songs, newest first."""
    folder = _songs_dir(state_dir)
    if not folder.is_dir():
        return []
    found = []
    for path in folder.iterdir():
        match = _SONG_NAME.match(path.name)
        if not match or int(match.group(1)) != int(owner_id):
            continue
        label = path.with_name(path.name + _TITLE_FILE)
        title = label.read_text(encoding="utf-8") if label.is_file() else ""
        found.append({"token": match.group(2), "title": title or "Song",
                      "bytes": path.stat().st_size, "at": path.stat().st_mtime})
    return sorted(found, key=lambda s: -s["at"])


def song_path(state_dir: Path | str, owner_id: int, token: Any) -> Path | None:
    """The file of this person's song *token*, or None if it is not theirs."""
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token):
        return None
    folder = _songs_dir(state_dir)
    if not folder.is_dir():
        return None
    for path in folder.glob(f"{int(owner_id)}-{token}.*"):
        if _SONG_NAME.match(path.name):
            return path
    return None


def delete_song(state_dir: Path | str, owner_id: int, token: Any) -> bool:
    path = song_path(state_dir, owner_id, token)
    if path is None:
        return False
    path.unlink(missing_ok=True)
    path.with_name(path.name + _TITLE_FILE).unlink(missing_ok=True)
    return True


def forget_songs(state_dir: Path | str, owner_id: int) -> None:
    """Every song of a profile that is being removed."""
    for song in songs_of(state_dir, owner_id):
        delete_song(state_dir, owner_id, song["token"])
