"""Backing up a phone to Ninaivu over the home network.

The family app can already take an upload. This is what turns that into a
backup somebody can rely on: the phone sends what is new since last time,
carries on from where it stopped when the connection or the tab goes away, and
can say — per photograph — whether it is safely here.

**Remembered per person and per phone.** A phone gets an id the first time it
backs up (kept in the browser), and every file it offers is remembered by what
the phone can say about it without reading it: name, size and the time it was
last modified. Offer the same photograph again next week and it is known to be
backed up already, without a byte being sent.

**Resumable, per file.** A file arrives in pieces, appended to a partial file
kept in Ninaivu's state folder, and the server says how much it has. A phone
that loses the Wi-Fi, locks its screen or has its tab closed asks again and
carries on from that byte, not from the start of a four-gigabyte video.

**Checked, and not stored twice.** When the last byte arrives the size must be
the size the phone said, and the file is hashed. If the library already holds
exactly those bytes — a photograph that came in by another route — it is
recorded as backed up and set aside rather than queued a second time.

**The household's rules still apply.** A finished file goes where every family
upload goes: into the review queue, for an administrator to approve, filed by
the date the photograph was taken. An administrator's own phone, or any phone
once the console's *trust phone backups* switch is on, is approved in one go
when the phone says it has finished a batch — one pause of the indexer for the
lot, rather than one per photograph.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import sqlite3
import threading
import time
import weakref
from pathlib import Path
from typing import Any, Iterable

from ..storage import db

log = logging.getLogger(__name__)

__all__ = ["init_schema", "check", "begin", "receive", "summary",
           "approve_finished", "BackupError", "RECEIVING", "STAGED", "DONE",
           "DUPLICATE", "FAILED"]

#: A file offered and part-way here.
RECEIVING = "receiving"
#: All here, checked, and waiting for an administrator.
STAGED = "staged"
#: In the library.
DONE = "done"
#: All here, and the library already had exactly these bytes.
DUPLICATE = "duplicate"
#: Arrived wrong (the size did not match, or it is not media).
FAILED = "failed"

#: States in which the phone need not send the file again.
SAFE = (STAGED, DONE, DUPLICATE)

#: The largest single file taken. Well above any phone video.
MAX_FILE_BYTES = 64 * 1024 ** 3
#: The largest piece taken in one request.
MAX_PIECE_BYTES = 32 * 1024 * 1024
#: How many files one check may ask about.
MAX_CHECK = 5000
#: Space kept free in the state folder whatever arrives.
KEEP_FREE_BYTES = 2 * 1024 ** 3


def has_room(folder: Path | str, size: int) -> bool:
    """Would *size* more bytes in *folder* still leave KEEP_FREE_BYTES free?

    For everything a family profile can send to be kept beside the index (a
    voice story, prints being looked at), not only phone backups and uploads.
    A folder not made yet is judged by the nearest one that is.
    """
    place = Path(folder)
    while not place.exists() and place.parent != place:
        place = place.parent
    try:
        return shutil.disk_usage(place).free - max(0, int(size)) >= KEEP_FREE_BYTES
    except OSError:
        return True

SCHEMA = """
CREATE TABLE IF NOT EXISTS phone_backups (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    device_id   TEXT NOT NULL,
    device      TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL,
    filename    TEXT NOT NULL,
    size        INTEGER NOT NULL,
    modified    REAL NOT NULL DEFAULT 0,
    received    INTEGER NOT NULL DEFAULT 0,
    state       TEXT NOT NULL DEFAULT 'receiving',
    sha256      TEXT NOT NULL DEFAULT '',
    upload_id   INTEGER,
    asset_id    INTEGER,
    error       TEXT NOT NULL DEFAULT '',
    started_at  REAL NOT NULL DEFAULT 0,
    finished_at REAL NOT NULL DEFAULT 0,
    UNIQUE(user_id, device_id, fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_phone_backups_device
    ON phone_backups(user_id, device_id, state);
"""

#: One writer per file at a time: two tabs on one phone must not interleave
#: pieces of the same photograph. Held weakly, so finished files cost nothing.
_file_locks: weakref.WeakValueDictionary[int, threading.Lock] = weakref.WeakValueDictionary()
_locks_guard = threading.Lock()


class BackupError(ValueError):
    """A request that cannot be honoured, with the words to say why.

    ``offset`` is set when the phone is simply out of step: it should carry on
    from there.
    """

    def __init__(self, message: str, status: int = 400, offset: int | None = None):
        super().__init__(message)
        self.status = status
        self.offset = offset


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _folder(cfg) -> Path:
    folder = Path(cfg.state_dir) / "phone-backup"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _part(cfg, row_id: int) -> Path:
    return _folder(cfg) / f"{int(row_id)}.part"


def fingerprint(name: str, size: int, modified: float) -> str:
    """What a phone can say about a file without reading it."""
    return f"{name}|{int(size)}|{int(round(float(modified or 0)))}"


def _lock_for(row_id: int) -> threading.Lock:
    with _locks_guard:
        lock = _file_locks.get(int(row_id))
        if lock is None:
            lock = threading.Lock()
            _file_locks[int(row_id)] = lock
        return lock


# ---------------------------------------------------------------------------
# Asking what is already here
# ---------------------------------------------------------------------------

def check(conn, user_id: int, device_id: str,
          files: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """For each offered file: already safe, part-way here, or new.

    Nothing is created; this is how the phone decides what to send and how it
    shows "412 already backed up" before anything moves.
    """
    wanted = list(files)[:MAX_CHECK]
    settle(conn, user_id)
    known: dict[str, sqlite3.Row] = {
        row["fingerprint"]: row for row in conn.execute(
            "SELECT id, fingerprint, state, received, size FROM phone_backups "
            "WHERE user_id=? AND device_id=?", (int(user_id), device_id))}
    answers = []
    for index, item in enumerate(wanted):
        key = fingerprint(str(item.get("name") or ""), int(item.get("size") or 0),
                          float(item.get("modified") or 0))
        row = known.get(key)
        if row is None:
            answers.append({"index": index, "state": "new", "offset": 0})
        else:
            answers.append({"index": index, "id": row["id"], "state": row["state"],
                            "offset": int(row["received"])})
    return answers


# ---------------------------------------------------------------------------
# Receiving
# ---------------------------------------------------------------------------

def begin(conn, cfg, user_id: int, device_id: str, device: str, *,
          name: str, size: int, modified: float) -> dict[str, Any]:
    """Start receiving a file, or say where to carry on from."""
    if size <= 0 or size > MAX_FILE_BYTES:
        raise BackupError("That file is empty, or larger than Ninaivu takes.")
    if time.time() - _swept["at"] > SWEEP_EVERY:
        _swept["at"] = time.time()
        try:
            sweep_abandoned(conn, cfg)
        except (OSError, sqlite3.Error) as exc:
            log.warning("could not clear abandoned phone backups: %s", exc)
    key = fingerprint(name, size, modified)
    row = conn.execute(
        "SELECT * FROM phone_backups WHERE user_id=? AND device_id=? AND fingerprint=?",
        (int(user_id), device_id, key)).fetchone()
    if row is not None and row["state"] in SAFE:
        return _answer(row)
    free = shutil.disk_usage(_folder(cfg)).free
    already = int(row["received"]) if row is not None else 0
    if free - (size - already) < KEEP_FREE_BYTES:
        raise BackupError("Ninaivu's computer is nearly out of space, so this "
                          "file cannot be taken right now.", 507)
    now = time.time()
    with db._write_lock:                                  # noqa: SLF001
        if row is None:
            cur = conn.execute(
                "INSERT INTO phone_backups(user_id, device_id, device, fingerprint, "
                "filename, size, modified, started_at) VALUES(?,?,?,?,?,?,?,?)",
                (int(user_id), device_id, device[:80], key, name, int(size),
                 float(modified or 0), now))
            row_id = cur.lastrowid
        else:
            # A file that failed its checks is offered afresh.
            row_id = row["id"]
            if row["state"] == FAILED:
                _part(cfg, row_id).unlink(missing_ok=True)
                conn.execute("UPDATE phone_backups SET state=?, received=0, error='', "
                             "device=? WHERE id=?", (RECEIVING, device[:80], row_id))
            else:
                # Carry on from what is actually on disk: a partial file lost
                # with a cleaned-out state folder starts again from nothing.
                part = _part(cfg, row_id)
                have = part.stat().st_size if part.exists() else 0
                conn.execute("UPDATE phone_backups SET device=?, received=? WHERE id=?",
                             (device[:80], have, row_id))
        conn.commit()
    return _answer(_row(conn, row_id))


#: A partial file nothing has been added to for this long is given up on.
ABANDONED_AFTER = 30 * 24 * 3600
#: How often starting a file also looks for abandoned ones.
SWEEP_EVERY = 24 * 3600
_swept = {"at": 0.0}


def sweep_abandoned(conn, cfg, older_than: float = ABANDONED_AFTER) -> int:
    """Clear partial files nobody has come back to.

    A phone that starts a long video and never returns leaves the part it sent
    in Ninaivu's state folder, on the disk the index lives on, for good. After
    a month it is removed and the file marked failed, which a phone offering
    it again simply starts afresh. Returns how many were cleared.
    """
    now = time.time()
    cleared = 0
    for row in conn.execute("SELECT id, started_at FROM phone_backups WHERE state=?",
                            (RECEIVING,)).fetchall():
        part = _part(cfg, row["id"])
        try:
            touched = part.stat().st_mtime if part.exists() else float(row["started_at"] or 0)
        except OSError:
            continue
        if now - touched < older_than:
            continue
        with _lock_for(row["id"]):
            part.unlink(missing_ok=True)
            with db._write_lock:                              # noqa: SLF001
                conn.execute(
                    "UPDATE phone_backups SET state=?, received=0, error=? "
                    "WHERE id=? AND state=?",
                    (FAILED, "Nothing more arrived for a month, so the part that had "
                             "was cleared. It starts again if it is offered.",
                     row["id"], RECEIVING))
                conn.commit()
        cleared += 1
    if cleared:
        log.info("phone backup: cleared %d abandoned partial file(s)", cleared)
    return cleared


def receive(conn, cfg, user_id: int, row_id: int, offset: int, piece: bytes,
            *, root: str, scope: str, max_visibility: int | None = None) -> dict[str, Any]:
    """Append one piece at *offset*. Finishes the file when it is all here."""
    if len(piece) > MAX_PIECE_BYTES:
        raise BackupError("That piece is too large.", 413)
    with _lock_for(row_id):
        row = _row(conn, row_id)
        if row is None or int(row["user_id"]) != int(user_id):
            raise BackupError("No such file is being backed up.", 404)
        if row["state"] in SAFE:
            return _answer(row)
        if row["state"] != RECEIVING:
            raise BackupError("Offer this file again to start it afresh.", 409)
        part = _part(cfg, row_id)
        have = part.stat().st_size if part.exists() else 0
        if offset != have:
            # The phone is out of step — a piece that was sent but whose answer
            # never arrived. Not an error: carry on from what is here.
            raise BackupError("Carry on from where Ninaivu has got to.", 409, offset=have)
        if have + len(piece) > int(row["size"]):
            raise BackupError("More arrived than the file's size.", 400)
        # Asked again for every piece, not only when the file was offered: two
        # phones sending long videos at once each passed that check against
        # the same free space, and between them could fill the disk the
        # library's index lives on.
        if shutil.disk_usage(_folder(cfg)).free - len(piece) < KEEP_FREE_BYTES:
            raise BackupError("Ninaivu's computer is nearly out of space, so this "
                              "file cannot be taken right now.", 507, offset=have)
        with open(part, "ab") as out:
            out.write(piece)
            out.flush()
        have += len(piece)
        with db._write_lock:                              # noqa: SLF001
            conn.execute("UPDATE phone_backups SET received=? WHERE id=?", (have, row_id))
            conn.commit()
        if have == int(row["size"]):
            _finish(conn, cfg, _row(conn, row_id), part, root=root, scope=scope,
                    max_visibility=max_visibility)
        return _answer(_row(conn, row_id))


def _finish(conn, cfg, row, part: Path, *, root: str, scope: str,
            max_visibility: int | None = None) -> None:
    """All the bytes are here: check them, and file them or set them aside."""
    from . import upload_review                              # noqa: PLC0415

    digest = _sha256(part)
    duplicate = _in_library(conn, int(row["size"]), digest, root=root, scope=scope,
                            max_visibility=max_visibility)
    now = time.time()
    if duplicate is not None:
        part.unlink(missing_ok=True)
        with db._write_lock:                              # noqa: SLF001
            conn.execute("UPDATE phone_backups SET state=?, sha256=?, asset_id=?, "
                         "finished_at=? WHERE id=?",
                         (DUPLICATE, digest, duplicate, now, row["id"]))
            conn.commit()
        return
    try:
        staged = upload_review.stage(
            conn, cfg, _FromFile(part), row["filename"], root, scope,
            int(row["user_id"]), mtime=float(row["modified"] or 0) or None)
    except (OSError, ValueError, sqlite3.Error) as exc:
        part.unlink(missing_ok=True)
        with db._write_lock:                              # noqa: SLF001
            conn.execute("UPDATE phone_backups SET state=?, sha256=?, error=?, "
                         "finished_at=? WHERE id=?",
                         (FAILED, digest, str(exc)[:300], now, row["id"]))
            conn.commit()
        log.warning("a phone backup of %s could not be staged: %s", row["filename"], exc)
        return
    part.unlink(missing_ok=True)
    with db._write_lock:                                  # noqa: SLF001
        conn.execute("UPDATE phone_backups SET state=?, sha256=?, upload_id=?, "
                     "finished_at=? WHERE id=?",
                     (STAGED, digest, staged["id"], now, row["id"]))
        conn.commit()


class _FromFile:
    """What :func:`upload_review.stage` expects of an upload, from a file."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def save(self, output) -> None:
        with open(self.path, "rb") as source:
            shutil.copyfileobj(source, output, 4 * 1024 * 1024)

    def link_into(self, target: Path) -> None:
        """Put the file at *target*, which must not exist, without copying it
        when the disk allows. The original name is removed by the caller."""
        try:
            os.link(self.path, target)
        except FileExistsError:
            raise
        except OSError:
            with open(target, "xb") as output:
                self.save(output)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        while chunk := source.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _in_library(conn, size: int, digest: str, *, root: str | None = None,
                scope: str | None = None, max_visibility: int | None = None) -> int | None:
    """The id of a library file with exactly these bytes, if there is one.

    Only files of the same size are read, which in practice is none or one.
    Only among the files the sender may see, in the library and folder their
    backups go to: a twin in another member's private folder could be deleted
    by them without this phone ever hearing, and the answer "already here"
    told the sender that private file existed.
    """
    where, params = "size=? AND trashed=0", [int(size)]
    if root is not None:
        where += " AND root=?"
        params.append(root)
    if scope:
        where += " AND (folder=? OR folder LIKE ? ESCAPE '\\')"
        like = scope.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params += [scope, like + "/%"]
    if max_visibility is not None:
        where += " AND visibility<=?"
        params.append(int(max_visibility))
    for row in conn.execute(
            f"SELECT id, root, rel_path FROM assets WHERE {where} LIMIT 20", params):
        path = Path(row["root"]) / row["rel_path"]
        try:
            if path.is_file() and _sha256(path) == digest:
                return int(row["id"])
        except OSError:
            continue
    return None


# ---------------------------------------------------------------------------
# Into the library, and what the phone is told
# ---------------------------------------------------------------------------

def approve_finished(conn, cfg, user_id: int, reviewer: int,
                     landed: set[str] | None = None) -> dict[str, int]:
    """Approve every staged phone backup of this person's. For callers that
    have already decided they may: an administrator, or trusted backups.
    *landed*, when given, collects the library folders they were filed in."""
    from . import upload_review                              # noqa: PLC0415

    approved = failed = 0
    rows = conn.execute(
        "SELECT pb.id, pb.upload_id FROM phone_backups pb "
        "JOIN pending_uploads pu ON pu.id = pb.upload_id "
        "WHERE pb.user_id=? AND pb.state=? AND pu.status='pending'",
        (int(user_id), STAGED)).fetchall()
    for row in rows:
        try:
            asset = upload_review.approve(conn, cfg, int(row["upload_id"]), reviewer)
        except (OSError, ValueError, sqlite3.Error) as exc:
            failed += 1
            log.warning("could not approve phone backup %s: %s", row["id"], exc)
            continue
        with db._write_lock:                              # noqa: SLF001
            conn.execute("UPDATE phone_backups SET state=?, asset_id=? WHERE id=?",
                         (DONE, asset["id"] if asset else None, row["id"]))
            conn.commit()
        if landed is not None and asset:
            landed.add(asset["root"])
        approved += 1
    return {"approved": approved, "failed": failed}


def settle(conn, user_id: int) -> None:
    """Catch up with reviews done in the console since the phone last asked."""
    with db._write_lock:                                  # noqa: SLF001
        conn.execute(
            "UPDATE phone_backups SET state=?, asset_id=(SELECT asset_id FROM "
            "pending_uploads pu WHERE pu.id = phone_backups.upload_id) "
            "WHERE user_id=? AND state=? AND upload_id IN "
            "(SELECT id FROM pending_uploads WHERE status='approved')",
            (DONE, int(user_id), STAGED))
        conn.execute(
            "UPDATE phone_backups SET state=?, error='An administrator turned it down.' "
            "WHERE user_id=? AND state=? AND upload_id IN "
            "(SELECT id FROM pending_uploads WHERE status NOT IN ('pending','approved'))",
            (FAILED, int(user_id), STAGED))
        # "Already here" holds only while the library's copy does. Once it has
        # been deleted the phone must send its own again, not be told forever
        # that the file is safe.
        conn.execute(
            "UPDATE phone_backups SET state=?, received=0, error='The copy that was "
            "already in the library has since been removed; it is sent again.' "
            "WHERE user_id=? AND state=? AND NOT EXISTS (SELECT 1 FROM assets a "
            "WHERE a.id = phone_backups.asset_id AND a.trashed=0)",
            (FAILED, int(user_id), DUPLICATE))
        conn.commit()


def summary(conn, user_id: int, device_id: str = "") -> dict[str, Any]:
    """What is safely here, for one phone and for all of this person's."""
    settle(conn, user_id)

    def counts(where: str, params: tuple) -> dict[str, Any]:
        row = conn.execute(
            "SELECT COUNT(*) AS files, "
            f"COALESCE(SUM(state IN ('{STAGED}','{DONE}','{DUPLICATE}')), 0) AS safe, "
            f"COALESCE(SUM(state='{DONE}'), 0) AS in_library, "
            f"COALESCE(SUM(state='{STAGED}'), 0) AS waiting_review, "
            f"COALESCE(SUM(state='{DUPLICATE}'), 0) AS already_here, "
            f"COALESCE(SUM(state='{RECEIVING}'), 0) AS partial, "
            f"COALESCE(SUM(state='{FAILED}'), 0) AS failed, "
            f"COALESCE(SUM(CASE WHEN state IN ('{STAGED}','{DONE}','{DUPLICATE}') "
            "THEN size ELSE 0 END), 0) AS safe_bytes, "
            "MAX(finished_at) AS last_backup "
            f"FROM phone_backups WHERE {where}", params).fetchone()
        return {k: row[k] for k in row.keys()}

    phones = [dict(r) for r in conn.execute(
        "SELECT device_id, MAX(device) AS device, COUNT(*) AS files, "
        "MAX(finished_at) AS last_backup FROM phone_backups WHERE user_id=? "
        "GROUP BY device_id ORDER BY last_backup DESC", (int(user_id),))]
    recent = [dict(r) for r in conn.execute(
        "SELECT id, filename, size, state, error, finished_at FROM phone_backups "
        "WHERE user_id=? AND device_id=? ORDER BY COALESCE(NULLIF(finished_at, 0), "
        "started_at) DESC LIMIT 30", (int(user_id), device_id))] if device_id else []
    return {
        "this_phone": counts("user_id=? AND device_id=?", (int(user_id), device_id))
        if device_id else None,
        "everything": counts("user_id=?", (int(user_id),)),
        "phones": phones,
        "recent": recent,
    }


def _row(conn, row_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM phone_backups WHERE id=?", (int(row_id),)).fetchone()


def _answer(row: sqlite3.Row) -> dict[str, Any]:
    return {"id": row["id"], "state": row["state"], "offset": int(row["received"]),
            "size": int(row["size"]), "error": row["error"] or ""}
