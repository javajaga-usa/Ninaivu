"""Phone keys: what lets a phone back itself up without anybody opening a page.

The family app's backup screen works, but only while somebody keeps it open:
a web page cannot read a phone's library by itself or keep going in the
background. Apps made for exactly that (PhotoSync and its kind) can, and what
they speak is WebDAV. So a family member makes a key for each phone here, types
it into such an app once, and the app sends new photographs home on its own
(``api/api_webdav.py``).

**A key can only add.** It signs in as its person for one thing: sending files
into that person's phone backups, which go where every backup goes — the
review queue, or the library when the household trusts phone backups. It
cannot list the library, open a photograph, or delete anything; the most a
lost phone's key gives whoever finds it is a way to send photographs for an
administrator to turn down. That is why a key needs no PIN and never expires:
it is revoked instead, from the profile that made it or from the console.

**Kept as a hash.** The key is shown once, when it is made. The table keeps its
SHA-256 (as the sessions table does for its tokens) and the last four
characters, so a phone's key can be told apart from another's in a list.

**Only while the person can back up.** A key stops working when its profile is
disabled, deleted or made a guest, and comes back only if the profile is made a
family member again — and not even then once it has been revoked.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from typing import Any

from ..storage import db

__all__ = ["init_schema", "make", "listed", "revoke", "lookup", "used",
           "device_id", "remember_path", "path_row", "paths_under", "move_path",
           "forget_user", "PhoneKeyError", "PREFIX", "MAX_KEYS"]

#: What every key starts with, so one pasted in the wrong box is recognisable.
PREFIX = "nk-"
#: Enough for every phone and tablet a person could have, and then some.
MAX_KEYS = 20
#: A phone's name as the person gave it.
MAX_NAME = 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS phone_keys (
    id           INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL,
    name         TEXT NOT NULL DEFAULT '',
    key_hash     TEXT NOT NULL UNIQUE,
    hint         TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL DEFAULT 0,
    last_used_at REAL NOT NULL DEFAULT 0,
    files        INTEGER NOT NULL DEFAULT 0,
    revoked_at   REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_phone_keys_user ON phone_keys(user_id);
CREATE TABLE IF NOT EXISTS phone_key_files (
    key_id    INTEGER NOT NULL,
    path      TEXT NOT NULL,
    backup_id INTEGER NOT NULL,
    PRIMARY KEY (key_id, path)
);
"""


class PhoneKeyError(ValueError):
    """A key that cannot be made or changed, with the words to say why."""


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _hash(key: str) -> str:
    return hashlib.sha256((key or "").encode("utf-8")).hexdigest()


def device_id(key_id: int) -> str:
    """The phone-backup device id a key's files are remembered under: one per
    key, so two phones of one person never answer for each other."""
    return f"phone-key-{int(key_id):06d}"


def make(conn: sqlite3.Connection, user_id: int, name: str) -> tuple[dict[str, Any], str]:
    """A new key for one of *user_id*'s phones: its row, and the key itself,
    which is never available again."""
    name = " ".join(str(name or "").split())[:MAX_NAME] or "Phone"
    live = conn.execute("SELECT COUNT(*) FROM phone_keys WHERE user_id=? AND revoked_at=0",
                        (int(user_id),)).fetchone()[0]
    if live >= MAX_KEYS:
        raise PhoneKeyError(f"A profile can have {MAX_KEYS} phone keys. Revoke one "
                        "you no longer use first.")
    key = PREFIX + secrets.token_urlsafe(24)
    with db._write_lock:                                      # noqa: SLF001
        cur = conn.execute(
            "INSERT INTO phone_keys(user_id, name, key_hash, hint, created_at) "
            "VALUES(?,?,?,?,?)", (int(user_id), name, _hash(key), key[-4:], time.time()))
        conn.commit()
    return _public(_row(conn, cur.lastrowid)), key


def listed(conn: sqlite3.Connection, user_id: int | None = None) -> list[dict[str, Any]]:
    """Keys still in use, newest first: one person's, or everybody's with the
    person's name beside each (for the console)."""
    where, params = "k.revoked_at=0", []
    if user_id is not None:
        where += " AND k.user_id=?"
        params.append(int(user_id))
    rows = conn.execute(
        "SELECT k.*, COALESCE(NULLIF(u.display_name, ''), u.username, "
        "'Former family member') AS person FROM phone_keys k "
        f"LEFT JOIN users u ON u.id = k.user_id WHERE {where} "
        "ORDER BY k.created_at DESC, k.id DESC", params).fetchall()
    return [{**_public(r), "person": r["person"]} for r in rows]


def revoke(conn: sqlite3.Connection, key_id: int, user_id: int | None = None) -> bool:
    """Stop a key working, for good. *user_id* limits it to that person's own
    keys. Whether there was such a key to revoke."""
    where, params = "id=? AND revoked_at=0", [int(key_id)]
    if user_id is not None:
        where += " AND user_id=?"
        params.append(int(user_id))
    with db._write_lock:                                      # noqa: SLF001
        cur = conn.execute(f"UPDATE phone_keys SET revoked_at=? WHERE {where}",
                           [time.time(), *params])
        conn.commit()
    return cur.rowcount > 0


def lookup(conn: sqlite3.Connection, key: str):
    """The key's row and the profile it signs in as, or None.

    None for anything but a live key of an active family member or
    administrator: a guest cannot back up a phone, and neither can a profile
    that has been switched off.
    """
    from ..server import auth                                 # noqa: PLC0415

    if not key or not key.startswith(PREFIX) or len(key) > 128:
        return None
    row = conn.execute("SELECT * FROM phone_keys WHERE key_hash=? AND revoked_at=0",
                       (_hash(key),)).fetchone()
    if row is None:
        return None
    user = auth.get_user(conn, int(row["user_id"]))
    if user is None or not user.active or \
            auth.ROLE_RANK.get(user.role, 0) < auth.ROLE_RANK[auth.ROLE_FAMILY]:
        return None
    return row, user


#: Written at most this often per key, not on every request a sync app makes.
_USED_EVERY = 60.0


def used(conn: sqlite3.Connection, key_id: int, *, files: int = 0) -> None:
    """Note that a key was used, and how many files it sent."""
    now = time.time()
    if not files:
        row = conn.execute("SELECT last_used_at FROM phone_keys WHERE id=?",
                           (int(key_id),)).fetchone()
        if row is not None and now - float(row["last_used_at"] or 0) < _USED_EVERY:
            return
    with db._write_lock:                                      # noqa: SLF001
        conn.execute("UPDATE phone_keys SET last_used_at=?, files=files+? WHERE id=?",
                     (now, int(files), int(key_id)))
        conn.commit()


# ---------------------------------------------------------------------------
# Where the phone thinks it put each file
# ---------------------------------------------------------------------------
#
# A sync app keeps its own folders on the server (often one per month) and may
# look in them to see what is already there. Ninaivu files a backup by the date
# it was taken, wherever the phone put it, so the phone's paths are only
# remembered: enough to answer "is 2024/05/IMG_0001.HEIC there?" truthfully.

def remember_path(conn: sqlite3.Connection, key_id: int, path: str, backup_id: int) -> None:
    with db._write_lock:                                      # noqa: SLF001
        conn.execute("INSERT OR REPLACE INTO phone_key_files(key_id, path, backup_id) "
                     "VALUES(?,?,?)", (int(key_id), path, int(backup_id)))
        conn.commit()


def path_row(conn: sqlite3.Connection, key_id: int, path: str) -> sqlite3.Row | None:
    """The backup a phone sent to *path*, if it is safely here."""
    return conn.execute(
        "SELECT pb.* FROM phone_key_files f JOIN phone_backups pb ON pb.id = f.backup_id "
        "WHERE f.key_id=? AND f.path=? AND pb.state IN ('staged','done','duplicate')",
        (int(key_id), path)).fetchone()


def paths_under(conn: sqlite3.Connection, key_id: int, folder: str,
                limit: int | None = None) -> list[tuple[str, sqlite3.Row]]:
    """Every safely-arrived file this key put under *folder* (``""`` for the
    top), at any depth, with its backup row.

    Read as a range of the (key, path) key, not with LIKE, which compares
    without case here and so walked every file the phone ever sent; and not
    cut off at ten thousand, which left a phone that keeps everything in one
    folder looking at a list with its newest files missing."""
    prefix = (folder.strip("/") + "/") if folder.strip("/") else ""
    where, params = "f.key_id=?", [int(key_id)]
    if prefix:
        # Every path that starts with "a/b/" sorts from "a/b/" up to "a/b0".
        where += " AND f.path >= ? AND f.path < ?"
        params += [prefix, prefix[:-1] + chr(ord("/") + 1)]
    sql = ("SELECT f.path AS path, pb.* FROM phone_key_files f "
           "JOIN phone_backups pb ON pb.id = f.backup_id "
           f"WHERE {where} AND pb.state IN ('staged','done','duplicate') ORDER BY f.path")
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [(r["path"], r) for r in conn.execute(sql, params).fetchall()]


def move_path(conn: sqlite3.Connection, key_id: int, source: str, target: str) -> bool:
    """A phone renaming what it sent. Only the remembered path changes; the
    photograph is where Ninaivu filed it."""
    with db._write_lock:                                      # noqa: SLF001
        row = conn.execute("SELECT backup_id FROM phone_key_files WHERE key_id=? AND path=?",
                           (int(key_id), source)).fetchone()
        if row is None:
            return False
        conn.execute("DELETE FROM phone_key_files WHERE key_id=? AND path=?",
                     (int(key_id), source))
        conn.execute("INSERT OR REPLACE INTO phone_key_files(key_id, path, backup_id) "
                     "VALUES(?,?,?)", (int(key_id), target, int(row["backup_id"])))
        conn.commit()
    return True


def forget_user(conn: sqlite3.Connection, user_id: int) -> None:
    """A deleted profile's keys, gone. Profile ids are reused, and a key left
    behind would have signed in as whoever was made next. The caller commits."""
    conn.execute("DELETE FROM phone_key_files WHERE key_id IN "
                 "(SELECT id FROM phone_keys WHERE user_id=?)", (int(user_id),))
    conn.execute("DELETE FROM phone_keys WHERE user_id=?", (int(user_id),))


def _row(conn: sqlite3.Connection, key_id: int) -> sqlite3.Row:
    return conn.execute("SELECT * FROM phone_keys WHERE id=?", (int(key_id),)).fetchone()


def _public(row: sqlite3.Row) -> dict[str, Any]:
    return {"id": row["id"], "user_id": row["user_id"], "name": row["name"],
            "hint": row["hint"], "created_at": row["created_at"],
            "last_used_at": row["last_used_at"], "files": row["files"]}
