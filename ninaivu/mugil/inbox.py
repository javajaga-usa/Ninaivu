"""Phone uploads while the Primary sleeps: the phone inbox on the Secondary.

A sync app on a phone (PhotoSync and its kind) sends new photographs to the
phone inbox, WebDAV at ``/dav/`` signed in with a phone key (api/api_webdav.py).
With two computers the Mac is often asleep, so the always-on Secondary answers
the same inbox for it:

* the Primary hands the Secondary its phone keys, as their hashes only, with
  whose each is (:func:`keys_to_hand`); a key revoked on the Primary stops
  working on the Secondary at the next hand-over;
* the Secondary keeps what a phone sends, whole and fingerprinted, in
  ``.ninaivu-phone-inbox`` on the originals disk (:class:`Inbox`), and tells
  the phone it is safely there;
* when the Primary is back, it collects every file, checks each against its
  fingerprint, and files it as the phone backup it is, exactly as if the phone
  had sent it to the Mac (:func:`collect`): the review queue, or straight into
  the library when the household trusts phone backups. Only then does the
  Secondary let its copy go; the original comes back to it with the next copy
  of the originals, like every other.

The inbox takes and keeps; it never shows anybody a photograph. A key that
the Primary does not know any more, or whose person cannot back up, is not
collected: its files stay on the Secondary until the key is known again.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import secrets
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

log = logging.getLogger(__name__)

#: On the Secondary's originals disk, beside the originals.
FOLDER = ".ninaivu-phone-inbox"
LEDGER = "inbox.db"
#: Kept free on the Secondary's disk whatever a phone sends.
KEEP_FREE = 2 * 1024 ** 3
#: Collected at most this many at a time.
BATCH = 200

_SCHEMA = """
CREATE TABLE IF NOT EXISTS keys (
    key_id   INTEGER PRIMARY KEY,
    key_hash TEXT NOT NULL UNIQUE,
    name     TEXT NOT NULL DEFAULT '',
    user_id  INTEGER NOT NULL,
    username TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS files (
    id          INTEGER PRIMARY KEY,
    key_id      INTEGER NOT NULL,
    path        TEXT NOT NULL,
    name        TEXT NOT NULL,
    file        TEXT NOT NULL DEFAULT '',
    size        INTEGER NOT NULL,
    sha256      TEXT NOT NULL,
    modified    REAL NOT NULL DEFAULT 0,
    finished_at REAL NOT NULL DEFAULT 0,
    taken_at    REAL NOT NULL DEFAULT 0,
    UNIQUE (key_id, path)
);
"""


def key_hash(secret: str) -> str:
    return hashlib.sha256((secret or "").encode("utf-8")).hexdigest()


class Refused(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class Inbox:
    """On the Secondary: what phones sent while the Primary was away."""

    def __init__(self, holding_folder: str | Path) -> None:
        self.base = Path(holding_folder) / FOLDER
        self._lock = threading.Lock()

    @contextlib.contextmanager
    def _ledger(self):
        self.base.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.base / LEDGER, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(_SCHEMA)
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -- keys ------------------------------------------------------------------
    def set_keys(self, keys: Iterable[dict[str, Any]]) -> int:
        """The Primary's live keys, in place of the ones it handed before."""
        rows = []
        for key in keys:
            try:
                rows.append((int(key["key_id"]), str(key["key_hash"]),
                             str(key.get("name") or "")[:60], int(key["user_id"]),
                             str(key.get("username") or "")[:200]))
            except (KeyError, TypeError, ValueError):
                continue
        with self._lock, self._ledger() as conn:
            conn.execute("DELETE FROM keys")
            conn.executemany("INSERT OR REPLACE INTO keys(key_id, key_hash, name, user_id, "
                             "username) VALUES (?,?,?,?,?)", rows)
        return len(rows)

    def lookup(self, secret: str) -> dict[str, Any] | None:
        if not secret or len(secret) > 128 or not (self.base / LEDGER).exists():
            return None
        with self._lock, self._ledger() as conn:
            row = conn.execute("SELECT * FROM keys WHERE key_hash=?",
                               (key_hash(secret),)).fetchone()
        return dict(row) if row else None

    # -- files -----------------------------------------------------------------
    def row(self, key_id: int, path: str) -> dict[str, Any] | None:
        """What a phone sent to *path*, whether waiting here or collected."""
        if not (self.base / LEDGER).exists():
            return None
        with self._lock, self._ledger() as conn:
            row = conn.execute("SELECT * FROM files WHERE key_id=? AND path=?",
                               (int(key_id), path)).fetchone()
        return dict(row) if row else None

    def under(self, key_id: int, folder: str) -> list[tuple[str, dict[str, Any]]]:
        if not (self.base / LEDGER).exists():
            return []
        prefix = (folder + "/") if folder else ""
        with self._lock, self._ledger() as conn:
            rows = conn.execute("SELECT * FROM files WHERE key_id=? AND substr(path, 1, ?)=? "
                                "ORDER BY path", (int(key_id), len(prefix), prefix)).fetchall()
        return [(r["path"], dict(r)) for r in rows]

    def move(self, key_id: int, path: str, destination: str) -> bool:
        with self._lock, self._ledger() as conn:
            cur = conn.execute("UPDATE OR REPLACE files SET path=? WHERE key_id=? AND path=?",
                               (destination, int(key_id), path))
        return cur.rowcount > 0

    def receive(self, key_id: int, path: str, name: str, stream, *, length: int | None,
                limit: int, modified: float = 0.0) -> bool:
        """Keep what the phone is sending for *path*; whether it was known
        before (a file sent again)."""
        if length is not None and length > limit:
            raise Refused(413, f"Ninaivu takes files up to {limit // 1024 ** 3} GB from a phone.")
        self.base.mkdir(parents=True, exist_ok=True)
        try:
            free = shutil.disk_usage(self.base).free
        except OSError:
            free = 0
        if free - (length or 0) < KEEP_FREE:
            raise Refused(507, "Ninaivu's computer is nearly out of space, so this file "
                               "cannot be taken right now.")
        files = self.base / "files"
        files.mkdir(exist_ok=True)
        partial = files / f".partial-{secrets.token_hex(8)}"
        fresh = hashlib.sha256()
        received = 0
        try:
            with open(partial, "xb") as out:
                while length is None or received < length:
                    piece = stream.read(1024 * 1024 if length is None
                                        else min(1024 * 1024, length - received))
                    if not piece:
                        break
                    received += len(piece)
                    if received > limit:
                        raise Refused(413, "That file is larger than Ninaivu takes from a phone.")
                    fresh.update(piece)
                    out.write(piece)
                out.flush()
                os.fsync(out.fileno())
            if (length is not None and received != length) or received == 0:
                raise Refused(400, "The file did not arrive whole. Send it again.")
            digest = fresh.hexdigest()
            stored = f"{digest[:2]}/{digest}-{secrets.token_hex(4)}"
            (files / digest[:2]).mkdir(exist_ok=True)
            os.replace(partial, files / stored)
        except Refused:
            partial.unlink(missing_ok=True)
            raise
        except OSError as exc:
            partial.unlink(missing_ok=True)
            log.warning("a phone's upload of %s could not be written: %s", name, exc)
            raise Refused(500, "The file could not be written. Send it again.") from exc
        with self._lock, self._ledger() as conn:
            old = conn.execute("SELECT file, taken_at FROM files WHERE key_id=? AND path=?",
                               (int(key_id), path)).fetchone()
            conn.execute(
                "INSERT OR REPLACE INTO files(key_id, path, name, file, size, sha256, modified, "
                "finished_at, taken_at) VALUES (?,?,?,?,?,?,?,?,0)",
                (int(key_id), path, name, stored, received, digest, float(modified or 0),
                 time.time()))
        if old is not None and old["file"] and not old["taken_at"]:
            (files / old["file"]).unlink(missing_ok=True)
        return old is not None

    def waiting(self, limit: int = BATCH) -> list[dict[str, Any]]:
        if not (self.base / LEDGER).exists():
            return []
        with self._lock, self._ledger() as conn:
            rows = conn.execute("SELECT id, key_id, path, name, size, sha256, modified "
                                "FROM files WHERE taken_at=0 ORDER BY id LIMIT ?",
                                (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def file_of(self, file_id: int) -> tuple[Path, dict[str, Any]] | None:
        if not (self.base / LEDGER).exists():
            return None
        with self._lock, self._ledger() as conn:
            row = conn.execute("SELECT * FROM files WHERE id=? AND taken_at=0",
                               (int(file_id),)).fetchone()
        if row is None or not row["file"]:
            return None
        path = self.base / "files" / row["file"]
        return (path, dict(row)) if path.is_file() else None

    def taken(self, ids: Iterable[int]) -> int:
        """The Primary has filed these: the copies here go, the record stays,
        so the phone is still told they are safely home."""
        done = 0
        with self._lock, self._ledger() as conn:
            for file_id in ids:
                row = conn.execute("SELECT file FROM files WHERE id=? AND taken_at=0",
                                   (int(file_id),)).fetchone()
                if row is None:
                    continue
                conn.execute("UPDATE files SET taken_at=?, file='' WHERE id=?",
                             (time.time(), int(file_id)))
                if row["file"]:
                    (self.base / "files" / row["file"]).unlink(missing_ok=True)
                done += 1
        return done

    def summary(self) -> dict[str, Any]:
        out = {"waiting": 0, "bytes": 0, "collected": 0, "keys": 0}
        if not (self.base / LEDGER).exists():
            return out
        with self._lock, self._ledger() as conn:
            row = conn.execute("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM files "
                               "WHERE taken_at=0").fetchone()
            out["waiting"], out["bytes"] = int(row[0]), int(row[1])
            out["collected"] = int(conn.execute(
                "SELECT COUNT(*) FROM files WHERE taken_at>0").fetchone()[0])
            out["keys"] = int(conn.execute("SELECT COUNT(*) FROM keys").fetchone()[0])
        return out


# -- the Primary's side ---------------------------------------------------------------

def keys_to_hand(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The live phone keys of everybody who may back up: hashes, never keys."""
    from ..media import phone_keys                                  # noqa: PLC0415
    from ..server import auth                                       # noqa: PLC0415
    phone_keys.init_schema(conn)
    out = []
    for row in conn.execute("SELECT id, user_id, name, key_hash FROM phone_keys "
                            "WHERE revoked_at=0").fetchall():
        user = auth.get_user(conn, int(row["user_id"]))
        if user is None or not user.active or \
                auth.ROLE_RANK.get(user.role, 0) < auth.ROLE_RANK[auth.ROLE_FAMILY]:
            continue
        out.append({"key_id": int(row["id"]), "key_hash": row["key_hash"], "name": row["name"],
                    "user_id": int(row["user_id"]), "username": user.username})
    return out


def destination(cfg, user) -> tuple[str, str] | None:
    """Where *user*'s phone backups go, as the family app decides it for a
    phone that sends to the Primary; None when that library is not here."""
    from ..server import auth                                       # noqa: PLC0415
    from ..storage import roots as roots_kit                        # noqa: PLC0415
    libraries = cfg.libraries or ([cfg.active_root] if cfg.active_root else [])
    if not libraries:
        return None
    roots, sub = auth.resolve_library(user.assigned_library, libraries)
    if not roots:
        return None
    root = cfg.active_root if cfg.active_root in roots else roots[0]
    if not roots_kit.root_present(root):
        return None
    return root, sub or (user.effective_scope or "")


def collect(cfg, conn: sqlite3.Connection, partner, *,
            file_backups: Callable[[sqlite3.Connection, int], Any] | None = None,
            stop: threading.Event | None = None) -> dict[str, int]:
    """On the Primary: hand the keys over, then take in what phones sent the
    Secondary. Totals of what was done."""
    from ..media import phone_backup, phone_keys                    # noqa: PLC0415
    from ..server import auth                                       # noqa: PLC0415
    from .courier import PartnerError                               # noqa: PLC0415
    totals = {"keys": 0, "collected": 0, "duplicate": 0, "failed": 0, "left": 0}
    phone_backup.init_schema(conn)
    keys = keys_to_hand(conn)
    partner.request("PUT", "/api/mugil/inbox/keys", body={"keys": keys})
    totals["keys"] = len(keys)
    known = {k["key_id"]: k for k in keys}
    to_file: set[int] = set()
    while not (stop is not None and stop.is_set()):
        items = (partner.request("GET", "/api/mugil/inbox") or {}).get("items") or []
        items = [i for i in items if isinstance(i, dict) and int(i.get("key_id") or 0) in known]
        if not items:
            break
        taken: list[int] = []
        for item in items:
            if stop is not None and stop.is_set():
                break
            key = known[int(item["key_id"])]
            user = auth.get_user(conn, key["user_id"])
            where = destination(cfg, user) if user is not None else None
            if where is None:
                totals["left"] += 1
                continue
            target = phone_backup.incoming(cfg)
            fresh = hashlib.sha256()
            try:
                with open(target, "xb") as out:
                    def write(chunk: bytes, out=out, fresh=fresh) -> None:
                        fresh.update(chunk)
                        out.write(chunk)
                    got = partner.download("/api/mugil/inbox/file",
                                           query={"id": int(item["id"])}, write=write)
                if got != int(item["size"]) or fresh.hexdigest() != item["sha256"]:
                    log.warning("A phone's file from the Secondary, %s, is not the one it "
                                "kept; left there", item.get("name"))
                    target.unlink(missing_ok=True)
                    totals["failed"] += 1
                    continue
                name = str(item["name"])
                answer = phone_backup.receive_whole(
                    conn, cfg, user.id, phone_keys.device_id(key["key_id"]), key["name"],
                    name=name, path=target, modified=float(item.get("modified") or 0),
                    root=where[0], scope=where[1], max_visibility=user.max_visibility)
            except PartnerError as exc:
                target.unlink(missing_ok=True)
                log.info("Could not collect a phone's file from the Secondary: %s", exc)
                totals["failed"] += 1
                continue
            except (OSError, phone_backup.BackupError) as exc:
                target.unlink(missing_ok=True)
                log.warning("A phone's file from the Secondary could not be filed: %s", exc)
                totals["failed"] += 1
                continue
            if answer["state"] not in phone_backup.SAFE:
                totals["failed"] += 1
                continue
            phone_keys.remember_path(conn, key["key_id"], str(item["path"]), int(answer["id"]))
            phone_keys.used(conn, key["key_id"], files=1)
            if answer["state"] == phone_backup.DUPLICATE:
                totals["duplicate"] += 1
            else:
                totals["collected"] += 1
            if answer["state"] == phone_backup.STAGED and (user.is_admin or cfg.phone_backup_trusted):
                to_file.add(user.id)
            taken.append(int(item["id"]))
        if taken:
            partner.request("POST", "/api/mugil/inbox/taken", body={"ids": taken})
        if len(taken) < len(items):
            break                       # the rest stay for the next run
    for user_id in to_file:
        if file_backups is not None:
            try:
                file_backups(conn, user_id)
            except Exception:                                       # noqa: BLE001
                log.exception("Could not file the phone backups collected from the Secondary")
    return totals


__all__ = ["Inbox", "Refused", "collect", "keys_to_hand", "destination", "key_hash", "FOLDER"]
