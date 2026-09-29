"""What has gone to the cloud, and what has not.

This is the file the two guarantees in :mod:`ninaivu.cloud` rest on, so it is
worth saying exactly what a row means and why it is keyed the way it is.

**A row is about a file, not about an asset id.** Asset ids are database
rowids: a rescan can retire one and SQLite will hand the number to something
else later. Keying completion on an id would mean a photograph indexed under a
recycled number inheriting somebody else's "already uploaded", which is the one
mistake that loses a file silently. So a row is keyed on the library root and
the path inside it.

**"Done" is permanent.** Nothing here deletes a completed row. Not a rescan,
not disconnecting the account, not reconnecting a different one. That is what
makes a file deleted from Drive stay deleted rather than reappearing on the
next pass — the record says it went, and going again is a decision somebody
has to make on purpose, with :func:`forget`.

**A changed file is a new file.** If the size at a path is not the size that
went up, what is sitting there now has never been backed up, and it is queued
again. Size rather than a hash because this question is asked of every file in
the library on every pass, and hashing a terabyte to answer "has anything
changed?" would cost more than the upload it is protecting. The hash each row
carries is of the bytes that were actually sent — taken as they went past, so
it costs nothing — and is the record of *what* went up, not the test for
whether it should.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from ..server.config import (AMBIGUOUS_EXTS, AUDIO_EXTS, IMAGE_EXTS,
                            RAW_EXTS, VIDEO_EXTS)
from ..utils import source_version as source_version_mod
from ..utils.source_version import source_version
from typing import Any, Callable, Iterable, Iterator

__all__ = [
    "PENDING", "UPLOADING", "DONE", "FAILED", "SKIPPED", "STATES",
    "PICTURE", "VIDEO", "PRIORITY", "kind_of_name",
    "init_schema", "remember", "record_done", "record_failure",
    "record_skipped", "release", "forget", "state_of", "pending_batch",
    "summary", "recent", "reset_failures", "MAX_ATTEMPTS",
    "set_aside", "save_resume", "has_uploaded",
]

PENDING = "pending"
UPLOADING = "uploading"
DONE = "done"
FAILED = "failed"
SKIPPED = "skipped"
STATES = (PENDING, UPLOADING, DONE, FAILED, SKIPPED)

PICTURE = "picture"
VIDEO = "video"

#: The order the queue works through, photographs first.
#:
#: A family library is mostly photographs by count and mostly video by size:
#: here, 1.6 TB still owed across 125,732 files, of which the video is the
#: large majority of the bytes. Sending in the order things were queued means
#: one 4 GB holiday film can hold up a thousand photographs behind it, and the
#: photographs are both the irreplaceable half and the half that gets safe
#: quickest. So they go first, video goes last, and everything else — audio, a
#: document that came along with a folder — goes in between rather than being
#: stranded at the end.
#:
#: This is the order of *sending*, not of owing. Nothing is skipped and nothing
#: is dropped: a video simply waits behind the pictures.
PRIORITY: tuple[tuple[str, ...], ...] = (
    (PICTURE,),
    ("audio", "unknown", ""),
    (VIDEO,),
)


def kind_of_name(rel_path: str) -> str:
    """Classify a queued file by its name alone. The disk is not consulted.

    :func:`ninaivu.server.config.media_kind` opens the file for the two
    extensions that video and transport streams both claim. This question is
    asked of rows whose drive may not be plugged in at the time — and is asked
    of every row in the queue at once during the one-off backfill — so a name
    that cannot be settled without looking is left ``unknown`` and sent in the
    middle of the queue rather than guessed at either end.
    """
    ext = os.path.splitext(rel_path)[1].lower()
    if ext in IMAGE_EXTS or ext in RAW_EXTS:
        return PICTURE
    if ext in AMBIGUOUS_EXTS:
        return "unknown"
    if ext in VIDEO_EXTS:
        return VIDEO
    if ext in AUDIO_EXTS:
        return "audio"
    return "unknown"

#: After this many failures a file stops being retried automatically. It stays
#: in the list, with its error, for somebody to look at — a queue that retries
#: a permanently broken file for ever never gets to the ones behind it.
MAX_ATTEMPTS = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS cloud_uploads (
    id            INTEGER PRIMARY KEY,
    root          TEXT NOT NULL,
    rel_path      TEXT NOT NULL,
    filename      TEXT NOT NULL DEFAULT '',
    size          INTEGER NOT NULL DEFAULT 0,
    digest        TEXT NOT NULL DEFAULT '',
    state         TEXT NOT NULL DEFAULT 'pending',
    remote_id     TEXT NOT NULL DEFAULT '',
    remote_folder TEXT NOT NULL DEFAULT '',
    resume_url    TEXT NOT NULL DEFAULT '',
    attempts      INTEGER NOT NULL DEFAULT 0,
    error         TEXT NOT NULL DEFAULT '',
    queued_at     REAL NOT NULL DEFAULT 0,
    done_at       REAL NOT NULL DEFAULT 0,
    UNIQUE(root, rel_path)
);

CREATE INDEX IF NOT EXISTS idx_cloud_state ON cloud_uploads(state);
CREATE INDEX IF NOT EXISTS idx_cloud_done  ON cloud_uploads(done_at);
CREATE INDEX IF NOT EXISTS idx_cloud_state_queue ON cloud_uploads(state, queued_at, id);
"""


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # Added with encrypted backup. A row from before it went up as it was.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(cloud_uploads)")}
    if "encrypted" not in columns:
        conn.execute("ALTER TABLE cloud_uploads ADD COLUMN encrypted INTEGER NOT NULL DEFAULT 0")
    if "source_version" not in columns:
        conn.execute("ALTER TABLE cloud_uploads ADD COLUMN source_version TEXT NOT NULL DEFAULT ''")
    # What the index said the file's timestamp was when it was queued. The
    # queue compares this rather than reading the file: `source_version` is a
    # strong stamp — it hashes both ends of the file — which the upload reads
    # anyway with the file open, but which cost 113 ms per file to take here.
    if "source_mtime" not in columns:
        conn.execute("ALTER TABLE cloud_uploads ADD COLUMN source_mtime REAL NOT NULL DEFAULT 0")
    # Added so photographs can be sent before video (see PRIORITY). Stored
    # rather than worked out per batch: the queue is asked for the next ten
    # files thousands of times over a run, and classifying by an expression
    # over 125,732 rows each time would mean sorting the whole queue to send
    # ten files. With the column and the index below, each tier is a range
    # scan of the rows it wants and nothing else is read.
    if "kind" not in columns:
        conn.execute("ALTER TABLE cloud_uploads ADD COLUMN kind TEXT NOT NULL DEFAULT ''")
    # Drive's own MD5 of what it holds. A resumed upload cannot hash the whole
    # file as it goes (the front of it went in an earlier run), so without this
    # the only check a restore had on such a file was its size.
    if "md5" not in columns:
        conn.execute("ALTER TABLE cloud_uploads ADD COLUMN md5 TEXT NOT NULL DEFAULT ''")
    # What the resumable session behind ``resume_url`` was opened for: whether
    # it was to take ciphertext, and how many bytes. A session only accepts the
    # bytes it was started with, so a URL whose file has since become a
    # different length, or changed between encrypted and plain, is not resumed.
    # Zero size means "not known" — a URL saved before these were kept.
    if "resume_encrypted" not in columns:
        conn.execute("ALTER TABLE cloud_uploads ADD COLUMN resume_encrypted "
                     "INTEGER NOT NULL DEFAULT 0")
    if "resume_size" not in columns:
        conn.execute("ALTER TABLE cloud_uploads ADD COLUMN resume_size "
                     "INTEGER NOT NULL DEFAULT 0")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cloud_kind_queue "
                 "ON cloud_uploads(state, kind, queued_at, id)")
    _backfill_kinds(conn)
    conn.commit()


def _backfill_kinds(conn: sqlite3.Connection) -> None:
    """Give a kind to rows queued before there was a column for it.

    The index is asked first, where it is in this database to ask — it decided
    by opening the file where a name alone could not settle it, and that is a
    better answer than any path can give. Measured against the 125,609 rows
    waiting here, the two agree on 125,329 of them; all 280 they differ on are
    the extensions video and transport streams both claim, which the index
    calls video and a name can only call unknown.

    Whatever is left over is classified from its name, which is all there is
    for a row whose file the index never saw. Nothing here opens a file: a
    row's drive may be elsewhere, and 125,609 of them are asked at once.

    Rows already classified are not re-read, so this costs its second once and
    nothing on every start after.
    """
    if _the_index_is_here(conn):
        conn.execute(
            "UPDATE cloud_uploads SET kind = COALESCE((SELECT a.kind FROM assets a "
            "WHERE a.root = cloud_uploads.root AND a.rel_path = cloud_uploads.rel_path), '') "
            "WHERE kind = ''")
    rows = conn.execute(
        "SELECT id, rel_path FROM cloud_uploads WHERE kind=''").fetchall()
    if not rows:
        return
    conn.executemany("UPDATE cloud_uploads SET kind=? WHERE id=?",
                     [(kind_of_name(row[1]), row[0]) for row in rows])


def _the_index_is_here(conn: sqlite3.Connection) -> bool:
    """Whether `assets` is in the same database as the queue.

    It is, in Ninaivu — `cloud_uploads` is a table in ``index.db``. But this
    module is handed a connection, and a caller that hands it a queue on its
    own should get a working queue rather than a missing-table error.
    """
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='assets'"
    ).fetchone())


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def remember(conn: sqlite3.Connection, root: str, rel_path: str, *,
             size: int = 0, filename: str = "") -> str:
    """Queue a file, unless it has already gone. Returns the state it is in.

    The one place the "upload once" rule is enforced on the way *in*: a file
    that is already done stays done, and this does not touch it. Everything
    else — a fresh file, one that failed, one left mid-flight by a crash — is
    put back in the queue.

    The one exception is a *different file at the same path*. If the size has
    changed, the photograph that went up is not the photograph sitting there
    now, and the new one has never been backed up. Size rather than a hash
    because this runs over every file in the library on every pass, and hashing
    a terabyte to answer "has anything changed?" would cost more than the
    upload it is protecting.
    """
    row = conn.execute(
        "SELECT state, size, source_version FROM cloud_uploads WHERE root=? AND rel_path=?",
        (root, rel_path)).fetchone()

    try:
        version = source_version(Path(root) / rel_path)
    except OSError:
        version = ""
    changed = row is not None and bool(version and row["source_version"] and version != row["source_version"])
    if row is not None and row["state"] == DONE:
        same = not (size and row["size"] and int(size) != int(row["size"]))
        if same and not changed:
            return DONE

    now = time.time()
    if row is None:
        conn.execute(
            "INSERT INTO cloud_uploads(root, rel_path, filename, size, state, "
            "queued_at, kind) VALUES(?,?,?,?,?,?,?)",
            (root, rel_path, filename or os.path.basename(rel_path), size,
             PENDING, now, kind_of_name(rel_path)))
    else:
        # An upload interrupted by a crash comes back as pending rather than
        # staying "uploading" for ever; the resume URL is kept so it can carry
        # on from where it stopped instead of starting the file again.
        #
        # Attempts start again from zero. Everything that reaches here — a file
        # set aside, one left mid-flight, a changed file at a settled path — is
        # being offered afresh, and deserves a full set of tries rather than
        # inheriting a count from something else's bad night.
        conn.execute(
            "UPDATE cloud_uploads SET state=?, size=?, queued_at=?, error='', "
            "attempts=0, kind=? WHERE root=? AND rel_path=?",
            (PENDING, size, now, kind_of_name(rel_path), root, rel_path))
    conn.execute("UPDATE cloud_uploads SET source_version=?, "
                 "resume_url=CASE WHEN source_version=? THEN resume_url ELSE '' END "
                 "WHERE root=? AND rel_path=?", (version, version, root, rel_path))
    conn.commit()
    return PENDING


def record_done(conn: sqlite3.Connection, root: str, rel_path: str, *,
                remote_id: str, digest: str = "", folder: str = "",
                encrypted: bool = False, md5: str = "") -> None:
    """Mark a file as uploaded. This is the row that never gets cleared.

    ``digest`` is of the bytes Drive holds — for an encrypted upload, the
    ciphertext. ``md5`` is Drive's own checksum of the same bytes, when it
    was asked for (a resumed upload, which has no whole-file ``digest``).
    """
    conn.execute(
        "UPDATE cloud_uploads SET state=?, remote_id=?, digest=?, md5=?, "
        "remote_folder=?, resume_url='', resume_size=0, resume_encrypted=0, "
        "error='', done_at=?, encrypted=? "
        "WHERE root=? AND rel_path=?",
        (DONE, remote_id, digest, md5 or "", folder, time.time(), int(bool(encrypted)),
         root, rel_path))
    conn.commit()


def record_failure(conn: sqlite3.Connection, root: str, rel_path: str,
                   error: str, *, resume_url: str = "") -> int:
    """Count a failure and keep the reason. Returns the attempt count."""
    conn.execute(
        "UPDATE cloud_uploads SET attempts=attempts+1, error=?, state=?, "
        "resume_url=? WHERE root=? AND rel_path=?",
        (str(error)[:500], PENDING, resume_url, root, rel_path))
    conn.commit()
    row = conn.execute(
        "SELECT attempts FROM cloud_uploads WHERE root=? AND rel_path=?",
        (root, rel_path)).fetchone()
    attempts = int(row["attempts"]) if row else 0
    if attempts >= MAX_ATTEMPTS:
        conn.execute(
            "UPDATE cloud_uploads SET state=? WHERE root=? AND rel_path=?",
            (FAILED, root, rel_path))
        conn.commit()
    return attempts


def record_skipped(conn: sqlite3.Connection, root: str, rel_path: str,
                   reason: str) -> None:
    """Set a file aside without counting it as a failure.

    There is a real difference between "this did not work" and "this was not
    meant to go", and collapsing the two does damage in both directions. A
    hidden photograph is not a broken upload: retrying it five times and then
    listing it under failures would tell an administrator something is wrong
    when the app is doing exactly what it was told. Equally, a file that has
    been moved away is not a fault to be chased.

    A skipped row keeps its reason and stays out of the queue — but it is not
    *done*, so if the reason goes away (the photograph is un-hidden, the file
    comes back) the next scan queues it like any other.
    """
    conn.execute(
        "UPDATE cloud_uploads SET state=?, error=?, resume_url='' "
        "WHERE root=? AND rel_path=?",
        (SKIPPED, str(reason)[:500], root, rel_path))
    conn.commit()


def mark_uploading(conn: sqlite3.Connection, root: str, rel_path: str) -> None:
    conn.execute("UPDATE cloud_uploads SET state=? WHERE root=? AND rel_path=?",
                 (UPLOADING, root, rel_path))
    conn.commit()


def release(conn: sqlite3.Connection, root: str, rel_path: str) -> None:
    """Put a file back in the queue after a deliberate stop.

    Pausing, or the upload window closing part-way through a large video, is
    not a failure and must not be recorded as one. The attempt count is left
    alone — five pauses are not five bad nights, and letting them accumulate
    would turn an upload somebody kept stopping into a permanent failure with
    no cause anybody could find. The resume URL is kept, so carrying on later
    costs the bytes that have not gone yet rather than the whole file.
    """
    conn.execute("UPDATE cloud_uploads SET state=? WHERE root=? AND rel_path=?",
                 (PENDING, root, rel_path))
    conn.commit()


def set_aside(conn: sqlite3.Connection, root: str, rel_path: str,
              reason: str) -> None:
    """Put a file back in the queue after a failure that was not its own.

    Google limiting the whole account, or the permission running out, says
    nothing about this file: every file would have failed the same way. Such
    a failure used to be counted against whichever files happened to be in
    flight, so an afternoon of rate limits left a queue of files marked
    FAILED that had nothing wrong with them. Like :func:`release`, the attempt
    count and the resume URL are left alone; unlike it, the reason is kept so
    the console can say what the upload is waiting for.
    """
    conn.execute("UPDATE cloud_uploads SET state=?, error=? WHERE root=? AND rel_path=?",
                 (PENDING, str(reason)[:500], root, rel_path))
    conn.commit()


def save_resume(conn: sqlite3.Connection, root: str, rel_path: str,
                url: str, *, encrypted: bool = False, size: int = 0) -> None:
    """Keep the resumable-upload URL, so a dropped connection costs seconds.

    ``encrypted`` and ``size`` say what the session was opened to receive, so
    a later run can tell whether it can still take what would be sent now.
    """
    conn.execute(
        "UPDATE cloud_uploads SET resume_url=?, resume_encrypted=?, resume_size=? "
        "WHERE root=? AND rel_path=?",
        (url, int(bool(encrypted)), int(size or 0), root, rel_path))
    conn.commit()


def has_uploaded(conn: sqlite3.Connection) -> bool:
    """Whether this index records any file as having reached Drive.

    A machine whose record is empty has backed nothing up — or is a new
    computer that has not had its index restored yet. The copy of the index
    in Drive (:mod:`.index_copy`) uses this to refuse to replace a good copy
    with the near-empty index of such a machine.
    """
    try:
        return conn.execute("SELECT 1 FROM cloud_uploads WHERE state=? LIMIT 1",
                            (DONE,)).fetchone() is not None
    except sqlite3.OperationalError:
        return False                     # no table yet: nothing has gone up


def forget(conn: sqlite3.Connection, root: str, rel_path: str) -> bool:
    """Drop a file's record entirely, so it would be uploaded again.

    Deliberately not called by anything automatic. Sending a photograph to the
    cloud a second time because it vanished from Drive is a decision, and a
    decision somebody makes — the whole point of the record is that it does not
    happen by itself.
    """
    cursor = conn.execute(
        "DELETE FROM cloud_uploads WHERE root=? AND rel_path=?",
        (root, rel_path))
    conn.commit()
    return cursor.rowcount > 0


def reset_failures(conn: sqlite3.Connection) -> int:
    """Put everything that gave up back in the queue, attempts cleared."""
    cursor = conn.execute(
        "UPDATE cloud_uploads SET state=?, attempts=0, error='' WHERE state=?",
        (PENDING, FAILED))
    conn.commit()
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def state_of(conn: sqlite3.Connection, root: str, rel_path: str) -> str | None:
    row = conn.execute(
        "SELECT state FROM cloud_uploads WHERE root=? AND rel_path=?",
        (root, rel_path)).fetchone()
    return row["state"] if row else None


def pending_batch(conn: sqlite3.Connection, limit: int = 25,
                  exclude_roots: Iterable[str] = ()) -> list[dict[str, Any]]:
    """The next few files to send: photographs first, then the rest, then video.

    Within each of those, oldest queued first — so the order things were asked
    for is still honoured, just one kind at a time. See :data:`PRIORITY` for
    why, and note that nothing here decides what is *owed*: a video left at the
    back of the queue is as owed as it ever was.

    Anything already part-way to Drive comes ahead of all of it. A resumable
    upload holds bytes that have already gone; setting it aside to start a
    photograph would spend them again, and on a 4 GB film that is the whole
    saving the resume was for.

    *exclude_roots* steps over the library folders a run has found to be
    unreachable — an external drive that is not plugged in. Their files are
    still owed and stay pending; taking them out of the batch is what lets a
    second library carry on uploading instead of the run grinding through
    thousands of rows that cannot move, and what lets the run end without
    having touched them.
    """
    skipped = [str(root) for root in exclude_roots]
    where = "state IN (?, ?)"
    common: list[Any] = [PENDING, UPLOADING]
    if skipped:
        where += f" AND root NOT IN ({','.join('?' * len(skipped))})"
        common.extend(skipped)

    found: list[dict[str, Any]] = []
    seen: set[int] = set()

    def take(extra: str, extra_args: list[Any]) -> None:
        room = limit - len(found)
        if room <= 0:
            return
        rows = conn.execute(
            f"SELECT * FROM cloud_uploads WHERE {where}{extra} "
            "ORDER BY queued_at, id LIMIT ?",
            (*common, *extra_args, room)).fetchall()
        for row in rows:
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            found.append(dict(row))

    take(" AND state=? AND resume_url<>''", [UPLOADING])
    for kinds in PRIORITY:
        take(f" AND kind IN ({','.join('?' * len(kinds))})", list(kinds))
    return found


def summary(conn: sqlite3.Connection) -> dict[str, Any]:
    counts = {state: 0 for state in STATES}
    for row in conn.execute(
            "SELECT state, COUNT(*) n, COALESCE(SUM(size),0) bytes "
            "FROM cloud_uploads GROUP BY state"):
        counts[row["state"]] = int(row["n"])
    sent = conn.execute(
        "SELECT COALESCE(SUM(size),0) b FROM cloud_uploads WHERE state=?",
        (DONE,)).fetchone()["b"]
    waiting = conn.execute(
        "SELECT COALESCE(SUM(size),0) b FROM cloud_uploads WHERE state IN (?,?)",
        (PENDING, UPLOADING)).fetchone()["b"]
    total = sum(counts.values())
    # What is still owed, split the way the queue sends it, so the Cloud page
    # can say "the photographs go first" and be checkable on it.
    waiting_kinds = {row["kind"]: (int(row["n"]), int(row["bytes"])) for row in conn.execute(
        "SELECT kind, COUNT(*) n, COALESCE(SUM(size),0) bytes FROM cloud_uploads "
        "WHERE state IN (?,?) GROUP BY kind", (PENDING, UPLOADING))}
    pictures, picture_bytes = waiting_kinds.pop(PICTURE, (0, 0))
    videos, video_bytes = waiting_kinds.pop(VIDEO, (0, 0))
    encrypted = conn.execute(
        "SELECT COUNT(*) FROM cloud_uploads WHERE state=? AND encrypted=1", (DONE,)).fetchone()[0]
    return {
        **counts,
        "encrypted": int(encrypted),
        "unencrypted": counts[DONE] - int(encrypted),
        "total": total,
        "bytes_sent": int(sent or 0),
        "bytes_waiting": int(waiting or 0),
        "waiting_pictures": pictures,
        "waiting_videos": videos,
        "waiting_other": sum(n for n, _b in waiting_kinds.values()),
        "bytes_waiting_pictures": picture_bytes,
        "bytes_waiting_videos": video_bytes,
        "percent": round(counts[DONE] * 100 / total, 1) if total else 0.0,
    }


def recent(conn: sqlite3.Connection, limit: int = 50,
           state: str | None = None) -> list[dict[str, Any]]:
    if state and state in STATES:
        rows = conn.execute(
            "SELECT * FROM cloud_uploads WHERE state=? "
            "ORDER BY COALESCE(NULLIF(done_at,0), queued_at) DESC LIMIT ?",
            (state, limit))
    else:
        rows = conn.execute(
            "SELECT * FROM cloud_uploads "
            "ORDER BY COALESCE(NULLIF(done_at,0), queued_at) DESC LIMIT ?",
            (limit,))
    return [dict(r) for r in rows]


def iter_all(conn: sqlite3.Connection) -> Iterator[dict[str, Any]]:
    for row in conn.execute("SELECT * FROM cloud_uploads ORDER BY id"):
        yield dict(row)


#: Files written into the queue per transaction. Committed as it goes, so an
#: upload can begin on the first of them rather than after the last: a library
#: of 156,000 files queued in one transaction showed nothing at all until the
#: whole walk had finished.
QUEUE_BATCH = 2000


def queue_missing(conn: sqlite3.Connection, assets: Iterable[dict[str, Any]],
                  on_queued: Callable[[int], None] | None = None) -> int:
    """Queue new or changed files, preserving settled unchanged ones.

    Nothing here reads the files. Size and timestamp come from the index,
    which the scanner keeps current, and together they are what says a file has
    changed and must go up again — including one edited in place at the same
    size. The strong stamp that pins the exact bytes (`source_version`, which
    hashes both ends of the file) is read and recorded by the upload itself
    (see :meth:`SyncEngine._one`), where the file is open anyway. Taking it
    here cost 113 ms per file: five hours before a 156,000-file library sent
    anything.

    A caller that does not know the timestamp — anything but
    :meth:`CloudService.queue_library` — falls back to that strong stamp, and
    so keeps reading the file.

    *on_queued* hears the running total after each batch, for the console.
    """
    existing = {(r["root"], r["rel_path"]): dict(r)
                for r in conn.execute(
                    "SELECT root, rel_path, state, size, source_version, "
                    "source_mtime, resume_url FROM cloud_uploads")}
    inserts: list[tuple[Any, ...]] = []
    updates: list[tuple[Any, ...]] = []
    #: Files whose timestamp moved but whose contents did not: only the new
    #: timestamp is written down, and nothing is sent.
    restamped: list[tuple[Any, ...]] = []
    queued = 0
    now = time.time()

    def flush() -> None:
        nonlocal queued, inserts, updates, restamped
        if restamped:
            conn.executemany("UPDATE cloud_uploads SET source_mtime=? "
                             "WHERE root=? AND rel_path=?", restamped)
            conn.commit()
            restamped = []
        if not inserts and not updates:
            return
        conn.executemany(
            "INSERT INTO cloud_uploads(root,rel_path,filename,size,source_mtime,"
            "queued_at,source_version,kind) VALUES(?,?,?,?,?,?,?,?)", inserts)
        conn.executemany(
            "UPDATE cloud_uploads SET state='pending',size=?,source_mtime=?,queued_at=?,"
            "resume_url=?,error='',attempts=0,kind=? WHERE root=? AND rel_path=?", updates)
        conn.commit()
        queued += len(inserts) + len(updates)
        inserts = []
        updates = []
        if on_queued:
            on_queued(queued)

    for asset in assets:
        root, rel = asset["root"], asset["rel_path"]
        size = int(asset.get("size") or 0)
        mtime = float(asset.get("mtime") or 0)
        # What the index called it, when the caller passes it on. The index
        # decided by opening the file where a name alone could not settle it,
        # which is a better answer than this can reach from the path — and the
        # queue is the one place that answer is wanted in bulk.
        kind = str(asset.get("kind") or "") or kind_of_name(rel)
        row = existing.get((root, rel))
        if row is not None:
            changed = bool(size and row["size"] and size != row["size"])
            if mtime and row["source_mtime"]:
                moved = abs(mtime - float(row["source_mtime"])) > 1e-6
                if moved and not changed and row["source_version"]:
                    # The timestamp moved and the size did not. An edit does
                    # that — but so does copying the library to another disk
                    # or machine without keeping timestamps, and reading that
                    # as an edit of every file sent the whole library to Drive
                    # a second time: days of uploading, and a duplicate of
                    # everything. So the contents decide: the size and both
                    # ends of the file, against what they were when it went.
                    same = source_version_mod.same_content(
                        row["source_version"], Path(root) / rel)
                    if same is None:
                        continue            # cannot tell now; asked again next pass
                    if same:
                        restamped.append((mtime, root, rel))
                        existing[(root, rel)] = {**row, "source_mtime": mtime}
                        if len(restamped) >= QUEUE_BATCH:
                            flush()
                        continue
                changed = changed or moved
            elif not mtime:
                # No timestamp from the caller: read the file, as this did
                # before, so a same-size rewrite is still noticed.
                try:
                    version = source_version(Path(root) / rel)
                except OSError:
                    version = ""
                changed = changed or bool(
                    version and row["source_version"] and version != row["source_version"])
            # An unchanged upload in flight is left alone as well: this runs on a
            # request thread while the engine works, and the resume URL it saved
            # a moment ago is not in the snapshot taken above — writing the
            # snapshot's empty one back made the next run send the whole file
            # again. ``pending_batch`` already offers UPLOADING rows.
            if row["state"] in (DONE, PENDING, FAILED, UPLOADING) and not changed:
                continue
            # A part-sent file that has not changed can carry on where it
            # stopped; one that changed starts again.
            resume = "" if changed else row["resume_url"]
            updates.append((size, mtime or row["source_mtime"], now, resume,
                            kind, root, rel))
        else:
            inserts.append((root, rel, asset.get("filename") or os.path.basename(rel),
                            size, mtime, now, "", kind))
        existing[(root, rel)] = {"state": PENDING, "size": size, "source_mtime": mtime,
                                 "source_version": row["source_version"] if row else "",
                                 "resume_url": ""}
        if len(inserts) + len(updates) >= QUEUE_BATCH:
            flush()
    flush()
    return queued
