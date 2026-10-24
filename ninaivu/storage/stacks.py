"""Photo stacks (``photo_stacks``): a burst, or a photograph and its copies, as one tile.

A camera held down takes ten frames of the same moment, and an edited
photograph is saved beside the one it was made from. The scan already knows
which pictures are near-identical (``dup_group``, from the picture
fingerprint); a stack is such a group taken on the same day in the same
folder. The timeline then shows its best frame alone, with how many there
are on the tile, and the viewer shows the rest beside it. Nothing is moved
or deleted: the other frames are only left out of the timeline, and every
other place (albums, people, search, the duplicates page, the folders
screen) still lists every one of them.

A stack only ever holds photographs that every viewer sees alike: the same
visibility, the same explicit flag and the same "Only to me" owner. So a
viewer who may see the top photograph may see every frame under it, and
one who may not sees none of them through it — a hidden frame is never
stacked under a family photograph, nor a family photograph under a hidden
one. The same day and folder keep it inside every date limit and folder a
profile is held to.

Which frame is on top is the sharpest, as "Keep only the best" picks it
(``db.best_of``), unless somebody chose another; a stack somebody asked to
see as separate photographs stays apart. Both are kept in ``stack_choices``
by the stack's key, so a rebuild keeps them.

The marks are worked out again whenever the library has changed (the
gallery asks, :func:`refresh`), at most every :data:`REFRESH_SECONDS`. In
between, a trigger loosens a stack the moment one of its photographs is
hidden, flagged, binned, moved or removed: every frame of it is shown again
until the next rebuild, so nothing is ever left out of sight under a top
photograph the viewer can no longer see.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
import time
from typing import Any, Iterable

__all__ = ["SCHEMA", "create", "key_of", "plan", "build", "refresh", "members",
           "choose_top", "set_apart"]

log = logging.getLogger(__name__)

#: What a stack is made of in the library: what each frame must share with
#: the others, besides being near-identical.
_SHARED = ("dup_group", "root", "folder", "date_key", "visibility", "nsfw", "private_to")

#: What it takes to change a stack's members. Any of them moving on one
#: photograph loosens its whole stack until it is built again.
_LOOSENED_BY = ("trashed", "visibility", "nsfw", "private_to", "dup_group",
                "root", "folder", "date_key", "kind", "live_clip", "raw_twin")

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS stack_choices (
    key        TEXT PRIMARY KEY,
    -- The photograph somebody put on top, or NULL for the sharpest.
    cover      INTEGER,
    -- 1: shown as separate photographs, as somebody asked.
    apart      INTEGER NOT NULL DEFAULT 0,
    chosen_by  INTEGER,
    chosen_at  REAL NOT NULL
);
DROP TRIGGER IF EXISTS stacks_loosen_on_change;
CREATE TRIGGER stacks_loosen_on_change
AFTER UPDATE OF {", ".join(_LOOSENED_BY)} ON assets
WHEN OLD.stack_key IS NOT NULL AND ({" OR ".join(f"OLD.{c} IS NOT NEW.{c}" for c in _LOOSENED_BY)})
BEGIN UPDATE assets SET stacked = 0, stack_count = 0 WHERE stack_key = OLD.stack_key; END;
DROP TRIGGER IF EXISTS stacks_loosen_on_delete;
CREATE TRIGGER stacks_loosen_on_delete
AFTER DELETE ON assets WHEN OLD.stack_key IS NOT NULL
BEGIN UPDATE assets SET stacked = 0, stack_count = 0 WHERE stack_key = OLD.stack_key; END;
"""

#: Meta key holding the library counter the marks were last built at.
BUILT_KEY = "stacks_built"

#: The least time between two rebuilds asked for by the gallery. A scan that
#: writes for an hour moves the library counter on every batch, and each
#: rebuild reads every grouped photograph.
REFRESH_SECONDS = 15.0

#: How long the gallery waits for the index's write lock to write the marks.
#: A scan holds it through each batch; the marks can wait for the next ask.
REFRESH_WAIT_SECONDS = 0.5

_last_try: dict[str, float] = {}
_lock = threading.Lock()


def create(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def key_of(row: dict[str, Any] | sqlite3.Row) -> str:
    """The stack a photograph belongs to, from what its frames share."""
    text = "\x1f".join("" if row[c] is None else str(row[c]) for c in _SHARED)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def plan(rows: Iterable[dict[str, Any]], choices: dict[str, dict[str, Any]]
         ) -> dict[int, tuple[str | None, int, int]]:
    """``asset id -> (stack_key, stacked, stack_count)`` for every row given.

    *rows* are the photographs that may stack (grouped, not binned, not a
    pair's other half); one that has no near-identical frame beside it is
    given no key.
    """
    from . import db                                            # noqa: PLC0415
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(key_of(row), []).append(row)
    out: dict[int, tuple[str | None, int, int]] = {}
    for key, items in groups.items():
        if len(items) < 2:
            for item in items:
                out[int(item["id"])] = (None, 0, 0)
            continue
        choice = choices.get(key) or {}
        if choice.get("apart"):
            for item in items:
                out[int(item["id"])] = (key, 0, 0)
            continue
        ids = {int(item["id"]) for item in items}
        top = choice.get("cover")
        if top not in ids:
            top = db.best_of(items)
        for item in items:
            aid = int(item["id"])
            out[aid] = (key, 0, len(items)) if aid == top else (key, 1, 0)
    return out


def _choices(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    try:
        return {row[0]: {"cover": row[1], "apart": bool(row[2])} for row in conn.execute(
            "SELECT key, cover, apart FROM stack_choices")}
    except sqlite3.OperationalError:
        return {}


def build(conn: sqlite3.Connection, *, wait: float = -1) -> dict[str, int]:
    """Mark every stack in the library afresh. Returns what changed.

    *wait* is how long to wait for the index's write lock, in seconds (-1:
    for as long as it takes). When it is not had in that time nothing is
    written, and ``changed`` is -1: the gallery does not wait behind a scan's
    batch for marks that can be made next time.
    """
    from . import db                                            # noqa: PLC0415
    started = time.time()
    lock = db._write_lock                                       # noqa: SLF001
    if not lock.acquire(timeout=wait):
        return {"stacks": 0, "changed": -1}
    try:
        # Read and written in one transaction, so a frame hidden between the
        # two cannot be stacked again under a photograph it no longer shares
        # its visibility with (and the counter kept is the one after both).
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            changed, stacks = _write_marks(conn)
            db.set_meta(conn, BUILT_KEY, str(db.library_generation(conn)))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    finally:
        lock.release()
    if changed:
        log.info("photo stacks: %s stacks, %s rows rewritten in %.2fs",
                 f"{stacks:,}", f"{len(changed):,}", time.time() - started)
    return {"stacks": stacks, "changed": len(changed)}


def _write_marks(conn: sqlite3.Connection) -> tuple[list[tuple], int]:
    columns = ", ".join(("id", *_SHARED, "sharpness", "width", "height", "size"))
    rows = [dict(row) for row in conn.execute(
        f"SELECT {columns} FROM assets WHERE dup_group IS NOT NULL AND trashed = 0 "
        "AND kind = 'picture' AND live_clip = 0 AND raw_twin = 0")]
    wanted = plan(rows, _choices(conn))
    held = {int(row[0]): (row[1], int(row[2]), int(row[3])) for row in conn.execute(
        "SELECT id, stack_key, stacked, stack_count FROM assets WHERE stack_key IS NOT NULL")}
    # A photograph that held a key and is no longer among those that may
    # stack (binned, its twin gone) lets go of it.
    for aid in held:
        wanted.setdefault(aid, (None, 0, 0))
    changed = [(key, stacked, count, aid) for aid, (key, stacked, count) in wanted.items()
               if held.get(aid, (None, 0, 0)) != (key, stacked, count)]
    if changed:
        conn.executemany(
            "UPDATE assets SET stack_key = ?, stacked = ?, stack_count = ? WHERE id = ?",
            changed)
    return changed, sum(1 for _, _, count in wanted.values() if count)


def refresh(conn: sqlite3.Connection, *, force: bool = False) -> bool:
    """Build the marks again if the library changed since. True if it ran."""
    from . import db                                            # noqa: PLC0415
    generation = db.library_generation(conn)
    if not force and generation is not None and \
            db.get_meta(conn, BUILT_KEY) == str(generation):
        return False
    where = _database_name(conn)
    with _lock:
        now = time.monotonic()
        if not force and now - _last_try.get(where, -REFRESH_SECONDS) < REFRESH_SECONDS:
            return False
        _last_try[where] = now
    try:
        done = build(conn, wait=-1 if force else REFRESH_WAIT_SECONDS)
    except sqlite3.Error as exc:
        # The gallery is drawn without stacks rather than not at all.
        log.warning("photo stacks could not be built: %s", exc)
        return False
    return done["changed"] >= 0


def _database_name(conn: sqlite3.Connection) -> str:
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        return str(row[2] or id(conn))
    except sqlite3.Error:
        return str(id(conn))


def members(conn: sqlite3.Connection, asset_id: int) -> tuple[str | None, list[int]]:
    """The stack *asset_id* is in, and every photograph in it, oldest first.

    Not narrowed to any viewer: the caller passes the ids through
    ``db.query_assets`` with the viewer's own limits.
    """
    row = conn.execute("SELECT stack_key FROM assets WHERE id = ?", (int(asset_id),)).fetchone()
    key = row[0] if row else None
    if not key:
        return None, []
    ids = [int(r[0]) for r in conn.execute(
        "SELECT id FROM assets WHERE stack_key = ? AND trashed = 0 "
        "ORDER BY COALESCE(captured_at, mtime), id", (key,))]
    return key, ids


def _choose(conn: sqlite3.Connection, key: str, *, cover: int | None, apart: bool,
            user_id: int | None) -> None:
    from . import db                                            # noqa: PLC0415
    with db._write_lock:                                        # noqa: SLF001
        conn.execute(
            "INSERT INTO stack_choices(key, cover, apart, chosen_by, chosen_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET cover = excluded.cover, "
            "apart = excluded.apart, chosen_by = excluded.chosen_by, "
            "chosen_at = excluded.chosen_at",
            (key, cover, 1 if apart else 0, user_id, time.time()))
        conn.commit()
    build(conn)


def choose_top(conn: sqlite3.Connection, key: str, asset_id: int,
               user_id: int | None = None) -> None:
    """Put *asset_id* on top of its stack, and keep the stack together."""
    _choose(conn, key, cover=int(asset_id), apart=False, user_id=user_id)


def set_apart(conn: sqlite3.Connection, key: str, apart: bool,
              user_id: int | None = None) -> None:
    """Show a stack as separate photographs, or as one again."""
    held = conn.execute("SELECT cover FROM stack_choices WHERE key = ?", (key,)).fetchone()
    _choose(conn, key, cover=held[0] if held else None, apart=apart, user_id=user_id)

