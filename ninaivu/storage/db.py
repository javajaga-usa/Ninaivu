"""SQLite index for Media Vault.

Replaces the old ``metadata.json`` blob. Everything the UI needs to draw a
page comes from one indexed query, and full-text search runs through FTS5
when the local SQLite build has it (with a LIKE fallback when it does not).

Some sections live in files of their own beside it: albums and occasions
(``db_albums``), on this day and the map (``db_memories``), share links
(``db_shares``), the integrity check's records (``db_bitrot``), faces and
people (``db_faces``), and search vectors (``embeddings``). Every name in them
is re-exported here, so ``db.<name>`` stays the one place to read them, and a
replacement made on ``db`` (a test's monkeypatch) is seen by them too where
they call back into ``db``. Import them through ``db``, never on their own.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import roots as roots_kit

log = logging.getLogger(__name__)

#: The index's shape, as every version reads it before opening it
#: (storage/upgrade.py). 7 is 4.0: a 3.x Ninaivu finds a number it does not
#: know and does not open the index, and says how to restore the copy taken
#: before the update. Going back from 4.0 is that restore (Go back in the
#: Control Panel does it), not 3.x running over an index 4.0 has stepped on;
#: the shape changes themselves are the numbered steps in migrations.py.
SCHEMA_VERSION = 7

_local = threading.local()


class _WriteLock:
    """The index's one write lock, which the thread holding it may take again.

    Every write in the package takes it around its statements and its commit.
    The job engine's writer (:func:`write_group`) holds it across a group of
    such writes and makes their commits one, so the functions it calls take
    it a second time on the same thread; a plain lock would wait on itself.
    For every other caller it is the plain lock it always was.
    """

    __slots__ = ("_lock", "_owner", "_depth")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._owner: int | None = None
        self._depth = 0

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        me = threading.get_ident()
        if self._owner == me:
            self._depth += 1
            return True
        if not self._lock.acquire(blocking, timeout):
            return False
        self._owner, self._depth = me, 1
        return True

    def release(self) -> None:
        if self._owner != threading.get_ident():
            raise RuntimeError("the index write lock is not held by this thread")
        self._depth -= 1
        if not self._depth:
            self._owner = None
            self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()


_write_lock = _WriteLock()


# ---------------------------------------------------------------------------
# Connection handling
# ---------------------------------------------------------------------------

def _distance_km(lat1: float | None, lon1: float | None,
                 lat2: float | None, lon2: float | None) -> float | None:
    """Kilometres between two coordinates, or ``None`` if either is missing.

    ``None`` rather than a large number: a photograph with no coordinates is
    unknown, not far away, and a comparison against NULL correctly excludes
    it instead of quietly ranking it last.
    """
    if None in (lat1, lon1, lat2, lon2):
        return None
    from ..media.occasions import haversine_km

    return haversine_km(float(lat1), float(lon1), float(lat2), float(lon2))


#: Megabytes of page cache each connection keeps (server/tuning.py sets it).
CACHE_MB = 16


def set_cache_mb(megabytes: int) -> None:
    """The page cache for connections opened from now on."""
    global CACHE_MB
    CACHE_MB = max(2, int(megabytes))


#: How hard each commit is pushed to the disk. ``NORMAL`` with WAL keeps the
#: database consistent through a crash or a power cut, but the last commits
#: before a power cut (not a crash of Ninaivu alone) can be lost: they were in
#: the write-ahead log, not yet synced. That is the trade made for speed: a
#: scan commits thousands of times, and ``FULL`` syncs every one. What could be
#: lost is the last moments of indexing, which the next scan redoes, or an
#: edit made in the second before the power went. A household that would
#: rather pay the speed for those seconds sets ``NINAIVU_DB_SYNCHRONOUS=full``
#: (external audit 2026-10-11, DATA-01; https://www.sqlite.org/pragma.html#pragma_synchronous).
#: WAL also needs the state folder on a local disk: SQLite's WAL is not for a
#: network share (https://www.sqlite.org/wal.html).
SYNCHRONOUS_CHOICES = ("NORMAL", "FULL")


def synchronous() -> str:
    """The ``PRAGMA synchronous`` new connections use: NORMAL unless
    ``NINAIVU_DB_SYNCHRONOUS`` says FULL. Anything else is NORMAL, logged."""
    told = os.environ.get("NINAIVU_DB_SYNCHRONOUS", "").strip().upper()
    if not told:
        return "NORMAL"
    if told not in SYNCHRONOUS_CHOICES:
        log.warning("NINAIVU_DB_SYNCHRONOUS=%s is not normal or full; using normal", told)
        return "NORMAL"
    return told


#: What the write-ahead log is cut back to once it starts over (see connect).
WAL_SIZE_LIMIT = 64 * 1024 * 1024


#: Index files no thread should keep reading: the Pi's replica serves each
#: hand-over from a file of its own (ninaivu/replica/receive.py), and a web
#: thread holding the one before lets go of it at its next connect. A handful
#: at most; the oldest are forgotten.
_retired: list[str] = []


def retire(db_path: Path | str) -> None:
    """Have every thread close its connection to *db_path* at its next connect."""
    key = str(db_path)
    if key not in _retired:
        _retired.append(key)
        del _retired[:-8]


def release_within(folder: Path | str, *also: Path | str) -> None:
    """Let go of the databases in *folder* before it is removed: this thread's
    connections to them are closed now, and *also* (the ones other threads
    may hold) are retired, so each thread closes its own at its next connect.
    Windows does not remove a file a connection still holds."""
    for path in also:
        retire(path)
    root = os.path.normcase(os.path.abspath(str(folder)))
    for key in list(getattr(_local, "conns", None) or {}):
        if os.path.normcase(os.path.abspath(key)).startswith(root + os.sep):
            retire(key)
            close(key)


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Return this thread's connection to ``db_path``, creating it if needed."""
    key = str(db_path)
    cache: dict[str, sqlite3.Connection] = getattr(_local, "conns", None) or {}
    if _retired and cache:
        for old in [k for k in cache if k in _retired and k != key]:
            try:
                cache.pop(old).close()
            except sqlite3.Error:
                pass
    conn = cache.get(key)
    if conn is not None:
        try:
            if conn.total_changes >= 0:     # raises once somebody has closed it
                return conn
        except sqlite3.ProgrammingError:
            # Closed by a caller that took it for its own. Handed back as it
            # was, every later step on this thread failed with "Cannot operate
            # on a closed database" — on the start-up thread, all of start-up.
            cache.pop(key, None)

    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(key, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # The write-ahead log is reused rather than shrunk, so without a limit it
    # stays the size of its largest burst for good: 1.1 GB after the tagger
    # wrote 40,000 rows while three readers overlapped (208,000 items), on
    # disk and in every crash recovery from then on. Cut back to this the
    # next time it starts over.
    conn.execute(f"PRAGMA journal_size_limit={int(WAL_SIZE_LIMIT)}")
    conn.execute(f"PRAGMA synchronous={synchronous()}")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA temp_store=MEMORY")
    # What is deleted from the index is overwritten where it sat, not left in
    # free pages: an erased photograph's caption, the text read from it and
    # its place otherwise lived on in the file, and in every backup of it.
    # FAST costs nothing beyond the pages a delete already writes.
    conn.execute("PRAGMA secure_delete=FAST")
    # 16 MB of page cache per connection, eight times SQLite's default. The
    # assets table carries captions and OCR text, so a library of a few
    # hundred thousand rows is far larger than 2 MB, and every aggregate that
    # reads it (the facets, the storage report, the folder view) paged it back
    # in from the operating system on each call.
    # Sized to the machine since the Tuning page: 8 MB on a Pi, more on a
    # computer with memory to spare (server/tuning.py).
    conn.execute(f"PRAGMA cache_size=-{int(CACHE_MB) * 1024}")
    # Great-circle distance, so "near this photograph" can be one SQL
    # predicate instead of a Python pass over the whole library. SQLite's own
    # trig functions are a compile-time option and cannot be relied on; a
    # registered callback can. It is only ever reached for rows a bounding box
    # already let through.
    conn.create_function("ninaivu_km", 4, _distance_km, deterministic=True)
    cache[key] = conn
    _local.conns = cache
    return conn


def end_open_transactions(failed: bool = False) -> int:
    """Close any transaction this thread's connections left open. Returns how
    many there were.

    A connection lives as long as its thread, so one left inside a
    transaction keeps the write lock (or an old snapshot) into whatever the
    thread does next. A piece of work that ended cleanly has what it wrote
    committed, as the next commit on the thread would have done; one that
    failed, or whose commit cannot go through, is rolled back.
    """
    ended = 0
    for conn in list(getattr(_local, "conns", {}).values()):
        try:
            if not conn.in_transaction:
                continue
            ended += 1
            if failed:
                conn.rollback()
                continue
            try:
                conn.commit()
            except sqlite3.Error:
                conn.rollback()
        except sqlite3.Error:
            pass
    return ended


def close(db_path: Path | str) -> None:
    """Close this thread's connection to *db_path*, if it has one."""
    conn = (getattr(_local, "conns", None) or {}).pop(str(db_path), None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass


class _GroupConnection:
    """A connection whose commits wait for the end of the group.

    Handed out by :func:`write_group`. Everything is the connection's own
    except ``commit``, which does nothing until the group ends, and
    ``rollback``, which undoes only the write it is called in (see
    :meth:`GroupWrites.item`): one photograph's failure must not take the
    rest of the group with it.
    """

    __slots__ = ("_conn",)

    def __init__(self, conn: sqlite3.Connection) -> None:
        object.__setattr__(self, "_conn", conn)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._conn, name, value)

    def commit(self) -> None:
        """Left for the end of the group."""

    def rollback(self) -> None:
        raise _ItemRolledBack()

    def __enter__(self) -> "_GroupConnection":
        return self

    def __exit__(self, *exc: Any) -> None:
        """Neither commits nor rolls back: the group (or the item) does."""


class _ItemRolledBack(Exception):
    """A write in a group asked to be undone."""


class GroupWrites:
    """The open group of :func:`write_group`: one transaction, one commit."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = _GroupConnection(conn)
        self._real = conn
        self.written = 0
        self.failed = 0

    @contextlib.contextmanager
    def item(self):
        """One write of the group, undone alone if it fails.

        Yields the connection to write with. An exception inside is logged
        and swallowed after its own writes are rolled back; the group goes on.
        """
        self._real.execute("SAVEPOINT ninaivu_item")
        try:
            yield self.conn
        except Exception as exc:  # noqa: BLE001 - one item of many
            self._real.execute("ROLLBACK TO ninaivu_item")
            self._real.execute("RELEASE ninaivu_item")
            self.failed += 1
            if not isinstance(exc, _ItemRolledBack):
                log.warning("a write in a group was undone: %s", exc)
            return
        self._real.execute("RELEASE ninaivu_item")
        self.written += 1


@contextlib.contextmanager
def write_group(conn: sqlite3.Connection):
    """Many writes to the index under one hold of the write lock and one commit.

    Each photograph's text or faces was its own transaction: the write lock
    taken, a commit (a sync of the write-ahead log) and the lock let go, a
    few hundred times a minute on a large machine, every family member's
    star or caption waiting its turn between them. The job engine's writer
    gathers what its workers found and writes it here in groups instead:
    the lock is held for the few milliseconds the group's statements take,
    and the disk syncs once. Use :meth:`GroupWrites.item` around each write.
    """
    with _write_lock:
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        group = GroupWrites(conn)
        try:
            yield group
        except BaseException:
            conn.rollback()
            raise
        conn.commit()


def close_all() -> None:
    for conn in getattr(_local, "conns", {}).values():
        try:
            conn.close()
        except sqlite3.Error:
            pass
    _local.conns = {}


def has_fts5(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts_probe USING fts5(x)")
        conn.execute("DROP TABLE IF EXISTS _fts_probe")
        return True
    except sqlite3.OperationalError:
        return False


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS pending_uploads (
    id INTEGER PRIMARY KEY,
    storage_key TEXT NOT NULL UNIQUE,
    filename TEXT NOT NULL,
    root TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT '',
    uploaded_by INTEGER,
    uploaded_at REAL NOT NULL,
    record TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    reviewed_by INTEGER,
    reviewed_at REAL,
    asset_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_pending_uploads_status ON pending_uploads(status, id);

CREATE TABLE IF NOT EXISTS assets (
    id              INTEGER PRIMARY KEY,
    root            TEXT NOT NULL,
    rel_path        TEXT NOT NULL,
    filename        TEXT NOT NULL,
    folder          TEXT NOT NULL DEFAULT '',
    ext             TEXT NOT NULL DEFAULT '',
    kind            TEXT NOT NULL,
    size            INTEGER NOT NULL DEFAULT 0,
    mtime           REAL    NOT NULL DEFAULT 0,
    captured_at     REAL,
    date_key        TEXT NOT NULL DEFAULT '',
    date_source     TEXT NOT NULL DEFAULT 'mtime',
    width           INTEGER,
    height          INTEGER,
    duration        REAL,
    orientation     INTEGER DEFAULT 1,
    camera          TEXT,
    lens            TEXT,
    iso             INTEGER,
    f_number        REAL,
    exposure        TEXT,
    focal_length    REAL,
    gps_lat         REAL,
    gps_lon         REAL,
    country         TEXT,
    city            TEXT,
    thumb           TEXT,
    blurhash        TEXT,
    color           TEXT,
    phash           TEXT,
    dup_group       TEXT,
    -- Focus and exposure, measured at a fixed size so one threshold holds
    -- across cameras. NULL means never measured (video, audio, or a scan
    -- with quality_scan off) -- distinct from a genuinely dark photograph.
    sharpness       REAL,
    brightness      REAL,
    quality         TEXT NOT NULL DEFAULT '[]',
    quality_version INTEGER NOT NULL DEFAULT 0,
    -- Which pass of the video keyframe tagger judged this clip. Videos are
    -- tagged from several moments rather than one poster frame, and that
    -- sampling evolves independently of the image tagger's AI_VERSION.
    keyframe_version INTEGER NOT NULL DEFAULT 0,
    -- Words read out of the picture itself: a shop sign, a menu, the back of
    -- a postcard. Indexed for search, never shown as a caption -- it is what
    -- the photograph contains, not what it is about.
    ocr_text        TEXT,
    ocr_version     INTEGER NOT NULL DEFAULT 0,
    -- Which pass of the place-name lookup filled `city` and `country`.
    place_version   INTEGER NOT NULL DEFAULT 0,
    -- The auto-album this belongs to, rebuilt after every scan. Not written
    -- by the asset upsert: it is derived from the whole library at once, not
    -- from one file, and a rescan must not blank it on the way past.
    occasion_id     INTEGER,
    tags            TEXT NOT NULL DEFAULT '[]',
    caption         TEXT,
    nsfw            INTEGER NOT NULL DEFAULT 0,
    nsfw_score      REAL NOT NULL DEFAULT 0,
    -- Who decided the three above: 'auto' (the scan or the AI pass) or
    -- 'manual' (an admin, by hand). A manual value outlives every rescan and
    -- re-tag, the way a manual rotation or date does.
    tags_source     TEXT NOT NULL DEFAULT 'auto',
    caption_source  TEXT NOT NULL DEFAULT 'auto',
    nsfw_source     TEXT NOT NULL DEFAULT 'auto',
    -- How much the picture looks like a screenshot, a document or a screen,
    -- 0..1, and which version of that judgement made it (media/screens.py).
    screen_score    REAL,
    screen_version  INTEGER NOT NULL DEFAULT 0,
    -- How much the picture looks like people in nothing but their underwear,
    -- 0..1, and which version of that judgement made it (media/revealing.py).
    revealing       REAL NOT NULL DEFAULT 0,
    revealing_version INTEGER NOT NULL DEFAULT 0,
    -- 0 public (guests too) · 1 family · 2 hidden (admins only)
    visibility      INTEGER NOT NULL DEFAULT 1,
    vis_source      TEXT NOT NULL DEFAULT 'default',
    -- "Only to me": the profile that took this item out of everybody else's
    -- view, and what its visibility was before, to put back when they bring
    -- it back. While set, the item's visibility is held at hidden (2), so
    -- every listing that already keeps hidden items from family members and
    -- guests keeps this one from them too; only its owner reaches it, from
    -- their own "Only to me" view (see :func:`private_owner_clause`).
    private_to      INTEGER,
    private_was     INTEGER,
    private_was_src TEXT,
    -- Clockwise turn applied to get this the right way up, and who decided:
    -- exif · faces · ai · manual · none. Thumbnails are baked with it already
    -- applied; this is what the browser needs to show the original to match.
    rotation        INTEGER NOT NULL DEFAULT 0,
    rot_source      TEXT NOT NULL DEFAULT 'none',
    live_video_path TEXT,
    is_live         INTEGER NOT NULL DEFAULT 0,
    -- Set on the *clip* half of a live photo, so the pair counts as the one
    -- thing the household took rather than as a photograph and a video.
    live_clip       INTEGER NOT NULL DEFAULT 0,
    -- A RAW taken together with a JPEG or HEIC of the same frame: the picture
    -- carries the RAW's path, the RAW is marked as the twin. One shot, shown
    -- once, with the RAW kept beside it to download.
    raw_path        TEXT,
    raw_twin        INTEGER NOT NULL DEFAULT 0,
    -- Photo stacks (``photo_stacks``, features/stacks/storage.py): the frames of a
    -- burst and a photograph's edited copies share a key; the one shown
    -- carries how many there are, the rest are marked as stacked under it.
    -- Written only by features/stacks/storage.py, never by a scan.
    stack_key       TEXT,
    stacked         INTEGER NOT NULL DEFAULT 0,
    stack_count     INTEGER NOT NULL DEFAULT 0,
    trashed         INTEGER NOT NULL DEFAULT 0,
    ai_version      INTEGER NOT NULL DEFAULT 0,
    face_version    INTEGER NOT NULL DEFAULT 0,
    indexed_at      REAL NOT NULL DEFAULT 0,
    -- Set the moment an admin says "keep" on the largest-files screen. NULL
    -- forever means nobody has looked at this file there yet -- it is not a
    -- judgement about the file, only about whether it still needs asking
    -- about, so nothing else in Ninaivu ever reads or changes it.
    large_file_reviewed_at REAL,
    -- When this file was first indexed. Unlike indexed_at (the thumbnail's
    -- version, moved by an edit, a turn or a redrawn tile) it never changes
    -- on a rescan: "what is new" without the change journal reads it.
    added_at        REAL,
    UNIQUE(root, rel_path)
);

CREATE TABLE IF NOT EXISTS embeddings (
    asset_id INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
    model    TEXT NOT NULL,
    dim      INTEGER NOT NULL,
    vector   BLOB NOT NULL
);

-- Auto-albums. Deliberately not rows in `albums`: those are made by a person
-- and carry their name and ownership, while these are derived and thrown away
-- wholesale on every rescan. Sharing one table would mean a scan could rename
-- or delete something somebody built by hand.
CREATE TABLE IF NOT EXISTS occasions (
    id         INTEGER PRIMARY KEY,
    root       TEXT NOT NULL,
    key        TEXT NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    place      TEXT,
    started_at REAL NOT NULL DEFAULT 0,
    ended_at   REAL NOT NULL DEFAULT 0,
    days       INTEGER NOT NULL DEFAULT 1,
    count      INTEGER NOT NULL DEFAULT 0,
    UNIQUE(root, key)
);

CREATE TABLE IF NOT EXISTS albums (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    cover_id   INTEGER REFERENCES assets(id) ON DELETE SET NULL,
    -- Who made it. NULL means "made before Ninaivu recorded this", which is
    -- treated as everyone's, so upgrading does not orphan existing albums.
    created_by INTEGER
);

-- Two computers: originals this computer let go of, kept whole on the
-- Secondary and off site (ninaivu/mugil/free.py). The row in assets stays;
-- the file is sent for when it is opened. Nothing here counts as missing.
CREATE TABLE IF NOT EXISTS mugil_let_go (
    root      TEXT NOT NULL,
    rel_path  TEXT NOT NULL,
    size      INTEGER NOT NULL,
    mtime     REAL NOT NULL,
    algo      TEXT NOT NULL,
    digest    TEXT NOT NULL,
    let_go_at REAL NOT NULL,
    PRIMARY KEY (root, rel_path)
);

CREATE TABLE IF NOT EXISTS album_items (
    album_id INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    added_at REAL NOT NULL,
    PRIMARY KEY (album_id, asset_id)
);

-- Every bulk visibility change, and what it overwrote.
--
-- Setting a folder rule rewrites the visibility of everything beneath it, and
-- the per-item decisions it lands on are gone. That is fine while the change
-- was the one intended, and a privacy incident when it was not: choose the
-- wrong button on the whole library and photographs an admin had deliberately
-- hidden become visible to the family, with nothing left that remembers they
-- were hidden. So the previous state is written down before it is replaced.
CREATE TABLE IF NOT EXISTS visibility_batches (
    id          INTEGER PRIMARY KEY,
    created_at  REAL NOT NULL,
    created_by  INTEGER,
    scope       TEXT NOT NULL,          -- 'folder' | 'items'
    root        TEXT,
    folder      TEXT,
    visibility  INTEGER NOT NULL,
    affected    INTEGER NOT NULL DEFAULT 0,
    exposed     INTEGER NOT NULL DEFAULT 0,   -- became visible to more people
    had_rule    INTEGER NOT NULL DEFAULT 0,   -- was there a folder rule before?
    prior_rule  INTEGER,                      -- …and what was it
    undone_at   REAL
);

CREATE TABLE IF NOT EXISTS visibility_undo (
    batch_id   INTEGER NOT NULL REFERENCES visibility_batches(id) ON DELETE CASCADE,
    asset_id   INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    visibility INTEGER NOT NULL,
    vis_source TEXT NOT NULL,
    PRIMARY KEY (batch_id, asset_id)
);

-- What has been deleted, and where it went.
--
-- The files themselves live in a plain `_deleted` folder in the library, so
-- they can be recovered with a file manager and no database at all. This table
-- only remembers where each one *came from*, which is what lets the console
-- put it back exactly rather than approximately.
CREATE TABLE IF NOT EXISTS recycled (
    id          INTEGER PRIMARY KEY,
    asset_id    INTEGER,
    root        TEXT NOT NULL,
    rel_path    TEXT NOT NULL,
    filename    TEXT NOT NULL,
    size        INTEGER NOT NULL DEFAULT 0,
    thumb       TEXT,
    bin_path    TEXT NOT NULL,
    deleted_at  REAL NOT NULL,
    deleted_by  INTEGER,
    restored_at REAL
);

-- What was erased from the bin for good: where it was, and how big. Only so
-- that putting the library back from the second copy or Google Drive does not
-- bring back a photograph somebody went to the trouble of erasing.
CREATE TABLE IF NOT EXISTS erased (
    root      TEXT NOT NULL,
    rel_path  TEXT NOT NULL,
    size      INTEGER NOT NULL DEFAULT 0,
    erased_at REAL NOT NULL,
    PRIMARY KEY (root, rel_path, size)
);

CREATE TABLE IF NOT EXISTS scan_runs (
    id         INTEGER PRIMARY KEY,
    root       TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at   REAL,
    total      INTEGER NOT NULL DEFAULT 0,
    processed  INTEGER NOT NULL DEFAULT 0,
    added      INTEGER NOT NULL DEFAULT 0,
    updated    INTEGER NOT NULL DEFAULT 0,
    removed    INTEGER NOT NULL DEFAULT 0,
    errors     INTEGER NOT NULL DEFAULT 0,
    status     TEXT NOT NULL DEFAULT 'running'
);

-- Tokenized, secure guest sharing links
CREATE TABLE IF NOT EXISTS shares (
    id          INTEGER PRIMARY KEY,
    token       TEXT NOT NULL UNIQUE,
    scope       TEXT NOT NULL DEFAULT 'album', -- 'album' | 'asset'
    target_id   INTEGER NOT NULL,
    created_at  REAL NOT NULL,
    created_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    expires_at  REAL,
    password    TEXT,
    view_count  INTEGER NOT NULL DEFAULT 0
);

-- Bitrot and data integrity scrubber logs
CREATE TABLE IF NOT EXISTS bitrot_records (
    id            INTEGER PRIMARY KEY,
    asset_id      INTEGER REFERENCES assets(id) ON DELETE CASCADE,
    root          TEXT NOT NULL,
    rel_path      TEXT NOT NULL,
    expected_hash TEXT,
    actual_hash   TEXT,
    -- baseline   first sighting; nothing to compare against yet
    -- verified   the bytes are the same as last time
    -- changed    different bytes, but the file was edited (mtime/size moved)
    -- corrupt    different bytes with the file otherwise untouched
    -- missing    not on disk
    -- unreadable on disk but could not be opened
    status        TEXT NOT NULL,
    -- What the file looked like when this fingerprint was taken. Without
    -- these, a changed hash is ambiguous: an edit and bit rot produce exactly
    -- the same evidence, and reporting an edit as corruption teaches people
    -- to ignore the warning.
    file_mtime    REAL,
    file_size     INTEGER,
    checked_at    REAL NOT NULL
);

-- Named people clusters
CREATE TABLE IF NOT EXISTS people_clusters (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    avatar_asset_id INTEGER REFERENCES assets(id) ON DELETE SET NULL,
    created_at      REAL NOT NULL,
    -- The mean of every *confirmed* face, re-normalised. Suggestions are
    -- ranked against this, so it is rebuilt on every confirm and reject
    -- rather than cached and left to drift.
    centroid        BLOB,
    face_count      INTEGER NOT NULL DEFAULT 0,
    confirmed_count INTEGER NOT NULL DEFAULT 0,
    cover_face_id   INTEGER,
    updated_at      REAL NOT NULL DEFAULT 0
);

-- One detected face. Rows are owned by their asset and go with it.
--
-- There is no path from here to a filesystem location: a face is reachable
-- only through its asset, so every visibility and scope rule that governs the
-- photograph governs the face automatically. That is deliberate. A people
-- browser that resolved faces independently would be a way to learn who
-- appears in a hidden photograph without being allowed to open it.
CREATE TABLE IF NOT EXISTS faces (
    id          INTEGER PRIMARY KEY,
    asset_id    INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    person_id   INTEGER REFERENCES people_clusters(id) ON DELETE SET NULL,
    -- How this face came to be attached to that person:
    --   none      nobody yet
    --   auto      the matcher was confident enough to decide alone
    --   confirmed a person said yes
    -- Only 'confirmed' rows build a centroid, so one bad automatic guess
    -- cannot teach itself into the model of who somebody is.
    source      TEXT NOT NULL DEFAULT 'none',
    confidence  REAL NOT NULL DEFAULT 0,
    cluster_key TEXT,
    bbox        TEXT NOT NULL,
    landmarks   TEXT,
    det_score   REAL NOT NULL DEFAULT 0,
    sharpness   REAL NOT NULL DEFAULT 0,
    quality     REAL NOT NULL DEFAULT 0,
    embedding   BLOB NOT NULL,
    thumb       TEXT,
    model       TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL DEFAULT 0
);

-- "This face is not that person." Permanent, and consulted before every
-- suggestion. Without it the review queue asks the same rejected question
-- after every rescan, which is how a review queue stops being used.
CREATE TABLE IF NOT EXISTS face_rejections (
    face_id    INTEGER NOT NULL REFERENCES faces(id) ON DELETE CASCADE,
    person_id  INTEGER NOT NULL REFERENCES people_clusters(id) ON DELETE CASCADE,
    created_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (face_id, person_id)
);

-- "Not bothered": an unnamed face the household does not want to name, a
-- stranger in the background or someone in a crowd. Kept out of the groups
-- waiting for a name, the review queues and the guesses, and carried across
-- a new search for faces like a rejection. Removing the row shows it again.
CREATE TABLE IF NOT EXISTS faces_ignored (
    face_id    INTEGER PRIMARY KEY REFERENCES faces(id) ON DELETE CASCADE,
    created_at REAL NOT NULL DEFAULT 0
);

-- The largest photograph number ever given out (NEW_ASSET_ID), so a number
-- is never given to a second file.
CREATE TRIGGER IF NOT EXISTS assets_high_water AFTER DELETE ON assets
WHEN OLD.id >= COALESCE((SELECT CAST(value AS INTEGER) FROM meta
                         WHERE key='assets_high_water'), 0)
BEGIN
    INSERT OR REPLACE INTO meta(key, value) VALUES ('assets_high_water', OLD.id);
END;

-- A confirmed name that a new search for faces could not put on any face it
-- found (replace_asset_faces): the box no longer overlapped any new one well
-- enough to be sure. Kept, not dropped, so the next search can carry it, and
-- so the People page can ask about it. Its box is where the face was.
CREATE TABLE IF NOT EXISTS faces_not_carried (
    id         INTEGER PRIMARY KEY,
    asset_id   INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    person_id  INTEGER NOT NULL REFERENCES people_clusters(id) ON DELETE CASCADE,
    bbox       TEXT NOT NULL,
    model      TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0
);

-- What each file is, by its bytes (storage/fingerprints.py; kept only with
-- the content_fingerprints setting on). The size and modified time are the
-- file's when the fingerprint was taken: one that no longer matches its row
-- says nothing about the file now, and is taken again.
CREATE TABLE IF NOT EXISTS content_fingerprints (
    asset_id INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
    algo     TEXT NOT NULL,
    digest   TEXT NOT NULL,
    size     INTEGER NOT NULL,
    mtime    REAL NOT NULL,
    taken_at REAL NOT NULL DEFAULT 0
);
"""

#: Replaced by the two gallery indexes below, and dropped on upgrade so the
#: planner cannot keep preferring it: it covers neither ``nsfw`` nor ``kind``,
#: so counting a family member's view through it read every row of the table.
#: Indexes earlier releases made that are dropped on start. ``idx_shares_token``
#: duplicated the index ``UNIQUE`` already keeps on the same column.
_RETIRED_INDEXES = ("idx_assets_sort", "idx_shares_token")

_INDEX_SCHEMA = """
-- The gallery. One index answers the count (every filter column is in it, so
-- no row is read); the other hands pages over already in date order, so a page
-- of a million-photo library is a short index walk rather than a sort of the
-- whole table. The ORDER BY expression must match `_SORTS` exactly. `date_key`
-- rides along for the family date policy (server/date_policy.py), which is in
-- every viewer's visibility clause: without it each row the walk passes over
-- is read from the table, and a deep page takes seconds. `live_clip` and
-- `raw_twin` ride along for the same reason: the second half of a live photo
-- or of a RAW+JPEG shot is left out of every listing. `stacked` too: the
-- frames under a photo stack's top one are left out of the timeline while
-- `photo_stacks` is on (features/stacks/storage.py).
CREATE INDEX IF NOT EXISTS idx_assets_filter   ON assets(root, trashed, nsfw, visibility, kind, date_key, live_clip, raw_twin, stacked);
CREATE INDEX IF NOT EXISTS idx_assets_gallery  ON assets(root, COALESCE(captured_at, mtime) DESC, id DESC, trashed, nsfw, visibility, kind, date_key, live_clip, raw_twin, stacked);
-- The name and size sorts, the same shape: in order, with the filter columns
-- carried so skipping hidden rows reads no table pages. Expressions and
-- collation must match `_SORTS`.
CREATE INDEX IF NOT EXISTS idx_assets_name     ON assets(root, filename COLLATE NOCASE, id, trashed, nsfw, visibility, kind, date_key, live_clip, raw_twin, stacked);
CREATE INDEX IF NOT EXISTS idx_assets_size     ON assets(root, size, id, trashed, nsfw, visibility, kind, date_key, live_clip, raw_twin, stacked);
-- Dates first, for a date range and the family date policy, with the filter
-- columns carried as in the indexes above. Newer SQLite (3.5x, with stat4)
-- picks this index for a count whose date bound looks selective; narrow, it
-- then read a table row for every photograph on the visible side of the cutoff.
CREATE INDEX IF NOT EXISTS idx_assets_date     ON assets(root, date_key, trashed, nsfw, visibility, kind, live_clip, raw_twin, stacked);
CREATE INDEX IF NOT EXISTS idx_assets_kind     ON assets(root, kind);
CREATE INDEX IF NOT EXISTS idx_assets_vis      ON assets(root, visibility);
-- A profile's own "Only to me" items: a handful, found without a walk.
-- Partial: whole, it held a NULL for every photograph, ANALYZE then said a
-- lookup matched the whole library, and the sidebar's count read all of it
-- (120 ms a call at 200,000) instead of the handful.
CREATE INDEX IF NOT EXISTS idx_assets_private  ON assets(private_to) WHERE private_to IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_assets_phash    ON assets(phash);
-- Duplicates: a small share of a library, found without a walk. Partial, as
-- idx_assets_private is: whole, it held a NULL for every other photograph,
-- and the duplicates review and the sidebar's count of them read every row
-- the viewer may see (half a second each at 210,000 items) to find a tenth.
CREATE INDEX IF NOT EXISTS idx_assets_dupgroup ON assets(dup_group) WHERE dup_group IS NOT NULL;
-- Photo stacks: a few thousand rows of a library, found without a walk.
CREATE INDEX IF NOT EXISTS idx_assets_stack    ON assets(stack_key) WHERE stack_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_assets_folder   ON assets(root, folder);
CREATE INDEX IF NOT EXISTS idx_assets_aiver    ON assets(ai_version);
CREATE INDEX IF NOT EXISTS idx_assets_live     ON assets(root, is_live);
-- What every scan looks for again and usually does not find: sound files
-- without a picture, files never read, photographs scored by older rules, and
-- the photograph of each pair. Each is a handful of rows in a library of
-- hundreds of thousands, and with nothing narrower SQLite read every row to
-- find them (the statistics say most rows share a kind, and every row a
-- library folder). The WHERE of each must appear word for word in the query
-- that reads it, or SQLite cannot tell the index answers it.
CREATE INDEX IF NOT EXISTS idx_assets_audio    ON assets(root, id) WHERE kind='audio';
CREATE INDEX IF NOT EXISTS idx_assets_unread   ON assets(root, trashed) WHERE thumb IS NOT NULL AND width IS NULL;
CREATE INDEX IF NOT EXISTS idx_assets_quality  ON assets(quality_version) WHERE kind='picture';
CREATE INDEX IF NOT EXISTS idx_assets_stills   ON assets(root) WHERE is_live=1;
CREATE INDEX IF NOT EXISTS idx_assets_raws     ON assets(root) WHERE COALESCE(raw_path,'')<>'';
-- The map. Every pan groups what is on screen; with the filter columns and
-- whether there is a thumbnail (`storage/geo.py` _COVER) carried, the groups
-- are read from the index alone — 156 ms to 63 for a country of 90,000
-- photographs. SQLite reads an indexed expression in place from 3.41.
CREATE INDEX IF NOT EXISTS idx_assets_gps      ON assets(root, gps_lat, gps_lon, trashed, nsfw, visibility, kind, date_key, (thumb IS NOT NULL));
CREATE INDEX IF NOT EXISTS idx_recycled_when   ON recycled(restored_at, deleted_at DESC);
CREATE INDEX IF NOT EXISTS idx_bitrot_status   ON bitrot_records(status, checked_at DESC);
-- The storage check writes one row per file per pass and reads back each
-- file's latest. Without this, "latest row for this file" is a scan of every
-- pass ever made: the status page took 23 seconds at 20,000 files checked
-- three times, and the check itself got slower with every pass.
CREATE INDEX IF NOT EXISTS idx_bitrot_asset    ON bitrot_records(asset_id, id);
-- What hangs off an asset. Deleting one cascades into these tables, and the
-- revoke trigger below reads shares by target; with no index leading on the
-- asset, each deleted photograph scanned each table in full, under the write
-- lock: 500 deletions took two seconds instead of thirty milliseconds.
CREATE INDEX IF NOT EXISTS idx_album_items_asset ON album_items(asset_id);
CREATE INDEX IF NOT EXISTS idx_visundo_asset   ON visibility_undo(asset_id);
CREATE INDEX IF NOT EXISTS idx_shares_target   ON shares(scope, target_id);
-- Opening one occasion reads its photographs by this column alone.
CREATE INDEX IF NOT EXISTS idx_assets_occasion ON assets(occasion_id);
-- Never searched by model. It exists so COUNT(*) has something narrow to
-- walk: without it the count behind every AI search's cache check reads every
-- vector's page, ~260 ms at 100,000 photos and seconds at a million.
CREATE INDEX IF NOT EXISTS idx_embeddings_model ON embeddings(model);
-- And this one is for the count that *joins*, which the one above cannot
-- help with. `asset_id` is the rowid, so joining assets to embeddings probes
-- each row by rowid and reads the page that vector sits on — 358 MB of blob
-- read to count 183,157 rows, which on a cold cache is 23 seconds and is the
-- whole reason the gallery used to sit empty after a restart. Indexing the
-- rowid explicitly gives the join something 1 MB wide to walk instead: the
-- same count in 0.03s, and nothing paged in that nobody wanted.
CREATE INDEX IF NOT EXISTS idx_embeddings_asset ON embeddings(asset_id);
CREATE INDEX IF NOT EXISTS idx_faces_asset    ON faces(asset_id);
-- "Photographs of this person" is an EXISTS over faces by (person, asset).
-- With only person_id indexed, and no statistics yet for faces, the planner
-- walked every face of the person for every photograph in the library.
CREATE INDEX IF NOT EXISTS idx_faces_person   ON faces(person_id, asset_id);
CREATE INDEX IF NOT EXISTS idx_faces_cluster  ON faces(cluster_key);
CREATE INDEX IF NOT EXISTS idx_faces_unnamed  ON faces(person_id, quality DESC);
CREATE INDEX IF NOT EXISTS idx_fingerprints_digest ON content_fingerprints(digest, size);
"""

#: Columns the full-text index carries. Kept as a list because adding one
#: means rebuilding the index, and :func:`_heal_fts` needs to know what the
#: index on disk is missing.
FTS_COLUMNS = ("filename", "folder", "tags", "caption", "camera", "ocr_text", "city")
#: The ones a guest's words are matched against: not the place, not the camera.
GUEST_FTS_COLUMNS = tuple(c for c in FTS_COLUMNS if c not in ("camera", "city"))

_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS assets_fts USING fts5(
    filename, folder, tags, caption, camera, ocr_text, city,
    content='assets', content_rowid='id', tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS assets_ai AFTER INSERT ON assets BEGIN
    INSERT INTO assets_fts(rowid, filename, folder, tags, caption, camera, ocr_text, city)
    VALUES (new.id, new.filename, new.folder, new.tags, new.caption, new.camera,
            new.ocr_text, new.city);
END;

CREATE TRIGGER IF NOT EXISTS assets_ad AFTER DELETE ON assets BEGIN
    INSERT INTO assets_fts(assets_fts, rowid, filename, folder, tags, caption,
                           camera, ocr_text, city)
    VALUES ('delete', old.id, old.filename, old.folder, old.tags, old.caption,
            old.camera, old.ocr_text, old.city);
END;

CREATE TRIGGER IF NOT EXISTS assets_au AFTER UPDATE OF
    filename, folder, tags, caption, camera, ocr_text, city ON assets BEGIN
    INSERT INTO assets_fts(assets_fts, rowid, filename, folder, tags, caption,
                           camera, ocr_text, city)
    VALUES ('delete', old.id, old.filename, old.folder, old.tags, old.caption,
            old.camera, old.ocr_text, old.city);
    INSERT INTO assets_fts(rowid, filename, folder, tags, caption, camera, ocr_text, city)
    VALUES (new.id, new.filename, new.folder, new.tags, new.caption, new.camera,
            new.ocr_text, new.city);
END;
"""


def folder_clause(column: str, folder: str, *, include_self: bool = True
                  ) -> tuple[str, list[Any]]:
    """SQL matching *folder* and everything beneath it, as plain comparisons.

    ``folder/…`` is exactly the strings from ``folder/`` up to, but not
    including, ``folder0`` — ``0`` is the character after ``/`` — so a subtree
    is one range over the folder index. The outer bound is what lets SQLite
    use that index at all; the inner test keeps out look-alike siblings such
    as ``folder-2019``, which sort inside the outer bound.

    This replaced ``LIKE 'folder/%' ESCAPE``. LIKE could not use the index, so
    one folder of a million-item library took seconds to list; it needed
    escaping, or an underscore in a folder name matched a sibling; and it
    ignored letter case, where the by-id guard does not. Comparisons need no
    escaping and agree with the guard.
    """
    below = f"{folder}/"
    if not include_self:
        return f"({column} >= ? AND {column} < ?)", [below, f"{folder}0"]
    return (f"({column} >= ? AND {column} < ? AND ({column} = ? OR {column} >= ?))",
            [folder, f"{folder}0", folder, below])


def columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def ensure_columns(
    conn: sqlite3.Connection,
    table: str,
    wanted: dict[str, str],
) -> list[str]:
    """Add any of ``wanted`` that ``table`` is missing. Returns what was added.

    ``CREATE TABLE IF NOT EXISTS`` silently does nothing when the table already
    exists, so a column added to the schema text never reaches a database that
    was created before it. This runs on *every* start and repairs that, which
    means a new column can never again depend on somebody remembering to bump
    :data:`SCHEMA_VERSION`. It is a no-op once the columns are there.

    ``wanted`` maps a column name to the rest of its ``ADD COLUMN`` clause;
    SQLite requires a constant default, so no ``DEFAULT (expr)`` here.
    """
    have = columns(conn, table)
    if not have:  # the table itself does not exist yet — nothing to repair
        return []
    added = []
    for name, decl in wanted.items():
        if name in have:
            continue
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            added.append(name)
        except sqlite3.OperationalError:
            # Lost a race with another process doing the same repair.
            if name not in columns(conn, table):
                raise
    if added:
        conn.commit()
    return added


#: Columns that arrived after a release shipped, per table. Anything added to
#: a ``CREATE TABLE`` below must be added here too.
LATE_COLUMNS: dict[str, dict[str, str]] = {
    "assets": {
        "folder": "TEXT NOT NULL DEFAULT ''",
        "ext": "TEXT NOT NULL DEFAULT ''",
        "size": "INTEGER NOT NULL DEFAULT 0",
        "mtime": "REAL NOT NULL DEFAULT 0",
        "captured_at": "REAL",
        "date_key": "TEXT NOT NULL DEFAULT ''",
        "date_source": "TEXT NOT NULL DEFAULT 'mtime'",
        "width": "INTEGER",
        "height": "INTEGER",
        "duration": "REAL",
        "orientation": "INTEGER DEFAULT 1",
        "camera": "TEXT",
        "lens": "TEXT",
        "iso": "INTEGER",
        "f_number": "REAL",
        "exposure": "TEXT",
        "focal_length": "REAL",
        "gps_lat": "REAL",
        "gps_lon": "REAL",
        # 1 where the location was filled in from a photograph taken close in
        # time (storage/geo.py), not read from the file; undone as a set.
        "location_inferred": "INTEGER NOT NULL DEFAULT 0",
        "country": "TEXT",
        "city": "TEXT",
        "thumb": "TEXT",
        "blurhash": "TEXT",
        "color": "TEXT",
        "phash": "TEXT",
        "dup_group": "TEXT",
        "sharpness": "REAL",
        "brightness": "REAL",
        "quality": "TEXT NOT NULL DEFAULT '[]'",
        "quality_version": "INTEGER NOT NULL DEFAULT 0",
        "keyframe_version": "INTEGER NOT NULL DEFAULT 0",
        "ocr_text": "TEXT",
        "ocr_version": "INTEGER NOT NULL DEFAULT 0",
        "place_version": "INTEGER NOT NULL DEFAULT 0",
        "occasion_id": "INTEGER",
        "tags": "TEXT NOT NULL DEFAULT '[]'",
        "caption": "TEXT",
        "nsfw": "INTEGER NOT NULL DEFAULT 0",
        "nsfw_score": "REAL NOT NULL DEFAULT 0",
        "tags_source": "TEXT NOT NULL DEFAULT 'auto'",
        "caption_source": "TEXT NOT NULL DEFAULT 'auto'",
        "nsfw_source": "TEXT NOT NULL DEFAULT 'auto'",
        "screen_score": "REAL",
        "screen_version": "INTEGER NOT NULL DEFAULT 0",
        "revealing": "REAL NOT NULL DEFAULT 0",
        "revealing_version": "INTEGER NOT NULL DEFAULT 0",
        "visibility": "INTEGER NOT NULL DEFAULT 1",
        "vis_source": "TEXT NOT NULL DEFAULT 'default'",
        "private_to": "INTEGER",
        "private_was": "INTEGER",
        "private_was_src": "TEXT",
        "rotation": "INTEGER NOT NULL DEFAULT 0",
        "rot_source": "TEXT NOT NULL DEFAULT 'none'",
        "live_video_path": "TEXT",
        "is_live": "INTEGER NOT NULL DEFAULT 0",
        "live_clip": "INTEGER NOT NULL DEFAULT 0",
        "raw_path": "TEXT",
        "raw_twin": "INTEGER NOT NULL DEFAULT 0",
        "stack_key": "TEXT",
        "stacked": "INTEGER NOT NULL DEFAULT 0",
        "stack_count": "INTEGER NOT NULL DEFAULT 0",
        "trashed": "INTEGER NOT NULL DEFAULT 0",
        "ai_version": "INTEGER NOT NULL DEFAULT 0",
        "face_version": "INTEGER NOT NULL DEFAULT 0",
        "indexed_at": "REAL NOT NULL DEFAULT 0",
        "large_file_reviewed_at": "REAL",
        "added_at": "REAL",
    },
    "recycled": {
        "thumb": "TEXT",
        "metadata": "TEXT",
    },
    "albums": {
        "created_by": "INTEGER",
    },
    "people_clusters": {
        "centroid": "BLOB",
        "face_count": "INTEGER NOT NULL DEFAULT 0",
        "confirmed_count": "INTEGER NOT NULL DEFAULT 0",
        "cover_face_id": "INTEGER",
        "updated_at": "REAL NOT NULL DEFAULT 0",
    },
    "faces": {
        "cluster_key": "TEXT",
        "sharpness": "REAL NOT NULL DEFAULT 0",
        "model": "TEXT NOT NULL DEFAULT ''",
    },
    "bitrot_records": {
        "file_mtime": "REAL",
        "file_size": "INTEGER",
    },
}


def heal_schema(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Apply :data:`LATE_COLUMNS` to whatever tables exist, and repair drift."""
    healed = {
        table: added
        for table, wanted in LATE_COLUMNS.items()
        if (added := ensure_columns(conn, table, wanted))
    }
    if "added_at" in healed.get("assets", []):
        # The best record an older index has of when each file arrived.
        with _write_lock:
            conn.execute("UPDATE assets SET added_at = indexed_at WHERE added_at IS NULL")
            conn.commit()
    if _restore_user_assets_cascade(conn):
        healed.setdefault("user_assets", []).append("foreign keys")
    _seed_high_water(conn)
    return healed


def _seed_high_water(conn: sqlite3.Connection) -> None:
    """Carry the bin's photograph numbers into the high-water mark.

    An index from before the assets_high_water trigger has no mark, so the
    first photograph indexed after the newest one went to the bin took that
    one's number, and with it the job engine's memory of it (D-28). The bin
    still knows the numbers it took.
    """
    try:
        row = conn.execute(
            "SELECT MAX(asset_id) FROM recycled WHERE asset_id IS NOT NULL").fetchone()
    except sqlite3.Error:
        return                                    # no bin table yet
    binned = int(row[0] or 0)
    if not binned:
        return
    with _write_lock:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('assets_high_water', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value "
            "WHERE CAST(meta.value AS INTEGER) < CAST(excluded.value AS INTEGER)",
            (str(binned),))
        conn.commit()


def _restore_user_assets_cascade(conn: sqlite3.Connection) -> bool:
    """Give ``user_assets`` back its foreign keys on an upgraded database.

    ``_migrate`` creates this table to carry the old per-asset favourites over,
    and creates it without constraints. ``AUTH_SCHEMA`` then declares the
    correct table — but as ``CREATE TABLE IF NOT EXISTS``, which is a no-op
    once the table is there. The result is an install where deleting an asset
    leaves its favourites behind; because SQLite reuses rowids, the next scan
    silently re-attaches one person's stars to somebody else's photographs.

    SQLite cannot add a constraint in place, so the table is rebuilt. Rows that
    no longer point at a real user or asset are dropped on the way — they are
    precisely the orphans this is here to stop mattering.
    """
    if "user_assets" not in _tables(conn):
        return False
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='user_assets'"
    ).fetchone()
    if sql is None or "REFERENCES" in (sql["sql"] or ""):
        return False
    if not {"users", "assets"} <= _tables(conn):
        return False        # nothing to point at yet; try again next start

    # No ``with _write_lock`` here: ``init_db`` calls this (through
    # ``heal_schema``) while already holding that lock, and it is a plain
    # ``threading.Lock``, so taking it again on the same thread would hang the
    # server at start-up — before it binds a port, and without a log line.
    # ``BEGIN IMMEDIATE`` below already keeps other writers out of the rebuild.
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""
            CREATE TABLE user_assets_rebuilt (
                user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
                favorite INTEGER NOT NULL DEFAULT 0,
                rating   INTEGER NOT NULL DEFAULT 0,
                seen_at  REAL,
                PRIMARY KEY (user_id, asset_id)
            )""")
        conn.execute("""
            INSERT INTO user_assets_rebuilt(user_id, asset_id, favorite, rating, seen_at)
            SELECT ua.user_id, ua.asset_id, ua.favorite, ua.rating, ua.seen_at
            FROM user_assets ua
            JOIN users  u ON u.id = ua.user_id
            JOIN assets a ON a.id = ua.asset_id""")
        conn.execute("DROP TABLE user_assets")
        conn.execute("ALTER TABLE user_assets_rebuilt RENAME TO user_assets")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user_assets_fav "
                     "ON user_assets(user_id, favorite) WHERE favorite = 1")
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        with suppress(sqlite3.Error):
            conn.execute("DROP TABLE IF EXISTS user_assets_rebuilt")
            conn.commit()
        return False
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
    return True


#: The ``meta`` key set while the full-text index is dropped but not refilled.
FTS_REBUILD_KEY = "fts_rebuild_pending"


def _heal_fts(conn: sqlite3.Connection) -> bool:
    """Replace a full-text index that predates a column, and say if it did.

    ``CREATE VIRTUAL TABLE IF NOT EXISTS`` cannot widen an existing FTS5
    table, and there is no ALTER for one either. So an index built before
    ``ocr_text`` existed would silently go on searching five columns while
    the triggers wrote six — which fails outright on the next write.

    Dropping and recreating is cheap in code and costs one reindex of text
    Ninaivu already holds. Nothing in the library is read again.
    """
    if "assets_fts" not in _tables(conn):
        # No index yet over photographs already there: the index was made
        # where SQLite had no FTS5 (the Python in 2.2.0's portable downloads
        # had none). The table made next starts empty, and the first caption
        # edited after that, asking it to forget words it never held, failed
        # with "database disk image is malformed". Filled like a widened one.
        if conn.execute("SELECT 1 FROM assets LIMIT 1").fetchone() is None:
            return False
        set_meta(conn, FTS_REBUILD_KEY, "1")
        conn.commit()
        return True
    have = [row[1] for row in conn.execute("PRAGMA table_info(assets_fts)")]
    if all(column in have for column in FTS_COLUMNS):
        return False
    for trigger in ("assets_ai", "assets_ad", "assets_au"):
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    conn.execute("DROP TABLE IF EXISTS assets_fts")
    # Committed with the drop, and cleared only once the rebuild has been
    # committed too. The rebuild is a separate step; a start that is killed
    # between the two would otherwise leave a new, empty index that the next
    # start finds already wide enough — and search would find nothing, for
    # good. The flag is what tells that next start to fill it.
    set_meta(conn, FTS_REBUILD_KEY, "1")
    conn.commit()
    return True


def _narrow_fts_update_trigger(conn: sqlite3.Connection) -> None:
    """Drop an update trigger that fires on every column, so it is remade.

    It used to rewrite a row's full-text entry on *any* update — a duplicate
    group, a live-photo flag, an occasion id — none of which the index holds.
    Rebuilding occasions touched every row twice, which on a 180,000-item
    library held the write lock long enough for the cloud upload, waiting on
    it, to give up with "database is locked".
    """
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' "
                       "AND name='assets_au'").fetchone()
    if row is not None and "UPDATE OF" not in (row[0] or "").upper():
        conn.execute("DROP TRIGGER assets_au")


# ---------------------------------------------------------------------------
# Library-wide figures, remembered until the library changes
# ---------------------------------------------------------------------------
#
# The gallery's facets, the sidebar's counts, the timeline and the console's
# folder totals each read the whole library, and each was worked out afresh on
# every request: on 100,000 items the facets alone were half a second on every
# gallery load, and search suggestions paid the same again on every keystroke.
# What they read changes far less often than it is asked for. So the database
# counts its own changes — triggers bump a number in ``meta`` whenever a
# column those figures read is written, by any connection or process — and a
# figure is kept until that number moves.

#: The ``meta`` key the triggers bump.
GENERATION_KEY = "assets_generation"

#: Every column a remembered figure reads. A figure that starts reading
#: another column must add it here, or it will be answered stale after that
#: column changes. The indexing pipeline's own columns — thumbnails, hashes,
#: sharpness, occasions — are deliberately absent: a scan writes them to every
#: row, and none of the figures reads them.
#:
#: ``tags`` is not here: only the facets read it, and the tagger writes it to
#: every row for days on a first pass. It has a counter of its own
#: (:data:`TAGS_GENERATION_KEY`), so the timeline and the sidebar's counts are
#: not worked out again on every request while it runs.
GENERATION_COLUMNS = ("root", "rel_path", "folder", "kind", "size", "visibility",
                      "trashed", "nsfw", "nsfw_source", "revealing", "date_key", "camera",
                      "dup_group", "is_live", "live_clip", "raw_twin",
                      "stacked", "stack_count")

#: Bumped when a row's tags change. Only what reads tags keys on it.
TAGS_GENERATION_KEY = "assets_tags_generation"

#: Bumped when faces are found, moved between people, or a person is named,
#: renamed or given another cover. The people list keys on it.
FACES_GENERATION_KEY = "faces_generation"

#: Bumped when occasions are rebuilt, an item joins or leaves one, or gains
#: or loses its thumbnail (an occasion's cover is its newest item with one).
OCCASIONS_GENERATION_KEY = "occasions_generation"

#: Bumped when the matcher's unnamed groups change: a face given another
#: group (every regroup reissues them), or hidden from naming or shown again.
#: Who a face belongs to is :data:`FACES_GENERATION_KEY`'s; the console's
#: list of groups waiting for a name keys on both.
FACE_GROUPS_GENERATION_KEY = "face_groups_generation"

#: Bumped when a row's location or place name changes, or it gains or loses
#: its thumbnail (a place's cover is its newest picture with one). The map's
#: place list and the places a search phrase is read against key on it.
PLACES_GENERATION_KEY = "places_generation"

#: Bumped when a search vector is added or removed — not when one is replaced,
#: which leaves the set of searchable photographs as it was.
EMBEDDINGS_GENERATION_KEY = "embeddings_generation"

#: Bumped when what a gallery tile is drawn from changes: its shape, its
#: thumbnail, its turn, its colour. Only the layout's ETag keys on it — a scan
#: writes these to every row, and no remembered figure reads them.
LAYOUT_GENERATION_KEY = "assets_layout_generation"

#: Bumped when a photograph joins or leaves an album. The album list keys on
#: it, with the library's own counter for what each viewer may see of them.
ALBUMS_GENERATION_KEY = "albums_generation"

#: Bumped when anything the scan's whole-library checks read changes: a row
#: added or removed, its name, what it is, who may see it and why, whether it
#: is a pair's half, how it scored as a screen, and when and where it was
#: taken. A scan that finds the number where its last check left it, with the
#: same settings, skips that check: hiding screenshots by name and grouping
#: occasions each read every row, and on a library where nothing changed they
#: found nothing to do, every time the watcher saw one photograph arrive.
#: Not one of :data:`CHANGE_COUNTERS`: the tagger moves it with every screen
#: score, and no viewer's cached layout depends on those.
SCAN_GENERATION_KEY = "assets_scan_generation"
SCAN_COLUMNS = ("root", "rel_path", "kind", "trashed", "nsfw", "visibility",
                "vis_source", "is_live", "live_video_path", "live_clip",
                "raw_path", "raw_twin", "screen_score", "screen_version",
                "captured_at", "mtime", "gps_lat", "gps_lon", "city")

_PLACE_COLUMNS = ("gps_lat", "gps_lon", "city", "country")
_LAYOUT_COLUMNS = ("width", "height", "indexed_at", "rotation", "color",
                   "duration", "thumb")

#: Bumped when who may see an item changes: its visibility, the explicit flag,
#: or its going into the bin. Open family pages ask for it with each activity
#: tick and bring their gallery up to date when it moves, so an item kept
#: "Only to me", sent for screening or hidden by an admin leaves every open
#: screen within seconds (static/js/app.js, onActivity). Not one of
#: :data:`CHANGE_COUNTERS`: :data:`GENERATION_KEY` already covers the layout.
SEEN_GENERATION_KEY = "assets_seen_generation"
SEEN_COLUMNS = ("visibility", "nsfw", "trashed")

#: Every counter the triggers keep.
CHANGE_COUNTERS = (GENERATION_KEY, TAGS_GENERATION_KEY, FACES_GENERATION_KEY,
                   OCCASIONS_GENERATION_KEY, PLACES_GENERATION_KEY,
                   EMBEDDINGS_GENERATION_KEY, LAYOUT_GENERATION_KEY,
                   ALBUMS_GENERATION_KEY)


def _changed(columns: Sequence[str]) -> str:
    # The tagger and the scan write a row's values back unchanged far more
    # often than they change them; a bump for those threw every remembered
    # figure away for nothing.
    return " OR ".join(f"OLD.{c} IS NOT NEW.{c}" for c in columns)


def _generation_schema() -> str:
    def bump(key: str) -> str:
        return (f"UPDATE meta SET value = CAST(value AS INTEGER) + 1 "
                f"WHERE key = '{key}';")

    seeds = "\n".join(
        f"INSERT OR IGNORE INTO meta(key, value) VALUES ('{key}', "
        f"CAST(abs(random() % 1000000000000) AS TEXT));"
        for key in (*CHANGE_COUNTERS, SCAN_GENERATION_KEY, FACE_GROUPS_GENERATION_KEY,
                    SEEN_GENERATION_KEY))
    main, tags = bump(GENERATION_KEY), bump(TAGS_GENERATION_KEY)
    faces, occasions = bump(FACES_GENERATION_KEY), bump(OCCASIONS_GENERATION_KEY)
    places, vectors = bump(PLACES_GENERATION_KEY), bump(EMBEDDINGS_GENERATION_KEY)
    layout = bump(LAYOUT_GENERATION_KEY)
    scanned = bump(SCAN_GENERATION_KEY)
    albums = bump(ALBUMS_GENERATION_KEY)
    groups = bump(FACE_GROUPS_GENERATION_KEY)
    seen = bump(SEEN_GENERATION_KEY)
    thumbed = "(OLD.thumb IS NULL) <> (NEW.thumb IS NULL)"
    # ``source`` too: confirming a face the matcher had already given to the
    # same person changes nothing else, and the face status counts confirmed
    # faces.
    face_columns = ("asset_id", "person_id", "quality", "source")
    person_columns = ("name", "cover_face_id", "avatar_asset_id")
    occasion_columns = ("id", "root", "key", "title", "place", "started_at",
                        "ended_at", "days", "count")
    triggers = {
        "assets_generation_insert": f"AFTER INSERT ON assets BEGIN {main} END",
        "assets_generation_delete": f"AFTER DELETE ON assets BEGIN {main} END",
        "assets_generation_update":
            f"AFTER UPDATE OF {', '.join(GENERATION_COLUMNS)} ON assets "
            f"WHEN {_changed(GENERATION_COLUMNS)} BEGIN {main} END",
        "assets_seen_generation_delete": f"AFTER DELETE ON assets BEGIN {seen} END",
        "assets_seen_generation_update":
            f"AFTER UPDATE OF {', '.join(SEEN_COLUMNS)} ON assets "
            f"WHEN {_changed(SEEN_COLUMNS)} BEGIN {seen} END",
        "assets_scan_generation_insert": f"AFTER INSERT ON assets BEGIN {scanned} END",
        "assets_scan_generation_delete": f"AFTER DELETE ON assets BEGIN {scanned} END",
        "assets_scan_generation_update":
            f"AFTER UPDATE OF {', '.join(SCAN_COLUMNS)} ON assets "
            f"WHEN {_changed(SCAN_COLUMNS)} BEGIN {scanned} END",
        "assets_tags_generation_update":
            f"AFTER UPDATE OF tags ON assets WHEN {_changed(('tags',))} "
            f"BEGIN {tags} END",
        "faces_generation_insert": f"AFTER INSERT ON faces BEGIN {faces} END",
        "faces_generation_delete": f"AFTER DELETE ON faces BEGIN {faces} END",
        "faces_generation_update":
            f"AFTER UPDATE OF {', '.join(face_columns)} ON faces "
            f"WHEN {_changed(face_columns)} BEGIN {faces} END",
        "face_groups_generation_update":
            "AFTER UPDATE OF cluster_key ON faces "
            f"WHEN OLD.cluster_key IS NOT NEW.cluster_key BEGIN {groups} END",
        "face_groups_generation_hide":
            f"AFTER INSERT ON faces_ignored BEGIN {groups} END",
        "face_groups_generation_show":
            f"AFTER DELETE ON faces_ignored BEGIN {groups} END",
        "people_generation_insert":
            f"AFTER INSERT ON people_clusters BEGIN {faces} END",
        "people_generation_delete":
            f"AFTER DELETE ON people_clusters BEGIN {faces} END",
        # Not on the centroid and counts, which move on every confirmation
        # and which the people list does not read.
        "people_generation_update":
            f"AFTER UPDATE OF {', '.join(person_columns)} ON people_clusters "
            f"WHEN {_changed(person_columns)} BEGIN {faces} END",
        "occasions_generation_insert":
            f"AFTER INSERT ON occasions BEGIN {occasions} END",
        "occasions_generation_delete":
            f"AFTER DELETE ON occasions BEGIN {occasions} END",
        # A rebuild refreshes every occasion's row, nearly always unchanged.
        "occasions_generation_update":
            f"AFTER UPDATE ON occasions WHEN {_changed(occasion_columns)} "
            f"BEGIN {occasions} END",
        "assets_occasions_generation_update":
            "AFTER UPDATE OF occasion_id, thumb ON assets "
            f"WHEN OLD.occasion_id IS NOT NEW.occasion_id OR {thumbed} "
            f"BEGIN {occasions} END",
        "assets_places_generation_update":
            f"AFTER UPDATE OF {', '.join(_PLACE_COLUMNS)}, thumb ON assets "
            f"WHEN {_changed(_PLACE_COLUMNS)} OR {thumbed} BEGIN {places} END",
        "embeddings_generation_insert":
            f"AFTER INSERT ON embeddings BEGIN {vectors} END",
        "embeddings_generation_delete":
            f"AFTER DELETE ON embeddings BEGIN {vectors} END",
        "assets_layout_generation_update":
            f"AFTER UPDATE OF {', '.join(_LAYOUT_COLUMNS)} ON assets "
            f"WHEN {_changed(_LAYOUT_COLUMNS)} BEGIN {layout} END",
        "album_items_generation_insert":
            f"AFTER INSERT ON album_items BEGIN {albums} END",
        "album_items_generation_delete":
            f"AFTER DELETE ON album_items BEGIN {albums} END",
        "album_items_generation_update":
            "AFTER UPDATE ON album_items WHEN OLD.album_id IS NOT NEW.album_id "
            "OR OLD.asset_id IS NOT NEW.asset_id OR OLD.added_at IS NOT NEW.added_at "
            f"BEGIN {albums} END",
    }
    # Remade on every start rather than IF NOT EXISTS, so a change to the
    # column list or the conditions reaches a database that already has the
    # old triggers.
    return seeds + "\n" + "\n".join(
        f"DROP TRIGGER IF EXISTS {name};\nCREATE TRIGGER {name} {body};"
        for name, body in triggers.items())


#: Bumped when the record of where a file's copies are changes: an upload
#: finishing or failing, a file reaching the second copy or the off-site one,
#: or a library file changing under them (its ``mtime``, which those copies
#: are compared by). Not one of :data:`CHANGE_COUNTERS`: the gallery's ETag
#: fingerprints those, and an upload in progress must not cost every viewer
#: their cached layout. The storage report and the copies page key on it.
COPIES_GENERATION_KEY = "copies_generation"

#: What each copy record is read by. The upload's attempts, resume link and
#: timestamps move constantly while it runs and are read by none of them.
_COPY_COLUMNS = {
    "cloud_uploads": ("root", "rel_path", "state", "error", "remote_id"),
    "mirror_copies": ("root", "rel_path", "size", "mtime"),
    "offsite_copies": ("root", "rel_path", "size", "mtime"),
}


def ensure_copies_counter(conn: sqlite3.Connection) -> None:
    """Keep :data:`COPIES_GENERATION_KEY` for whichever copy tables exist.

    Those tables belong to features that create them when first used, so the
    triggers are made here, by what reads the counter, after it has made the
    tables it joins. Versioned names rather than drop-and-create: this runs on
    every request of those pages, and remaking a trigger is a schema change.
    """
    bump = (f"UPDATE meta SET value = CAST(value AS INTEGER) + 1 "
            f"WHERE key = '{COPIES_GENERATION_KEY}';")
    have = _tables(conn)
    script = [f"INSERT OR IGNORE INTO meta(key, value) VALUES ('{COPIES_GENERATION_KEY}', "
              "CAST(abs(random() % 1000000000000) AS TEXT));",
              "CREATE TRIGGER IF NOT EXISTS assets_copies_generation_v1 "
              "AFTER UPDATE OF mtime ON assets WHEN OLD.mtime IS NOT NEW.mtime "
              f"BEGIN {bump} END;"]
    for table, columns in _COPY_COLUMNS.items():
        if table not in have:
            continue
        script += [
            f"CREATE TRIGGER IF NOT EXISTS {table}_generation_insert_v1 "
            f"AFTER INSERT ON {table} BEGIN {bump} END;",
            f"CREATE TRIGGER IF NOT EXISTS {table}_generation_delete_v1 "
            f"AFTER DELETE ON {table} BEGIN {bump} END;",
            f"CREATE TRIGGER IF NOT EXISTS {table}_generation_update_v1 "
            f"AFTER UPDATE OF {', '.join(columns)} ON {table} "
            f"WHEN {_changed(columns)} BEGIN {bump} END;"]
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    missing = [line for line in script[1:] if line.split()[5] not in names]
    if missing or meta_counter(conn, COPIES_GENERATION_KEY) is None:
        with _write_lock:
            conn.executescript("\n".join([script[0], *missing]))


def library_generation(conn: sqlite3.Connection) -> int | None:
    """The number the triggers keep, or None where there is none to trust.

    It starts at a random value rather than zero, so two databases — a test's
    and the next test's, or a library and a restored copy of another — never
    agree on it by coincidence.
    """
    return meta_counter(conn, GENERATION_KEY)


def change_counters(conn: sqlite3.Connection) -> tuple[int, ...] | None:
    """Every counter at once — a fingerprint of the whole index for an ETag —
    or None if any is missing."""
    try:
        held = dict(conn.execute(
            f"SELECT key, value FROM meta WHERE key IN "
            f"({','.join('?' * len(CHANGE_COUNTERS))})", CHANGE_COUNTERS).fetchall())
        return tuple(int(held[key]) for key in CHANGE_COUNTERS)
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        return None


def meta_counter(conn: sqlite3.Connection, key: str) -> int | None:
    """One of the counters the triggers keep, or None if it is missing."""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key=?",
                           (key,)).fetchone()
    except sqlite3.Error:
        return None
    try:
        return int(row[0]) if row is not None else None
    except (TypeError, ValueError):
        return None


_AGGREGATES: dict[tuple, tuple[Any, Any, float]] = {}
_aggregates_lock = threading.Lock()

#: Enough for every viewer's ceiling and folder in a household several times
#: over; the least recently asked-for goes first.
_AGGREGATES_MAX = 256


def cached_aggregate(conn: sqlite3.Connection, key: tuple, compute, *,
                     also: Sequence[str] = (), shared: bool = False,
                     max_age: float | None = None):
    """``compute()``, or its answer from before if the library has not changed.

    *key* must say everything the answer depends on besides the library's
    rows — the SQL with its viewer and date-policy clauses and its parameters
    is the simple way. Each caller gets its own copy, so one that edits what
    it was handed cannot edit the next caller's answer.

    A figure worked out while a change was being committed may reflect that
    change and still be filed under the number from before it. That is safe:
    the number has moved by the time anybody reads it again, so it is simply
    worked out once more. A figure is never filed under a newer number than the
    rows it was read from.

    *also* names further counters (``TAGS_GENERATION_KEY`` and the like) the
    figure depends on besides the library's own; it is kept until any of them
    moves. ``shared=True`` hands every caller the same object instead of a
    copy, for an answer that cannot be edited (a tuple of numbers) and is too
    big to copy on every request.

    *max_age*, in seconds, is for a figure that also depends on rows the
    number does not follow: it is worked out again once it is that old.
    """
    import copy

    generation = library_generation(conn)
    if generation is None:
        return compute()
    if also:
        counters = tuple(meta_counter(conn, name) for name in also)
        if None in counters:
            return compute()
        generation = (generation, *counters)
    try:
        path = conn.execute("PRAGMA database_list").fetchone()[2] or ""
    except sqlite3.Error:
        return compute()
    full_key = (path, *key)
    with _aggregates_lock:
        held = _AGGREGATES.get(full_key)
        if (held is not None and held[0] == generation
                and (max_age is None or time.monotonic() - held[2] < max_age)):
            # Most recently used last, so the oldest is first to go.
            _AGGREGATES[full_key] = _AGGREGATES.pop(full_key)
            return held[1] if shared else copy.deepcopy(held[1])
    taken = time.monotonic()
    value = compute()
    with _aggregates_lock:
        _AGGREGATES.pop(full_key, None)
        _AGGREGATES[full_key] = (generation, value, taken)
        while len(_AGGREGATES) > _AGGREGATES_MAX:
            _AGGREGATES.pop(next(iter(_AGGREGATES)))
    return value if shared else copy.deepcopy(value)


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


#: Database files this process has already brought up to date.
_READY: set[str] = set()


def ready_connection(db_path: Path | str) -> sqlite3.Connection:
    """A connection to a database this process has already set up.

    The schema replay, the healing and the statistics check in init_db ran
    once per library folder on every scan, each under the write lock. A
    scan only needs the schema to be there, so after the first time the
    file is opened plainly. A file that has gone (the index put back from a
    backup while running) is set up again.
    """
    key = str(db_path)
    if key in _READY and (key == ":memory:" or os.path.exists(key)):
        return connect(db_path)
    return init_db(db_path)


def init_db(db_path: Path | str) -> sqlite3.Connection:
    """Create or migrate the schema and return a connection."""
    conn = connect(db_path)
    key = str(db_path)
    with _write_lock:
        # An index from before the first step kept is not opened: it is told
        # to go through 4.4 first (storage/migrations.py), before this
        # function changes a thing in it.
        from . import migrations as _migrations           # noqa: PLC0415
        try:
            _migrations.refuse_older(conn, _migrations.INDEX, path=db_path)
        except _migrations.IndexTooOld:
            conn.close()
            raise
        # Read the stored version *before* the schema script runs — on a brand
        # new file the meta table does not exist yet, which means version 0.
        try:
            existing = int(get_meta(conn, "schema_version", "0") or 0)
        except sqlite3.OperationalError:
            existing = 0
        conn.executescript(_SCHEMA)
        # Version-independent: repairs a database whose recorded version is
        # already current but which predates a column added since.
        heal_schema(conn)
        # What was said in videos, from before the household asked for that
        # feature gone (October 2026): dropped, with its trigger.
        conn.executescript(
            "DROP TRIGGER IF EXISTS video_words_follow_asset;"
            "DROP TABLE IF EXISTS video_words;"
            "DROP TABLE IF EXISTS video_words_done;")
        # Every feature's tables, on or off, each from its own manifest and
        # in the order they were always made: smart albums, "who is this?"
        # links, the family tree, photo books, the handover plan, voice
        # stories, time capsules, screening, descriptions, colour copies,
        # moments inside videos, then and now, a life story, letters read
        # aloud, photo stacks and the change journal (ninaivu/features).
        # The journal's triggers are put in place only while the
        # change_journal setting is on (storage/journal.py).
        from .. import features                           # noqa: PLC0415
        features.create_tables(conn)
        # Who sees which dates, written down once: a new library starts with
        # family and guests on or after the cutoff; one already in use keeps
        # what it had (server/date_policy.py).
        from ..server import date_policy                  # noqa: PLC0415
        date_policy.settle(conn, new=not existing)
        # Old links cannot prove which incarnation of a reused row ID they
        # referred to. Retire them once; new links are revoked on any deletion,
        # including scanner cleanup and direct SQL, before that ID can be reused.
        if get_meta(conn, "share_delete_guard", "0") != "1":
            conn.execute("UPDATE shares SET target_id=-1")
            set_meta(conn, "share_delete_guard", "1")
        conn.executescript("""
            CREATE TRIGGER IF NOT EXISTS revoke_asset_shares AFTER DELETE ON assets
            BEGIN UPDATE shares SET target_id=-1 WHERE scope='asset' AND target_id=OLD.id; END;
            CREATE TRIGGER IF NOT EXISTS revoke_album_shares AFTER DELETE ON albums
            BEGIN UPDATE shares SET target_id=-1 WHERE scope='album' AND target_id=OLD.id; END;
        """)
        # A photograph made admin-only or flagged after it was shared on its
        # own stops being shared. An administrator may still share a hidden
        # photograph on purpose — the link is made after the decision — but a
        # link made while it was family-visible must not outlive hiding it,
        # and nobody remembers which links they once sent.
        if {"visibility", "nsfw"} <= columns(conn, "assets"):
            conn.executescript("""
            CREATE TRIGGER IF NOT EXISTS revoke_asset_shares_on_hide
            AFTER UPDATE OF visibility, nsfw ON assets
            WHEN (NEW.visibility >= 2 AND OLD.visibility < 2)
              OR (COALESCE(NEW.nsfw, 0) <> 0 AND COALESCE(OLD.nsfw, 0) = 0)
            BEGIN UPDATE shares SET target_id=-1 WHERE scope='asset' AND target_id=NEW.id; END;
        """)
        # An item somebody keeps "Only to me" stays hidden from everybody
        # else whatever writes its visibility next: a folder rule, an undo, a
        # bulk "Show to", an import. Only clearing ``private_to`` (see
        # :func:`clear_private`) lets it go back.
        if {"visibility", "private_to"} <= columns(conn, "assets"):
            conn.executescript("""
            CREATE TRIGGER IF NOT EXISTS keep_private_hidden
            AFTER UPDATE OF visibility ON assets
            WHEN NEW.private_to IS NOT NULL AND NEW.visibility < 2
            BEGIN UPDATE assets SET visibility=2, vis_source='private' WHERE id=NEW.id; END;
        """)
        conn.executescript(_generation_schema())
        for index in _RETIRED_INDEXES:
            conn.execute(f"DROP INDEX IF EXISTS {index}")
        # A dropped index takes its statistics with it, and ANALYZE ran again
        # only once the library had doubled or halved: after the update that
        # reshaped the gallery's indexes SQLite guessed, and a search on
        # 208,000 photographs ran for minutes. Measured again below.
        unmeasured = bool(_drop_changed_indexes(conn, _INDEX_SCHEMA))
        conn.executescript(_INDEX_SCHEMA)
        unmeasured = unmeasured or bool(_indexes_without_statistics(conn))
        if has_fts5(conn):
            widened = _heal_fts(conn)
            _narrow_fts_update_trigger(conn)
            conn.executescript(_FTS_SCHEMA)
            if widened or get_meta(conn, FTS_REBUILD_KEY, "0") == "1":
                # The table is new and empty; this fills it from `assets`,
                # which is where an external-content index keeps its text.
                # Also run when an earlier start dropped the table and never
                # got this far (see _heal_fts); the flag goes in the same
                # commit as the rebuild, so it cannot be lost without it.
                conn.execute("INSERT INTO assets_fts(assets_fts) VALUES('rebuild')")
                set_meta(conn, FTS_REBUILD_KEY, "0")
            set_meta(conn, "fts", "1")
        else:
            set_meta(conn, "fts", "0")
        # Never lowered: an older version started on this index (by hand,
        # past storage/upgrade.py) must not make the next start of the newer
        # one believe its own changes were never made.
        set_meta(conn, "schema_version", str(max(existing, SCHEMA_VERSION)))
        _enforce_admin_only_kinds(conn)
        conn.commit()
        # The steps that must happen once and in order, after the schema
        # script: migration 1 is what 3.7 has (storage/migrations.py).
        from . import migrations                          # noqa: PLC0415
        migrations.run(conn, migrations.INDEX, database="index")
    refresh_statistics(conn, force=unmeasured)
    # The sound of any story whose photograph went while Ninaivu was not
    # looking: deleted by a tool, or erased from the bin by the last run.
    from ..features.stories import storage as stories                                 # noqa: PLC0415
    stories.sweep(conn)
    _READY.add(key)
    return conn


_INDEX_LINE = re.compile(
    r"CREATE INDEX IF NOT EXISTS\s+(\w+)\s+(ON\s+.+?);\s*$", re.IGNORECASE | re.MULTILINE)


def _drop_changed_indexes(conn: sqlite3.Connection, schema: str) -> list[str]:
    """Drop indexes whose stored definition no longer matches *schema*.

    ``CREATE INDEX IF NOT EXISTS`` only ever looks at the name, so an index
    whose columns change between releases would otherwise keep its old shape
    on every database that already had it. Dropped here, it is recreated from
    the current definition straight after. Returns the names dropped.
    """
    def squash(text: str) -> str:
        return re.sub(r"\s+", "", text).lower()

    stored = {row[0]: row[1] or "" for row in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index'")}
    dropped = []
    for name, body in _INDEX_LINE.findall(schema):
        have = stored.get(name)
        if have is None:
            continue
        # sqlite_master keeps the statement as written, minus IF NOT EXISTS.
        if squash(have) != squash(f"CREATE INDEX {name} {body}"):
            conn.execute(f"DROP INDEX {name}")
            dropped.append(name)
    return dropped


def _indexes_without_statistics(conn: sqlite3.Connection) -> list[str]:
    """Indexes on ``assets`` that ANALYZE has not measured, once it has measured
    the table at all (a new, empty library has no statistics and needs none)."""
    try:
        measured = {row[0] for row in conn.execute(
            "SELECT idx FROM sqlite_stat1 WHERE tbl='assets'")}
    except sqlite3.OperationalError:
        return []
    if not measured:
        return []
    return [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='assets' "
        "AND name NOT LIKE 'sqlite_autoindex%'") if row[0] not in measured]


#: How far the library must move from its size at the last ANALYZE before the
#: statistics are worth rebuilding: by half or double, and by at least this many
#: rows, so a household adding a few photos a day never pays for it.
_STATS_MIN_CHANGE = 1000


def refresh_statistics(conn: sqlite3.Connection, *, force: bool = False) -> bool:
    """Run ANALYZE when the library has changed size enough to matter.

    Without statistics SQLite has to guess how selective each column is, and
    it guesses that ``visibility <= ?`` narrows a table a great deal. It holds
    three values. On a large library that guess picks an index that reads every
    row and then sorts them all — seconds per gallery page instead of
    milliseconds. ``analysis_limit`` bounds the work to a sample per index, so
    this stays well under a second at a million rows on SQLite 3.32 and later;
    older builds ignore the pragma and analyse in full.

    Returns whether statistics were rebuilt.
    """
    # Faces are counted too: they are found in a pass after the photographs
    # are indexed, so a library whose statistics were taken with an empty
    # faces table kept them, and the person filter picked the wrong index
    # for as long as the number of photographs stayed put.
    rows = int(conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0])
    faces = int(conn.execute("SELECT COUNT(*) FROM faces").fetchone()[0])
    try:
        last = int(get_meta(conn, "stats_rows", "-1") or -1)
        last_faces = int(get_meta(conn, "stats_faces", "-1") or -1)
    except ValueError:
        last = last_faces = -1
    if not force and last >= 0:
        moved = False
        for now, then in ((rows, last), (faces, max(last_faces, 0))):
            low, high = sorted((now, then))
            if high - low >= _STATS_MIN_CHANGE and high > 2 * low:
                moved = True
        if not moved:
            return False
    with _write_lock:
        conn.execute("PRAGMA analysis_limit=1000")
        conn.execute("ANALYZE")
        # The photographs' table read in full. Sampled, SQLite took a library
        # folder holding 300,000 rows for one holding a thousand, chose the
        # filter index for the gallery, and sorted the whole library for every
        # page: 1.3 seconds for each 25,000 tiles instead of 0.2. In full it
        # is a third of a second at 300,000 rows, and runs only when the
        # library has doubled or halved.
        conn.execute("PRAGMA analysis_limit=0")
        conn.execute("ANALYZE assets")
        set_meta(conn, "stats_rows", str(rows))
        set_meta(conn, "stats_faces", str(faces))
        conn.commit()
    return True


def _enforce_admin_only_kinds(conn: sqlite3.Connection) -> int:
    """Lift any administrator-only kind that is sitting below hidden.

    Run at every start rather than once as a migration. A library indexed
    before this rule existed has audio at family level; so does one whose
    database was restored from an old backup, or copied from another machine.
    A rule that only applied to libraries built after a particular release
    would be a rule with a hole in it.
    """
    if not ADMIN_ONLY_KINDS:
        return 0
    names = ",".join("?" * len(ADMIN_ONLY_KINDS))
    cur = conn.execute(
        f"UPDATE assets SET visibility=2, vis_source='kind' "
        f"WHERE kind IN ({names}) AND visibility < 2", ADMIN_ONLY_KINDS)
    return cur.rowcount or 0


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def fts_enabled(conn: sqlite3.Connection) -> bool:
    return get_meta(conn, "fts", "0") == "1"


# ---------------------------------------------------------------------------
# Asset writes
# ---------------------------------------------------------------------------

#: The id a new row of ``assets`` takes: one past the largest ever used, as
#: AUTOINCREMENT would give, without rebuilding the table. SQLite on its own
#: takes one past the largest *now*, so the next photograph indexed after the
#: newest one was erased took its number, and with it a film's or the job
#: engine's memory of the old one (audit C-22). The largest ever used is kept
#: by the assets_high_water trigger.
NEW_ASSET_ID = ("(SELECT MAX(COALESCE((SELECT MAX(id) FROM assets), 0), "
                "COALESCE((SELECT CAST(value AS INTEGER) FROM meta "
                "WHERE key='assets_high_water'), 0)) + 1)")

_ASSET_FIELDS = (
    "root", "rel_path", "filename", "folder", "ext", "kind", "size", "mtime",
    "captured_at", "date_key", "date_source", "width", "height", "duration",
    "orientation", "camera", "lens", "iso", "f_number", "exposure",
    "focal_length", "gps_lat", "gps_lon", "country", "city",
    "thumb", "blurhash", "color",
    "phash", "dup_group", "sharpness", "brightness", "quality",
    "quality_version",
    "tags", "caption", "nsfw", "nsfw_score",
    "visibility", "vis_source", "rotation", "rot_source",
    "live_video_path", "is_live",
    "ai_version", "indexed_at", "added_at",
)

#: Columns a rescan must never clobber — an admin's visibility decision
#: outlives the file being re-indexed.
#: A rotation somebody set by hand outlives a rescan too, and is restored in
#: :func:`bulk_upsert` rather than listed here — a rescan of a file whose EXIF
#: changed *should* pick the new tag up, so only the manual case is protected.
_PRESERVE_ON_UPSERT = {"root", "rel_path", "visibility", "vis_source", "added_at"}

#: NOT NULL columns need a value even when the probe could not supply one.
_DEFAULTS: dict[str, Any] = {
    "folder": "", "ext": "", "size": 0, "mtime": 0.0, "date_key": "",
    "date_source": "mtime", "orientation": 1, "tags": "[]", "quality": "[]", "quality_version": 0,
    "nsfw": 0, "nsfw_score": 0.0, "ai_version": 0,
    "visibility": 1, "vis_source": "default",
    "rotation": 0, "rot_source": "none",
    "is_live": 0,
}


def _normalise(record: dict[str, Any]) -> dict[str, Any]:
    payload = {k: record.get(k) for k in _ASSET_FIELDS}
    for listy in ("tags", "quality"):
        if isinstance(payload.get(listy), (list, tuple)):
            payload[listy] = json.dumps(list(payload[listy]))
    for key, fallback in _DEFAULTS.items():
        if payload.get(key) is None:
            payload[key] = fallback
    payload["indexed_at"] = payload.get("indexed_at") or time.time()
    payload["added_at"] = payload.get("added_at") or payload["indexed_at"]
    # Whether this record carries a content verdict at all. The scan never
    # works one out — the AI pass does, afterwards — so a record without one
    # must not stand for "not explicit" and unhide what was screened out.
    payload["nsfw_given"] = int(record.get("nsfw") is not None
                                or record.get("nsfw_score") is not None)
    return payload


#: What the upsert does with the descriptive columns on a rescan. An admin's
#: own tags, caption or content flag (``*_source='manual'``) outlive it, the
#: way a manual rotation or date does. And a record that simply has nothing to
#: say about them — the scan writes ``tags=[]`` and no verdict, because the AI
#: pass fills those in later — keeps what the row already had: re-indexing a
#: file used to wipe its tags and caption and clear its explicit-content flag,
#: showing a screened-out photograph to everybody until the re-tag caught up.
#: A value the probe really read (an audio file's own tags, an EXIF
#: description) still replaces an automatic one.
_KEEP_DESCRIBED = {
    "tags": "tags=CASE WHEN assets.tags_source='manual' OR excluded.tags='[]' "
            "THEN assets.tags ELSE excluded.tags END",
    "caption": "caption=CASE WHEN assets.caption_source='manual' "
               "OR excluded.caption IS NULL "
               "THEN assets.caption ELSE excluded.caption END",
    "nsfw": "nsfw=CASE WHEN assets.nsfw_source='manual' OR :nsfw_given=0 "
            "THEN assets.nsfw ELSE excluded.nsfw END",
    "nsfw_score": "nsfw_score=CASE WHEN assets.nsfw_source='manual' "
                  "OR :nsfw_given=0 "
                  "THEN assets.nsfw_score ELSE excluded.nsfw_score END",
}


def upsert_asset(conn: sqlite3.Connection, record: dict[str, Any]) -> int:
    """Insert or update one asset, preserving user data (favorite/rating)."""
    payload = _normalise(record)

    cols = "id, " + ", ".join(_ASSET_FIELDS)
    placeholders = NEW_ASSET_ID + ", " + ", ".join(f":{f}" for f in _ASSET_FIELDS)

    # A rotation somebody set by hand survives a rescan; one Ninaivu worked out
    # for itself does not, because a rescan is exactly when a better answer
    # arrives — a repaired EXIF tag, or a detector that has since improved.
    # Listing these in _PRESERVE_ON_UPSERT would have frozen both cases.
    keep_manual = {
        "rotation": "rotation=CASE WHEN assets.rot_source='manual' "
                    "THEN assets.rotation ELSE excluded.rotation END",
        "rot_source": "rot_source=CASE WHEN assets.rot_source='manual' "
                      "THEN 'manual' ELSE excluded.rot_source END",
        # `indexed_at` is the thumbnail's cache version: it is in the URL every
        # browser in the house was told would never change. A full rescan
        # re-reads and rewrites every file, and stamping a new time on all of
        # them turned the whole library into a cache miss at once — for
        # photographs whose pixels were identical.
        #
        # So the stamp only moves when something about the picture did. Same
        # size, same modified time, same turn, same thumbnail name: the same
        # photograph, keeping the version it had. A manual rotation is excluded
        # deliberately — a scan writes thumbnails from what it detects, so the
        # two can genuinely disagree, and a cache miss is the safe way to be
        # wrong.
        "indexed_at": "indexed_at=CASE WHEN assets.mtime=excluded.mtime "
                      "AND assets.size=excluded.size "
                      "AND assets.rotation=excluded.rotation "
                      "AND assets.rot_source<>'manual' "
                      "AND assets.thumb IS NOT NULL "
                      "AND assets.thumb=excluded.thumb "
                      "THEN assets.indexed_at ELSE excluded.indexed_at END",
    }
    for field in ("captured_at", "date_key", "date_source"):
        keep_manual[field] = (f"{field}=CASE WHEN assets.date_source='manual' "
                              f"THEN assets.{field} ELSE excluded.{field} END")
    keep_manual.update(_KEEP_DESCRIBED)
    updates = ", ".join(
        keep_manual.get(f, f"{f}=excluded.{f}")
        for f in _ASSET_FIELDS if f not in _PRESERVE_ON_UPSERT
    )
    with _write_lock:
        conn.execute(
            f"INSERT INTO assets ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT(root, rel_path) DO UPDATE SET {updates}",
            payload,
        )
        _hide_imported_hidden(conn)
        conn.commit()
    # Not ``cur.lastrowid``: sqlite3 sets it from sqlite3_last_insert_rowid()
    # whatever the statement did, so on the DO UPDATE path it still holds the
    # rowid of the *previous* insert on this connection — a scan run, an audit
    # line, a session — and the caller would go on to use that as the asset.
    # The lookup is one indexed read.
    row = conn.execute(
        "SELECT id FROM assets WHERE root=? AND rel_path=?",
        (payload["root"], payload["rel_path"]),
    ).fetchone()
    return int(row["id"]) if row else 0


def bulk_upsert(conn: sqlite3.Connection, records: Sequence[dict[str, Any]]) -> None:
    if not records:
        return
    rows = [_normalise(record) for record in records]

    cols = "id, " + ", ".join(_ASSET_FIELDS)
    placeholders = NEW_ASSET_ID + ", " + ", ".join(f":{f}" for f in _ASSET_FIELDS)

    # A rotation somebody set by hand survives a rescan; one Ninaivu worked out
    # for itself does not, because a rescan is exactly when a better answer
    # arrives — a repaired EXIF tag, or a detector that has since improved.
    # Listing these in _PRESERVE_ON_UPSERT would have frozen both cases.
    keep_manual = {
        "rotation": "rotation=CASE WHEN assets.rot_source='manual' "
                    "THEN assets.rotation ELSE excluded.rotation END",
        "rot_source": "rot_source=CASE WHEN assets.rot_source='manual' "
                      "THEN 'manual' ELSE excluded.rot_source END",
        # `indexed_at` is the thumbnail's cache version: it is in the URL every
        # browser in the house was told would never change. A full rescan
        # re-reads and rewrites every file, and stamping a new time on all of
        # them turned the whole library into a cache miss at once — for
        # photographs whose pixels were identical.
        #
        # So the stamp only moves when something about the picture did. Same
        # size, same modified time, same turn, same thumbnail name: the same
        # photograph, keeping the version it had. A manual rotation is excluded
        # deliberately — a scan writes thumbnails from what it detects, so the
        # two can genuinely disagree, and a cache miss is the safe way to be
        # wrong.
        "indexed_at": "indexed_at=CASE WHEN assets.mtime=excluded.mtime "
                      "AND assets.size=excluded.size "
                      "AND assets.rotation=excluded.rotation "
                      "AND assets.rot_source<>'manual' "
                      "AND assets.thumb IS NOT NULL "
                      "AND assets.thumb=excluded.thumb "
                      "THEN assets.indexed_at ELSE excluded.indexed_at END",
    }
    for field in ("captured_at", "date_key", "date_source"):
        keep_manual[field] = (f"{field}=CASE WHEN assets.date_source='manual' "
                              f"THEN assets.{field} ELSE excluded.{field} END")
    keep_manual.update(_KEEP_DESCRIBED)
    updates = ", ".join(
        keep_manual.get(f, f"{f}=excluded.{f}")
        for f in _ASSET_FIELDS if f not in _PRESERVE_ON_UPSERT
    )
    with _write_lock:
        conn.executemany(
            f"INSERT INTO assets ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT(root, rel_path) DO UPDATE SET {updates}",
            rows,
        )
        _hide_imported_hidden(conn)
        conn.commit()


def _hide_imported_hidden(conn: sqlite3.Connection) -> None:
    """Hide what an export marked hidden, in the transaction that indexes it.

    The import applies what the export said once the scan announces the files
    indexed (storage/importer.py), and a scan commits a batch at a time long
    before that — or never says so at all when it is stopped. A photograph
    hidden in iCloud was in the family gallery for all of that time. Called
    holding ``_write_lock``, before the commit.
    """
    try:
        conn.execute(
            "UPDATE assets SET visibility=2, vis_source='item' WHERE visibility < 2 "
            "AND id IN (SELECT a.id FROM imports i JOIN assets a "
            "ON a.root = i.root AND a.rel_path = i.rel_path "
            "WHERE i.applied = 0 AND i.state = 'copied' AND i.hidden = 1)")
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        # No import has ever run here, so there is no table to look in.


#: The descriptive columns, each with the column that says who set it.
_DESCRIBED_BY = {"tags": "tags_source", "caption": "caption_source",
                 "nsfw": "nsfw_source", "nsfw_score": "nsfw_source"}


def update_asset(conn: sqlite3.Connection, asset_id: int, **fields: Any) -> None:
    """Write *fields* to one asset.

    Tags, a caption or the content flag written through here are taken as an
    admin's own decision — this is the path the asset editor saves by — and
    marked ``manual`` so no rescan or re-tag takes them back. The AI pass
    writes its verdicts through :func:`store_ai_fields` instead. A caller that
    means otherwise names the source column itself.
    """
    if not fields:
        return
    if isinstance(fields.get("tags"), (list, tuple)):
        fields["tags"] = json.dumps(list(fields["tags"]))
    for field, source in _DESCRIBED_BY.items():
        if field in fields and source not in fields:
            fields[source] = "manual"
    assignments = ", ".join(f"{k}=?" for k in fields)
    with _write_lock:
        conn.execute(
            f"UPDATE assets SET {assignments} WHERE id=?",
            (*fields.values(), asset_id),
        )
        conn.commit()


def store_ai_fields(conn: sqlite3.Connection, asset_id: int, **fields: Any) -> None:
    """Write what the AI pass worked out, leaving an admin's own values alone.

    Tags, caption and the content flag are only written where their source is
    not ``manual``; everything else (``ai_version`` and the like) is written as
    given. The check is in the same statement as the write, so an admin who
    saves a caption while the pass is running is not overwritten a moment
    later by a verdict read before the save.
    """
    if not fields:
        return
    if isinstance(fields.get("tags"), (list, tuple)):
        fields["tags"] = json.dumps(list(fields["tags"]))
    assignments = []
    for key in fields:
        source = _DESCRIBED_BY.get(key)
        if source is None:
            assignments.append(f"{key}=?")
        else:
            assignments.append(f"{key}=CASE WHEN {source}='manual' "
                               f"THEN {key} ELSE ? END")
    with _write_lock:
        # Not onto an item hidden since the pass read its list: a pass runs
        # for hours, and an administrator who hides a passport photograph in
        # the middle of it must not have its reading written a minute later.
        conn.execute(
            f"UPDATE assets SET {', '.join(assignments)} WHERE id=? AND {AI_MAY_READ}",
            (*fields.values(), asset_id),
        )
        conn.commit()


#: Refuse to prune more than this share of a root in one pass unless the
#: caller insists. A genuine tidy-up removes a handful of files; a drive that
#: has half-appeared removes hundreds, and that is the case worth stopping.
PRUNE_CEILING = 0.5

#: …but only once there are enough rows for a proportion to mean anything.
#: "2 of 3 files" is an ordinary afternoon's tidying; "2,000 of 3,000" is a
#: drive that is not really there. Below this the empty-walk and incomplete-walk
#: rules still apply — they are the ones that catch the dangerous cases.
PRUNE_FLOOR = 25


class PruneRefused(Exception):
    """Raised instead of deleting when the evidence for deleting is weak."""

    def __init__(self, reason: str, stale: int, total: int) -> None:
        super().__init__(reason)
        self.reason = reason
        self.stale = stale
        self.total = total


def under_folders(folders: Iterable[str] | None) -> tuple[str, list[str]]:
    """SQL limiting ``rel_path`` to files inside *folders*, and its parameters.

    *folders* are relative to the library folder, "/"-separated. A range on
    ``rel_path`` rather than ``LIKE``, so the ``UNIQUE(root, rel_path)`` index
    answers it and an underscore or a percent sign in a folder's name is only
    a character: "/" sorts just before "0", so ``>= 'a/' AND < 'a0'`` is
    everything below ``a``. No folders, or ``None``: the whole library folder.
    """
    folders = sorted({str(f).strip("/") for f in (folders or ()) if str(f).strip("/")})
    if not folders:
        return "", []
    clauses = " OR ".join("(rel_path >= ? AND rel_path < ?)" for _ in folders)
    params: list[str] = []
    for folder in folders:
        params += [folder + "/", folder + "0"]
    return f" AND ({clauses})", params


def delete_missing(conn: sqlite3.Connection, root: str, present: Iterable[str],
                   *, complete: bool = True, force: bool = False,
                   under: Iterable[str] | None = None) -> list[str]:
    """Drop rows for files that no longer exist. Returns their thumb names.

    "No longer exists" is a claim about the disk, and it is only true if the
    walk that produced *present* actually saw the whole tree. An external drive
    that is unplugged leaves its mountpoint behind as an empty directory, and a
    folder that cannot be listed yields nothing — in both cases every file
    looks deleted. Pruning on that evidence destroys the index, the thumbnails,
    every album's membership and every visibility decision the admin made, and
    the rescan afterwards cannot bring the albums back.

    So this refuses, by raising :class:`PruneRefused`, when:

    * the walk reported itself incomplete (``complete=False``);
    * ``present`` is empty while the database holds rows for this root;
    * the prune would remove more than :data:`PRUNE_CEILING` of a root holding
      at least :data:`PRUNE_FLOOR` files.

    ``force=True`` carries out the deletion anyway, for the caller who has
    established that the files really are gone.

    *under* limits all of it to the files inside those folders (see
    :func:`under_folders`), for a scan that read only them: *present* is then
    what it found there, and the rules above are judged on those rows alone.
    """
    present_set = set(present)
    # Let go of on purpose, and kept on the other computer: not missing.
    from .locations import let_go_paths                   # noqa: PLC0415
    present_set.update(let_go_paths(conn, root))
    within, params = under_folders(under)
    rows = conn.execute(
        "SELECT id, rel_path, thumb FROM assets WHERE root=?" + within, (root, *params)
    ).fetchall()
    stale = [r for r in rows if r["rel_path"] not in present_set]
    if not stale:
        return []

    if not force:
        total = len(rows)
        # Before any of the proportions below: a folder that is not there is
        # not a folder whose photographs were deleted. Every rule after this
        # one is a judgement about how much of a walk went missing, and none
        # of them should ever have to be made about a drive that is simply
        # unplugged. See ninaivu/storage/roots.py.
        if not roots_kit.available(root):
            raise PruneRefused(
                f"{root} is not connected, so nothing can be said about what "
                f"is in it", len(stale), total)
        if not complete:
            raise PruneRefused(
                "the scan could not read part of this folder, so files it did "
                "not see may still be there", len(stale), total)
        if not present_set:
            raise PruneRefused(
                "the folder read as empty — if this is a removable drive, it "
                "is probably not plugged in", len(stale), total)
        if total >= PRUNE_FLOOR and len(stale) / total > PRUNE_CEILING:
            raise PruneRefused(
                f"{len(stale)} of {total} files would be removed at once, "
                f"which looks like a disappearing drive rather than a tidy-up",
                len(stale), total)

    with _write_lock:
        conn.executemany(
            "DELETE FROM assets WHERE id=?", [(r["id"],) for r in stale]
        )
        conn.commit()
    return [r["thumb"] for r in stale if r["thumb"]]


def start_scan_run(conn: sqlite3.Connection, root: str) -> int:
    """Open a row for a scan that is starting. Returns its id."""
    with _write_lock:
        cur = conn.execute(
            "INSERT INTO scan_runs(root, started_at) VALUES(?,?)",
            (str(root), time.time()))
        conn.commit()
        return int(cur.lastrowid)


def finish_scan_run(conn: sqlite3.Connection, run_id: int, *,
                    status: str = "done", **counts: Any) -> None:
    """Close it, with what it did and how it ended.

    A run with no ``ended_at`` never finished — which is exactly what the next
    start needs to know. *status* is how it ended: ``done``, ``error``, or
    ``idle`` for a scan that was cancelled or stood down for the archive. It
    was not recorded at all until now, so every row in the history said
    ``running``, including the ones that had plainly finished — which made the
    one row that really was still running impossible to pick out.
    """
    fields = {k: int(v or 0) for k, v in counts.items()
              if k in {"total", "processed", "added", "updated", "removed", "errors"}}
    sets = "".join(f", {k}=:{k}" for k in fields)
    with _write_lock:
        conn.execute(
            f"UPDATE scan_runs SET ended_at=:ended, status=:status{sets} "
            f"WHERE id=:id",
            {"ended": time.time(), "status": str(status), "id": int(run_id),
             **fields})
        conn.commit()


def last_scan(conn: sqlite3.Connection, root: str) -> dict[str, Any] | None:
    """The most recent scan of this folder, however it ended — or did not."""
    row = conn.execute(
        "SELECT * FROM scan_runs WHERE root=? ORDER BY started_at DESC, id DESC "
        "LIMIT 1", (str(root),)).fetchone()
    return dict(row) if row else None


def scan_history(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    """Recent scans, newest first — for the console."""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM scan_runs ORDER BY started_at DESC LIMIT ?", (int(limit),))]


def existing_signatures(conn: sqlite3.Connection, root: str,
                        under: Iterable[str] | None = None,
                        ) -> dict[str, tuple[float, int, int]]:
    """``rel_path -> (mtime, size, ai_version)`` for change detection, for the
    whole library folder or only the files *under* some folders in it."""
    within, params = under_folders(under)
    # Plain tuples: a named row made for each of 200,000 files, only to be
    # taken apart again, was most of the time this took.
    cursor = conn.cursor()
    cursor.row_factory = None
    return {rel: (mtime, size, ai_version) for rel, mtime, size, ai_version in cursor.execute(
        "SELECT rel_path, mtime, size, ai_version FROM assets WHERE root=?" + within,
        (root, *params))}


def live_paths(conn: sqlite3.Connection, root: str,
               under: Iterable[str] | None = None) -> set[str]:
    """Every rel_path in *root*, or *under* some of its folders, that is not
    in the bin."""
    within, params = under_folders(under)
    return {r[0] for r in conn.execute(
        "SELECT rel_path FROM assets WHERE root=? AND trashed=0" + within,
        (root, *params))}


def respell_asset(conn: sqlite3.Connection, root: str, old_rel: str,
                  new_rel: str) -> str | None:
    """Give the row indexed as *old_rel* the spelling the disk uses now.

    The same file, its name in another Unicode form: indexed on Windows as
    "VADIVÉL" with a composed É, listed on a Mac with the E and its accent
    apart, and not openable there by the old spelling. Nothing about the file
    changed, so the row, and everything people did with it, stays.

    If *new_rel* was indexed as well — the first scan on the new computer took
    it for a new file — that row is the duplicate. Its album places,
    favourites and ratings move to the older row where it has none of its own,
    and the duplicate goes. Its faces, vectors and storage-check result go
    with it; the older row has its own. Returns the duplicate's thumbnail
    name, for the caller to remove, or None.
    """
    folder, _, filename = new_rel.rpartition("/")
    with _write_lock:
        old = conn.execute("SELECT id, thumb FROM assets WHERE root=? AND rel_path=?",
                           (root, old_rel)).fetchone()
        if old is None:
            return None
        dup = conn.execute("SELECT id, thumb FROM assets WHERE root=? AND rel_path=?",
                           (root, new_rel)).fetchone()
        gone = None
        if dup is not None:
            # Stories and capsule places too: the duplicate's would otherwise
            # go with its row (seen while fixing nested library folders).
            present = _tables(conn)
            for table in ("album_items", "user_assets", "stories", "capsule_items"):
                if table in present:
                    conn.execute(f"UPDATE OR IGNORE {table} SET asset_id=? WHERE asset_id=?",
                                 (old["id"], dup["id"]))
            conn.execute("DELETE FROM assets WHERE id=?", (dup["id"],))
            if dup["thumb"] and dup["thumb"] != old["thumb"]:
                gone = dup["thumb"]
        conn.execute("UPDATE assets SET rel_path=?, filename=?, folder=? WHERE id=?",
                     (new_rel, filename, folder, old["id"]))
        try:
            conn.execute("UPDATE bitrot_records SET rel_path=? WHERE root=? AND rel_path=?",
                         (new_rel, root, old_rel))
        except sqlite3.OperationalError:
            pass                      # never had a storage check
        # The cloud backup keeps its own record by path. One copy of the file
        # in Drive is the aim: keep the record that has already been sent, or
        # the older one, and drop the other.
        try:
            records = {r["rel_path"]: r for r in conn.execute(
                "SELECT id, rel_path, state FROM cloud_uploads "
                "WHERE root=? AND rel_path IN (?, ?)", (root, old_rel, new_rel))}
        except sqlite3.OperationalError:
            records = {}              # never backed up to the cloud
        if old_rel in records:
            keep, drop = records[old_rel], records.get(new_rel)
            if drop is not None and drop["state"] == "done" and keep["state"] != "done":
                keep, drop = drop, keep
            if drop is not None:
                conn.execute("DELETE FROM cloud_uploads WHERE id=?", (drop["id"],))
            conn.execute("UPDATE cloud_uploads SET rel_path=?, filename=? WHERE id=?",
                         (new_rel, filename, keep["id"]))
        conn.commit()
    return gone


#: How long the total of a word search is kept (query_assets).
WORDS_TOTAL_SECONDS = 30.0


# ---------------------------------------------------------------------------
# Per-user state
# ---------------------------------------------------------------------------

def set_user_asset(conn: sqlite3.Connection, user_id: int, asset_id: int,
                   **fields: Any) -> None:
    """Set this viewer's favourite/rating for one asset."""
    allowed = {k: int(v) for k, v in fields.items() if k in ("favorite", "rating")}
    if not allowed or not user_id:
        return
    cols = ", ".join(allowed)
    placeholders = ", ".join("?" * len(allowed))
    updates = ", ".join(f"{k}=excluded.{k}" for k in allowed)
    with _write_lock:
        conn.execute(
            f"INSERT INTO user_assets(user_id, asset_id, {cols}) "
            f"VALUES(?,?,{placeholders}) "
            f"ON CONFLICT(user_id, asset_id) DO UPDATE SET {updates}",
            (user_id, asset_id, *allowed.values()),
        )
        conn.execute(
            "DELETE FROM user_assets WHERE user_id=? AND asset_id=? "
            "AND favorite=0 AND rating=0",
            (user_id, asset_id),
        )
        conn.commit()


def bulk_set_user_asset(conn: sqlite3.Connection, user_id: int,
                        asset_ids: Sequence[int], **fields: Any) -> int:
    allowed = {k: int(v) for k, v in fields.items() if k in ("favorite", "rating")}
    if not allowed or not user_id or not asset_ids:
        return 0
    cols = ", ".join(allowed)
    placeholders = ", ".join("?" * len(allowed))
    updates = ", ".join(f"{k}=excluded.{k}" for k in allowed)
    with _write_lock:
        conn.executemany(
            f"INSERT INTO user_assets(user_id, asset_id, {cols}) "
            f"VALUES(?,?,{placeholders}) "
            f"ON CONFLICT(user_id, asset_id) DO UPDATE SET {updates}",
            [(user_id, aid, *allowed.values()) for aid in asset_ids],
        )
        # Only the rows just written can have become empty; the whole of the
        # viewer's rows was swept before, for every favourite toggled.
        conn.executemany(
            "DELETE FROM user_assets WHERE user_id=? AND asset_id=? "
            "AND favorite=0 AND rating=0",
            [(user_id, aid) for aid in asset_ids],
        )
        conn.commit()
    return len(asset_ids)

def library_stats(conn: sqlite3.Connection, roots: Sequence[str] | str | None = None, *,
                  viewer_id: int = 0, max_visibility: int = 1,
                  scope: str | None = None, gate: Any = None) -> dict[str, Any]:
    """Counts for the sidebar, limited to what this viewer may see: *gate*'s
    (a ``server.visibility.Viewer``) when it is given."""
    if gate is not None:
        viewer_id, max_visibility = gate.viewer_id, gate.max_visibility
        gate_sql, gate_params = gate.sql("a")
        where = [gate_sql, "a.trashed = 0"]
        params: list[Any] = list(gate_params)
        # The caller's own "Only to me" count is by folder and sub-folder.
        roots, scope = gate.roots, gate.scope
        roots_sql, roots_params = roots_clause("a", roots)
        if int(max_visibility) < 2:
            where.append("a.nsfw = 0")
    else:
        # One alias (`a`) everywhere, so the scope clause can be reused verbatim.
        roots_sql, roots_params = roots_clause("a", roots if roots is not None else [])
        where = [roots_sql, "a.trashed = 0", visibility_clause("a", max_visibility)]
        params = [*roots_params, int(max_visibility)]
        # Flagged content is admin-only in every listing (query_assets, and the
        # API's per-item guard); counting it for anybody else said how much of it
        # there was, and in which years and folders.
        if int(max_visibility) < 2:
            where.append("a.nsfw = 0")
        scope_sql, scope_params = scope_clause("a", scope)
        if scope_sql:
            where.append(scope_sql)
            params.extend(scope_params)
    where_sql = " AND ".join(where)

    def whole_library() -> dict[str, Any]:
        row = conn.execute(
            # A live photo counts once, as the photograph it is. Its clip is the
            # other half of that same thing, not a video of its own — counting it
            # again put 5,674 three-second fragments into the video total on the
            # library this was found on, twenty-eight per cent of it.
            # A RAW shot with a JPEG is the same photograph again, so it is
            # not counted either; its bytes are, because they are on disk.
            f"SELECT COALESCE(SUM(a.live_clip=0 AND a.raw_twin=0), 0) n, "
            f"COALESCE(SUM(a.size),0) bytes, "
            f"SUM(a.kind='picture' AND a.raw_twin=0) pictures, "
            f"SUM(a.kind='video' AND a.live_clip=0) videos, "
            f"SUM(a.is_live=1) live, "
            f"SUM(a.kind='audio') audio, SUM(a.nsfw=1) nsfw, "
            f"SUM(a.visibility=0) public, SUM(a.visibility=2) hidden, "
            f"MIN(a.date_key) first_date, MAX(a.date_key) last_date "
            f"FROM assets a WHERE {where_sql}",
            params,
        ).fetchone()
        dupes = conn.execute(
            f"SELECT COUNT(DISTINCT a.dup_group) n FROM assets a "
            f"WHERE {where_sql} AND a.dup_group IS NOT NULL "
            f"AND a.live_clip=0 AND a.raw_twin=0", params,
        ).fetchone()["n"]
        return {**dict(row), "dupes": dupes}

    # The same for everybody with this viewer's limits, and unchanged until the
    # library is: remembered. The two below are not — one is this viewer's
    # own, the other is written by the tagger, not by a scan.
    row = cached_aggregate(conn, ("library_stats", where_sql, tuple(params)),
                           whole_library)
    dupes = row["dupes"]

    # Written by the tagger, which the library's number does not follow, so
    # kept until the analysed items' own number moves. Counted within what
    # this viewer may see, like every other number here: it was counted by
    # folder alone, so a member was told how many items were analysed among
    # the private, hidden and flagged ones too (decided 9 November 2026).
    embedded = cached_aggregate(
        conn, ("embedded", where_sql, tuple(params)),
        lambda: conn.execute(
            f"SELECT COUNT(*) n FROM embeddings e JOIN assets a ON a.id = e.asset_id "
            f"WHERE {where_sql}", params,
        ).fetchone()["n"], also=(EMBEDDINGS_GENERATION_KEY,))

    # From this person's favourites outward, not from the library inward.
    # SQLite chose to walk every visible item and look each one up among the
    # favourites — sixty milliseconds on 100,000 items for a count that is
    # usually a few hundred. CROSS JOIN is SQLite's way of fixing the order.
    favorites = conn.execute(
        f"SELECT COUNT(*) n FROM user_assets ua "
        f"CROSS JOIN assets a ON a.id = ua.asset_id "
        f"WHERE ua.user_id = ? AND ua.favorite = 1 AND {where_sql}",
        (int(viewer_id), *params),
    ).fetchone()["n"]

    return {
        "count": row["n"] or 0,
        "bytes": row["bytes"] or 0,
        "pictures": row["pictures"] or 0,
        "videos": row["videos"] or 0,
        #: Photographs that came with a clip. Not added to the totals — each
        #: one is already counted among the pictures — it is how many of them
        #: move when you hold them.
        "live": row["live"] or 0,
        "audio": row["audio"] or 0,
        "favorites": favorites or 0,
        "nsfw": row["nsfw"] or 0,
        "public": row["public"] or 0,
        "hidden": row["hidden"] or 0,
        "duplicate_groups": dupes or 0,
        "embedded": embedded or 0,
        # This viewer's own "Only to me" items. Nobody else's: the number is
        # theirs alone, as their favourites are.
        "private": private_count(conn, roots, viewer_id, scope),
        "first_date": row["first_date"] or "",
        "last_date": row["last_date"] or "",
    }


def facets(conn: sqlite3.Connection, roots: Sequence[str] | str | None = None,
           limit: int = 40, *, max_visibility: int = 1, scope: str | None = None,
           gate: Any = None) -> dict[str, Any]:
    """Tag cloud, folder list, camera list and per-year counts.

    Everything here leaks information about the library, so it is filtered by
    the same visibility and scope rules as the gallery itself: those of
    *gate* (a ``server.visibility.Viewer``) when it is given.
    """
    if gate is not None:
        gate_sql, base = gate.sql("")
        max_visibility = gate.max_visibility
        guard = gate_sql + " AND trashed=0 AND live_clip=0 AND raw_twin=0"
        if int(max_visibility) < 2:
            guard += " AND nsfw=0"
    else:
        roots_sql, roots_params = roots_clause("assets", roots if roots is not None else [])
        scope_sql, scope_params = scope_clause("assets", scope)
        guard = (roots_sql.replace("assets.", "") + " AND trashed=0 AND "
                 + "live_clip=0 AND raw_twin=0 AND "
                 + visibility_clause("", max_visibility))
        # As in library_stats: below an admin, flagged content is not counted
        # in the folder, camera and year lists either — only the tags left it out.
        if int(max_visibility) < 2:
            guard += " AND nsfw=0"
        if scope_sql:
            guard += " AND " + scope_sql.replace("assets.", "")
        base = [*roots_params, int(max_visibility), *scope_params]

    # The tag cloud reads tags, which have a counter of their own. Worked out
    # once at the longest length anybody asks for and cut to each caller's:
    # the sidebar's forty and the search box's two hundred were two reads of
    # every row's tags, and the first letter typed after a start (or after
    # the tagger moved anything) waited 1.2 s on 208,000 items for the second.
    kept = max(int(limit), FACETS_KEPT)
    found = cached_aggregate(conn, ("facets", guard, tuple(base), kept),
                             lambda: _facets(conn, guard, base, kept),
                             also=(TAGS_GENERATION_KEY,))
    if kept == int(limit):
        return found
    return {key: (value if key == "years" else value[:max(0, int(limit))])
            for key, value in found.items()}


#: The most facets of each kind anybody asks for (/api/suggest).
FACETS_KEPT = 200


def _facets(conn: sqlite3.Connection, guard: str, base: list[Any],
            limit: int) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for (raw,) in conn.execute(
        f"SELECT tags FROM assets WHERE {guard} AND nsfw=0 AND tags != '[]'", base
    ):
        try:
            for tag in json.loads(raw or "[]"):
                counts[tag] = counts.get(tag, 0) + 1
        except (TypeError, ValueError):
            continue
    top_tags = sorted(counts.items(), key=lambda kv: -kv[1])[:limit]

    # Folders, cameras and years in one pass over the library rather than
    # three: grouped by all three at once, then summed each way here. The
    # groups number in the thousands where the rows number in the hundreds
    # of thousands, and it was the three reads of every row that cost.
    folders: dict[str, int] = {}
    cameras: dict[str, int] = {}
    years: dict[str, int] = {}
    for folder, camera, year, n in conn.execute(
        f"SELECT folder, camera, substr(date_key,1,4) y, COUNT(*) n FROM assets "
        f"WHERE {guard} GROUP BY folder, camera, y", base,
    ):
        folders[folder] = folders.get(folder, 0) + n
        if camera:
            cameras[camera] = cameras.get(camera, 0) + n
        if year:
            years[year] = years.get(year, 0) + n

    def top(counted: dict[str, int]) -> list[tuple[str, int]]:
        return sorted(counted.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]

    return {
        "tags": [{"name": t, "count": c} for t, c in top_tags],
        "folders": [{"name": f, "count": n} for f, n in top(folders)],
        "cameras": [{"name": c, "count": n} for c, n in top(cameras)],
        "years": [{"year": y, "count": n}
                  for y, n in sorted(years.items(), reverse=True)],
    }


def timeline(conn: sqlite3.Connection, roots: Sequence[str] | str | None = None, *,
             max_visibility: int = 1, scope: str | None = None,
             gate: Any = None) -> list[dict[str, Any]]:
    """How many items each day holds, for the caller's limits: *gate*'s (a
    ``server.visibility.Viewer``) when it is given."""
    if gate is not None:
        gate_sql, gate_params = gate.sql("")
        guard = (gate_sql + " AND trashed=0 AND nsfw=0 AND live_clip=0 AND raw_twin=0"
                 " AND date_key != ''")
        params = tuple(gate_params)
    else:
        roots_sql, roots_params = roots_clause("assets", roots if roots is not None else [])
        scope_sql, scope_params = scope_clause("assets", scope)
        guard = (roots_sql.replace("assets.", "")
                 + " AND trashed=0 AND nsfw=0 AND live_clip=0 AND raw_twin=0 AND "
                 + visibility_clause("", max_visibility) + " AND date_key != ''")
        if scope_sql:
            guard += " AND " + scope_sql.replace("assets.", "")
        params = (*roots_params, int(max_visibility), *scope_params)

    def days() -> list[dict[str, Any]]:
        rows = conn.execute(
            f"SELECT date_key, COUNT(*) n FROM assets WHERE {guard} "
            f"GROUP BY date_key ORDER BY date_key DESC", params,
        ).fetchall()
        return [{"date": r["date_key"], "count": r["n"]} for r in rows]

    return cached_aggregate(conn, ("timeline", guard, params), days)


def picture_years(conn: sqlite3.Connection, roots: Sequence[str] | str | None = None, *,
                  max_visibility: int = 1,
                  scope: str | None = None, gate: Any = None) -> list[dict[str, Any]]:
    """How many photographs this viewer can see in each year, newest first.

    The same limits as :func:`timeline`, for photographs only: what the
    ready-made books (features/books/routes.py) offer a "best of the year" for.
    *gate*, a ``server.visibility.Viewer``, is the caller's limits when given.
    """
    if gate is not None:
        gate_sql, gate_params = gate.sql("")
        guard = (gate_sql + " AND trashed=0 AND nsfw=0 AND live_clip=0 AND raw_twin=0"
                 " AND kind='picture' AND date_key != ''")
        params = tuple(gate_params)
    else:
        roots_sql, roots_params = roots_clause("assets", roots if roots is not None else [])
        scope_sql, scope_params = scope_clause("assets", scope)
        guard = (roots_sql.replace("assets.", "")
                 + " AND trashed=0 AND nsfw=0 AND live_clip=0 AND raw_twin=0 AND "
                 + "kind='picture' AND "
                 + visibility_clause("", max_visibility) + " AND date_key != ''")
        if scope_sql:
            guard += " AND " + scope_sql.replace("assets.", "")
        params = (*roots_params, int(max_visibility), *scope_params)

    def years() -> list[dict[str, Any]]:
        rows = conn.execute(
            f"SELECT substr(date_key, 1, 4) y, COUNT(*) n FROM assets WHERE {guard} "
            f"GROUP BY y ORDER BY y DESC", params,
        ).fetchall()
        return [{"year": int(r["y"]), "count": r["n"]} for r in rows
                if str(r["y"]).isdigit()]

    return cached_aggregate(conn, ("picture_years", guard, params), years)



# The gallery's queries (db_query.py) first: the sections below import
# their clauses from db at their own import.
from .db_query import (  # noqa: E402,F401 - re-exported
    _also_said,
    _escape_fts,
    _fts_phrase,
    _json_value,
    _like_escape,
    _page_in_one_step,
    _random_order,
    _SHUFFLE_PRIME,
    _SORTS,
    _story_words,
    ADMIN_ONLY_KINDS,
    BRIEF_COLUMNS,
    get_asset,
    get_asset_brief,
    hidden_from,
    kind_guard,
    private_owner_clause,
    query_assets,
    roots_clause,
    row_to_dict,
    scope_clause,
    visibility_clause,
    visible_to,
)

# Search vectors live in embeddings.py; these names are part of db's interface.
from .embeddings import (  # noqa: E402,F401 - re-exported
    embedding_store, forget_embeddings, load_embeddings, store_embedding,
    visible_embedding_ids,
)


# The sections that live in their own files; these names are part of db's
# interface, and db.<name> is where everybody reads them.
from .db_albums import (  # noqa: E402,F401 - re-exported
    album_add,
    album_asset_ids,
    album_date,
    album_owner,
    album_remove,
    best_of,
    create_album,
    delete_album,
    duplicate_groups,
    earliest_date,
    get_album,
    list_albums,
    list_occasions,
    occasion_inputs,
    rebuild_occasions,
    update_album,
)
from .db_memories import (  # noqa: E402,F401 - re-exported
    PICK_BRIGHTNESS,
    PICK_FORGOTTEN_YEARS,
    PICK_MIN_SHARPNESS,
    PICK_WEIGHTS,
    _ON_DAY_SQL,
    _YEAR_SQL,
    _pick_score,
    candidates_for_the_day,
    pick_for_the_day,
    query_geo_points,
    query_on_this_day,
)
from .db_shares import (  # noqa: E402,F401 - re-exported
    create_share,
    delete_share,
    get_share,
    increment_share_views,
    list_shares,
)
from .db_bitrot import (  # noqa: E402,F401 - re-exported
    BITROT_STATES,
    current_bitrot_issues,
    get_bitrot_summary,
    last_bitrot_fingerprint,
    prune_bitrot_records,
    record_bitrot_check,
    record_bitrot_checks,
)
from .db_visibility import (  # noqa: E402,F401 - re-exported
    AI_MAY_READ,
    PRIVATE_SOURCE,
    REVEALING_SOURCE,
    SCREEN_SOURCE,
    UNDO_HISTORY,
    _companion,
    _forget_rows,
    _kind_floor,
    _refollow_folder_rules,
    _scope_sql,
    _snapshot_folder,
    _trim_undo_history,
    _without_rule,
    ai_may_read,
    apply_folder_rules_to_new,
    clear_folder_rule,
    clear_private,
    companion_map,
    flag_explicit_scores,
    flag_revealing,
    folder_counts,
    folder_rule_for,
    folder_rules,
    forget_ai_reading,
    hide_screens,
    live_clip_of,
    preview_clear_folder_rule,
    preview_folder_visibility,
    private_count,
    raw_twin_of,
    restore_screens,
    screen_clause,
    screens_not_explicit,
    search_shield_clause,
    set_folder_visibility,
    set_private,
    set_visibility,
    store_revealing_scores,
    store_screen_scores,
    undo_batches,
    undo_visibility_batch,
    unflag_explicit_scores,
    unflag_revealing,
    visibility_for_folder,
    with_companions,
    with_live_clips,
)
from .db_faces import (  # noqa: E402,F401 - re-exported
    CARRY_IOU,
    CARRY_IOU_CENTRED,
    _FACE_EDITS,
    _IGNORED,
    _box_iou,
    _box_of,
    _centre_inside,
    _face_counts,
    _face_visibility_sql,
    _faces_changed,
    _list_people,
    _pair_boxes,
    _person_cluster_dict,
    _turn_point,
    assets_needing_faces,
    cluster_face_ids,
    count_assets_needing_faces,
    count_hidden_faces,
    count_unnamed_clusters,
    create_or_update_person_cluster,
    create_person_cluster,
    delete_person_cluster,
    face_edits,
    face_model_in_use,
    face_schema_version,
    face_stats,
    faces_for_asset,
    faces_not_carried,
    get_face,
    get_person_cluster,
    hidden_face_ids,
    hide_cluster,
    hide_faces,
    list_people,
    list_people_clusters,
    list_unnamed_clusters,
    load_faces,
    mark_faces_scanned,
    mark_large_files_reviewed,
    merge_people,
    person_face_ids,
    reject_face_for_person,
    rejections,
    rename_person_cluster,
    replace_asset_faces,
    reset_face_pass,
    save_cluster_keys,
    save_person_centroid,
    set_face_person,
    set_faces_person,
    show_cluster,
    turn_faces,
)
