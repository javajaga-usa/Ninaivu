"""Voice stories: somebody saying what a photograph is, in their own voice.

"This is my father's shop in 1968." A grandparent knows that and the
photograph does not, and the knowing goes when they do. So anybody in the
household may record a few minutes about a photograph or a video, in Tamil or
English, and it is kept with that item: played back in the viewer, and found by
the gallery's search through whatever words were typed beside it.

Where the sound lives. The bytes are kept under Ninaivu's state folder, in
``stories/`` beside the index — never in the library. The library is the
household's photographs, copied to the second disk and to Drive and imported
from phones; a recording made *about* a photograph is not one of them, and
writing it there would put it in the gallery as an audio item of its own.

Who may hear one. A story has no visibility of its own: it is part of its item,
and is shown to exactly whoever may open that item — the per-asset guard in
the API decides, the same one the file and the faces use. That is also why the
words are searched through ``db.query_assets``, inside each viewer's limits,
and never listed on their own.

What happens when the item goes. Rows go with their asset by the foreign key,
and again by a trigger for a connection that has foreign keys off (a tool, a
migration that turned them off for a moment). A row leaving, however it leaves,
writes its file's name into ``stories_gone``, and :func:`sweep` deals with
those files once nothing could bring the row back. Only a story somebody
deleted, or one whose photograph was erased from the bin, has its sound
deleted. Any other way a row goes (a photograph moved or renamed in Finder,
which the scan sees as one file gone and one new, or a library folder taken
out of the console) moves the recording to ``stories/_orphaned/`` with a note
of the photograph it was about: the recording is the only copy there is. A photograph put in the recycle bin
keeps its stories in the bin's entry (storage/recycle.py), so its sound is kept
until that entry is erased or restored. SQL can delete rows; only Python can
delete files, and this is the bridge between the two that does not depend on
every place that deletes an asset remembering to call it.

Nothing here leaves the house. No recording or word of one is sent to any
outside service; the copy of the index kept in Drive leaves the tables out
(cloud/index_copy.py). With the on-device AI engine switched on, what is said
is written down on this computer (runtime/speech.py, storage/story_words.py)
and searched with the typed words.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

__all__ = ["SCHEMA", "FOLDER", "MAX_BYTES", "MAX_TEXT", "MAX_SPEAKER", "AUDIO_TYPES",
           "sniff", "add", "get", "for_asset", "counts", "delete", "sweep",
           "folder_for", "words_clause", "matching_asset_ids"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS stories (
    id          INTEGER PRIMARY KEY,
    asset_id    INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    file        TEXT NOT NULL,
    mime        TEXT NOT NULL,
    duration    REAL,
    size        INTEGER NOT NULL DEFAULT 0,
    speaker     TEXT NOT NULL DEFAULT '',
    text        TEXT NOT NULL DEFAULT '',
    created_by  INTEGER,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stories_asset ON stories(asset_id);

-- Files whose row has gone, for sweep() to delete. Written by the trigger
-- below, so a row removed by a cascade, by hand or by any other module is
-- noticed as surely as one removed here.
CREATE TABLE IF NOT EXISTS stories_gone (
    file     TEXT PRIMARY KEY,
    gone_at  REAL NOT NULL
);

-- The foreign key already does this; it is only on where the connection
-- turned it on, and a story left pointing at a reused asset id would be
-- played on a photograph it was never about.
CREATE TRIGGER IF NOT EXISTS stories_follow_asset AFTER DELETE ON assets
BEGIN DELETE FROM stories WHERE asset_id = OLD.id; END;

-- What each story was about, noted as its photograph's row goes, so a
-- recording kept by sweep() can still say which photograph it belonged to.
-- A BEFORE trigger, because the stories are still there to read.
CREATE TABLE IF NOT EXISTS stories_left (
    file        TEXT PRIMARY KEY,
    root        TEXT NOT NULL DEFAULT '',
    rel_path    TEXT NOT NULL DEFAULT '',
    speaker     TEXT NOT NULL DEFAULT '',
    text        TEXT NOT NULL DEFAULT '',
    created_at  REAL
);

CREATE TRIGGER IF NOT EXISTS stories_note_asset BEFORE DELETE ON assets
BEGIN
    INSERT OR REPLACE INTO stories_left(file, root, rel_path, speaker, text, created_at)
    SELECT file, OLD.root, OLD.rel_path, speaker, text, created_at
    FROM stories WHERE asset_id = OLD.id;
END;

CREATE TRIGGER IF NOT EXISTS stories_leave_file AFTER DELETE ON stories
BEGIN
    INSERT OR IGNORE INTO stories_gone(file, gone_at)
    VALUES (OLD.file, CAST(strftime('%s', 'now') AS REAL));
END;

-- What was said in a story, written down by the on-device AI engine
-- (storage/story_words.py). Worked out from the recording, so it is never
-- the only copy of anything: it goes with its story, and is made again for
-- a story brought back from the bin.
CREATE TABLE IF NOT EXISTS story_words (
    story_id   INTEGER PRIMARY KEY REFERENCES stories(id) ON DELETE CASCADE,
    language   TEXT NOT NULL DEFAULT '',
    words      TEXT NOT NULL DEFAULT '',
    english    TEXT NOT NULL DEFAULT '',
    segments   TEXT NOT NULL DEFAULT '[]',
    model      TEXT NOT NULL DEFAULT '',
    version    INTEGER NOT NULL DEFAULT 0,
    failures   INTEGER NOT NULL DEFAULT 0,
    tried      INTEGER NOT NULL DEFAULT 0,
    error      TEXT NOT NULL DEFAULT '',
    made_at    REAL NOT NULL
);

CREATE TRIGGER IF NOT EXISTS story_words_follow_story AFTER DELETE ON stories
BEGIN DELETE FROM story_words WHERE story_id = OLD.id; END;
"""

#: The folder beside the index the recordings are kept in.
FOLDER = "stories"

#: The largest recording accepted. Five minutes of a phone's voice memo is a
#: few megabytes; this leaves room for an uncompressed WAV someone uploads.
MAX_BYTES = 25 * 1024 * 1024
#: Typed words and a speaker's name, at most.
MAX_TEXT = 4000
MAX_SPEAKER = 60
#: The longest duration believed, in seconds. The page says how long its own
#: recording is; an uploaded file's length comes from the browser that played
#: it. Either is only ever shown, never trusted for anything else.
MAX_DURATION = 6 * 3600

#: The types kept, and the extension each is stored under. Recording in a
#: browser gives audio/webm (Chrome, Edge, Android) or audio/ogg (Firefox), and
#: audio/mp4 on an iPhone; uploads add the usual voice-memo and music formats.
AUDIO_TYPES = {
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/flac": ".flac",
    "audio/aac": ".aac",
}

#: Other names browsers and phones give the same things.
_ALIASES = {
    "audio/x-m4a": "audio/mp4", "audio/m4a": "audio/mp4", "audio/x-mp4": "audio/mp4",
    "audio/mp3": "audio/mpeg", "audio/x-mpeg": "audio/mpeg",
    "audio/x-wav": "audio/wav", "audio/wave": "audio/wav", "audio/vnd.wave": "audio/wav",
    "audio/x-flac": "audio/flac", "audio/opus": "audio/ogg", "audio/x-aac": "audio/aac",
    "audio/aacp": "audio/aac",
}

_BY_EXTENSION = {".webm": "audio/webm", ".weba": "audio/webm", ".ogg": "audio/ogg",
                 ".oga": "audio/ogg", ".opus": "audio/ogg", ".m4a": "audio/mp4",
                 ".mp4": "audio/mp4", ".mp3": "audio/mpeg", ".wav": "audio/wav",
                 ".flac": "audio/flac", ".aac": "audio/aac"}

_FILE_NAME = re.compile(r"^[0-9a-f]{32}\.[a-z0-9]{2,4}$")


def init(conn: sqlite3.Connection) -> None:
    """Made with the index (``db.init_db``); here for a database made before."""
    conn.executescript(SCHEMA)


# ---------------------------------------------------------------------------
# What a recording is
# ---------------------------------------------------------------------------

def declared_type(content_type: str | None, filename: str | None = "") -> str | None:
    """The audio type an upload says it is, or None when it says it is not audio.

    A browser sends ``audio/webm;codecs=opus``; a file picker on some phones
    sends nothing useful (``application/octet-stream``), and then the name is
    all there is to go on.
    """
    base = (content_type or "").split(";")[0].strip().lower()
    base = _ALIASES.get(base, base)
    if base in AUDIO_TYPES:
        return base
    if base and base not in ("application/octet-stream", "binary/octet-stream"):
        return None
    return _BY_EXTENSION.get(Path(filename or "").suffix.lower())


def sniff(head: bytes) -> str | None:
    """The audio container these first bytes begin, or None.

    Checked as well as the declared type: what is stored is later served from
    Ninaivu's own address with that type, so a page or a program wearing an
    audio type's name must not get that far.
    """
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "audio/webm"                      # Matroska / WebM
    if head.startswith(b"OggS"):
        return "audio/ogg"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "audio/mp4"
    if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        return "audio/wav"
    if head.startswith(b"fLaC"):
        return "audio/flac"
    if head.startswith(b"ID3"):
        return "audio/mpeg"
    if len(head) >= 2 and head[0] == 0xFF and head[1] & 0xF6 == 0xF0:
        return "audio/aac"                       # ADTS
    if len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0:
        return "audio/mpeg"                      # a bare MPEG audio frame
    return None


def clean_text(value: Any, limit: int) -> str:
    """Words as typed, trimmed, without control characters, at most *limit*."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("must be text")
    text = "".join(ch for ch in value if ch in "\n\t" or ch >= " ").strip()
    if len(text) > limit:
        raise ValueError(f"must be at most {limit} characters")
    return text


def clean_duration(value: Any) -> float | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds != seconds or seconds <= 0:        # NaN, or nothing
        return None
    return round(min(seconds, MAX_DURATION), 1)


# ---------------------------------------------------------------------------
# Rows and files
# ---------------------------------------------------------------------------

def folder_for(state_dir: Path | str) -> Path:
    return Path(state_dir) / FOLDER


def _folder_of(conn: sqlite3.Connection) -> Path | None:
    """``stories/`` beside the database this connection has open, or None for
    one held in memory."""
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main":
            return Path(row[2]).parent / FOLDER if row[2] else None
    return None


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def add(conn: sqlite3.Connection, folder: Path | str, asset_id: int, data: bytes, *,
        mime: str, created_by: int | None, speaker: str = "", text: str = "",
        duration: float | None = None) -> dict[str, Any]:
    """Keep one recording for *asset_id*. Returns its row.

    The file is written first, then the row: a crash between the two leaves a
    file nothing names, which :func:`sweep` notices, never a row whose sound
    is missing.
    """
    from . import db                                            # noqa: PLC0415

    if mime not in AUDIO_TYPES:
        raise ValueError("not an audio type")
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    name = secrets.token_hex(16) + AUDIO_TYPES[mime]
    target = folder / name
    partial = target.with_suffix(target.suffix + ".part")
    with open(partial, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(partial, target)
    try:
        with db._write_lock:                                    # noqa: SLF001
            cursor = conn.execute(
                "INSERT INTO stories(asset_id, file, mime, duration, size, speaker, text, "
                "created_by, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (int(asset_id), name, mime, duration, len(data), speaker, text,
                 created_by, time.time()))
            conn.commit()
    except BaseException:
        conn.rollback()
        with contextlib.suppress(OSError):
            target.unlink()
        raise
    return get(conn, int(cursor.lastrowid))


def get(conn: sqlite3.Connection, story_id: int) -> dict[str, Any] | None:
    return _row(conn.execute("SELECT * FROM stories WHERE id=?", (int(story_id),)).fetchone())


def for_asset(conn: sqlite3.Connection, asset_id: int) -> list[dict[str, Any]]:
    """An item's stories, oldest first: the order they were told in."""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM stories WHERE asset_id=? ORDER BY created_at, id", (int(asset_id),))]


def counts(conn: sqlite3.Connection, asset_ids: list[int]) -> dict[int, int]:
    """How many stories each of these items has, for those with any. The
    caller has already decided the viewer may see every one of them."""
    found: dict[int, int] = {}
    ids = [int(i) for i in asset_ids]
    for start in range(0, len(ids), 500):
        piece = ids[start:start + 500]
        for row in conn.execute(
                f"SELECT asset_id, COUNT(*) n FROM stories WHERE asset_id IN "
                f"({','.join('?' * len(piece))}) GROUP BY asset_id", piece):
            found[int(row[0])] = int(row[1])
    return found


def delete(conn: sqlite3.Connection, story_id: int, folder: Path | str | None = None) -> bool:
    """Remove one story, and its sound with it."""
    from . import db                                            # noqa: PLC0415

    row = conn.execute("SELECT file FROM stories WHERE id=?", (int(story_id),)).fetchone()
    with db._write_lock:                                        # noqa: SLF001
        gone = conn.execute("DELETE FROM stories WHERE id=?", (int(story_id),)).rowcount
        conn.commit()
    if gone:
        sweep(conn, folder, erase={row[0]} if row else ())
    return bool(gone)


#: Where a recording goes when its photograph's row went without anybody
#: deleting the story: kept, never deleted, with a note beside it.
ORPHANED = "_orphaned"


def files_named_in(text: str | None) -> set[str]:
    """The story file names a recycle-bin entry's metadata mentions."""
    return set(re.findall(r"[0-9a-f]{32}\.[a-z0-9]{2,4}", text or ""))


def _keep(conn: sqlite3.Connection, where: Path, name: str) -> bool:
    """Move one recording nothing names any more to ``_orphaned/``, with a
    note of the photograph it was about. False when it could not be moved."""
    import json                                                 # noqa: PLC0415

    source = where / name
    if not source.exists():
        return True
    kept = where / ORPHANED
    note: dict[str, Any] = {"file": name, "kept_at": time.time()}
    with contextlib.suppress(sqlite3.OperationalError):
        row = conn.execute("SELECT root, rel_path, speaker, text, created_at "
                           "FROM stories_left WHERE file=?", (name,)).fetchone()
        if row:
            note.update(root=row[0], rel_path=row[1], speaker=row[2], text=row[3],
                        created_at=row[4])
    try:
        kept.mkdir(exist_ok=True)
        os.replace(source, kept / name)
        (kept / (name + ".json")).write_text(
            json.dumps(note, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as exc:
        log.warning("voice story %s could not be kept aside: %s", name, exc)
        return (kept / name).exists()
    log.warning("voice story %s kept in %s: the photograph it was about (%s) "
                "left the index", name, kept, note.get("rel_path") or "unknown")
    return True


def sweep(conn: sqlite3.Connection, folder: Path | str | None = None, *,
          erase: Any = ()) -> int:
    """Settle the files of stories whose rows have gone. Returns how many
    were deleted or kept aside.

    A file is left alone while a row still names it (the same story put back,
    say) and while an entry in the recycle bin holds its row: restoring the
    photograph from the bin brings the story back, and it should still speak.
    A name in *erase* (a story somebody deleted, or one whose photograph was
    erased from the bin) has its file deleted. Every other one is moved to
    ``_orphaned/``: a photograph moved in Finder leaves the index as surely as
    one deleted, and the recording may be the only copy of a voice.
    Cheap when there is nothing to do, which is nearly always: one read of a
    table that is usually empty.
    """
    from . import db                                            # noqa: PLC0415

    try:
        names = [r[0] for r in conn.execute("SELECT file FROM stories_gone")]
    except sqlite3.OperationalError:
        return 0                     # an index from before stories
    if not names:
        return 0
    where = Path(folder) if folder is not None else _folder_of(conn)
    if where is None:
        return 0
    removed, settled = 0, []
    for name in names:
        if conn.execute("SELECT 1 FROM stories WHERE file=? LIMIT 1", (name,)).fetchone():
            settled.append(name)
            continue
        with contextlib.suppress(sqlite3.OperationalError):
            if conn.execute(
                    "SELECT 1 FROM recycled WHERE restored_at IS NULL AND metadata LIKE ? "
                    "LIMIT 1", (f"%{name}%",)).fetchone():
                continue             # still in the bin, with its photograph
        # Only a name this module made, and only inside its own folder: a row
        # written by hand must not be able to reach any other file.
        if _FILE_NAME.match(name):
            path = where / name
            if name not in erase:
                if not _keep(conn, where, name):
                    continue
                removed += 1
                settled.append(name)
                continue
            try:
                path.unlink()
                removed += 1
            except FileNotFoundError:
                pass
            except OSError as exc:
                log.warning("voice story %s could not be deleted: %s", name, exc)
                continue
        settled.append(name)
    if settled:
        with db._write_lock:                                    # noqa: SLF001
            conn.executemany("DELETE FROM stories_gone WHERE file=?", [(n,) for n in settled])
            with contextlib.suppress(sqlite3.OperationalError):
                conn.executemany("DELETE FROM stories_left WHERE file=?",
                                 [(n,) for n in settled])
            conn.commit()
    return removed


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

#: The most words of a search matched against stories, and the most items a
#: story search hands to a ranked (image-search) answer.
_MAX_WORDS = 8
_MAX_HITS = 2000


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _words_where(text: str) -> tuple[str, list[Any]] | None:
    words = [w for w in re.split(r"\s+", text.strip()) if w][:_MAX_WORDS]
    if not words:
        return None
    test = " AND ".join([f"({_SAID}) LIKE ? ESCAPE '\\'"] * len(words))
    return test, [f"%{_like_escape(w)}%" for w in words]


#: Everything a story says, for a word search: the words typed beside it, the
#: speaker's name, and what the engine wrote down from the recording, in its
#: own language and in English. A subquery, so the clause still reads one
#: row per story and a story with no written words is searched as before.
_SAID = ("s.text || ' ' || s.speaker || ' ' || COALESCE((SELECT w.words || ' ' || w.english "
         "FROM story_words w WHERE w.story_id = s.id), '')")


def words_clause(text: str) -> tuple[str, list[Any]] | None:
    """SQL choosing the asset ids with a story holding every word of *text*.

    Every word, anywhere in the typed words, the speaker's name or what was
    said in the recording (once the engine has written it down), as the
    full-text index treats a caption: "father shop" finds "my father's shop".
    A substring match rather than tokens, because Tamil is written with vowel
    signs a tokenizer is not always kind to, and a household's stories are
    few enough that reading them is nothing. None when there are no words.
    """
    where = _words_where(text)
    if where is None:
        return None
    return f"SELECT s.asset_id FROM stories s WHERE {where[0]}", where[1]


def matching_asset_ids(conn: sqlite3.Connection, text: str) -> list[int]:
    """Items with a story matching *text*, newest story first. Not narrowed
    to any viewer: the caller passes them through ``db.query_assets``."""
    where = _words_where(text)
    if where is None:
        return []
    try:
        rows = conn.execute(
            f"SELECT s.asset_id FROM stories s WHERE {where[0]} "
            "GROUP BY s.asset_id ORDER BY MAX(s.created_at) DESC LIMIT ?",
            [*where[1], _MAX_HITS]).fetchall()
    except sqlite3.OperationalError:
        return []                    # an index from before stories
    return [int(r[0]) for r in rows]
