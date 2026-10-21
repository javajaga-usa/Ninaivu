"""Words spoken in videos: what is said in each home video, line by line.

A family's videos are full of things nobody will ever type: Paati's "Happy
birthday, kanna", the toast at the wedding, a child counting the candles.
The picture search finds the cake (storage/video_moments.py); this keeps what
was *said*, as the on-device engine's speech model wrote it down
(media/video_words.py, the same Whisper that writes down voice stories), each
line with the second it starts and ends at, so a search for the words opens
the video there and the viewer can show them under it.

Two tables:

* ``video_words``: one row per line, ``(asset_id, at)``, with its ``end``,
  the language it was heard in, the words in that language (``text``) and,
  for a line not in English, an English translation (``english``) when the
  model can make one.
* ``video_words_done``: one row per video listened to, with the version of
  what listened (:func:`wanted_version`: this pass's :data:`VERSION` and the
  speech model's own), the file's ``mtime`` then (a video replaced on disk is
  listened to again), the language most of it was in, how many lines it
  gave, and the failures for a video that could not be read.

Shaped like story_words.py: a video whose sound cannot be written down is
tried :data:`MAX_FAILURES` times; after that it is stamped with the version
it failed at, so it is not tried again until a better model makes it owed.

Only what AI may read is kept (``db.AI_MAY_READ``): an item an administrator
hides loses its words (:func:`forget`, from db.forget_ai_reading), and one
shown again is listened to again. Who may read the words is who may open the
video: the routes go through the gallery's own limits (api/api_video_words.py).
"""
from __future__ import annotations

import sqlite3
import time
from collections import Counter
from typing import Any, Iterable, Sequence

#: Bumped when the way videos are listened to changes enough to do it again.
VERSION = 1
#: Videos shorter than this are left alone: a two-second clip says nothing.
MIN_SECONDS = 4.0
#: And longer than this: a whole day's camera left running is not a home
#: video anybody will search for words in, and would hold the speech model
#: for a night on its own.
MAX_SECONDS = 4 * 3600.0
#: Attempts at a video before it is set aside until a newer model or pass.
MAX_FAILURES = 2
#: The most characters kept of one line, in each language.
MAX_LINE = 2000
#: The most videos one search answers with, and lines of each.
MAX_RESULTS = 24
PER_CLIP = 3
#: Lines read of a search before they are grouped into videos.
_MAX_HITS = 2000

#: What Whisper writes for music, applause or a room's murmur when it hears
#: no words it can make out: captions from the videos it was taught on. A
#: line that is only one of these is left out, never searched or shown.
HEARD_FOR_NOTHING = frozenset({
    "thank you", "thank you.", "thanks for watching", "thanks for watching!",
    "thank you for watching", "thank you for watching.", "thank you for watching!",
    "please subscribe", "subscribe", "like and subscribe", "bye", "bye.", "you",
    "subtitles by the amara.org community", "[music]", "(music)", "♪", "♪♪", "...",
    "[applause]", "(applause)", "[laughter]", "(laughs)",
})

SCHEMA = """
CREATE TABLE IF NOT EXISTS video_words (
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    at       REAL NOT NULL,
    "end"    REAL NOT NULL DEFAULT 0,
    language TEXT NOT NULL DEFAULT '',
    text     TEXT NOT NULL DEFAULT '',
    english  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (asset_id, at)
);

CREATE TABLE IF NOT EXISTS video_words_done (
    asset_id INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
    version  INTEGER NOT NULL DEFAULT 0,
    mtime    REAL NOT NULL DEFAULT 0,
    language TEXT NOT NULL DEFAULT '',
    lines    INTEGER NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0,
    error    TEXT NOT NULL DEFAULT '',
    made_at  REAL NOT NULL DEFAULT 0
);

CREATE TRIGGER IF NOT EXISTS video_words_follow_asset AFTER DELETE ON assets
BEGIN
    DELETE FROM video_words WHERE asset_id = OLD.id;
    DELETE FROM video_words_done WHERE asset_id = OLD.id;
END;
"""

#: The videos worth listening to: long enough and not too long, in the bin
#: or hidden from AI never, and not the moving half of a Live Photo.
_CLIPS = ("a.trashed = 0 AND a.kind = 'video' AND a.live_clip = 0 AND a.visibility < 2 "
          "AND COALESCE(a.duration, 0) >= ? AND COALESCE(a.duration, 0) <= ?")


def create(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def wanted_version(speech_version: int) -> int:
    """What a video's words are stamped with when made by a speech model
    stamped *speech_version* (runtime/speech.py ``version_of``); 0 for none."""
    return 1000 * VERSION + int(speech_version) if speech_version > 0 else 0


def _owed_where(version: int) -> tuple[str, list[Any]]:
    return (f"{_CLIPS} AND (d.asset_id IS NULL OR "
            "(d.failures < ? AND d.version = 0) OR "
            "(d.version > 0 AND (d.version < ? OR d.mtime != COALESCE(a.mtime, 0))))",
            [MIN_SECONDS, MAX_SECONDS, MAX_FAILURES, int(version)])


def owed(conn: sqlite3.Connection, version: int, limit: int = 50,
         after: int = 0) -> list[sqlite3.Row]:
    """Videos not yet listened to at *version*, or changed on disk since;
    newest first by id, from below *after* when it is given (0: from the
    top). Rows of ``id``, ``root``, ``rel_path``, ``duration``, ``mtime``."""
    if version <= 0:
        return []
    where, params = _owed_where(version)
    if after:
        where += " AND a.id < ?"
        params.append(int(after))
    try:
        return conn.execute(
            "SELECT a.id, a.root, a.rel_path, a.duration, COALESCE(a.mtime, 0) AS mtime "
            "FROM assets a LEFT JOIN video_words_done d ON d.asset_id = a.id "
            f"WHERE {where} ORDER BY a.id DESC LIMIT ?", [*params, int(limit)]).fetchall()
    except sqlite3.OperationalError:
        return []


def count_owed(conn: sqlite3.Connection, version: int) -> int:
    if version <= 0:
        return 0
    where, params = _owed_where(version)
    try:
        return int(conn.execute(
            "SELECT COUNT(*) FROM assets a LEFT JOIN video_words_done d ON d.asset_id = a.id "
            f"WHERE {where}", params).fetchone()[0])
    except sqlite3.OperationalError:
        return 0


def _plain(text: Any) -> str:
    return " ".join(str(text or "").split())[:MAX_LINE]


def heard_for_nothing(text: str) -> bool:
    """Whether *text* is only what Whisper writes for a sound with no words."""
    return text.strip().lower() in HEARD_FOR_NOTHING


def lines_of(segments: Iterable[dict[str, Any]], language: str = "") -> list[dict[str, Any]]:
    """The pieces the speech model wrote down as lines to keep: in order,
    empty ones and :data:`HEARD_FOR_NOTHING` left out, and one line a start."""
    kept: dict[float, dict[str, Any]] = {}
    for piece in segments:
        text, english = _plain(piece.get("text")), _plain(piece.get("english"))
        if heard_for_nothing(text):
            text = ""
        if heard_for_nothing(english):
            english = ""
        if not text and not english:
            continue
        at = round(float(piece.get("start") or 0), 2)
        end = max(at, round(float(piece.get("end") or 0), 2))
        kept[at] = {"at": at, "end": end, "text": text, "english": english,
                    "language": str(piece.get("language") or language or "")[:8]}
    return [kept[at] for at in sorted(kept)]


def store(conn: sqlite3.Connection, asset_id: int, lines: Sequence[dict[str, Any]], *,
          version: int, mtime: float) -> bool:
    """Keep a video's lines (from :func:`lines_of`) in place of any it had.
    False when the video went, or was hidden from AI, meanwhile."""
    from . import db                                            # noqa: PLC0415

    rows = [(int(asset_id), float(line["at"]), float(line["end"]), str(line["language"]),
             str(line["text"]), str(line["english"])) for line in lines]
    heard = Counter(line["language"] for line in lines if line["language"])
    language = heard.most_common(1)[0][0] if heard else ""
    with db._write_lock:                                        # noqa: SLF001
        if not conn.execute(f"SELECT 1 FROM assets WHERE id=? AND {db.AI_MAY_READ}",
                            (int(asset_id),)).fetchone():
            return False
        conn.execute("DELETE FROM video_words WHERE asset_id=?", (int(asset_id),))
        conn.executemany('INSERT OR REPLACE INTO video_words(asset_id, at, "end", language, '
                         "text, english) VALUES (?,?,?,?,?,?)", rows)
        conn.execute(
            "INSERT INTO video_words_done(asset_id, version, mtime, language, lines, failures, "
            "error, made_at) VALUES (?,?,?,?,?,0,'',?) ON CONFLICT(asset_id) DO UPDATE SET "
            "version=excluded.version, mtime=excluded.mtime, language=excluded.language, "
            "lines=excluded.lines, failures=0, error='', made_at=excluded.made_at",
            (int(asset_id), int(version), float(mtime or 0), language, len(rows), time.time()))
        conn.commit()
    return True


def fail(conn: sqlite3.Connection, asset_id: int, error: str, *, version: int,
         mtime: float) -> bool:
    """Count one failed listen to a video. True when it is now set aside
    (until a newer model or pass, or the file changes)."""
    from . import db                                            # noqa: PLC0415

    with db._write_lock:                                        # noqa: SLF001
        if not conn.execute("SELECT 1 FROM assets WHERE id=?", (int(asset_id),)).fetchone():
            return False
        row = conn.execute("SELECT failures, version FROM video_words_done WHERE asset_id=?",
                           (int(asset_id),)).fetchone()
        failures = (int(row[0]) if row and int(row[1]) == 0 else 0) + 1
        set_aside = failures >= MAX_FAILURES
        conn.execute(
            "INSERT INTO video_words_done(asset_id, version, mtime, language, lines, failures, "
            "error, made_at) VALUES (?,?,?,'',0,?,?,?) ON CONFLICT(asset_id) DO UPDATE SET "
            "version=excluded.version, mtime=excluded.mtime, language='', lines=0, "
            "failures=excluded.failures, error=excluded.error, made_at=excluded.made_at",
            (int(asset_id), int(version) if set_aside else 0, float(mtime or 0), failures,
             str(error)[:300], time.time()))
        conn.commit()
    return set_aside


def forget(conn: sqlite3.Connection, ids: Iterable[int]) -> int:
    """Take back the words of these items (hidden from AI). The caller holds
    ``db._write_lock`` and commits."""
    ids = [int(i) for i in ids]
    gone = 0
    try:
        for at in range(0, len(ids), 500):
            part = ids[at:at + 500]
            marks = ",".join("?" * len(part))
            gone += conn.execute(f"DELETE FROM video_words WHERE asset_id IN ({marks})",
                                 part).rowcount
            conn.execute(f"DELETE FROM video_words_done WHERE asset_id IN ({marks})", part)
    except sqlite3.OperationalError:
        return 0                                    # an index from before video words
    return gone


def _line(row: sqlite3.Row | tuple) -> dict[str, Any]:
    return {"at": float(row[0]), "end": float(row[1]), "language": str(row[2] or ""),
            "text": str(row[3] or ""), "english": str(row[4] or "")}


def of_clip(conn: sqlite3.Connection, asset_id: int) -> list[dict[str, Any]]:
    """A video's lines, in order."""
    try:
        return [_line(r) for r in conn.execute(
            'SELECT at, "end", language, text, english FROM video_words WHERE asset_id=? '
            "ORDER BY at", (int(asset_id),))]
    except sqlite3.OperationalError:
        return []


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    """How many videos have been listened to, and how many lines they hold."""
    try:
        row = conn.execute("SELECT COUNT(*), COALESCE(SUM(lines), 0) FROM video_words_done "
                           "WHERE version > 0").fetchone()
        return {"clips": int(row[0]), "lines": int(row[1])}
    except sqlite3.OperationalError:
        return {"clips": 0, "lines": 0}


def words(text: str) -> list[str]:
    """The words of a search, lowered, at most eight of them."""
    found: list[str] = []
    for word in str(text or "").lower().split():
        word = word.strip(".,!?;:\"'()[]")
        if word and word not in found:
            found.append(word)
    return found[:8]


def search(conn: sqlite3.Connection, text: str, *, limit: int = MAX_RESULTS * 4,
           per_clip: int = PER_CLIP) -> list[dict[str, Any]]:
    """The videos with a line holding every word of *text* (in the language
    it was said in, or its English), most lines first and then newest:
    ``{"id", "hits", "lines": [...]}``, at most *limit* videos and *per_clip*
    lines each. A substring match, as voice stories are searched, because
    Tamil is written with vowel signs a tokenizer is not always kind to. Only
    candidates: the caller narrows them to what the viewer may see."""
    wanted = words(text)
    if not wanted:
        return []
    said = "(LOWER(w.text) || ' ' || LOWER(w.english))"
    where = " AND ".join([f"INSTR({said}, ?) > 0"] * len(wanted))
    try:
        rows = conn.execute(
            'SELECT w.asset_id, w.at, w."end", w.language, w.text, w.english '
            "FROM video_words w JOIN video_words_done d ON d.asset_id = w.asset_id "
            f"WHERE d.version > 0 AND {where} ORDER BY w.asset_id DESC, w.at LIMIT ?",
            [*wanted, _MAX_HITS]).fetchall()
    except sqlite3.OperationalError:
        return []
    clips: dict[int, dict[str, Any]] = {}
    for row in rows:
        clip = clips.setdefault(int(row[0]), {"id": int(row[0]), "hits": 0, "lines": []})
        clip["hits"] += 1
        if len(clip["lines"]) < per_clip:
            clip["lines"].append(_line(row[1:]))
    ranked = sorted(clips.values(), key=lambda c: (-c["hits"], -c["id"]))
    return ranked[:limit]


def _stamp(seconds: float) -> str:
    millis = int(round(max(0.0, float(seconds)) * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def vtt(lines: Sequence[dict[str, Any]], which: str = "spoken") -> str:
    """The lines as WebVTT subtitles for the viewer's video: in the language
    they were said in (*which* "spoken"), or in English ("english": the
    translation, or the words themselves for a line said in English)."""
    out = ["WEBVTT", ""]
    for number, line in enumerate(lines, 1):
        if which == "english":
            words_shown = line["english"] or (line["text"] if line["language"] == "en" else "")
        else:
            words_shown = line["text"] or line["english"]
        words_shown = " ".join(str(words_shown or "").split()).replace("-->", "->")
        if not words_shown:
            continue
        end = max(float(line["end"]), float(line["at"]) + 0.5)
        out += [str(number), f"{_stamp(line['at'])} --> {_stamp(end)}", words_shown, ""]
    return "\n".join(out)
