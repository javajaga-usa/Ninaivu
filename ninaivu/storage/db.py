"""SQLite index for Media Vault.

Replaces the old ``metadata.json`` blob. Everything the UI needs to draw a
page comes from one indexed query, and full-text search runs through FTS5
when the local SQLite build has it (with a LIKE fallback when it does not).
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import roots as roots_kit

SCHEMA_VERSION = 6

_local = threading.local()
_write_lock = threading.Lock()


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


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Return this thread's connection to ``db_path``, creating it if needed."""
    key = str(db_path)
    cache: dict[str, sqlite3.Connection] = getattr(_local, "conns", None) or {}
    conn = cache.get(key)
    if conn is not None:
        return conn

    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(key, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
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
    -- 0 public (guests too) · 1 family · 2 hidden (admins only)
    visibility      INTEGER NOT NULL DEFAULT 1,
    vis_source      TEXT NOT NULL DEFAULT 'default',
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
    trashed         INTEGER NOT NULL DEFAULT 0,
    ai_version      INTEGER NOT NULL DEFAULT 0,
    face_version    INTEGER NOT NULL DEFAULT 0,
    indexed_at      REAL NOT NULL DEFAULT 0,
    -- Set the moment an admin says "keep" on the largest-files screen. NULL
    -- forever means nobody has looked at this file there yet -- it is not a
    -- judgement about the file, only about whether it still needs asking
    -- about, so nothing else in Ninaivu ever reads or changes it.
    large_file_reviewed_at REAL,
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
-- is read from the table, and a deep page takes seconds.
CREATE INDEX IF NOT EXISTS idx_assets_filter   ON assets(root, trashed, nsfw, visibility, kind, date_key);
CREATE INDEX IF NOT EXISTS idx_assets_gallery  ON assets(root, COALESCE(captured_at, mtime) DESC, id DESC, trashed, nsfw, visibility, kind, date_key);
-- The name and size sorts, the same shape: in order, with the filter columns
-- carried so skipping hidden rows reads no table pages. Expressions and
-- collation must match `_SORTS`.
CREATE INDEX IF NOT EXISTS idx_assets_name     ON assets(root, filename COLLATE NOCASE, id, trashed, nsfw, visibility, kind, date_key);
CREATE INDEX IF NOT EXISTS idx_assets_size     ON assets(root, size, id, trashed, nsfw, visibility, kind, date_key);
CREATE INDEX IF NOT EXISTS idx_assets_date     ON assets(root, date_key);
CREATE INDEX IF NOT EXISTS idx_assets_kind     ON assets(root, kind);
CREATE INDEX IF NOT EXISTS idx_assets_vis      ON assets(root, visibility);
CREATE INDEX IF NOT EXISTS idx_assets_phash    ON assets(phash);
CREATE INDEX IF NOT EXISTS idx_assets_dupgroup ON assets(dup_group);
CREATE INDEX IF NOT EXISTS idx_assets_folder   ON assets(root, folder);
CREATE INDEX IF NOT EXISTS idx_assets_aiver    ON assets(ai_version);
CREATE INDEX IF NOT EXISTS idx_assets_live     ON assets(root, is_live);
CREATE INDEX IF NOT EXISTS idx_assets_gps      ON assets(root, gps_lat, gps_lon);
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
        "visibility": "INTEGER NOT NULL DEFAULT 1",
        "vis_source": "TEXT NOT NULL DEFAULT 'default'",
        "rotation": "INTEGER NOT NULL DEFAULT 0",
        "rot_source": "TEXT NOT NULL DEFAULT 'none'",
        "live_video_path": "TEXT",
        "is_live": "INTEGER NOT NULL DEFAULT 0",
        "live_clip": "INTEGER NOT NULL DEFAULT 0",
        "trashed": "INTEGER NOT NULL DEFAULT 0",
        "ai_version": "INTEGER NOT NULL DEFAULT 0",
        "face_version": "INTEGER NOT NULL DEFAULT 0",
        "indexed_at": "REAL NOT NULL DEFAULT 0",
        "large_file_reviewed_at": "REAL",
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
    if _restore_user_assets_cascade(conn):
        healed.setdefault("user_assets", []).append("foreign keys")
    return healed


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


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an older database up to :data:`SCHEMA_VERSION` in place."""
    have = columns(conn, "assets")
    if not have:
        return

    # v3 → v4: visibility levels, and per-user favourites/ratings.
    if "visibility" not in have:
        conn.execute("ALTER TABLE assets ADD COLUMN visibility INTEGER NOT NULL DEFAULT 1")
        conn.execute("ALTER TABLE assets ADD COLUMN vis_source TEXT NOT NULL DEFAULT 'default'")

    if "library" not in columns(conn, "users") and "users" in _tables(conn):
        conn.execute("ALTER TABLE users ADD COLUMN library TEXT")

    # v5 -> v6: live photos, location tags
    if "is_live" not in have:
        conn.execute("ALTER TABLE assets ADD COLUMN live_video_path TEXT")
        conn.execute("ALTER TABLE assets ADD COLUMN is_live INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE assets ADD COLUMN country TEXT")
        conn.execute("ALTER TABLE assets ADD COLUMN city TEXT")

    # `live_clip` is not here: it arrived after v6 shipped, so it belongs in
    # LATE_COLUMNS, which runs on every start rather than only on a version
    # bump. Adding it here as well would do nothing for the databases that
    # need it — theirs already say version 6, so this function never runs.

    if "favorite" in have or "rating" in have:
        # Carry existing stars over to the first admin, then retire the
        # asset-level columns so a per-user join can own those names.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_assets (
                user_id  INTEGER NOT NULL,
                asset_id INTEGER NOT NULL,
                favorite INTEGER NOT NULL DEFAULT 0,
                rating   INTEGER NOT NULL DEFAULT 0,
                seen_at  REAL,
                PRIMARY KEY (user_id, asset_id)
            )""")
        owner = conn.execute(
            "SELECT id FROM users WHERE role='admin' AND active=1 ORDER BY id LIMIT 1"
        ).fetchone() if "users" in _tables(conn) else None
        if owner:
            conn.execute(
                "INSERT OR IGNORE INTO user_assets(user_id, asset_id, favorite, rating) "
                "SELECT ?, id, favorite, rating FROM assets "
                "WHERE favorite = 1 OR rating > 0",
                (int(owner["id"]),),
            )
            # Only retire the old columns once their contents are somewhere
            # else. With no admin to own them the copy above does not run, so
            # dropping here would destroy every favourite and star in the
            # library. Leaving the columns in place means this branch — which
            # is guarded on their presence, not on schema_version — runs again
            # on the next start, by which time an admin exists.
            for column in ("favorite", "rating"):
                if column in have:
                    try:
                        conn.execute(f"ALTER TABLE assets DROP COLUMN {column}")
                    except sqlite3.OperationalError:
                        # SQLite < 3.35 cannot drop columns; leave them unused.
                        pass
    conn.commit()


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
        return False
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
GENERATION_COLUMNS = ("root", "rel_path", "folder", "kind", "size", "visibility",
                      "trashed", "nsfw", "date_key", "tags", "camera",
                      "dup_group", "is_live", "live_clip")


def _generation_schema() -> str:
    bump = (f"UPDATE meta SET value = CAST(value AS INTEGER) + 1 "
            f"WHERE key = '{GENERATION_KEY}';")
    # Remade on every start rather than IF NOT EXISTS, so a change to the
    # column list reaches a database that already has the old triggers.
    return f"""
        INSERT OR IGNORE INTO meta(key, value)
            VALUES ('{GENERATION_KEY}', CAST(abs(random() % 1000000000000) AS TEXT));
        DROP TRIGGER IF EXISTS assets_generation_insert;
        DROP TRIGGER IF EXISTS assets_generation_delete;
        DROP TRIGGER IF EXISTS assets_generation_update;
        CREATE TRIGGER assets_generation_insert AFTER INSERT ON assets
            BEGIN {bump} END;
        CREATE TRIGGER assets_generation_delete AFTER DELETE ON assets
            BEGIN {bump} END;
        CREATE TRIGGER assets_generation_update AFTER UPDATE OF
            {", ".join(GENERATION_COLUMNS)} ON assets
            BEGIN {bump} END;
    """


def library_generation(conn: sqlite3.Connection) -> int | None:
    """The number the triggers keep, or None where there is none to trust.

    It starts at a random value rather than zero, so two databases — a test's
    and the next test's, or a library and a restored copy of another — never
    agree on it by coincidence.
    """
    try:
        row = conn.execute("SELECT value FROM meta WHERE key=?",
                           (GENERATION_KEY,)).fetchone()
    except sqlite3.Error:
        return None
    try:
        return int(row[0]) if row is not None else None
    except (TypeError, ValueError):
        return None


_AGGREGATES: dict[tuple, tuple[int, Any]] = {}
_aggregates_lock = threading.Lock()
#: Enough for every viewer's ceiling and folder in a household several times
#: over; the least recently asked-for goes first.
_AGGREGATES_MAX = 256


def cached_aggregate(conn: sqlite3.Connection, key: tuple, compute):
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
    """
    import copy

    generation = library_generation(conn)
    if generation is None:
        return compute()
    try:
        path = conn.execute("PRAGMA database_list").fetchone()[2] or ""
    except sqlite3.Error:
        return compute()
    full_key = (path, *key)
    with _aggregates_lock:
        held = _AGGREGATES.get(full_key)
        if held is not None and held[0] == generation:
            # Most recently used last, so the oldest is first to go.
            _AGGREGATES[full_key] = _AGGREGATES.pop(full_key)
            return copy.deepcopy(held[1])
    value = compute()
    with _aggregates_lock:
        _AGGREGATES.pop(full_key, None)
        _AGGREGATES[full_key] = (generation, value)
        while len(_AGGREGATES) > _AGGREGATES_MAX:
            _AGGREGATES.pop(next(iter(_AGGREGATES)))
    return copy.deepcopy(value)


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
        # Read the stored version *before* the schema script runs — on a brand
        # new file the meta table does not exist yet, which means version 0.
        try:
            existing = int(get_meta(conn, "schema_version", "0") or 0)
        except sqlite3.OperationalError:
            existing = 0
        conn.executescript(_SCHEMA)
        if existing and existing < SCHEMA_VERSION:
            _migrate(conn)
        # Version-independent: repairs a database whose recorded version is
        # already current but which predates a column added since.
        heal_schema(conn)
        # Smart albums: searches kept under a name (storage/smart.py).
        from .smart import SCHEMA as SMART_SCHEMA         # noqa: PLC0415
        conn.executescript(SMART_SCHEMA)
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
        conn.executescript(_generation_schema())
        for index in _RETIRED_INDEXES:
            conn.execute(f"DROP INDEX IF EXISTS {index}")
        _drop_changed_indexes(conn, _INDEX_SCHEMA)
        conn.executescript(_INDEX_SCHEMA)
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
    refresh_statistics(conn)
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
    "ai_version", "indexed_at",
)

#: Columns a rescan must never clobber — an admin's visibility decision
#: outlives the file being re-indexed.
#: A rotation somebody set by hand outlives a rescan too, and is restored in
#: :func:`bulk_upsert` rather than listed here — a rescan of a file whose EXIF
#: changed *should* pick the new tag up, so only the manual case is protected.
_PRESERVE_ON_UPSERT = {"root", "rel_path", "visibility", "vis_source"}

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

    cols = ", ".join(_ASSET_FIELDS)
    placeholders = ", ".join(f":{f}" for f in _ASSET_FIELDS)

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

    cols = ", ".join(_ASSET_FIELDS)
    placeholders = ", ".join(f":{f}" for f in _ASSET_FIELDS)

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
        conn.commit()


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


def delete_missing(conn: sqlite3.Connection, root: str, present: Iterable[str],
                   *, complete: bool = True, force: bool = False) -> list[str]:
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
    """
    present_set = set(present)
    rows = conn.execute(
        "SELECT id, rel_path, thumb FROM assets WHERE root=?", (root,)
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


def existing_signatures(conn: sqlite3.Connection, root: str) -> dict[str, tuple[float, int, int]]:
    """``rel_path -> (mtime, size, ai_version)`` for change detection."""
    rows = conn.execute(
        "SELECT rel_path, mtime, size, ai_version FROM assets WHERE root=?", (root,)
    ).fetchall()
    return {r["rel_path"]: (r["mtime"], r["size"], r["ai_version"]) for r in rows}


def live_paths(conn: sqlite3.Connection, root: str) -> set[str]:
    """Every rel_path in *root* that is not in the bin."""
    return {r[0] for r in conn.execute(
        "SELECT rel_path FROM assets WHERE root=? AND trashed=0", (root,))}


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
            for table in ("album_items", "user_assets"):
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


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

_SORTS = {
    "date_desc": "COALESCE(a.captured_at, a.mtime) DESC, a.id DESC",
    "date_asc": "COALESCE(a.captured_at, a.mtime) ASC, a.id ASC",
    "name_asc": "a.filename COLLATE NOCASE ASC, a.id ASC",
    "name_desc": "a.filename COLLATE NOCASE DESC, a.id DESC",
    "size_desc": "a.size DESC, a.id DESC",
    "size_asc": "a.size ASC, a.id ASC",
    # The id last, as every other order has: without it, photographs with the
    # same stars and the same time tied, and SQLite was free to put them in a
    # different order on each page — one repeated, another never shown.
    "rating_desc": "COALESCE(ua.rating, 0) DESC, COALESCE(a.captured_at, a.mtime) DESC, "
                   "a.id DESC",
    # Replaced in query_assets by a seeded shuffle when a seed is given (see
    # _random_order); without one each query draws a fresh order.
    "random": "RANDOM()",
}

#: A prime just under 2**31: the shuffle below is a permutation modulo it.
_SHUFFLE_PRIME = 2147483647


def _random_order(seed: int) -> str:
    """A shuffled order that stays the same from one page to the next.

    ``ORDER BY RANDOM()`` drew a new order for every query, so paging through
    it with OFFSET repeated some photographs and never showed others. This
    scrambles the id with a multiply-and-add modulo a prime, which is a
    permutation for any non-zero multiplier: the same seed gives the same order
    on every page. It is used only when the caller passes a seed: without one
    the query keeps ``RANDOM()``, so pressing shuffle again gives a new order
    rather than the same one all day. The numbers are integers worked out
    here, never user text, so they are inlined.
    """
    seed = abs(int(seed))
    multiplier = (seed * 2654435761) % (_SHUFFLE_PRIME - 1) + 1
    shift = seed % _SHUFFLE_PRIME
    return f"((a.id * {multiplier} + {shift}) % {_SHUFFLE_PRIME}), a.id"


def _like_escape(text: str) -> str:
    """*text* with LIKE's wildcards made literal, for ``LIKE ? ESCAPE '\\'``."""
    return (str(text).replace("\\", "\\\\")
            .replace("%", "\\%").replace("_", "\\_"))


def _escape_fts(query: str) -> str:
    """Turn user text into a safe FTS5 prefix query."""
    terms = []
    for raw in query.replace('"', " ").split():
        cleaned = "".join(ch for ch in raw if ch.isalnum() or ch in "_-")
        if cleaned:
            terms.append(f'"{cleaned}"*')
    return " AND ".join(terms)


def roots_clause(alias: str, roots: Sequence[str] | str) -> tuple[str, list[Any]]:
    """SQL restricting a query to one or more library folders."""
    if isinstance(roots, str):
        roots = [roots]
    roots = list(roots)
    if not roots:
        # No roots at all means no rows — never "everything".
        return "0 = 1", []
    if len(roots) == 1:
        return f"{alias}.root = ?", [roots[0]]
    return f"{alias}.root IN ({','.join('?' * len(roots))})", roots


def scope_clause(alias: str, scope: str | None) -> tuple[str, list[Any]]:
    """SQL confining results to a folder subtree (``""``/None = no limit)."""
    if not scope:
        return "", []
    return folder_clause(f"{alias}.folder", scope)


#: Kinds nobody but an administrator sees, whatever an item's own visibility
#: says.
#:
#: Audio is here because of what ends up in a family's media folders. A library
#: swept up from old drives and phone backups collects voice notes, voicemail,
#: call recordings, dictation, therapy sessions, meeting audio — things nobody
#: chose to put in a gallery and would not think to go looking for to hide. A
#: photograph is looked at on purpose; a sound file plays the moment it is
#: tapped, in a room with other people in it. So audio is administrator-only,
#: and it is a rule rather than a default: it does not depend on an item's
#: stored visibility, and there is no setting on the family app that lifts it.
#:
#: Photographs and video are unaffected — they are what the gallery is for.
ADMIN_ONLY_KINDS = ("audio",)


def kind_guard(alias: str, max_visibility: int) -> str:
    """SQL keeping administrator-only kinds away from everybody else.

    Returns an empty string for an administrator, who sees everything. The
    kind names are inlined rather than parameterised because they are module
    constants, never user input — which keeps this a drop-in for the
    visibility clause without changing any caller's parameter list.
    """
    if int(max_visibility) >= 2 or not ADMIN_ONLY_KINDS:
        return ""
    dot = f"{alias}." if alias else ""
    names = ",".join("'%s'" % k for k in ADMIN_ONLY_KINDS)
    return f"{dot}kind NOT IN ({names})"


def visibility_clause(alias: str, max_visibility: int) -> str:
    """The whole of what a viewer may see, as one SQL fragment, one parameter.

    Every query that shows media to somebody goes through this. Two rules live
    in it — the per-item visibility ceiling, and the kinds that are
    administrator-only — and having one fragment is what stops a later query
    from remembering the first rule and forgetting the second.
    """
    dot = f"{alias}." if alias else ""
    from ..server import date_policy
    clause = f"{dot}visibility <= ? AND {date_policy.sql(alias)}"
    guard = kind_guard(alias, max_visibility)
    return f"{clause} AND {guard}" if guard else clause


def hidden_from(asset: dict[str, Any] | None, max_visibility: int) -> bool:
    """Is this item one of the administrator-only kinds, for this viewer?"""
    if not asset:
        return False
    if int(max_visibility) >= 2:
        return False
    return str(asset.get("kind") or "") in ADMIN_ONLY_KINDS


def query_assets(
    conn: sqlite3.Connection,
    roots: Sequence[str] | str,
    *,
    viewer_id: int = 0,
    max_visibility: int = 1,
    scope: str | None = None,
    text: str = "",
    kinds: Sequence[str] | None = None,
    favorites: bool = False,
    min_rating: int = 0,
    date_from: str = "",
    date_to: str = "",
    folder: str = "",
    tag: str = "",
    camera: str = "",
    visibility: int | None = None,
    duplicates_only: bool = False,
    is_live: bool = False,
    min_size: int = 0,
    exclude_reviewed_large: bool = False,
    quality: str = "",
    occasion: int | None = None,
    album: int | None = None,
    near: tuple[float, float] | None = None,
    radius_km: float = 5.0,
    person: int | None = None,
    people: Sequence[int] | None = None,
    place: str = "",
    include_nsfw: bool = False,
    include_trashed: bool = False,
    ids: Sequence[int] | None = None,
    sort: str = "date_desc",
    limit: int = 200,
    offset: int = 0,
    columns: Sequence[str] | None = None,
    seed: int | None = None,
    with_total: bool = True,
    guest_search: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    """Filtered, paginated asset query.

    ``seed`` fixes the shuffle for ``sort="random"`` (see :func:`_random_order`).

    ``with_total=False`` skips the count and returns -1 for it. The callers
    that narrow a list of ids to what the viewer may see never read the
    total, and the count ran the whole WHERE a second time for each of them.

    ``columns`` narrows each row to those asset columns plus ``favorite`` and
    ``rating``, returned as read with no JSON decoding. It is for callers that
    take tens of thousands of rows at once — the gallery layout — where decoding
    every row's tags and quality flags costs more than the query itself.

    Two access controls are applied on *every* call and cannot be bypassed by
    a request parameter: ``max_visibility`` (derived from the caller's role)
    and ``scope`` (the folder the admin confined that profile to). Favourites
    and ratings come from the caller's own ``user_assets`` rows.
    """
    roots_sql, roots_params = roots_clause("a", roots)
    where = [roots_sql, visibility_clause("a", max_visibility)]
    params: list[Any] = [*roots_params, int(max_visibility)]

    scope_sql, scope_params = scope_clause("a", scope)
    if scope_sql:
        where.append(scope_sql)
        params.extend(scope_params)

    if not include_trashed:
        where.append("a.trashed = 0")
    if not include_nsfw:
        where.append("a.nsfw = 0")
    if visibility is not None:
        where.append("a.visibility = ?")
        params.append(int(visibility))
    if kinds:
        where.append("a.kind IN (%s)" % ",".join("?" * len(kinds)))
        params.extend(kinds)
        # Asking for videos should not hand back the clip halves of live
        # photos: the household sees the still in with the photographs and
        # holds it to play this, so listing it again on its own is the same
        # item twice, and it made the Videos view disagree with the count
        # beside it. Asking for pictures and videos together still reaches
        # everything, because the still is in there.
        if "video" in kinds and "picture" not in kinds:
            where.append("a.live_clip = 0")
    if favorites:
        where.append("COALESCE(ua.favorite, 0) = 1")
    if min_rating:
        where.append("COALESCE(ua.rating, 0) >= ?")
        params.append(min_rating)
    if date_from:
        where.append("a.date_key >= ?")
        params.append(date_from)
    if date_to:
        where.append("a.date_key <= ?")
        params.append(date_to)
    if folder:
        folder_sql, folder_params = folder_clause("a.folder", folder)
        where.append(folder_sql)
        params.extend(folder_params)
    if tag:
        where.append("a.tags LIKE ? ESCAPE '\\'")
        params.append(f'%"{_like_escape(tag)}"%')
    if camera:
        where.append("a.camera = ?")
        params.append(camera)
    if duplicates_only:
        where.append("a.dup_group IS NOT NULL")
    if is_live:
        where.append("a.is_live = 1")
    if min_size:
        where.append("a.size >= ?")
        params.append(int(min_size))
    if exclude_reviewed_large:
        # "Keep" on the largest-files screen, not a statement about anything
        # else — see the column's comment on the assets table.
        where.append("a.large_file_reviewed_at IS NULL")
    if quality:
        # Stored the way tags are, and matched the same way: the flag
        # vocabulary is fixed and short, so a LIKE on the JSON beats a second
        # table nobody would ever query on its own.
        where.append("a.quality LIKE ? ESCAPE '\\'")
        params.append(f'%"{_like_escape(quality)}"%')
    if occasion:
        where.append("a.occasion_id = ?")
        params.append(int(occasion))
    if album:
        from ..server import date_policy
        if not date_policy.allows({"date_key": album_date(conn, album)}):
            where.append("0=1")
        where.append("EXISTS (SELECT 1 FROM album_items ai WHERE ai.asset_id = a.id "
                     "AND ai.album_id = ?)")
        params.append(int(album))
    if near:
        lat, lon = float(near[0]), float(near[1])
        radius = max(0.01, float(radius_km))
        # A bounding box first, so the callback below only ever runs on
        # plausible neighbours rather than on every row in the library. One
        # degree of latitude is ~111 km everywhere; a degree of longitude
        # shrinks towards the poles, hence the cosine.
        lat_pad = radius / 111.0
        lon_pad = radius / max(1.0, 111.0 * math.cos(math.radians(lat)))
        where.append("a.gps_lat BETWEEN ? AND ?")
        params.extend([lat - lat_pad, lat + lat_pad])
        where.append("a.gps_lon BETWEEN ? AND ?")
        params.extend([lon - lon_pad, lon + lon_pad])
        where.append("ninaivu_km(a.gps_lat, a.gps_lon, ?, ?) <= ?")
        params.extend([lat, lon, radius])
    if person:
        # A subquery rather than a join: a photograph with three faces of the
        # same person must appear once, not three times, and EXISTS says that
        # without a DISTINCT that would also collapse the count query.
        where.append("EXISTS (SELECT 1 FROM faces pf WHERE pf.asset_id = a.id "
                     "AND pf.person_id = ?)")
        params.append(int(person))
    for other in people or ():
        # Everyone named must be in it: "Maya and Arjun" is the photographs
        # with both of them, not either.
        where.append("EXISTS (SELECT 1 FROM faces pf WHERE pf.asset_id = a.id "
                     "AND pf.person_id = ?)")
        params.append(int(other))
    if place:
        where.append("(a.city = ? COLLATE NOCASE OR a.country = ? COLLATE NOCASE)")
        params.extend([place, place])
    if ids is not None:
        if not ids:
            return [], 0
        where.append("a.id IN (%s)" % ",".join("?" * len(ids)))
        params.extend(ids)

    # The per-viewer join goes first so its bound parameter leads the list.
    mine = ("LEFT JOIN user_assets ua ON ua.asset_id = a.id AND ua.user_id = ?")
    join_params: list[Any] = [int(viewer_id)]

    join = ""
    order = _SORTS.get(sort, _SORTS["date_desc"])
    if sort == "random" and seed is not None:
        order = _random_order(seed)

    if text:
        if fts_enabled(conn):
            match = _escape_fts(text)
            if match and guest_search:
                # A guest is told neither where a photograph was taken nor
                # with what, so the words are not matched against either.
                match = "{%s} : (%s)" % (" ".join(GUEST_FTS_COLUMNS), match)
            if match:
                join = "JOIN assets_fts f ON f.rowid = a.id"
                where.append("assets_fts MATCH ?")
                params.append(match)
                if sort == "date_desc":
                    order = "f.rank, " + order
        else:
            # Escaped: "100%" or "my_trip" are things people type, and
            # unescaped the % and _ in them matched anything at all.
            like = f"%{_like_escape(text.lower())}%"
            where.append(
                "(LOWER(a.filename) LIKE ? ESCAPE '\\' OR LOWER(a.tags) LIKE ? ESCAPE '\\' "
                "OR LOWER(COALESCE(a.caption,'')) LIKE ? ESCAPE '\\' "
                "OR LOWER(a.folder) LIKE ? ESCAPE '\\' "
                + ("OR 0)" if guest_search else
                   "OR LOWER(COALESCE(a.city,'')) LIKE ? ESCAPE '\\')")
            )
            params.extend([like] * (4 if guest_search else 5))

    where_sql = " AND ".join(where)
    if columns is not None:
        unknown = [c for c in columns if not c.isidentifier()]
        if unknown:
            raise ValueError(f"not column names: {unknown}")
    selected = ", ".join(f"a.{c}" for c in columns) if columns is not None else "a.*"
    projection = (f"{selected}, COALESCE(ua.favorite, 0) AS mine_favorite, "
                  "COALESCE(ua.rating, 0) AS mine_rating")
    from_sql = f"FROM assets a {mine} {join}"
    all_params = [*join_params, *params]

    # The per-viewer join adds no rows (it is a LEFT JOIN on its primary key),
    # so the count only needs it when a filter reads from it. Without it the
    # count can be answered from an index alone.
    if favorites or min_rating:
        count_sql, count_params = from_sql, all_params
    else:
        count_sql, count_params = f"FROM assets a {join}", params
    total = conn.execute(
        f"SELECT COUNT(*) AS n {count_sql} WHERE {where_sql}", count_params
    ).fetchone()["n"] if with_total else -1

    page_sql = (f"SELECT {projection} {from_sql} WHERE {where_sql} "
                f"ORDER BY {order} LIMIT ? OFFSET ?")
    page_params = (*all_params, limit, offset)
    if columns is not None:
        # Plain tuples zipped with the names once, rather than a Row per item
        # turned into a dict and two keys renamed: the gallery asks for 25,000
        # at a time, and that conversion was a third of its time in Python.
        cursor = conn.cursor()
        cursor.row_factory = None
        cursor.execute(page_sql, page_params)
        names = [d[0] for d in cursor.description]
        names[-2:] = ["favorite", "rating"]          # mine_favorite, mine_rating
        lean = [dict(zip(names, values)) for values in cursor.fetchall()]
        for data in lean:
            data["favorite"] = bool(data["favorite"])
            data["rating"] = int(data["rating"] or 0)
        return lean, int(total)
    rows = conn.execute(page_sql, page_params).fetchall()
    return [row_to_dict(r) for r in rows], int(total)


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    try:
        data["tags"] = json.loads(data.get("tags") or "[]")
    except (TypeError, ValueError):
        data["tags"] = []
    try:
        data["quality"] = json.loads(data.get("quality") or "[]")
    except (TypeError, ValueError):
        data["quality"] = []
    data["favorite"] = bool(data.pop("mine_favorite", 0))
    data["rating"] = int(data.pop("mine_rating", 0) or 0)
    data["nsfw"] = bool(data.get("nsfw"))
    data["trashed"] = bool(data.get("trashed"))
    data["visibility"] = int(data.get("visibility", 1))
    data["is_live"] = bool(data.get("is_live", 0))
    data["live_video_path"] = data.get("live_video_path")
    return data


def get_asset(conn: sqlite3.Connection, asset_id: int,
              viewer_id: int = 0) -> dict[str, Any] | None:
    """Raw fetch by id. Callers must still check visibility and scope —
    :func:`visible_to` does that."""
    row = conn.execute(
        "SELECT a.*, COALESCE(ua.favorite,0) AS mine_favorite, "
        "COALESCE(ua.rating,0) AS mine_rating FROM assets a "
        "LEFT JOIN user_assets ua ON ua.asset_id=a.id AND ua.user_id=? "
        "WHERE a.id=?",
        (int(viewer_id), asset_id),
    ).fetchone()
    return row_to_dict(row) if row else None


#: What a route needs to decide whether an item may be served at all (see
#: ``api._guard``) plus what the thumbnail route reads. No captions, OCR
#: text or JSON columns: the gallery asks for thumbnails hundreds at a time.
BRIEF_COLUMNS = ("id", "root", "rel_path", "folder", "kind", "visibility", "date_key",
                 "trashed", "nsfw", "thumb", "mtime", "rotation", "indexed_at")


def get_asset_brief(conn: sqlite3.Connection, asset_id: int) -> dict[str, Any] | None:
    """The guard columns of one asset, as read, with no JSON decoding."""
    row = conn.execute(
        f"SELECT {', '.join(BRIEF_COLUMNS)} FROM assets WHERE id=?", (asset_id,)).fetchone()
    return dict(row) if row else None


def visible_to(asset: dict[str, Any] | None, max_visibility: int,
               scope: str | None) -> bool:
    """Whether one asset may be shown to a viewer with these limits."""
    if not asset:
        return False
    from ..server import date_policy
    if not date_policy.allows(asset):
        return False
    if int(asset.get("visibility", 1)) > max_visibility:
        return False
    if hidden_from(asset, max_visibility):
        # Administrator-only by kind. This is the check that covers every route
        # fetching one item by id — the media stream, the download, the
        # metadata — where a list query's guard never runs.
        return False
    if scope:
        folder = asset.get("folder") or ""
        if folder != scope and not folder.startswith(f"{scope}/"):
            return False
    return True


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


# ---------------------------------------------------------------------------
# Visibility
# ---------------------------------------------------------------------------

def _kind_floor(visibility: int) -> str:
    """SQL stopping a change from making an administrator-only kind visible.

    Audio never drops below hidden, however it is asked for — item by item, by
    a folder rule, or by a rule inherited by files scanned in later. Enforcing
    it on the way *in* as well as on the way out means the stored state and
    what people can actually open are the same thing, so the console never
    shows a voice memo marked "family" that no family member can play.
    """
    if int(visibility) >= 2 or not ADMIN_ONLY_KINDS:
        return ""
    names = ",".join("'%s'" % k for k in ADMIN_ONLY_KINDS)
    return f" AND kind NOT IN ({names})"


def with_live_clips(conn: sqlite3.Connection, asset_ids: Sequence[int]) -> list[int]:
    """*asset_ids*, and the motion clip of every live photo among them.

    A live photo is two rows: the still, and its clip (``live_clip=1``), which
    the household reaches by holding the still. Hiding, flagging or deleting
    the still has to take its clip with it: on its own the still went, and the
    clip stayed in every listing and played the same moment, with sound.
    """
    ids = [int(i) for i in asset_ids]
    if not ids:
        return ids
    clips: list[int] = []
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        placeholders = ",".join("?" * len(chunk))
        clips.extend(int(r["id"]) for r in conn.execute(
            f"SELECT c.id FROM assets s JOIN assets c ON c.root = s.root "
            f"AND c.rel_path = s.live_video_path COLLATE NOCASE "
            f"WHERE s.id IN ({placeholders}) AND s.is_live = 1 "
            f"AND COALESCE(s.live_video_path, '') <> '' AND c.live_clip = 1",
            chunk))
    seen = set(ids)
    return ids + [c for c in dict.fromkeys(clips) if c not in seen]


def set_visibility(conn: sqlite3.Connection, asset_ids: Sequence[int],
                   visibility: int, source: str = "manual",
                   user_id: int | None = None, record_undo: bool = True) -> int:
    """Set visibility on specific files, remembering what it was.

    Selecting a few hundred items and changing them is every bit as hard to
    reverse by hand as a folder rule is, so it goes through the same history.
    """
    if not asset_ids:
        return 0
    asset_ids = with_live_clips(conn, asset_ids)
    placeholders = ",".join("?" * len(asset_ids))
    batch_id = None
    if record_undo:
        with _write_lock:
            exposed = conn.execute(
                f"SELECT COUNT(*) n FROM assets WHERE id IN ({placeholders}) "
                f"AND visibility > ?", (*asset_ids, int(visibility))).fetchone()["n"]
            cur = conn.execute(
                "INSERT INTO visibility_batches(created_at, created_by, scope, "
                "visibility, exposed) VALUES(?,?,'items',?,?)",
                (time.time(), user_id, int(visibility), int(exposed or 0)))
            batch_id = int(cur.lastrowid)
            conn.execute(
                f"INSERT INTO visibility_undo(batch_id, asset_id, visibility, vis_source) "
                f"SELECT ?, id, visibility, vis_source FROM assets "
                f"WHERE id IN ({placeholders})", (batch_id, *asset_ids))
            conn.execute("UPDATE visibility_batches SET affected=? WHERE id=?",
                         (len(asset_ids), batch_id))
            conn.commit()
    with _write_lock:
        cur = conn.execute(
            f"UPDATE assets SET visibility=?, vis_source=? WHERE id IN "
            f"({placeholders}){_kind_floor(visibility)}",
            (int(visibility), source, *asset_ids),
        )
        changed = cur.rowcount
        conn.commit()
    if batch_id is not None:
        _trim_undo_history(conn)
    if int(visibility) >= 2:
        forget_ai_reading(conn, ids=list(asset_ids))
    return changed if changed >= 0 else len(asset_ids)


#: How many bulk visibility changes stay undoable. Twenty is a long way back
#: for a thing that is usually noticed within seconds, and bounds the table.
UNDO_HISTORY = 20


def _scope_sql(root: str, folder: str) -> tuple[str, list[Any]]:
    """WHERE clause for "this folder and everything under it"."""
    if folder:
        folder_sql, folder_params = folder_clause("folder", folder)
        return f"root=? AND {folder_sql}", [root, *folder_params]
    return "root=?", [root]


def _snapshot_folder(conn: sqlite3.Connection, root: str, folder: str,
                     visibility: int, user_id: int | None) -> int:
    """Write down what every affected file looks like now. Returns the batch id."""
    where, params = _scope_sql(root, folder)
    prior = conn.execute(
        "SELECT visibility FROM folder_rules WHERE root=? AND folder=?",
        (root, folder)).fetchone()
    exposed = conn.execute(
        f"SELECT COUNT(*) n FROM assets WHERE {where} AND visibility > ?",
        [*params, visibility]).fetchone()["n"]

    with _write_lock:
        cur = conn.execute(
            "INSERT INTO visibility_batches(created_at, created_by, scope, root, "
            "folder, visibility, exposed, had_rule, prior_rule) "
            "VALUES(?,?,'folder',?,?,?,?,?,?)",
            (time.time(), user_id, root, folder, visibility, int(exposed or 0),
             1 if prior else 0,
             int(prior["visibility"]) if prior else None))
        batch_id = int(cur.lastrowid)
        # Only rows that actually change are worth remembering, which on a
        # library where most files already sit at the target level keeps this
        # to a handful rather than a copy of the whole table.
        conn.execute(
            f"INSERT INTO visibility_undo(batch_id, asset_id, visibility, vis_source) "
            f"SELECT ?, id, visibility, vis_source FROM assets "
            f"WHERE {where} AND (visibility <> ? OR vis_source <> 'folder')",
            [batch_id, *params, visibility])
        conn.commit()
    return batch_id


def _trim_undo_history(conn: sqlite3.Connection) -> None:
    """Keep the history bounded. Foreign keys take the snapshots with it."""
    with _write_lock:
        conn.execute(
            "DELETE FROM visibility_batches WHERE id NOT IN "
            "(SELECT id FROM visibility_batches ORDER BY id DESC LIMIT ?)",
            (UNDO_HISTORY,))
        conn.commit()


def undo_batches(conn: sqlite3.Connection, limit: int = 10) -> list[dict[str, Any]]:
    """The recent bulk visibility changes, newest first."""
    rows = conn.execute(
        "SELECT b.*, (SELECT COUNT(*) FROM visibility_undo u WHERE u.batch_id=b.id) "
        "restorable FROM visibility_batches b ORDER BY b.id DESC LIMIT ?",
        (limit,)).fetchall()
    return [dict(r) for r in rows]


def undo_visibility_batch(conn: sqlite3.Connection, batch_id: int | None = None
                          ) -> dict[str, Any]:
    """Put a bulk visibility change back exactly as it was.

    Restores every file's own visibility *and* where that visibility came
    from, plus the folder rule that was replaced — so undoing is a real return
    to the previous state rather than a second sweeping change on top of the
    first.
    """
    if batch_id is None:
        row = conn.execute(
            "SELECT id FROM visibility_batches WHERE undone_at IS NULL "
            "ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return {"ok": False, "error": "There is nothing to undo."}
        batch_id = int(row["id"])

    batch = conn.execute("SELECT * FROM visibility_batches WHERE id=?",
                         (batch_id,)).fetchone()
    if batch is None:
        return {"ok": False, "error": "That change is no longer in the history."}
    if batch["undone_at"]:
        return {"ok": False, "error": "That change has already been undone."}

    with _write_lock:
        cur = conn.execute(
            "UPDATE assets SET visibility = (SELECT u.visibility FROM visibility_undo u "
            " WHERE u.batch_id=? AND u.asset_id=assets.id), "
            "vis_source = (SELECT u.vis_source FROM visibility_undo u "
            " WHERE u.batch_id=? AND u.asset_id=assets.id) "
            "WHERE id IN (SELECT asset_id FROM visibility_undo WHERE batch_id=?)",
            (batch_id, batch_id, batch_id))
        restored = cur.rowcount

        if batch["scope"] == "folder":
            if batch["had_rule"]:
                conn.execute(
                    "INSERT INTO folder_rules(root, folder, visibility, created_at, "
                    "created_by) VALUES(?,?,?,?,?) ON CONFLICT(root, folder) DO UPDATE "
                    "SET visibility=excluded.visibility, created_at=excluded.created_at",
                    (batch["root"], batch["folder"], batch["prior_rule"],
                     time.time(), batch["created_by"]))
            else:
                conn.execute("DELETE FROM folder_rules WHERE root=? AND folder=?",
                             (batch["root"], batch["folder"]))
        conn.execute("UPDATE visibility_batches SET undone_at=? WHERE id=?",
                     (time.time(), batch_id))
        conn.commit()
    return {"ok": True, "batch_id": batch_id, "restored": restored,
            "folder": batch["folder"], "visibility": int(batch["visibility"])}


def preview_folder_visibility(conn: sqlite3.Connection, root: str, folder: str,
                              visibility: int) -> dict[str, Any]:
    """What applying this rule would do, before it is applied.

    The number that matters is ``exposed``: how many files that are currently
    out of reach would become visible to more people. Everything else is
    reassurance; that one is the reason to stop and read.
    """
    folder = (folder or "").strip().strip("/")
    where, params = _scope_sql(root, folder)
    wanted = int(visibility)

    row = conn.execute(
        f"SELECT COUNT(*) total,"
        f" SUM(CASE WHEN visibility > ? THEN 1 ELSE 0 END) exposed,"
        f" SUM(CASE WHEN visibility < ? THEN 1 ELSE 0 END) restricted,"
        f" SUM(CASE WHEN visibility = 2 AND vis_source = 'hidden' THEN 1 ELSE 0 END) from_disk,"
        f" SUM(CASE WHEN vis_source = 'item' THEN 1 ELSE 0 END) decided_individually"
        f" FROM assets WHERE {where}",
        [wanted, wanted, *params],
    ).fetchone()

    below_sql, below_params = (folder_clause("folder", folder, include_self=False)
                               if folder else ("1=1", []))
    rules = conn.execute(
        f"SELECT COUNT(*) n FROM folder_rules WHERE root=? AND folder <> ? AND {below_sql}",
        (root, folder, *below_params),
    ).fetchone()

    return {
        "folder": folder,
        "visibility": wanted,
        "total": int(row["total"] or 0),
        # Currently less visible than the new setting, so this widens them.
        "exposed": int(row["exposed"] or 0),
        "restricted": int(row["restricted"] or 0),
        "hidden_by_the_filesystem": int(row["from_disk"] or 0),
        "decided_individually": int(row["decided_individually"] or 0),
        "child_rules": int(rules["n"] or 0),
    }


#: The visibility source of an item hidden as a screenshot or document.
SCREEN_SOURCE = "screen"


def hide_screens(conn: sqlite3.Connection, root: str, *,
                 threshold: float | None = None,
                 ids: Sequence[int] | None = None) -> int:
    """Hide pictures that are screenshots, documents or photographs of screens.

    By name always; by picture when *threshold* is given (a measured model is
    loaded). Only a level nobody chose for the item is replaced, and nothing
    already hidden is touched. *ids* narrows the pass to items just judged.
    Returns how many items were hidden.
    """
    from ..media import screens                      # noqa: PLC0415

    sources = ",".join("'%s'" % s for s in screens.REPLACEABLE_SOURCES)
    base = (f"root=? AND kind='picture' AND visibility < 2 "
            f"AND vis_source IN ({sources})")
    params: list[Any] = [str(root)]
    if ids is not None:
        if not ids:
            return 0
        base += f" AND id IN ({','.join('?' * len(ids))})"
        params += [int(i) for i in ids]

    by_name, name_params = screens.name_sql()
    hidden = 0
    with _write_lock:
        cur = conn.execute(
            f"UPDATE assets SET visibility=2, vis_source='{SCREEN_SOURCE}' "
            f"WHERE {base} AND {by_name}", [*params, *name_params])
        hidden += cur.rowcount or 0
        if threshold is not None:
            cur = conn.execute(
                f"UPDATE assets SET visibility=2, vis_source='{SCREEN_SOURCE}' "
                f"WHERE {base} AND screen_version=? AND screen_score >= ?",
                [*params, screens.SCREEN_VERSION, float(threshold)])
            hidden += cur.rowcount or 0
        conn.commit()
    if hidden:
        forget_ai_reading(conn, root=str(root), ids=ids)
    return hidden


#: What every AI pass may look at: nothing at the administrator-only level.
#: That level is where a household puts what is not for the gallery (a
#: passport, a bank statement, a medical letter; and every screenshot and
#: document the automatic rule finds), and what is not for the gallery is not
#: for the tagger, the text reader or the face finder either. Their words and
#: faces would otherwise sit in the search index and the database in plain
#: text, a second copy of exactly what was hidden.
AI_MAY_READ = "visibility < 2"


def ai_may_read(conn: sqlite3.Connection, asset_id: int) -> bool:
    """Whether AI may still read this item now: it is not hidden. An item
    that is not in the index at all is not refused."""
    row = conn.execute("SELECT visibility FROM assets WHERE id=?", (asset_id,)).fetchone()
    return row is None or int(row[0] or 0) < 2


def forget_ai_reading(conn: sqlite3.Connection, *, root: str | None = None,
                      ids: Sequence[int] | None = None) -> int:
    """Take back what the AI passes made of administrator-only items.

    Text read from the picture, the search vector, faces nobody confirmed, and
    tags and a caption the tagger wrote all go; an admin's own tags, caption
    and confirmed faces stay, as they do through a re-tag. The pass versions
    go back to zero, so an item shown again later is analysed then like a new
    one, and the passes' queries (:data:`AI_MAY_READ`) keep it out until then.
    Sound files are left alone: no pass reads them, and their tags are the
    file's own. Returns how many items had something taken back.
    """
    where = ["visibility >= 2", "kind != 'audio'"]
    params: list[Any] = []
    if root is not None:
        where.append("root=?")
        params.append(str(root))
    if ids is not None:
        if not ids:
            return 0
        where.append(f"id IN ({','.join('?' * len(ids))})")
        params += [int(i) for i in ids]
    where.append(
        "(ai_version > 0 OR ocr_version > 0 OR face_version > 0 "
        "OR keyframe_version > 0 OR ocr_text IS NOT NULL "
        "OR EXISTS (SELECT 1 FROM embeddings e WHERE e.asset_id=assets.id) "
        "OR EXISTS (SELECT 1 FROM faces f WHERE f.asset_id=assets.id "
        "AND f.source <> 'confirmed'))")
    try:
        from ..ai import LABELS                       # noqa: PLC0415
        vocabulary = set(LABELS)
    except Exception:                                 # noqa: BLE001
        vocabulary = set()
    # Read under the same lock as the write: an admin saving tags on a hidden
    # item in between had them replaced by the values read before the save,
    # and an item shown again in between lost its reading all the same.
    with _write_lock:
        rows = conn.execute(
            "SELECT id, tags, tags_source, caption, caption_source FROM assets "
            f"WHERE {' AND '.join(where)}", params).fetchall()
        if not rows:
            return 0
        found, dropped = _forget_rows(conn, rows, vocabulary)
    if dropped:
        forget_embeddings()
    return len(found)


def _forget_rows(conn: sqlite3.Connection, rows, vocabulary: set[str]  # noqa: ANN001
                 ) -> tuple[list[int], int]:
    """The writes of :func:`forget_ai_reading`. Called holding ``_write_lock``."""
    updates = []
    for row in rows:
        try:
            tags = [str(t) for t in json.loads(row["tags"] or "[]")]
        except (TypeError, ValueError):
            tags = []
        keep_tags = row["tags_source"] == "manual"
        caption = row["caption"]
        # The tagger's caption is its first few tags joined; a description from
        # the file or a Takeout record is somebody's words and is kept.
        if caption and row["caption_source"] != "manual":
            words = [w.strip() for w in str(caption).split(",") if w.strip()]
            if words and all(w in vocabulary or w in tags for w in words):
                caption = None
        updates.append((row["tags"] if keep_tags else "[]", caption, int(row["id"])))
    found = [u[2] for u in updates]
    dropped = 0
    conn.executemany("UPDATE assets SET tags=?, caption=? WHERE id=?", updates)
    # In pieces: a whole library folder set to admins only is tens of
    # thousands of ids, past what one statement may be given.
    for at in range(0, len(found), 500):
        part = found[at:at + 500]
        marks = ",".join("?" * len(part))
        conn.execute(
            "UPDATE assets SET ocr_text=NULL, ocr_version=0, ai_version=0, "
            f"face_version=0, keyframe_version=0 WHERE id IN ({marks})", part)
        dropped += max(0, conn.execute(
            f"DELETE FROM embeddings WHERE asset_id IN ({marks})", part).rowcount)
        conn.execute(
            f"DELETE FROM faces WHERE asset_id IN ({marks}) AND source <> 'confirmed'",
            part)
    conn.commit()
    return found, dropped


def screen_clause(conn: sqlite3.Connection, alias: str = "") -> tuple[str, list[Any]]:
    """SQL true for a picture that is a screenshot, a document or a screen.

    The same judgement the hiding rule makes — its name, or a score from the
    search model at or over that model's threshold — plus anything the rule has
    already hidden. It does not depend on the setting or on visibility, so an
    item an administrator has since shown again is still found by it.
    """
    from ..media import screens                      # noqa: PLC0415

    dot = f"{alias}." if alias else ""
    # Plain SQL throughout. A Python function here was called for every row of
    # the library — SQLite does not keep the cheap LIKEs in front of it — and
    # that was most of a second on every open of the Folders screen.
    by_name, params = screens.name_sql(alias)
    parts = [f"{dot}vis_source='{SCREEN_SOURCE}'"]
    threshold = screens.THRESHOLDS.get(get_meta(conn, "ai_model_id", "") or "")
    if threshold is not None:
        parts.append(f"({dot}screen_version=? AND {dot}screen_score >= ?)")
        params = [screens.SCREEN_VERSION, float(threshold), *params]
    parts.append(by_name)
    return f"({dot}kind='picture' AND ({' OR '.join(parts)}))", params


def store_screen_scores(conn: sqlite3.Connection,
                        scores: Sequence[tuple[int, float]]) -> None:
    """Record how much each picture looks like a screen, with this version."""
    from ..media import screens                      # noqa: PLC0415

    if not scores:
        return
    with _write_lock:
        conn.executemany(
            "UPDATE assets SET screen_score=?, screen_version=? WHERE id=?",
            [(round(float(score), 4), screens.SCREEN_VERSION, int(asset_id))
             for asset_id, score in scores])
        conn.commit()


def restore_screens(conn: sqlite3.Connection) -> int:
    """Undo the rule: every item it hid goes back to the level it would have.

    That is its folder's rule where one covers it, and family otherwise — the
    same decision indexing makes. Scores are kept, so turning the setting back
    on hides the same items again without judging anything twice.
    """
    rows = conn.execute(
        f"SELECT id, root, folder FROM assets WHERE vis_source='{SCREEN_SOURCE}'"
    ).fetchall()
    rules: dict[str, list[dict[str, Any]]] = {}
    updates: list[tuple[int, str, int]] = []
    for row in rows:
        root = row["root"]
        if root not in rules:
            rules[root] = folder_rules(conn, root)
        found = folder_rule_for(rules[root], row["folder"] or "")
        if found is not None:
            updates.append((int(found[0]), "folder", int(row["id"])))
        else:
            updates.append((1, "default", int(row["id"])))
    with _write_lock:
        conn.executemany(
            "UPDATE assets SET visibility=?, vis_source=? WHERE id=?", updates)
        conn.commit()
    return len(updates)


def set_folder_visibility(conn: sqlite3.Connection, root: str, folder: str,
                          visibility: int, user_id: int | None = None,
                          record_undo: bool = True) -> int:
    """Apply a visibility level to a folder and everything under it.

    The rule is remembered, so files scanned into that folder later inherit
    it instead of quietly reappearing at the default level.

    Unless ``record_undo`` is off, the visibility of every file this is about
    to overwrite is written to :func:`undo_batches` first, so a wrong click can
    be put back exactly rather than approximately.
    """
    folder = (folder or "").strip().strip("/")
    now = time.time()
    batch_id = None
    if record_undo:
        batch_id = _snapshot_folder(conn, root, folder, int(visibility), user_id)
    with _write_lock:
        conn.execute(
            "INSERT INTO folder_rules(root, folder, visibility, created_at, created_by) "
            "VALUES(?,?,?,?,?) ON CONFLICT(root, folder) DO UPDATE SET "
            "visibility=excluded.visibility, created_at=excluded.created_at, "
            "created_by=excluded.created_by",
            (root, folder, int(visibility), now, user_id),
        )
        # A screenshot or document hidden by the automatic rule stays hidden
        # when its folder is opened up: making the holiday folder public is
        # not a decision about the boarding passes in it. An administrator can
        # still show one on its own, which is a decision about that item.
        floor = _kind_floor(visibility)
        if int(visibility) < 2:
            floor += f" AND vis_source <> '{SCREEN_SOURCE}'"
        if folder:
            folder_sql, folder_params = folder_clause("folder", folder)
            cur = conn.execute(
                "UPDATE assets SET visibility=?, vis_source='folder' "
                f"WHERE root=? AND {folder_sql}" + floor,
                (int(visibility), root, *folder_params),
            )
        else:
            cur = conn.execute(
                "UPDATE assets SET visibility=?, vis_source='folder' WHERE root=?"
                + floor,
                (int(visibility), root),
            )
        if batch_id is not None:
            conn.execute("UPDATE visibility_batches SET affected=? WHERE id=?",
                         (cur.rowcount, batch_id))
        conn.commit()
    _trim_undo_history(conn)
    if int(visibility) >= 2:
        forget_ai_reading(conn, root=root)
    return cur.rowcount


def clear_folder_rule(conn: sqlite3.Connection, root: str, folder: str) -> None:
    with _write_lock:
        conn.execute("DELETE FROM folder_rules WHERE root=? AND folder=?",
                     (root, (folder or "").strip().strip("/")))
        conn.commit()


def folder_rules(conn: sqlite3.Connection, root: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT folder, visibility, created_at FROM folder_rules WHERE root=? "
        "ORDER BY folder",
        (root,),
    ).fetchall()
    return [dict(r) for r in rows]


def apply_folder_rules_to_new(conn: sqlite3.Connection, root: str,
                              rules: Sequence[dict[str, Any]]) -> int:
    """Put files that took the default level under the folder rule covering
    them. Returns how many changed.

    A rule changes the files already indexed when it is set; a scan already
    running read the rules before that, so the files it writes afterwards
    land at the default level. Only ``default`` rows move: anything decided
    item by item, hidden on disk, or held back by its kind keeps its level.
    The broadest rule goes first, so the most specific one has the last word.
    """
    changed = 0
    hidden: list[int] = []
    with _write_lock:
        for rule in sorted(rules, key=lambda r: len(r["folder"] or "")):
            where, params = _scope_sql(root, rule["folder"] or "")
            level = int(rule["visibility"])
            if level >= 2:
                hidden += [int(r[0]) for r in conn.execute(
                    f"SELECT id FROM assets WHERE {where} AND vis_source='default'",
                    params)]
            cur = conn.execute(
                f"UPDATE assets SET visibility=?, vis_source='folder' "
                f"WHERE {where} AND vis_source='default'" + _kind_floor(level),
                [level, *params])
            changed += max(0, cur.rowcount)
        conn.commit()
    if hidden:
        forget_ai_reading(conn, ids=hidden)
    return changed


def folder_rule_for(rules: Sequence[dict[str, Any]], folder: str
                    ) -> tuple[int, str] | None:
    """The most specific folder rule covering *folder*, as ``(visibility, at)``.

    ``at`` is the folder the rule was actually set on, which the caller needs
    in order to tell a decision made *about this folder* from one inherited
    from somewhere further up.
    """
    best: tuple[int, str] | None = None
    best_len = -1
    for rule in rules:
        prefix = rule["folder"]
        if not prefix:
            match = True
        else:
            match = folder == prefix or folder.startswith(f"{prefix}/")
        if match and len(prefix) > best_len:
            best_len = len(prefix)
            best = (int(rule["visibility"]), prefix)
    return best


def visibility_for_folder(rules: Sequence[dict[str, Any]], folder: str) -> int | None:
    """The most specific folder rule covering *folder*, if any."""
    found = folder_rule_for(rules, folder)
    return None if found is None else found[0]


def library_stats(conn: sqlite3.Connection, roots: Sequence[str] | str, *,
                  viewer_id: int = 0, max_visibility: int = 1,
                  scope: str | None = None) -> dict[str, Any]:
    """Counts for the sidebar, limited to what this viewer may see."""
    # One alias (`a`) everywhere, so the scope clause can be reused verbatim.
    roots_sql, roots_params = roots_clause("a", roots)
    where = [roots_sql, "a.trashed = 0", visibility_clause("a", max_visibility)]
    params: list[Any] = [*roots_params, int(max_visibility)]
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
            f"SELECT COUNT(*) n, COALESCE(SUM(a.size),0) bytes, "
            f"SUM(a.kind='picture') pictures, "
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
            f"WHERE {where_sql} AND a.dup_group IS NOT NULL", params,
        ).fetchone()["n"]
        return {**dict(row), "dupes": dupes}

    # The same for everybody with this viewer's limits, and unchanged until the
    # library is: remembered. The two below are not — one is this viewer's
    # own, the other is written by the tagger, not by a scan.
    row = cached_aggregate(conn, ("library_stats", where_sql, tuple(params)),
                           whole_library)
    dupes = row["dupes"]

    embedded = conn.execute(
        f"SELECT COUNT(*) n FROM embeddings e JOIN assets a ON a.id = e.asset_id "
        f"WHERE {roots_sql}", roots_params,
    ).fetchone()["n"]

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
        "first_date": row["first_date"] or "",
        "last_date": row["last_date"] or "",
    }


def facets(conn: sqlite3.Connection, roots: Sequence[str] | str, limit: int = 40, *,
           max_visibility: int = 1, scope: str | None = None) -> dict[str, Any]:
    """Tag cloud, folder list, camera list and per-year counts.

    Everything here leaks information about the library, so it is filtered by
    the same visibility and scope rules as the gallery itself.
    """
    roots_sql, roots_params = roots_clause("assets", roots)
    scope_sql, scope_params = scope_clause("assets", scope)
    guard = (roots_sql.replace("assets.", "") + " AND trashed=0 AND "
             + visibility_clause("", max_visibility))
    # As in library_stats: below an admin, flagged content is not counted in
    # the folder, camera and year lists either — only the tags left it out.
    if int(max_visibility) < 2:
        guard += " AND nsfw=0"
    if scope_sql:
        guard += " AND " + scope_sql.replace("assets.", "")
    base: list[Any] = [*roots_params, int(max_visibility), *scope_params]

    return cached_aggregate(conn, ("facets", guard, tuple(base), int(limit)),
                            lambda: _facets(conn, guard, base, limit))


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


def timeline(conn: sqlite3.Connection, roots: Sequence[str] | str, *,
             max_visibility: int = 1,
             scope: str | None = None) -> list[dict[str, Any]]:
    roots_sql, roots_params = roots_clause("assets", roots)
    scope_sql, scope_params = scope_clause("assets", scope)
    guard = (roots_sql.replace("assets.", "")
             + " AND trashed=0 AND nsfw=0 AND "
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


# ---------------------------------------------------------------------------
# Occasions (auto-albums)
# ---------------------------------------------------------------------------

def rebuild_occasions(conn: sqlite3.Connection, root: str, *,
                   gap_seconds: float, radius_km: float = 0.0,
                   min_items: int = 4) -> int:
    """Recompute every auto-album under *root*. Returns how many there are.

    Wholesale rather than incremental, and cheap enough to be: this reads
    five columns and no pixels. Rebuilding is also the only way an occasion
    can absorb a photograph found later without splitting in two, which is
    what an incremental pass would do.

    Only the visible library is grouped. Trashed files, and the kinds an
    administrator has hidden, are left out of the runs entirely rather than
    silently padding somebody's holiday.

    Hidden (admin-only) items are left out too. An occasion's title and place
    are shown to everyone who can see any of it, and a run that took its
    dates or its city from a hidden photograph told the family where and when
    that photograph was taken.
    """
    from ..media import occasions as occasions_mod

    rows = conn.execute(
        "SELECT id, captured_at, mtime, gps_lat, gps_lon, city FROM assets "
        "WHERE root=? AND trashed=0 AND nsfw=0 AND visibility < 2 "
        "ORDER BY COALESCE(captured_at, mtime) ASC, id ASC",
        (root,),
    ).fetchall()

    grouped = occasions_mod.group(
        rows, gap_seconds=gap_seconds, radius_km=radius_km,
        min_items=min_items,
    )

    # Computed wholesale, written as a difference. Clearing and refilling
    # every row rewrote the whole library on each scan — tens of seconds of
    # write lock on a large one, for a result that had usually not changed.
    # An occasion keeps its id while its key does, so only photographs that
    # moved between occasions are touched.
    with _write_lock:
        existing = {r["key"]: int(r["id"]) for r in conn.execute(
            "SELECT id, key FROM occasions WHERE root=?", (root,))}
        current = {int(r["id"]): int(r["occasion_id"]) for r in conn.execute(
            "SELECT id, occasion_id FROM assets "
            "WHERE root=? AND occasion_id IS NOT NULL", (root,))}
        wanted: dict[int, int] = {}
        kept: set[int] = set()
        for meta, run in grouped:
            fields = (meta["title"], meta["place"], meta["started_at"],
                      meta["ended_at"], meta["days"], meta["count"])
            occasion_id = existing.get(meta["key"])
            if occasion_id is not None:
                conn.execute(
                    "UPDATE occasions SET title=?, place=?, started_at=?, "
                    "ended_at=?, days=?, count=? WHERE id=?",
                    (*fields, occasion_id))
            else:
                cursor = conn.execute(
                    "INSERT INTO occasions(root, key, title, place, started_at, "
                    "ended_at, days, count) VALUES (?,?,?,?,?,?,?,?)",
                    (root, meta["key"], *fields))
                occasion_id = int(cursor.lastrowid)
            kept.add(occasion_id)
            for row in run:
                wanted[int(row["id"])] = occasion_id
        conn.executemany(
            "UPDATE assets SET occasion_id=NULL WHERE id=?",
            [(asset_id,) for asset_id in current.keys() - wanted.keys()])
        conn.executemany(
            "UPDATE assets SET occasion_id=? WHERE id=?",
            [(occasion_id, asset_id) for asset_id, occasion_id in wanted.items()
             if current.get(asset_id) != occasion_id])
        conn.executemany(
            "DELETE FROM occasions WHERE id=?",
            [(occasion_id,) for occasion_id in set(existing.values()) - kept])
        conn.commit()
    return len(grouped)


def list_occasions(conn: sqlite3.Connection, roots: Sequence[str] | str, *,
                viewer_id: int = 0, max_visibility: int = 1,
                scope: str | None = None,
                limit: int = 200, with_place: bool = True) -> list[dict[str, Any]]:
    """Auto-albums this viewer may see, newest occasion first.

    ``with_place=False`` (a guest) gives each occasion its dates for a title
    and no place: a guest is never told where a photograph was taken.

    The stored ``count`` is what the scan grouped; the count returned here is
    what *this* viewer can actually open. They differ for a guest scoped to
    one folder, and showing the stored number would advertise the rest of the
    library.
    """
    roots_sql, roots_params = roots_clause("a", roots)
    scope_sql, scope_params = scope_clause("a", scope)
    guard = (f"{roots_sql} AND a.trashed=0 AND a.nsfw=0 AND "
             + visibility_clause("a", max_visibility)
             + " AND a.occasion_id IS NOT NULL")
    if scope_sql:
        guard += " AND " + scope_sql
    rows = conn.execute(
        f"SELECT o.id, o.key, o.title, o.place, o.started_at, o.ended_at, "
        # The cover is the newest item that has a thumbnail. Plain MAX(id)
        # picked an MP3 for an administrator's occasion, which has no picture
        # and was a broken image asked for on every visit.
        f"o.days, COUNT(a.id) AS visible, "
        f"MAX(CASE WHEN a.thumb IS NOT NULL THEN a.id END) AS cover_id "
        f"FROM occasions o JOIN assets a ON a.occasion_id = o.id "
        f"WHERE {guard} GROUP BY o.id "
        f"ORDER BY o.started_at DESC LIMIT ?",
        (*roots_params, int(max_visibility), *scope_params, int(limit)),
    ).fetchall()
    def title(row) -> str:
        place = row["place"] or ""
        text = row["title"] or ""
        if with_place or not place:
            return text
        prefix = f"{place}, "
        return text[len(prefix):] if text.startswith(prefix) else ""

    return [
        {"id": r["id"], "key": r["key"], "title": title(r),
         "place": r["place"] if with_place else "", "started_at": r["started_at"],
         "ended_at": r["ended_at"], "days": r["days"],
         "count": r["visible"], "cover_id": r["cover_id"]}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Albums
# ---------------------------------------------------------------------------

def create_album(conn: sqlite3.Connection, name: str,
                 created_by: int | None = None) -> int:
    with _write_lock:
        cur = conn.execute(
            "INSERT INTO albums(name, created_at, created_by) VALUES(?, ?, ?) "
            "ON CONFLICT(name) DO NOTHING",
            (name, time.time(), created_by),
        )
        conn.commit()
    # ``rowcount`` tells an insert from the DO NOTHING no-op; ``lastrowid``
    # does not — it keeps the rowid of the previous insert on this connection,
    # whichever table that was, so a repeated name used to come back as some
    # other album's id (or a scan run's), and the caller's photographs went
    # into it.
    if cur.rowcount == 1:
        return int(cur.lastrowid)
    row = conn.execute("SELECT id FROM albums WHERE name=?", (name,)).fetchone()
    return int(row["id"])


def list_albums(conn: sqlite3.Connection,
                roots: Sequence[str] | str | None = None,
                max_visibility: int = 0,
                scope: str | None = None,
                viewer_id: int | None = None,
                is_admin: bool = False) -> list[dict[str, Any]]:
    """Albums the viewer can actually open, counted as the viewer sees them.

    An album's *name* is family-authored free text and its size is a fact
    about folders the viewer may have no access to, so neither may be returned
    for an album whose contents are entirely out of their reach. Passing no
    roots keeps the old unfiltered behaviour for internal callers.
    """
    if roots is None:
        rows = conn.execute(
            "SELECT al.id, al.name, al.created_at, al.cover_id, al.created_by, "
            "(SELECT COUNT(*) FROM album_items ai WHERE ai.album_id=al.id) n, "
            "COALESCE(al.cover_id, (SELECT ai.asset_id FROM album_items ai WHERE ai.album_id=al.id ORDER BY ai.added_at DESC LIMIT 1)) AS effective_cover_id "
            "FROM albums al ORDER BY al.name COLLATE NOCASE"
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if not d.get("cover_id") and d.get("effective_cover_id"):
                d["cover_id"] = d["effective_cover_id"]
            d.pop("effective_cover_id", None)
            result.append(d)
        return result

    roots_sql, params = roots_clause("a", roots)
    scope_sql, scope_params = scope_clause("a", scope)
    where = [roots_sql, visibility_clause("a", max_visibility), "a.trashed = 0", "a.nsfw = 0"]
    visible = list(params) + [int(max_visibility)]
    if scope_sql:
        where.append(scope_sql)
        visible += scope_params
    seen = " AND ".join(w for w in where if w)

    # The album's date (the same figure :func:`album_date` gives) rides in the
    # statement, and the chosen covers are looked up together afterwards: a
    # household with two hundred albums paid two further queries per album.
    rows = conn.execute(
        f"SELECT al.id, al.name, al.created_at, al.cover_id, al.created_by, "
        f"(SELECT COUNT(*) FROM album_items ai JOIN assets a ON a.id = ai.asset_id "
        f" WHERE ai.album_id = al.id AND {seen}) n, "
        f"(SELECT ai.asset_id FROM album_items ai JOIN assets a ON a.id = ai.asset_id "
        f" WHERE ai.album_id = al.id AND {seen} ORDER BY ai.added_at DESC LIMIT 1) AS effective_cover_id, "
        f"COALESCE((SELECT MIN(NULLIF(a.date_key, '')) FROM album_items ai "
        f" JOIN assets a ON a.id = ai.asset_id WHERE ai.album_id = al.id AND a.trashed = 0), '') AS date_key "
        f"FROM albums al ORDER BY al.name COLLATE NOCASE",
        visible + visible,
    ).fetchall()
    from ..server import date_policy
    wanted = {int(r["cover_id"]) for r in rows if r["cover_id"]}
    covers: dict[int, dict[str, Any]] = {}
    for start in range(0, len(wanted), 500):
        piece = list(wanted)[start:start + 500]
        covers.update({int(c["id"]): dict(c) for c in conn.execute(
            "SELECT id, root, folder, kind, visibility, date_key, trashed, nsfw "
            "FROM assets WHERE id IN (%s)" % ",".join("?" * len(piece)), piece)})
    root_list = [roots] if isinstance(roots, str) else roots
    # An album with nothing visible in it is not the viewer's business, but an
    # empty album they made themselves still is.
    out = []
    for r in rows:
        d = dict(r)
        if not date_policy.allows(d):
            continue
        cover = covers.get(int(d["cover_id"])) if d.get("cover_id") else None
        if not (cover and visible_to(cover, max_visibility, scope)
                and cover["root"] in root_list and not cover.get("trashed")
                and not cover.get("nsfw")):
            d["cover_id"] = d.get("effective_cover_id")
        d.pop("effective_cover_id", None)
        if d["n"] > 0 or (not date_policy.restricted() and ((viewer_id is not None and d["created_by"] == viewer_id) or is_admin)):
            out.append(d)
    return out


def album_add(conn: sqlite3.Connection, album_id: int, asset_ids: Sequence[int]) -> None:
    now = time.time()
    with _write_lock:
        conn.executemany(
            "INSERT OR IGNORE INTO album_items(album_id, asset_id, added_at) "
            "VALUES(?,?,?)",
            [(album_id, a, now) for a in asset_ids],
        )
        conn.commit()


def album_remove(conn: sqlite3.Connection, album_id: int, asset_ids: Sequence[int]) -> None:
    with _write_lock:
        conn.executemany(
            "DELETE FROM album_items WHERE album_id=? AND asset_id=?",
            [(album_id, a) for a in asset_ids],
        )
        conn.commit()


def album_asset_ids(conn: sqlite3.Connection, album_id: int) -> list[int]:
    rows = conn.execute(
        "SELECT asset_id FROM album_items WHERE album_id=? ORDER BY added_at",
        (album_id,),
    ).fetchall()
    return [int(r["asset_id"]) for r in rows]


def album_owner(conn: sqlite3.Connection, album_id: int) -> tuple[bool, int | None]:
    """``(exists, created_by)``. ``created_by`` is None for pre-ownership albums."""
    row = conn.execute(
        "SELECT created_by FROM albums WHERE id=?", (album_id,)).fetchone()
    if row is None:
        return False, None
    return True, (int(row["created_by"]) if row["created_by"] is not None else None)


def delete_album(conn: sqlite3.Connection, album_id: int) -> None:
    with _write_lock:
        conn.execute("DELETE FROM albums WHERE id=?", (album_id,))
        conn.commit()


def album_date(conn: sqlite3.Connection, album_id: int) -> str:
    """A named album is dated by its earliest non-trashed, dated file."""
    row = conn.execute(
        "SELECT MIN(NULLIF(a.date_key, '')) FROM album_items ai "
        "JOIN assets a ON a.id=ai.asset_id WHERE ai.album_id=? AND a.trashed=0",
        (album_id,),
    ).fetchone()
    return row[0] or ""


def get_album(conn: sqlite3.Connection, album_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id, name, created_at, cover_id, created_by FROM albums WHERE id=?",
        (album_id,)
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["date_key"] = album_date(conn, album_id)
    items = album_asset_ids(conn, album_id)
    d["item_ids"] = items
    d["n"] = len(items)
    return d


def update_album(conn: sqlite3.Connection, album_id: int,
                 name: str | None = None,
                 cover_id: int | None = None) -> None:
    sets = []
    params: list[Any] = []
    if name is not None:
        clean_name = str(name).strip()[:120]
        if clean_name:
            sets.append("name = ?")
            params.append(clean_name)
    if cover_id is not None:
        sets.append("cover_id = ?")
        params.append(int(cover_id) if int(cover_id) > 0 else None)
    if not sets:
        return
    params.append(album_id)
    with _write_lock:
        conn.execute(f"UPDATE albums SET {', '.join(sets)} WHERE id = ?", params)
        conn.commit()


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------

def duplicate_groups(conn: sqlite3.Connection, roots: Sequence[str] | str, *,
                     viewer_id: int = 0, max_visibility: int = 1,
                     scope: str | None = None) -> list[dict[str, Any]]:
    roots_sql, roots_params = roots_clause("a", roots)
    scope_sql, scope_params = scope_clause("a", scope)
    guard = (f"{roots_sql} AND a.trashed=0 AND a.nsfw=0 AND "
             + visibility_clause("a", max_visibility)
             + " AND a.dup_group IS NOT NULL")
    if scope_sql:
        guard += " AND " + scope_sql
    rows = conn.execute(
        f"SELECT a.*, COALESCE(ua.favorite,0) AS mine_favorite, "
        f"COALESCE(ua.rating,0) AS mine_rating FROM assets a "
        f"LEFT JOIN user_assets ua ON ua.asset_id=a.id AND ua.user_id=? "
        f"WHERE {guard} ORDER BY a.dup_group, a.size DESC",
        (int(viewer_id), *roots_params, int(max_visibility), *scope_params),
    ).fetchall()
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row["dup_group"], []).append(row_to_dict(row))
    return [
        {"group": key, "items": items, "best": best_of(items),
         "wasted": sum(i["size"] for i in items[1:])}
        for key, items in groups.items()
        if len(items) > 1
    ]


def best_of(items: Sequence[dict[str, Any]]) -> int | None:
    """Which frame of a near-identical group is the one worth keeping.

    Sharpest first — that is what separates one frame of a burst from the
    next. Resolution and file size break the tie, in that order, because two
    copies of the same shot differ by how much of it survived, not by
    content. Anything a person has already marked wins outright: a favourite
    or a starred frame is a decision, and Ninaivu does not overrule it.

    Returns the asset id, or ``None`` for an empty group.
    """
    if not items:
        return None

    def rank(item: dict[str, Any]) -> tuple:
        pixels = int(item.get("width") or 0) * int(item.get("height") or 0)
        return (
            1 if item.get("favorite") else 0,
            int(item.get("rating") or 0),
            # Never measured sorts below every measured frame rather than
            # above them, which -1.0 would not do for a genuinely flat photo.
            float(item["sharpness"]) if item.get("sharpness") is not None
            else float("-inf"),
            pixels,
            int(item.get("size") or 0),
        )

    return int(max(items, key=rank)["id"])


# ---------------------------------------------------------------------------
# Memories & Geo Explorer
# ---------------------------------------------------------------------------

#: "Taken on this month and day", for :func:`query_on_this_day` and
#: :func:`candidates_for_the_day`. ``date_key`` is the local day the scanner
#: settled on, and it decides whenever it is set. The timestamp is consulted
#: only for a row with no ``date_key``, only when it is a real capture time
#: (a dead clock is 0, which reads as 1 January 1970), and in local time:
#: ``captured_at`` is an instant made from the camera's local wall clock, so
#: read in UTC a photo taken at 2 a.m. in India was a memory of the day before
#: as well. ``mtime`` is never a capture date (scanner.capture_date).
_ON_DAY_SQL = ("(a.date_key LIKE ? OR (a.date_key = '' AND a.captured_at > 0 "
               "AND strftime('%m-%d', a.captured_at, 'unixepoch', 'localtime') = ?))")

#: The year a row was taken, by the same rules. ``SUBSTR('', 1, 4)`` is '' and
#: not NULL, so an undated row's year was '' and passed "before this year".
_YEAR_SQL = ("COALESCE(NULLIF(SUBSTR(a.date_key, 1, 4), ''), "
             "strftime('%Y', a.captured_at, 'unixepoch', 'localtime'))")


def query_on_this_day(
    conn: sqlite3.Connection,
    roots: Sequence[str] | str,
    *,
    month: int,
    day: int,
    viewer_id: int = 0,
    max_visibility: int = 1,
    scope: str | None = None,
    limit: int = 150,
) -> dict[str, Any]:
    """Return media items captured on this month and day across historical years."""
    roots_sql, roots_params = roots_clause("a", roots)
    where = [roots_sql, "a.trashed = 0", "a.nsfw = 0", visibility_clause("a", max_visibility)]
    params: list[Any] = [*roots_params, int(max_visibility)]

    scope_sql, scope_params = scope_clause("a", scope)
    if scope_sql:
        where.append(scope_sql)
        params.extend(scope_params)

    m_str = f"{month:02d}"
    d_str = f"{day:02d}"
    target_md = f"-{m_str}-{d_str}"

    where.append(_ON_DAY_SQL)
    params.extend([f"%{target_md}", f"{m_str}-{d_str}"])

    mine = "LEFT JOIN user_assets ua ON ua.asset_id = a.id AND ua.user_id = ?"
    join_params = [int(viewer_id)]
    where_sql = " AND ".join(where)

    projection = ("a.*, COALESCE(ua.favorite, 0) AS mine_favorite, "
                  "COALESCE(ua.rating, 0) AS mine_rating, "
                  + _YEAR_SQL + " AS year_str")

    rows = conn.execute(
        f"SELECT {projection} FROM assets a {mine} WHERE {where_sql} "
        f"ORDER BY COALESCE(a.captured_at, a.mtime) DESC LIMIT ?",
        (*join_params, *params, limit),
    ).fetchall()

    items = [row_to_dict(r) for r in rows]
    years_map: dict[str, list[dict[str, Any]]] = {}
    for idx, r in enumerate(rows):
        yr = str(r["year_str"]) if r["year_str"] else "Unknown"
        years_map.setdefault(yr, []).append(items[idx])

    return {
        "month": month,
        "day": day,
        "total": len(items),
        "items": items,
        "years": years_map,
    }


# ---------------------------------------------------------------------------
# Choosing one photograph worth sending
#
# "On this day" finds the candidates; this decides which single one a person
# should be shown. That is the whole difficulty. A day in this library offers
# a couple of hundred photographs across twenty years, and the largest trips
# run to two thousand frames — so a digest that sends a blurred burst frame,
# a screenshot or the ninth near-identical shot of the same moment teaches
# the household to ignore it, once, permanently.
#
# Nothing here is new analysis. The scan has already worked out which
# photographs have people in them, which are sharp, which are duplicates of
# each other and which are screenshots; this only reads those columns and
# puts them in an order. Ratings are deliberately not used: a household that
# has favourited four photographs out of two hundred thousand has told you
# nothing, and that silence is what a digest exists to fix.
# ---------------------------------------------------------------------------

#: What each signal is worth. Faces dominate on purpose — across this library
#: a little over half of everything has a person in it, and of the things
#: anybody wants sent on a Sunday morning, nearly all do.
PICK_WEIGHTS = {
    "face": 3.0,          # somebody is in it
    "faces_many": 1.5,    # a group: usually the picture of the occasion
    "sharp": 1.5,         # measured, not guessed
    "exposed": 0.5,       # not nearly black and not blown out
    "landscape": 0.2,     # reads better in a message than a tall crop
    "age": 2.0,           # see PICK_FORGOTTEN_YEARS
}

#: How far back a photograph has to be before it counts as fully forgotten.
#: The first version of this scored a flat bonus for "has faces and is sharp",
#: which on a real day tied a hundred and forty photographs at 6.2 and left
#: the choice between them to the sort order. Age is what actually separates
#: them: last year is a photograph you remember taking, and the point of a
#: digest is the ones you do not.
PICK_FORGOTTEN_YEARS = 15.0

#: Below this a photograph is soft enough that sending it is worse than
#: sending nothing. Measured at a fixed size by the quality pass, so one
#: threshold holds across cameras. The same default the scan flags "blurry"
#: at, because two thresholds for one idea is how they drift apart.
PICK_MIN_SHARPNESS = 45.0

#: Brightness is a mean on 0–1, not 0–255 (see `media.image_stats`, which
#: divides by 255). Getting that wrong is not a wrong answer but an empty
#: one: every photograph in the library sits inside 0.1 and 0.97, so a
#: 0–255 range refuses all of them. These are the scan's own `dark` and
#: `bright` thresholds.
PICK_BRIGHTNESS = (0.16, 0.86)


def pick_for_the_day(
    conn: sqlite3.Connection,
    roots: Sequence[str] | str,
    *,
    month: int,
    day: int,
    max_visibility: int = 1,
    scope: str | None = None,
    limit: int = 400,
) -> dict[str, Any] | None:
    """The one photograph from this day in past years worth sending, or None.

    None is a real answer and the caller must respect it: a day whose only
    photographs are screenshots, blurred, or hidden has nothing to say, and
    saying nothing is what keeps the household reading the ones that do
    arrive.

    Only pictures. A video cannot be embedded in a message, and a silent
    poster frame is a worse version of a photograph.
    """
    candidates = candidates_for_the_day(
        conn, roots, month=month, day=day,
        max_visibility=max_visibility, scope=scope, limit=limit)
    return candidates[0] if candidates else None


def candidates_for_the_day(
    conn: sqlite3.Connection,
    roots: Sequence[str] | str,
    *,
    month: int,
    day: int,
    max_visibility: int = 1,
    scope: str | None = None,
    limit: int = 400,
) -> list[dict[str, Any]]:
    """Every photograph from this day that could be sent, best first.

    Separate from :func:`pick_for_the_day` so that the console can show what
    the choice was made from, and so a test can assert on the order rather
    than on one winner.
    """
    roots_sql, roots_params = roots_clause("a", roots)
    where = [
        roots_sql,
        "a.trashed = 0",
        "a.nsfw = 0",
        # A message can hold a photograph and nothing else.
        "a.kind = 'picture'",
        # No thumbnail means nothing to embed, whatever else it has going
        # for it.
        "a.thumb IS NOT NULL",
        "a.thumb <> ''",
        visibility_clause("a", max_visibility),
    ]
    params: list[Any] = [*roots_params, int(max_visibility)]

    scope_sql, scope_params = scope_clause("a", scope)
    if scope_sql:
        where.append(scope_sql)
        params.extend(scope_params)

    target = f"{month:02d}-{day:02d}"
    where.append(_ON_DAY_SQL)
    params.extend([f"%-{target}", target])

    # This year is not a memory. The point is the years you have forgotten.
    where.append(_YEAR_SQL + " < ?")
    params.append(time.strftime("%Y"))

    rows = conn.execute(
        "SELECT a.*, "
        f"  {_YEAR_SQL} AS year_str, "
        "  (SELECT COUNT(*) FROM faces f WHERE f.asset_id = a.id) AS face_count "
        f"FROM assets a WHERE {' AND '.join(where)} "
        "ORDER BY COALESCE(a.captured_at, a.mtime) DESC LIMIT ?",
        (*params, int(limit)),
    ).fetchall()

    scored = []
    seen_groups: set[str] = set()
    for row in rows:
        item = row_to_dict(row)
        item["year"] = str(row["year_str"] or "")
        item["face_count"] = int(row["face_count"] or 0)
        score = _pick_score(item)
        if score is None:
            continue
        item["pick_score"] = round(score, 3)
        scored.append(item)

    scored.sort(key=lambda i: (-i["pick_score"], i.get("year") or ""))

    # One frame per burst. Duplicates are already grouped by the scan, and a
    # digest that offers the same moment twice has wasted the only two lines
    # anybody reads.
    spread = []
    for item in scored:
        group = item.get("dup_group") or ""
        if group and group in seen_groups:
            continue
        if group:
            seen_groups.add(group)
        spread.append(item)
    return spread


def _pick_score(item: dict[str, Any]) -> float | None:
    """How much this photograph is worth sending, or None to refuse it.

    Refusing is not scoring zero: a screenshot is not a weak memory, it is
    not a memory, and no amount of sharpness should let it through.
    """
    quality = item.get("quality")
    if isinstance(quality, str):
        try:
            quality = json.loads(quality)
        except ValueError:
            quality = []
    tags = {str(tag) for tag in (quality or [])}
    # The scan's own words for the things nobody wants sent to them.
    if tags & {"screenshot", "document", "screen"}:
        return None

    sharpness = item.get("sharpness")
    if sharpness is not None and float(sharpness) < PICK_MIN_SHARPNESS:
        return None
    brightness = item.get("brightness")
    if brightness is not None and not (
            PICK_BRIGHTNESS[0] <= float(brightness) <= PICK_BRIGHTNESS[1]):
        return None

    faces = int(item.get("face_count") or 0)
    score = 0.0
    if faces:
        score += PICK_WEIGHTS["face"]
    if faces > 1:
        # Two people is much more than one; six is not much more than five,
        # and beyond that it is a crowd nobody is the subject of.
        score += PICK_WEIGHTS["faces_many"] * min(1.0, (faces - 1) / 4.0)
    # Measured sharpness counts for something beyond the floor, but with a
    # ceiling: a very sharp photograph of nothing is still of nothing.
    # Sharpness runs from tens into the thousands in this library, so the
    # curve flattens early: past "clearly in focus" a sharper photograph is
    # not a better memory.
    if sharpness is not None:
        score += PICK_WEIGHTS["sharp"] * min(1.0, float(sharpness) / 300.0)
    if brightness is not None:
        middle = 1.0 - abs(float(brightness) - 0.5) / 0.5
        score += PICK_WEIGHTS["exposed"] * max(0.0, middle)
    width, height = item.get("width") or 0, item.get("height") or 0
    if width and height and width >= height:
        score += PICK_WEIGHTS["landscape"]
    year = item.get("year") or ""
    if year.isdigit():
        behind = int(time.strftime("%Y")) - int(year)
        score += PICK_WEIGHTS["age"] * min(1.0, behind / PICK_FORGOTTEN_YEARS)
    return score


def query_geo_points(
    conn: sqlite3.Connection,
    roots: Sequence[str] | str,
    *,
    viewer_id: int = 0,
    max_visibility: int = 1,
    scope: str | None = None,
    north: float | None = None,
    south: float | None = None,
    east: float | None = None,
    west: float | None = None,
    limit: int = 1500,
) -> list[dict[str, Any]]:
    """Return assets with GPS coordinates for map exploration."""
    roots_sql, roots_params = roots_clause("a", roots)
    where = [
        roots_sql,
        "a.trashed = 0",
        "a.nsfw = 0",
        "a.gps_lat IS NOT NULL",
        "a.gps_lon IS NOT NULL",
        visibility_clause("a", max_visibility),
    ]
    params: list[Any] = [*roots_params, int(max_visibility)]

    scope_sql, scope_params = scope_clause("a", scope)
    if scope_sql:
        where.append(scope_sql)
        params.extend(scope_params)

    if north is not None and south is not None:
        where.append("a.gps_lat BETWEEN ? AND ?")
        params.extend([min(south, north), max(south, north)])
    if east is not None and west is not None:
        if west <= east:
            where.append("a.gps_lon BETWEEN ? AND ?")
            params.extend([west, east])
        else:
            where.append("(a.gps_lon >= ? OR a.gps_lon <= ?)")
            params.extend([west, east])

    where_sql = " AND ".join(where)
    rows = conn.execute(
        f"SELECT a.id, a.filename, a.gps_lat, a.gps_lon, a.thumb, a.date_key, a.kind, "
        f"a.city, a.country, a.is_live, a.duration FROM assets a WHERE {where_sql} "
        f"ORDER BY COALESCE(a.captured_at, a.mtime) DESC LIMIT ?",
        (*params, limit),
    ).fetchall()

    return [
        {
            "id": int(r["id"]),
            "filename": r["filename"],
            "lat": float(r["gps_lat"]),
            "lon": float(r["gps_lon"]),
            "thumb": r["thumb"],
            "date": r["date_key"],
            "kind": r["kind"],
            "city": r["city"],
            "country": r["country"],
            "is_live": bool(r["is_live"]),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Tokenized Shares
# ---------------------------------------------------------------------------

def create_share(
    conn: sqlite3.Connection,
    token: str,
    scope: str,
    target_id: int,
    created_by: int | None = None,
    expires_at: float | None = None,
    password: str | None = None,
) -> dict[str, Any]:
    now = time.time()
    with _write_lock:
        conn.execute(
            "INSERT INTO shares(token, scope, target_id, created_at, created_by, expires_at, password, view_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
            (token, scope, target_id, now, created_by, expires_at, password),
        )
        conn.commit()
    return get_share(conn, token) or {}


def get_share(conn: sqlite3.Connection, token: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM shares WHERE token=?", (token,)).fetchone()
    if not row:
        return None
    d = dict(row)
    if d.get("expires_at") and time.time() > float(d["expires_at"]):
        d["expired"] = True
    else:
        d["expired"] = False
    return d


def increment_share_views(conn: sqlite3.Connection, token: str) -> None:
    with _write_lock:
        conn.execute("UPDATE shares SET view_count = view_count + 1 WHERE token=?", (token,))
        conn.commit()


def delete_share(conn: sqlite3.Connection, token: str) -> None:
    with _write_lock:
        conn.execute("DELETE FROM shares WHERE token=?", (token,))
        conn.commit()


def list_shares(conn: sqlite3.Connection, created_by: int | None = None) -> list[dict[str, Any]]:
    if created_by is not None:
        rows = conn.execute("SELECT * FROM shares WHERE created_by=? ORDER BY created_at DESC", (created_by,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM shares ORDER BY created_at DESC").fetchall()
    now = time.time()
    res = []
    for r in rows:
        d = dict(r)
        d["expired"] = bool(d.get("expires_at") and now > float(d["expires_at"]))
        d.pop("password", None)
        res.append(d)
    return res


# ---------------------------------------------------------------------------
# Bitrot Integrity Scrubber
# ---------------------------------------------------------------------------

def record_bitrot_check(
    conn: sqlite3.Connection,
    asset_id: int,
    root: str,
    rel_path: str,
    expected_hash: str | None,
    actual_hash: str | None,
    status: str,
    file_mtime: float | None = None,
    file_size: int | None = None,
) -> None:
    """Write down one check."""
    record_bitrot_checks(conn, [(asset_id, root, rel_path, expected_hash, actual_hash,
                                 status, file_mtime, file_size)])


def record_bitrot_checks(conn: sqlite3.Connection, checks: list[tuple]) -> None:
    """Write down several checks in one transaction, each ``(asset_id, root,
    rel_path, expected_hash, actual_hash, status, file_mtime, file_size)``.

    The storage check gathers about a second of files and writes them here:
    a commit per file was most of the cost of checking a library of small
    photographs. They are written and committed under the one lock, so no
    transaction is left open between files. It used to be (rows written with
    the lock taken and let go each time, the commit later): a scan that took
    the lock meanwhile waited on SQLite for the check's open transaction while
    the check waited for the lock, and after 30 seconds the scan failed with
    "database is locked".
    """
    if not checks:
        return
    now = time.time()
    with _write_lock:
        # A scan can remove an asset after the scrubber took its snapshot.
        # Check membership in the INSERT itself so that race cannot abort the
        # rest of the batch (or its alerts) with a foreign-key error.
        conn.executemany(
            "INSERT INTO bitrot_records(asset_id, root, rel_path, expected_hash, "
            "actual_hash, status, file_mtime, file_size, checked_at) "
            "SELECT ?, ?, ?, ?, ?, ?, ?, ?, ? "
            "WHERE EXISTS (SELECT 1 FROM assets WHERE id=?)",
            [(*check, now, check[0]) for check in checks],
        )
        conn.commit()


def last_bitrot_fingerprint(conn: sqlite3.Connection,
                            asset_id: int) -> dict[str, Any] | None:
    """The most recent recorded fingerprint for one asset, if there is one.

    Only rows that actually carry a hash count. A ``missing`` or ``unreadable``
    result records no bytes, so it must not displace the last good fingerprint
    — otherwise unplugging a drive would quietly erase the baseline that a
    later comparison depends on.
    """
    row = conn.execute(
        "SELECT * FROM bitrot_records "
        "WHERE asset_id = ? AND actual_hash IS NOT NULL "
        "ORDER BY id DESC LIMIT 1",
        (int(asset_id),),
    ).fetchone()
    return dict(row) if row else None


#: Every state a scrubber pass can leave a file in.
BITROT_STATES = ("baseline", "verified", "changed", "corrupt", "missing", "unreadable",
                 # Put back from a copy whose bytes matched (storage/repair.py).
                 "repaired")


def current_bitrot_issues(conn: sqlite3.Connection, limit: int = 60) -> list[dict[str, Any]]:
    """Files whose *latest* check found something wrong — not every row that
    ever did, so a file since repaired or put back is not still listed."""
    rows = conn.execute(
        "SELECT b.* FROM bitrot_records b "
        "WHERE b.id = (SELECT MAX(b2.id) FROM bitrot_records b2 WHERE b2.asset_id = b.asset_id) "
        "AND b.status IN ('corrupt', 'missing', 'unreadable') "
        "ORDER BY b.checked_at DESC LIMIT ?", (int(limit),)).fetchall()
    return [dict(r) for r in rows]


def get_bitrot_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """Where the library stands now — one answer per file, not per check.

    Each pass writes a row per file, so counting rows made every total grow
    with the number of runs: three photographs checked three times reported
    nine verified. What anybody actually wants is the current state of each
    file, which is its most recent row.
    """
    total_assets = conn.execute(
        "SELECT COUNT(*) n FROM assets WHERE trashed=0").fetchone()["n"]

    counts = {state: 0 for state in BITROT_STATES}
    rows = conn.execute(
        "SELECT b.status AS status, COUNT(*) AS n FROM bitrot_records b "
        "WHERE b.id = (SELECT MAX(b2.id) FROM bitrot_records b2 "
        "              WHERE b2.asset_id = b.asset_id) "
        "GROUP BY b.status"
    ).fetchall()
    for row in rows:
        counts[str(row["status"])] = int(row["n"])

    last_check = conn.execute(
        "SELECT MAX(checked_at) t FROM bitrot_records").fetchone()["t"]
    summary: dict[str, Any] = {
        "total_assets": total_assets,
        "last_checked_at": last_check,
    }
    summary.update(counts)
    # Anything a person should look at, as one number for the console.
    summary["issues"] = counts["corrupt"] + counts["missing"] + counts["unreadable"]
    return summary


def list_bitrot_records(conn: sqlite3.Connection, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    if status:
        rows = conn.execute(
            "SELECT * FROM bitrot_records WHERE status=? ORDER BY checked_at DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM bitrot_records ORDER BY checked_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def clear_bitrot_records(conn: sqlite3.Connection) -> None:
    with _write_lock:
        conn.execute("DELETE FROM bitrot_records")
        conn.commit()


# ---------------------------------------------------------------------------
# Faces and people
# ---------------------------------------------------------------------------
#
# One rule governs everything below: a face is never reachable except through
# its asset, and the asset is filtered by exactly the same clauses the gallery
# uses. Every helper here that a family member can reach takes ``roots``,
# ``max_visibility`` and ``scope`` and joins ``assets`` to apply them.
#
# The consequence worth stating plainly: a person's face count is the number
# of photographs *that viewer* may see them in. Two people looking at the same
# library see different numbers, and neither can learn from the difference
# that a hidden photograph exists.

#: Applied to every family-facing face query. Kept as one fragment for the
#: same reason :func:`visibility_clause` is: so a later query cannot remember
#: three of the four rules.
def _face_visibility_sql(alias: str, max_visibility: int,
                         scope: str | None,
                         roots: "Sequence[str] | str") -> tuple[str, list[Any]]:
    roots_sql, params = roots_clause(alias, roots)
    where = [roots_sql, visibility_clause(alias, max_visibility)]
    params = [*params, int(max_visibility)]
    scope_sql, scope_params = scope_clause(alias, scope)
    if scope_sql:
        where.append(scope_sql)
        params.extend(scope_params)
    where.append(f"{alias}.trashed = 0")
    where.append(f"{alias}.nsfw = 0")
    return " AND ".join(where), params


def replace_asset_faces(conn: sqlite3.Connection, asset_id: int,
                        faces: "Sequence[dict[str, Any]]", model: str) -> int:
    """Store the faces found in one asset, replacing whatever was there.

    Assignments an admin confirmed are carried across a re-detection by
    position: a face whose box overlaps a previously confirmed face keeps that
    person. Re-running detection is otherwise a quiet way to throw away a
    afternoon of somebody's naming.
    """
    now = time.time()
    with _write_lock:
        if not ai_may_read(conn, asset_id):
            # Hidden while the face pass was working through its list.
            return 0
        previous = conn.execute(
            "SELECT id, person_id, source, bbox FROM faces "
            "WHERE asset_id=? AND source='confirmed'", (asset_id,)
        ).fetchall()
        keep: list[tuple[tuple[float, float, float, float], int]] = []
        for row in previous:
            try:
                box = json.loads(row["bbox"])
                if row["person_id"]:
                    keep.append((tuple(float(v) for v in box), int(row["person_id"])))
            except (TypeError, ValueError):
                continue

        conn.execute("DELETE FROM faces WHERE asset_id=?", (asset_id,))
        written = 0
        for face in faces:
            bbox = list(face["bbox"])
            person_id, source, confidence = None, "none", 0.0
            for old_box, old_person in keep:
                if _box_iou(old_box, bbox) >= 0.5:
                    person_id, source, confidence = old_person, "confirmed", 1.0
                    break
            conn.execute(
                "INSERT INTO faces(asset_id, person_id, source, confidence, "
                "bbox, landmarks, det_score, sharpness, quality, embedding, "
                "thumb, model, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (asset_id, person_id, source, confidence,
                 json.dumps(bbox), json.dumps(face.get("landmarks") or []),
                 float(face.get("det_score") or 0), float(face.get("sharpness") or 0),
                 float(face.get("quality") or 0), face["embedding"],
                 face.get("thumb"), model, now),
            )
            written += 1
        conn.execute("UPDATE assets SET face_version=? WHERE id=?",
                     (int(face_schema_version()), asset_id))
        conn.commit()
    return written


def _box_iou(a: "Sequence[float]", b: "Sequence[float]") -> float:
    ax, ay, aw, ah = (float(v) for v in a)
    bx, by, bw, bh = (float(v) for v in b)
    left, top = max(ax, bx), max(ay, by)
    right, bottom = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    if right <= left or bottom <= top:
        return 0.0
    overlap = (right - left) * (bottom - top)
    union = aw * ah + bw * bh - overlap
    return overlap / union if union > 0 else 0.0


def face_schema_version() -> int:
    from ..media.faces import FACE_VERSION  # noqa: PLC0415 - avoids an import cycle
    return FACE_VERSION


def mark_faces_scanned(conn: sqlite3.Connection, asset_id: int,
                       version: int) -> None:
    with _write_lock:
        conn.execute("UPDATE assets SET face_version=? WHERE id=?",
                     (int(version), asset_id))
        conn.commit()


def assets_needing_faces(conn: sqlite3.Connection, root: str, version: int,
                         limit: int = 0) -> list[dict[str, Any]]:
    """Pictures whose faces have not been found at this detector version."""
    sql = ("SELECT id, rel_path, rotation FROM assets "
           "WHERE root=? AND trashed=0 AND kind='picture' AND face_version < ? "
           f"AND {AI_MAY_READ} "
           "ORDER BY COALESCE(captured_at, mtime) DESC")
    params: list[Any] = [root, int(version)]
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def count_assets_needing_faces(conn: sqlite3.Connection, root: str, version: int) -> int:
    """How many pictures :func:`assets_needing_faces` would list."""
    return conn.execute(
        "SELECT COUNT(*) FROM assets "
        "WHERE root=? AND trashed=0 AND kind='picture' AND face_version < ? "
        f"AND {AI_MAY_READ}",
        (root, int(version))).fetchone()[0]


def load_faces(conn: sqlite3.Connection, *, person_id: int | None = None,
               unassigned: bool = False, roots: "Sequence[str] | str | None" = None,
               min_quality: float = 0.0,
               limit: int = 0) -> list[dict[str, Any]]:
    """Faces with their embeddings, for the matcher. Console-side only."""
    where, params = ["1=1"], []
    if roots is not None:
        roots_sql, roots_params = roots_clause("a", roots)
        where.append(roots_sql)
        params.extend(roots_params)
    where.append("a.trashed = 0")
    if person_id is not None:
        where.append("f.person_id = ?")
        params.append(int(person_id))
    if unassigned:
        where.append("f.person_id IS NULL")
    if min_quality:
        where.append("f.quality >= ?")
        params.append(float(min_quality))
    sql = ("SELECT f.id, f.asset_id, f.person_id, f.source, f.quality, "
           "f.embedding, f.cluster_key, f.thumb, f.bbox "
           "FROM faces f JOIN assets a ON a.id = f.asset_id "
           f"WHERE {' AND '.join(where)} ORDER BY f.quality DESC")
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def list_people(conn: sqlite3.Connection, roots: "Sequence[str] | str", *,
                max_visibility: int = 1, scope: str | None = None,
                min_faces: int = 1) -> list[dict[str, Any]]:
    """Named people, counted as *this viewer* can see them.

    A person whose every appearance is in hidden photographs does not appear
    in the list at all for a family member — not as a name with a zero beside
    it, which would leak the fact that they are in the library somewhere.
    """
    guard, params = _face_visibility_sql("a", max_visibility, scope, roots)
    rows = conn.execute(
        "SELECT p.id, p.name, p.cover_face_id, p.avatar_asset_id, "
        "       COUNT(DISTINCT f.asset_id) AS photo_count, "
        "       COUNT(f.id) AS face_count, "
        "       MAX(f.id = p.cover_face_id) AS cover_seen "
        "FROM people_clusters p "
        "JOIN faces f ON f.person_id = p.id "
        "JOIN assets a ON a.id = f.asset_id "
        f"WHERE {guard} "
        "GROUP BY p.id, p.name, p.cover_face_id, p.avatar_asset_id "
        "HAVING COUNT(f.id) >= ? "
        "ORDER BY photo_count DESC, p.name COLLATE NOCASE",
        (*params, int(min_faces)),
    ).fetchall()
    people = []
    for row in rows:
        person = dict(row)
        # The stored cover was chosen from every face, but the crop is served
        # only to somebody who may open its photograph. A family member was
        # shown a broken cover for anyone whose best face is in a photograph
        # from before their date cutoff, or one marked NSFW. Their best face
        # among the photographs they can see instead.
        if not person.pop("cover_seen"):
            best = person_face_ids(conn, person["id"], roots,
                                   max_visibility=max_visibility, scope=scope,
                                   limit=1)
            person["cover_face_id"] = best[0]["id"] if best else None
        if person.get("avatar_asset_id") is not None:
            # The chosen avatar photograph, named only to someone who may open
            # it: the id alone says a hidden photograph exists.
            seen = conn.execute(
                f"SELECT 1 FROM assets a WHERE a.id = ? AND {guard}",
                (person["avatar_asset_id"], *params)).fetchone()
            if seen is None:
                person["avatar_asset_id"] = None
        people.append(person)
    return people


def person_face_ids(conn: sqlite3.Connection, person_id: int,
                    roots: "Sequence[str] | str", *, max_visibility: int = 1,
                    scope: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
    """This person's faces, filtered to what the viewer may see."""
    guard, params = _face_visibility_sql("a", max_visibility, scope, roots)
    rows = conn.execute(
        "SELECT f.id, f.asset_id, f.bbox, f.quality, f.source, f.confidence, f.thumb "
        "FROM faces f JOIN assets a ON a.id = f.asset_id "
        f"WHERE {guard} AND f.person_id = ? "
        "ORDER BY f.quality DESC LIMIT ?",
        (*params, int(person_id), int(limit)),
    ).fetchall()
    return [dict(r) for r in rows]


def faces_for_asset(conn: sqlite3.Connection, asset_id: int) -> list[dict[str, Any]]:
    """Every face in one asset. Callers must already have cleared the asset."""
    rows = conn.execute(
        "SELECT f.id, f.person_id, f.bbox, f.quality, f.source, f.confidence, "
        "       p.name AS person_name "
        "FROM faces f LEFT JOIN people_clusters p ON p.id = f.person_id "
        "WHERE f.asset_id = ? ORDER BY f.quality DESC", (asset_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_face(conn: sqlite3.Connection, face_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT f.*, a.root, a.folder, a.visibility, a.trashed, a.nsfw, a.kind, a.date_key "
        "FROM faces f JOIN assets a ON a.id = f.asset_id WHERE f.id = ?",
        (int(face_id),)
    ).fetchone()
    return dict(row) if row else None


def set_face_person(conn: sqlite3.Connection, face_id: int,
                    person_id: int | None, source: str = "confirmed",
                    confidence: float = 1.0) -> None:
    with _write_lock:
        conn.execute(
            "UPDATE faces SET person_id=?, source=?, confidence=? WHERE id=?",
            (person_id, source, float(confidence), int(face_id)))
        conn.commit()


def set_faces_person(conn: sqlite3.Connection,
                     rows: "Sequence[tuple[int, int | None, str, float]]") -> None:
    """:func:`set_face_person` for many faces, as ``(face_id, person_id,
    source, confidence)``, in one commit.

    Naming a group or a regroup assigns hundreds of faces at a time. One commit
    each was a disk sync each, with the write lock taken and given back
    between them, which kept every other writer waiting behind the naming.

    Only a ``confirmed`` row — a person saying so — is written whatever the
    face holds. Anything else is the matcher's guess, worked out from a read
    taken before the write, and in between somebody may have confirmed the face
    as someone else or said "not this person". So a guess only lands on a face
    that is still unassigned and unconfirmed, and never on a person it was
    rejected for; the rejection is checked in the same statement, not from a
    list read earlier.
    """
    if not rows:
        return
    human = [(person_id, source, float(confidence), int(face_id))
             for face_id, person_id, source, confidence in rows
             if source == "confirmed"]
    guessed = [(person_id, source, float(confidence), int(face_id), person_id)
               for face_id, person_id, source, confidence in rows
               if source != "confirmed"]
    with _write_lock:
        if human:
            conn.executemany(
                "UPDATE faces SET person_id=?, source=?, confidence=? WHERE id=?",
                human)
        if guessed:
            conn.executemany(
                "UPDATE faces SET person_id=?, source=?, confidence=? "
                "WHERE id=? AND person_id IS NULL AND source <> 'confirmed' "
                "AND NOT EXISTS (SELECT 1 FROM face_rejections r "
                "WHERE r.face_id = faces.id AND r.person_id = ?)",
                guessed)
        conn.commit()


def reject_face_for_person(conn: sqlite3.Connection, face_id: int,
                           person_id: int) -> None:
    """Record 'not this person', permanently, and detach if it was attached."""
    with _write_lock:
        conn.execute(
            "INSERT OR IGNORE INTO face_rejections(face_id, person_id, created_at) "
            "VALUES (?,?,?)", (int(face_id), int(person_id), time.time()))
        conn.execute(
            "UPDATE faces SET person_id=NULL, source='none', confidence=0 "
            "WHERE id=? AND person_id=?", (int(face_id), int(person_id)))
        conn.commit()


def rejections(conn: sqlite3.Connection,
               person_id: int | None = None) -> set[tuple[int, int]]:
    sql = "SELECT face_id, person_id FROM face_rejections"
    params: tuple = ()
    if person_id is not None:
        sql += " WHERE person_id = ?"
        params = (int(person_id),)
    return {(int(r["face_id"]), int(r["person_id"]))
            for r in conn.execute(sql, params).fetchall()}


def save_person_centroid(conn: sqlite3.Connection, person_id: int,
                         centroid: bytes | None, confirmed: int,
                         total: int, cover_face_id: int | None = None) -> None:
    with _write_lock:
        conn.execute(
            "UPDATE people_clusters SET centroid=?, confirmed_count=?, "
            "face_count=?, cover_face_id=COALESCE(?, cover_face_id), updated_at=? "
            "WHERE id=?",
            (centroid, int(confirmed), int(total), cover_face_id,
             time.time(), int(person_id)))
        conn.commit()


def save_cluster_keys(conn: sqlite3.Connection,
                      pairs: "Sequence[tuple[int, str | None]]") -> None:
    """Write the unnamed-cluster label back onto each face."""
    if not pairs:
        return
    with _write_lock:
        conn.executemany("UPDATE faces SET cluster_key=? WHERE id=?",
                         [(key, face_id) for face_id, key in pairs])
        conn.commit()


def list_unnamed_clusters(conn: sqlite3.Connection, roots: "Sequence[str] | str",
                          *, max_visibility: int = 2, scope: str | None = None,
                          min_size: int = 3, limit: int = 60) -> list[dict[str, Any]]:
    """Groups the matcher formed that nobody has named yet."""
    guard, params = _face_visibility_sql("a", max_visibility, scope, roots)
    rows = conn.execute(
        "SELECT f.cluster_key, COUNT(*) AS size, "
        "       COUNT(DISTINCT f.asset_id) AS photo_count, "
        "       MAX(f.quality) AS best_quality "
        "FROM faces f JOIN assets a ON a.id = f.asset_id "
        f"WHERE {guard} AND f.person_id IS NULL AND f.cluster_key IS NOT NULL "
        "GROUP BY f.cluster_key HAVING COUNT(*) >= ? "
        "ORDER BY size DESC LIMIT ?",
        (*params, int(min_size), int(limit)),
    ).fetchall()
    out = [dict(row) for row in rows]
    # The best face of each group in one statement rather than one per group:
    # SQLite hands back the other columns from the row that holds MAX(), which
    # is the documented way to ask for "the row with the highest quality".
    covers: dict[str, dict[str, Any]] = {}
    keys = [item["cluster_key"] for item in out]
    for start in range(0, len(keys), 500):
        piece = keys[start:start + 500]
        for cover in conn.execute(
                "SELECT cluster_key, id, asset_id, thumb, MAX(quality) AS quality "
                "FROM faces WHERE person_id IS NULL AND cluster_key IN (%s) "
                "GROUP BY cluster_key" % ",".join("?" * len(piece)), piece):
            covers[cover["cluster_key"]] = {
                "id": cover["id"], "asset_id": cover["asset_id"], "thumb": cover["thumb"]}
    for item in out:
        item["cover"] = covers.get(item["cluster_key"])
    return out


def count_unnamed_clusters(conn: sqlite3.Connection, roots: "Sequence[str] | str",
                           *, max_visibility: int = 2, scope: str | None = None,
                           min_size: int = 3, limit: int = 60) -> int:
    """How many groups :func:`list_unnamed_clusters` would list, without
    building them: the console's overview only wants the number."""
    guard, params = _face_visibility_sql("a", max_visibility, scope, roots)
    return int(conn.execute(
        "SELECT COUNT(*) FROM (SELECT f.cluster_key "
        "FROM faces f JOIN assets a ON a.id = f.asset_id "
        f"WHERE {guard} AND f.person_id IS NULL AND f.cluster_key IS NOT NULL "
        "GROUP BY f.cluster_key HAVING COUNT(*) >= ? LIMIT ?)",
        (*params, int(min_size), int(limit))).fetchone()[0])


def cluster_face_ids(conn: sqlite3.Connection, cluster_key: str,
                     limit: int = 500) -> list[int]:
    return [int(r["id"]) for r in conn.execute(
        "SELECT id FROM faces WHERE cluster_key=? AND person_id IS NULL "
        "ORDER BY quality DESC LIMIT ?", (cluster_key, int(limit)))]


def merge_people(conn: sqlite3.Connection, source_id: int, target_id: int) -> int:
    """Fold one person into another, keeping every confirmation."""
    if int(source_id) == int(target_id):
        return 0
    with _write_lock:
        cur = conn.execute("UPDATE faces SET person_id=? WHERE person_id=?",
                           (int(target_id), int(source_id)))
        conn.execute(
            "INSERT OR IGNORE INTO face_rejections(face_id, person_id, created_at) "
            "SELECT face_id, ?, created_at FROM face_rejections WHERE person_id=?",
            (int(target_id), int(source_id)))
        conn.execute("DELETE FROM people_clusters WHERE id=?", (int(source_id),))
        conn.commit()
    return cur.rowcount or 0


def mark_large_files_reviewed(conn: sqlite3.Connection, ids: Sequence[int]) -> int:
    """"Keep" on the largest-files screen: stop asking about these.

    Nothing about the file changes -- it is not touched, moved, or scored.
    The only effect is that :func:`query_assets` with
    ``exclude_reviewed_large=True`` stops returning it. There is no
    "un-keep" because there is nothing to undo: the file was never anything
    but itself, and the ordinary way to find it again is the ordinary way to
    find any file.
    """
    clean = [int(i) for i in ids if i]
    if not clean:
        return 0
    with _write_lock:
        cur = conn.execute(
            "UPDATE assets SET large_file_reviewed_at=? "
            f"WHERE id IN ({','.join('?' * len(clean))})",
            (time.time(), *clean))
        conn.commit()
    return cur.rowcount or 0


def face_stats(conn: sqlite3.Connection, root: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT COUNT(*) AS faces, "
        "       SUM(CASE WHEN f.person_id IS NOT NULL THEN 1 ELSE 0 END) AS assigned, "
        "       SUM(CASE WHEN f.source='confirmed' THEN 1 ELSE 0 END) AS confirmed, "
        "       COUNT(DISTINCT f.asset_id) AS photos "
        "FROM faces f JOIN assets a ON a.id=f.asset_id WHERE a.root=? AND a.trashed=0",
        (root,)).fetchone()
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM assets "
        "WHERE root=? AND trashed=0 AND kind='picture' AND face_version < ? "
        f"AND {AI_MAY_READ}",
        (root, int(face_schema_version()))).fetchone()
    # People with at least one face in this library, the same people the
    # People list can show. A bare count of names also counted names left with
    # no faces behind them, so the number and the list disagreed.
    people = conn.execute(
        "SELECT COUNT(DISTINCT f.person_id) AS n "
        "FROM faces f JOIN assets a ON a.id=f.asset_id "
        "WHERE f.person_id IS NOT NULL AND a.root=? AND a.trashed=0 AND a.nsfw=0",
        (root,)).fetchone()
    return {
        "faces": int(row["faces"] or 0),
        "assigned": int(row["assigned"] or 0),
        "confirmed": int(row["confirmed"] or 0),
        "photos_with_faces": int(row["photos"] or 0),
        "photos_pending": int(pending["n"] or 0),
        "people": int(people["n"] or 0),
    }


# ---------------------------------------------------------------------------
# People Clusters
# ---------------------------------------------------------------------------

def list_people_clusters(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM people_clusters ORDER BY name COLLATE NOCASE").fetchall()
    return [dict(r) for r in rows]


def create_or_update_person_cluster(
    conn: sqlite3.Connection,
    name: str,
    avatar_asset_id: int | None = None,
) -> dict[str, Any]:
    now = time.time()
    with _write_lock:
        row = conn.execute("SELECT id FROM people_clusters WHERE name=? COLLATE NOCASE", (name,)).fetchone()
        if row:
            cid = int(row["id"])
            if avatar_asset_id is not None:
                conn.execute("UPDATE people_clusters SET avatar_asset_id=? WHERE id=?", (avatar_asset_id, cid))
        else:
            cur = conn.execute(
                "INSERT INTO people_clusters(name, avatar_asset_id, created_at) VALUES (?, ?, ?)",
                (name, avatar_asset_id, now),
            )
            cid = int(cur.lastrowid)
        conn.commit()
    r = conn.execute("SELECT * FROM people_clusters WHERE id=?", (cid,)).fetchone()
    return _person_cluster_dict(r)


def delete_person_cluster(conn: sqlite3.Connection, cluster_id: int) -> None:
    with _write_lock:
        conn.execute("DELETE FROM people_clusters WHERE id=?", (cluster_id,))
        conn.commit()


def _person_cluster_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    """A ``people_clusters`` row, safe to hand straight to ``jsonify``.

    ``centroid`` is a packed float32 BLOB (see :func:`save_person_centroid`)
    that nothing outside this module ever reads back — matching always
    rebuilds a person's centroid live from their confirmed faces (see
    :meth:`FaceIndexer._load_people`). Left in, it is a raw ``bytes`` object
    Flask's JSON encoder cannot serialise, so any route that returns one of
    these rows straight from ``SELECT *`` — naming a cluster with a name that
    already belongs to somebody with confirmed faces, or renaming into one —
    500s the moment that person actually has a centroid, which is to say the
    moment they matter.
    """
    if row is None:
        return {}
    person = dict(row)
    person.pop("centroid", None)
    return person


def get_person_cluster(conn: sqlite3.Connection, person_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM people_clusters WHERE id=?", (int(person_id),)
    ).fetchone()
    return _person_cluster_dict(row) if row else None


def rename_person_cluster(conn: sqlite3.Connection, person_id: int,
                          new_name: str) -> dict[str, Any]:
    """Fix a name — or, if it is already somebody else's, fold this person
    into them instead.

    A rename and a merge are the same admission looked at from two sides:
    "I spelled that wrong" and "these are the same person" both mean the
    library should end up with one row, named once. Typing a name that
    already belongs to somebody else is how an admin says the second thing,
    so it is honoured as a merge rather than left to produce two people who
    happen to share a name. The match is case-insensitive for the same
    reason naming an unnamed cluster already is (see
    :func:`create_or_update_person_cluster`): "Priya" and "priya" are a typo
    of case, not two different people.
    """
    new_name = new_name.strip()
    target_id: int | None = None
    with _write_lock:
        existing = conn.execute(
            "SELECT id FROM people_clusters WHERE name=? COLLATE NOCASE AND id != ?",
            (new_name, int(person_id)),
        ).fetchone()
        if existing:
            target_id = int(existing["id"])
        else:
            conn.execute(
                "UPDATE people_clusters SET name=?, updated_at=? WHERE id=?",
                (new_name, time.time(), int(person_id)),
            )
        conn.commit()

    if target_id is not None:
        # merge_people takes the lock itself, so it is called after the one
        # above is released rather than nested inside it.
        merge_people(conn, int(person_id), target_id)
        return {"person": get_person_cluster(conn, target_id), "merged": True,
                "person_id": target_id}

    return {"person": get_person_cluster(conn, int(person_id)), "merged": False,
            "person_id": int(person_id)}


# Search vectors live in embeddings.py; these names are part of db's interface.
from .embeddings import (  # noqa: E402,F401 - re-exported
    embedding_store, forget_embeddings, load_embeddings, store_embedding,
    visible_embedding_ids,
)
