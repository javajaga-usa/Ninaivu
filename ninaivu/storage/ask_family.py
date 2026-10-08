"""Ask the family: "who is this?" questions sent to people with no account.

The people who can name the faces in a 1970s print are usually the ones
least likely to sign in to anything. So a family member picks a handful of
unnamed faces, Ninaivu makes a link, and the link goes to Paati by WhatsApp.
She types "my uncle Raman, at Chennai, around 1975" under a face, and the
answer waits here until somebody in the house accepts or dismisses it.

Three tables, all of them small:

* ``ask_questions`` — one link: its token, who made it, when it ends, whether
  it may show the whole photograph, and whether it was withdrawn.
* ``ask_faces`` — which faces the link asks about, by position. The position
  is what the public page calls a face; the face's own id never leaves the
  house. ``asked_visibility`` is the photograph's visibility when it was
  asked about, so a photograph hidden *after* the link was made drops out of
  it on the next view rather than going on being shown to strangers.
* ``ask_answers`` — what came back, waiting for a review.

A face is still reachable only through its photograph (see the comment on the
``faces`` table in db.py): every check that decides whether a face may be
shown goes through the photograph's row, and is made again on every view.
"""

from __future__ import annotations

import re
import sqlite3
import time
import unicodedata
from typing import Any, Sequence

__all__ = [
    "SCHEMA", "MAX_FACES", "NAME_MAX", "NOTE_MAX", "WHO_MAX", "MAX_ANSWERS",
    "clean_text", "create_question", "get_question", "question_faces",
    "list_questions", "revoke", "add_answers", "list_answers", "get_answer",
    "settle_answer",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS ask_questions (
    id          INTEGER PRIMARY KEY,
    token       TEXT NOT NULL UNIQUE,
    -- A profile id, without a foreign key: the index is also opened where
    -- there is no users table, and a key to one made every delete of a
    -- photograph fail there. auth.delete_user clears it instead, as it does
    -- for share links, so a reused id cannot bring a link back to life.
    created_by  INTEGER,
    created_at  REAL NOT NULL,
    expires_at  REAL,
    show_photo  INTEGER NOT NULL DEFAULT 0,
    revoked_at  REAL
);

CREATE TABLE IF NOT EXISTS ask_faces (
    question_id      INTEGER NOT NULL REFERENCES ask_questions(id) ON DELETE CASCADE,
    position         INTEGER NOT NULL,
    face_id          INTEGER NOT NULL REFERENCES faces(id) ON DELETE CASCADE,
    asked_visibility INTEGER NOT NULL,
    PRIMARY KEY (question_id, position)
);

CREATE TABLE IF NOT EXISTS ask_answers (
    id          INTEGER PRIMARY KEY,
    question_id INTEGER NOT NULL REFERENCES ask_questions(id) ON DELETE CASCADE,
    face_id     INTEGER NOT NULL REFERENCES faces(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    -- What the answerer called themselves, if anything. Not an account, and
    -- not checked: it is there so the review can say "Paati says".
    answered_by TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    -- pending | accepted | rejected | closed (another answer for the same
    -- face was accepted)
    status      TEXT NOT NULL DEFAULT 'pending',
    person_id   INTEGER REFERENCES people_clusters(id) ON DELETE SET NULL,
    reviewed_by INTEGER,
    reviewed_at REAL
);

CREATE INDEX IF NOT EXISTS idx_ask_answers_status ON ask_answers(status, question_id);
CREATE INDEX IF NOT EXISTS idx_ask_faces_face ON ask_faces(face_id);
"""

#: The most faces one link asks about: enough for a group photograph, few
#: enough that an elder on a phone gets to the end of the page.
MAX_FACES = 20
#: The longest name, note and "who are you" an answer may carry. A name is a
#: name; a note is a sentence or three. Anything longer is refused rather than
#: cut, so nobody's words are silently lost.
NAME_MAX = 80
NOTE_MAX = 500
WHO_MAX = 60
#: Answers one link may collect in all. A link is public; this bounds what
#: somebody who found one could pile into the review list.
MAX_ANSWERS = 200

_SPACES = re.compile(r"\s+")


def init(conn: sqlite3.Connection) -> None:
    """Made with the index (``db.init_db``); here for a database made before."""
    conn.executescript(SCHEMA)


def clean_text(value: Any, limit: int, what: str) -> str:
    """Typed text as it will be kept: one line of ordinary characters.

    Runs of white space become one space, and control and formatting
    characters other than the joiners Tamil needs are dropped — a note is
    shown to the family later, and invisible characters in it are a way to
    make one answer look like another. ValueError if it is not text or is
    longer than *limit* once tidied.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{what} must be text")
    kept = "".join(ch for ch in value
                   if ch in "‌‍" or not unicodedata.category(ch).startswith("C")
                   or ch in "\t\n\r")
    text = _SPACES.sub(" ", kept).strip()
    if len(text) > limit:
        raise ValueError(f"{what} can be at most {limit} characters")
    return text


# -- questions -----------------------------------------------------------------

def create_question(conn: sqlite3.Connection, *, token: str, created_by: int,
                    faces: Sequence[tuple[int, int]], expires_at: float | None,
                    show_photo: bool) -> dict[str, Any]:
    """Store a link asking about *faces*, given as ``(face_id, visibility)``."""
    from .db import _write_lock                                   # noqa: PLC0415

    if not 0 < len(faces) <= MAX_FACES:
        raise ValueError(f"Ask about 1 to {MAX_FACES} faces at a time")
    now = time.time()
    with _write_lock:
        cur = conn.execute(
            "INSERT INTO ask_questions(token, created_by, created_at, expires_at, show_photo) "
            "VALUES (?,?,?,?,?)",
            (token, int(created_by), now, expires_at, 1 if show_photo else 0))
        question_id = int(cur.lastrowid)
        conn.executemany(
            "INSERT INTO ask_faces(question_id, position, face_id, asked_visibility) "
            "VALUES (?,?,?,?)",
            [(question_id, n, int(face_id), int(visibility))
             for n, (face_id, visibility) in enumerate(faces, start=1)])
        conn.commit()
    return get_question(conn, token) or {}


def _question(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    question = dict(row)
    now = time.time()
    question["show_photo"] = bool(question.get("show_photo"))
    question["revoked"] = question.get("revoked_at") is not None
    question["expired"] = bool(question.get("expires_at")) and float(question["expires_at"]) <= now
    question["open"] = not question["revoked"] and not question["expired"]
    return question


def get_question(conn: sqlite3.Connection, token: str) -> dict[str, Any] | None:
    if not isinstance(token, str) or not token or len(token) > 64:
        return None
    return _question(conn.execute(
        "SELECT * FROM ask_questions WHERE token=?", (token,)).fetchone())


#: The photograph's columns a face is judged by, every time it is shown.
_ASSET_COLUMNS = ("root", "rel_path", "folder", "kind", "ext", "visibility", "date_key",
                  "trashed", "nsfw", "thumb", "mtime", "rotation")


def question_faces(conn: sqlite3.Connection, question_id: int) -> list[dict[str, Any]]:
    """The faces a link asks about, each with its photograph's current row.

    Nothing here decides what may be shown; the caller does that against the
    photograph's row, which is read fresh so a change since the link was made
    counts.
    """
    columns = ", ".join(f"a.{c}" for c in _ASSET_COLUMNS)
    rows = conn.execute(
        "SELECT q.position, q.face_id, q.asked_visibility, f.thumb AS face_thumb, "
        f"f.person_id, f.source, f.asset_id AS id, {columns} "
        "FROM ask_faces q JOIN faces f ON f.id = q.face_id "
        "JOIN assets a ON a.id = f.asset_id "
        "WHERE q.question_id=? ORDER BY q.position", (int(question_id),)).fetchall()
    return [dict(r) for r in rows]


def list_questions(conn: sqlite3.Connection,
                   created_by: int | None = None) -> list[dict[str, Any]]:
    """Links, newest first, with how many faces and answers each has."""
    where, params = "", []
    if created_by is not None:
        where, params = "WHERE q.created_by=?", [int(created_by)]
    rows = conn.execute(
        "SELECT q.*, "
        "  (SELECT COUNT(*) FROM ask_faces f WHERE f.question_id=q.id) AS face_count, "
        "  (SELECT COUNT(*) FROM ask_answers a WHERE a.question_id=q.id) AS answer_count, "
        "  (SELECT COUNT(*) FROM ask_answers a WHERE a.question_id=q.id "
        "     AND a.status='pending') AS pending_count "
        f"FROM ask_questions q {where} ORDER BY q.created_at DESC, q.id DESC LIMIT 200",
        params).fetchall()
    return [q for q in (_question(r) for r in rows) if q is not None]


def revoke(conn: sqlite3.Connection, token: str) -> bool:
    """Withdraw a link. Its answers stay to be reviewed."""
    from .db import _write_lock                                   # noqa: PLC0415

    with _write_lock:
        cur = conn.execute(
            "UPDATE ask_questions SET revoked_at=? WHERE token=? AND revoked_at IS NULL",
            (time.time(), token))
        conn.commit()
    return bool(cur.rowcount)


# -- answers -------------------------------------------------------------------

def add_answers(conn: sqlite3.Connection, question_id: int,
                answers: Sequence[tuple[int, str, str]], answered_by: str) -> int:
    """Keep ``(face_id, name, note)`` answers to one link.

    ValueError when the link has already collected :data:`MAX_ANSWERS`.
    """
    from .db import _write_lock                                   # noqa: PLC0415

    if not answers:
        return 0
    now = time.time()
    with _write_lock:
        held = int(conn.execute("SELECT COUNT(*) FROM ask_answers WHERE question_id=?",
                                (int(question_id),)).fetchone()[0])
        if held + len(answers) > MAX_ANSWERS:
            raise ValueError("This link has had all the answers it can take")
        conn.executemany(
            "INSERT INTO ask_answers(question_id, face_id, name, note, answered_by, created_at) "
            "VALUES (?,?,?,?,?,?)",
            [(int(question_id), int(face_id), name, note, answered_by, now)
             for face_id, name, note in answers])
        conn.commit()
    return len(answers)


_ANSWER_SQL = (
    "SELECT r.id, r.question_id, r.face_id, r.name, r.note, r.answered_by, r.created_at, "
    "       r.status, r.person_id, r.reviewed_at, q.created_by, q.token, "
    "       f.asset_id, f.person_id AS face_person_id, f.source AS face_source, "
    "       a.root, a.folder, a.kind, a.visibility, a.date_key, a.trashed, a.nsfw "
    "FROM ask_answers r JOIN ask_questions q ON q.id = r.question_id "
    "JOIN faces f ON f.id = r.face_id JOIN assets a ON a.id = f.asset_id ")


def list_answers(conn: sqlite3.Connection, *, status: str = "pending",
                 created_by: int | None = None, limit: int = 200) -> list[dict[str, Any]]:
    """Answers waiting for a review (or settled ones, by *status*), newest
    first, each with its photograph's row for the caller to judge."""
    where, params = ["r.status=?"], [status]
    if created_by is not None:
        where.append("q.created_by=?")
        params.append(int(created_by))
    rows = conn.execute(
        _ANSWER_SQL + f"WHERE {' AND '.join(where)} "
        "ORDER BY r.created_at DESC, r.id DESC LIMIT ?", (*params, int(limit))).fetchall()
    return [dict(r) for r in rows]


def get_answer(conn: sqlite3.Connection, answer_id: int) -> dict[str, Any] | None:
    row = conn.execute(_ANSWER_SQL + "WHERE r.id=?", (int(answer_id),)).fetchone()
    return dict(row) if row else None


def settle_answer(conn: sqlite3.Connection, answer_id: int, status: str,
                  reviewed_by: int, person_id: int | None = None) -> None:
    """Accept or reject one answer.

    Accepting one closes the other answers still waiting about the same face:
    it has a name now, and asking the family the same question twice in the
    review list is how a review list stops being read.
    """
    from .db import _write_lock                                   # noqa: PLC0415

    if status not in ("accepted", "rejected"):
        raise ValueError("status")
    now = time.time()
    with _write_lock:
        row = conn.execute("SELECT face_id FROM ask_answers WHERE id=?",
                           (int(answer_id),)).fetchone()
        if row is None:
            return
        conn.execute(
            "UPDATE ask_answers SET status=?, person_id=?, reviewed_by=?, reviewed_at=? "
            "WHERE id=?", (status, person_id, int(reviewed_by), now, int(answer_id)))
        if status == "accepted":
            conn.execute(
                "UPDATE ask_answers SET status='closed', reviewed_by=?, reviewed_at=? "
                "WHERE face_id=? AND status='pending' AND id<>?",
                (int(reviewed_by), now, int(row["face_id"]), int(answer_id)))
        conn.commit()
