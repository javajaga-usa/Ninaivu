"""Reviewing duplicates side by side: which copies are the same photograph,
and what the household decided about them.

Two things already say that two files are the same photograph, and until now
each was shown on its own:

* the **content fingerprint** (``content_fingerprints``, BLAKE3 of the bytes):
  two files with the same fingerprint are *exact* copies, byte for byte, and
  that is certain;
* the **perceptual hash** grouping the scan writes (``assets.dup_group``):
  pictures that *look* the same — a burst, a resized or re-saved copy, the
  one WhatsApp sent back. That is a judgment, and it can be wrong.

:func:`groups` joins the two into one list: files are in one group when
either says so, and each group says whether it is exact (every member the
same bytes) or near. The review screen (``static/js/pages/family/duplicates.js``)
shows a group's members side by side and asks which to keep.

What a decision does, and what it never does
--------------------------------------------

**Nothing is ever deleted and no file is touched.** A decision only changes
the index:

``hide``
    The others are hidden (visibility 2, ``vis_source='duplicate'``), through
    :func:`db.set_visibility`, so the bulk-change undo history has them too.
    The family no longer sees them; an administrator still does, as they see
    every hidden item, and finds them again under *Set aside*.
``merge``
    The same, after carrying onto the kept photograph what people gave the
    others: each person's favourite and stars (the higher of the two), every
    album the others were in, and a caption somebody wrote by hand when the
    kept one has none. A merged album keeps the others too (hidden, so the
    family sees one); nothing is taken out of anything.
``keep_all``
    "These are not duplicates": the group is left as it is and leaves the
    list. A photograph that joins it later brings it back.

Every decision can be put back (:func:`put_back`) exactly: what each change
overwrote is written down first (``duplicate_undo``), and putting back
leaves alone anything somebody changed since.

Who sees what
-------------

The list is read through the viewer's own limits (``server/visibility.py``),
so a family member never learns that a hidden or explicit copy exists: a
group is only a group of what they may see. Deciding is an administrator's
alone, because hiding is.
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections import defaultdict
from typing import Any, Iterable, Sequence

from . import db

#: The tables, made by migration 4 (storage/migrations.py).
SCHEMA = (
    # One row per photograph a decision was about. ``action`` is ``hidden``,
    # ``merged`` (set aside, for ``keeper_id``) or ``kept`` (not a duplicate).
    """CREATE TABLE IF NOT EXISTS duplicate_reviews (
    asset_id   INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
    keeper_id  INTEGER REFERENCES assets(id) ON DELETE SET NULL,
    action     TEXT NOT NULL,
    batch      INTEGER NOT NULL,
    decided_by INTEGER,
    decided_at REAL NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS idx_duplicate_reviews_batch ON duplicate_reviews(batch)",
    # What a decision overwrote, to put it back: ``what`` is ``visibility``
    # (asset_id, the old level and source), ``rating`` (the kept photograph's
    # favourite and stars for user_id before), ``album`` (album_id the kept
    # one was added to) or ``caption`` (the kept one's caption before).
    """CREATE TABLE IF NOT EXISTS duplicate_undo (
    batch    INTEGER NOT NULL,
    what     TEXT NOT NULL,
    asset_id INTEGER NOT NULL,
    ref      INTEGER,
    before   TEXT
)""",
    "CREATE INDEX IF NOT EXISTS idx_duplicate_undo_batch ON duplicate_undo(batch)",
)

#: Moved by every decision and every put back: the list remembered for a
#: viewer (``db.cached_aggregate``) is worked out again.
GENERATION_KEY = "duplicate_review_generation"

#: Where a hidden duplicate's visibility came from.
SOURCE = "duplicate"

ACTIONS = ("hide", "merge", "keep_all")

#: What one member of a group carries, besides the viewer's own marks.
COLUMNS = ("id", "dup_group", "filename", "ext", "kind", "folder", "rel_path", "size",
           "width", "height", "sharpness", "thumb", "indexed_at", "rotation", "date_key",
           "captured_at", "camera", "visibility", "nsfw", "caption")

#: The most members one group shows: a phash chain in a large library can
#: join hundreds of near-identical frames, and a side-by-side screen of that
#: many is no review. The rest are counted.
GROUP_CAP = 24


def create(conn: sqlite3.Connection) -> None:
    """Make the tables (migration 4; also safe to run again)."""
    for statement in SCHEMA:
        conn.execute(statement)


# ---------------------------------------------------------------------------
# The groups
# ---------------------------------------------------------------------------

class _Union:
    """Disjoint sets of asset ids."""

    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

    def find(self, x: int) -> int:
        parent = self.parent
        parent.setdefault(x, x)
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def join(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # The smaller id is the root, so a group's key is its oldest member.
            if rb < ra:
                ra, rb = rb, ra
            self.parent[rb] = ra


def link(rows: Iterable[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Members joined by either the same ``dup_group`` or the same ``digest``,
    as lists of two or more, each sorted by id."""
    rows = list(rows)
    union = _Union()
    first: dict[tuple[str, str], int] = {}
    for row in rows:
        aid = int(row["id"])
        union.find(aid)
        for key in (("p", row.get("dup_group")), ("d", row.get("digest"))):
            if not key[1]:
                continue
            seen = first.setdefault(key, aid)
            if seen != aid:
                union.join(seen, aid)
    found: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        found[union.find(int(row["id"]))].append(row)
    return [sorted(members, key=lambda r: int(r["id"]))
            for members in found.values() if len(members) > 1]


def _candidates(conn: sqlite3.Connection, where: str, params: Sequence[Any],
                viewer_id: int) -> list[dict[str, Any]]:
    """Every row the viewer may see that has a near twin or an exact one."""
    from . import fingerprints                                  # noqa: PLC0415
    picked = ", ".join(f"a.{c}" for c in COLUMNS)
    rows = conn.execute(
        f"SELECT {picked}, f.digest AS digest, "
        "COALESCE(ua.favorite,0) AS favorite, COALESCE(ua.rating,0) AS rating, "
        "r.action AS decided "
        "FROM assets a "
        "LEFT JOIN content_fingerprints f ON f.asset_id=a.id AND f.algo=? "
        "  AND f.size=a.size AND f.mtime=a.mtime "
        "LEFT JOIN user_assets ua ON ua.asset_id=a.id AND ua.user_id=? "
        "LEFT JOIN duplicate_reviews r ON r.asset_id=a.id "
        f"WHERE {where} AND a.trashed=0 AND a.live_clip=0 AND a.raw_twin=0 "
        "AND (a.dup_group IS NOT NULL OR f.digest IN ("
        "  SELECT digest FROM content_fingerprints WHERE algo=? "
        "  GROUP BY digest HAVING COUNT(*) > 1))",
        (fingerprints.ALGO, int(viewer_id), *params, fingerprints.ALGO)).fetchall()
    return [dict(r) for r in rows]


def _describe(members: list[dict[str, Any]]) -> dict[str, Any]:
    """One group as the review draws it, from its members still in question."""
    best = db.best_of(members)
    digests = {m.get("digest") for m in members}
    exact = len(digests) == 1 and None not in digests
    best_row = next(m for m in members if int(m["id"]) == best)
    for m in members:
        m["same_bytes"] = bool(m.get("digest")) and m.get("digest") == best_row.get("digest")
    shown = sorted(members, key=lambda m: (int(m["id"]) != best, -int(m.get("size") or 0),
                                           int(m["id"])))
    return {
        "group": f"g{int(members[0]['id'])}",
        "kind": "exact" if exact else "near",
        "best": best,
        "count": len(members),
        "wasted": sum(int(m.get("size") or 0) for m in members) - int(best_row.get("size") or 0),
        "items": shown[:GROUP_CAP],
    }


def groups(conn: sqlite3.Connection, where: str, params: Sequence[Any], *,
           viewer_id: int = 0) -> list[dict[str, Any]]:
    """The groups still to review, most space to free first.

    *where* and *params* are the viewer's own limits on ``assets a``
    (``Viewer.sql``, plus the explicit shield). A group is still to review
    while at least two of its members are not set aside and one of those has
    no decision yet: so a group somebody said "not duplicates" about comes
    back only when a new copy joins it, and a group whose others were hidden
    is done.
    """
    out = []
    for members in link(_candidates(conn, where, params, viewer_id)):
        # Set aside, and still hidden: one shown again since (the console's
        # undo of the last visibility change) is in question again.
        standing = [m for m in members
                    if not (m.get("decided") in ("hidden", "merged")
                            and int(m.get("visibility") or 0) >= 2)]
        if len(standing) < 2 or all(m.get("decided") == "kept" for m in standing):
            continue
        out.append(_describe(standing))
    out.sort(key=lambda g: (-g["wasted"], g["group"]))
    return out


def connected(conn: sqlite3.Connection, ids: Sequence[int]) -> bool:
    """Whether these photographs are one group by fingerprint or look alone:
    a decision is refused for files that are not duplicates of each other."""
    from . import fingerprints                                  # noqa: PLC0415
    wanted = sorted({int(i) for i in ids})
    if len(wanted) < 2:
        return False
    marks = ",".join("?" * len(wanted))
    rows = [dict(r) for r in conn.execute(
        f"SELECT a.id, a.dup_group, f.digest FROM assets a "
        "LEFT JOIN content_fingerprints f ON f.asset_id=a.id AND f.algo=? "
        "  AND f.size=a.size AND f.mtime=a.mtime "
        f"WHERE a.id IN ({marks})", (fingerprints.ALGO, *wanted))]
    if len(rows) != len(wanted):
        return False
    found = link(rows)
    return len(found) == 1 and len(found[0]) == len(wanted)


# ---------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------

def _moved(conn: sqlite3.Connection) -> None:
    db.set_meta(conn, GENERATION_KEY, str(time.time_ns()))


def _next_batch(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT MAX(b) FROM (SELECT MAX(batch) b FROM duplicate_reviews "
        "UNION ALL SELECT MAX(batch) FROM duplicate_undo)").fetchone()
    return int(row[0] or 0) + 1


def _carry(conn: sqlite3.Connection, batch: int, keeper: int,
           others: Sequence[int]) -> dict[str, int]:
    """Put onto *keeper* what people gave *others*, writing down what it was.
    Called holding the write lock, inside the decision's transaction."""
    marks = ",".join("?" * len(others))
    carried = {"ratings": 0, "albums": 0, "caption": 0}
    # Each person's favourite and stars: the higher of the two.
    for row in conn.execute(
            "SELECT user_id, MAX(favorite) fav, MAX(rating) stars FROM user_assets "
            f"WHERE asset_id IN ({marks}) GROUP BY user_id", tuple(others)).fetchall():
        mine = conn.execute(
            "SELECT favorite, rating FROM user_assets WHERE user_id=? AND asset_id=?",
            (row["user_id"], keeper)).fetchone()
        fav = max(int(row["fav"] or 0), int(mine["favorite"]) if mine else 0)
        stars = max(int(row["stars"] or 0), int(mine["rating"]) if mine else 0)
        if mine and (fav, stars) == (int(mine["favorite"]), int(mine["rating"])):
            continue
        if not mine and not fav and not stars:
            continue
        conn.execute(
            "INSERT INTO duplicate_undo(batch, what, asset_id, ref, before) VALUES(?,?,?,?,?)",
            (batch, "rating", keeper, row["user_id"],
             json.dumps([int(mine["favorite"]), int(mine["rating"])] if mine else None)))
        conn.execute(
            "INSERT INTO user_assets(user_id, asset_id, favorite, rating) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id, asset_id) DO UPDATE SET favorite=excluded.favorite, "
            "rating=excluded.rating", (row["user_id"], keeper, fav, stars))
        carried["ratings"] += 1
    # Every album the others were in.
    now = time.time()
    for row in conn.execute(
            f"SELECT DISTINCT album_id FROM album_items WHERE asset_id IN ({marks}) "
            "AND album_id NOT IN (SELECT album_id FROM album_items WHERE asset_id=?)",
            (*others, keeper)).fetchall():
        conn.execute("INSERT INTO album_items(album_id, asset_id, added_at) VALUES(?,?,?)",
                     (row["album_id"], keeper, now))
        conn.execute(
            "INSERT INTO duplicate_undo(batch, what, asset_id, ref) VALUES(?,?,?,?)",
            (batch, "album", keeper, row["album_id"]))
        carried["albums"] += 1
    # A caption written by hand, when the kept one has none of its own.
    kept = conn.execute("SELECT caption, caption_source FROM assets WHERE id=?",
                        (keeper,)).fetchone()
    if kept and not (kept["caption_source"] == "manual" and (kept["caption"] or "").strip()):
        written = conn.execute(
            f"SELECT caption FROM assets WHERE id IN ({marks}) AND caption_source='manual' "
            "AND TRIM(COALESCE(caption,''))<>'' ORDER BY id LIMIT 1", tuple(others)).fetchone()
        if written:
            conn.execute(
                "INSERT INTO duplicate_undo(batch, what, asset_id, before) VALUES(?,?,?,?)",
                (batch, "caption", keeper,
                 json.dumps([kept["caption"], kept["caption_source"], written["caption"]])))
            conn.execute("UPDATE assets SET caption=?, caption_source='manual' WHERE id=?",
                         (written["caption"], keeper))
            carried["caption"] = 1
    return carried


def decide(conn: sqlite3.Connection, keeper: int, others: Sequence[int], action: str, *,
           user_id: int | None = None) -> dict[str, Any]:
    """Record a decision on one group and carry it out. *others* are the
    members not kept (for ``keep_all``, every other member). The caller has
    checked that the viewer may see every one and that they are one group
    (:func:`connected`). Returns what happened, with the batch to put back."""
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {ACTIONS}")
    keeper = int(keeper)
    others = sorted({int(i) for i in others} - {keeper})
    if not others:
        raise ValueError("nothing to decide")
    now = time.time()
    carried: dict[str, int] = {}
    with db._write_lock:                                          # noqa: SLF001
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            batch = _next_batch(conn)
            # A member decided before (kept, then a new copy came) is decided again.
            marks = ",".join("?" * (len(others) + 1))
            conn.execute(f"DELETE FROM duplicate_reviews WHERE asset_id IN ({marks})",
                         (keeper, *others))
            if action == "keep_all":
                conn.executemany(
                    "INSERT INTO duplicate_reviews(asset_id, keeper_id, action, batch, "
                    "decided_by, decided_at) VALUES(?,NULL,'kept',?,?,?)",
                    [(a, batch, user_id, now) for a in (keeper, *others)])
            else:
                if action == "merge":
                    carried = _carry(conn, batch, keeper, others)
                hidden = db.with_live_clips(conn, others)
                hmarks = ",".join("?" * len(hidden))
                conn.execute(
                    "INSERT INTO duplicate_undo(batch, what, asset_id, ref, before) "
                    f"SELECT ?, 'visibility', id, visibility, vis_source FROM assets "
                    f"WHERE id IN ({hmarks})", (batch, *hidden))
                conn.executemany(
                    "INSERT INTO duplicate_reviews(asset_id, keeper_id, action, batch, "
                    "decided_by, decided_at) VALUES(?,?,?,?,?,?)",
                    [(a, keeper, "merged" if action == "merge" else "hidden", batch,
                      user_id, now) for a in others])
            _moved(conn)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    if action != "keep_all":
        # Its own history entry too, so the console's undo of the last
        # visibility change knows about it; it takes the lock itself.
        db.set_visibility(conn, others, 2, source=SOURCE, user_id=user_id)
    return {"batch": batch, "action": action, "keeper": keeper, "others": others,
            "carried": carried}


def put_back(conn: sqlite3.Connection, batch: int) -> dict[str, Any]:
    """Undo one decision: the others shown again as they were, and what a
    merge carried taken off the kept photograph — but only where nobody has
    changed it since. Returns how many photographs came back, or ``found``
    False for a batch there is no record of."""
    batch = int(batch)
    with db._write_lock:                                          # noqa: SLF001
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            decided = conn.execute("SELECT COUNT(*) FROM duplicate_reviews WHERE batch=?",
                                   (batch,)).fetchone()[0]
            undo = conn.execute(
                "SELECT what, asset_id, ref, before FROM duplicate_undo WHERE batch=? "
                "ORDER BY rowid DESC", (batch,)).fetchall()
            if not decided and not undo:
                conn.rollback()
                return {"found": False, "shown": 0}
            shown = 0
            for row in undo:
                what, aid, ref, before = row["what"], row["asset_id"], row["ref"], row["before"]
                if what == "visibility":
                    # Only while it is still hidden as a duplicate: hidden by
                    # hand since, or shown again, is somebody's later choice.
                    cur = conn.execute(
                        "UPDATE assets SET visibility=?, vis_source=? WHERE id=? "
                        "AND vis_source=? AND visibility=2",
                        (int(ref), before, aid, SOURCE))
                    shown += max(cur.rowcount, 0)
                elif what == "album":
                    conn.execute("DELETE FROM album_items WHERE album_id=? AND asset_id=?",
                                 (ref, aid))
                elif what == "rating":
                    was = json.loads(before) if before else None
                    if was is None:
                        conn.execute("DELETE FROM user_assets WHERE user_id=? AND asset_id=? "
                                     "AND seen_at IS NULL", (ref, aid))
                        conn.execute("UPDATE user_assets SET favorite=0, rating=0 "
                                     "WHERE user_id=? AND asset_id=?", (ref, aid))
                    else:
                        conn.execute("UPDATE user_assets SET favorite=?, rating=? "
                                     "WHERE user_id=? AND asset_id=?", (was[0], was[1], ref, aid))
                elif what == "caption":
                    old, source, written = json.loads(before)
                    conn.execute("UPDATE assets SET caption=?, caption_source=? "
                                 "WHERE id=? AND caption IS ?", (old, source, aid, written))
            conn.execute("DELETE FROM duplicate_undo WHERE batch=?", (batch,))
            conn.execute("DELETE FROM duplicate_reviews WHERE batch=?", (batch,))
            _moved(conn)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return {"found": True, "shown": shown}


def set_aside(conn: sqlite3.Connection, *, limit: int = 60,
              offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    """The decisions that set photographs aside, newest first, one entry per
    decision: the kept photograph and the ones hidden for it. Administrators
    only; the caller checks."""
    total = conn.execute(
        "SELECT COUNT(DISTINCT batch) FROM duplicate_reviews "
        "WHERE action IN ('hidden','merged')").fetchone()[0]
    batches = conn.execute(
        "SELECT batch, MAX(keeper_id) keeper, MAX(action) action, MAX(decided_at) at, "
        "MAX(decided_by) who FROM duplicate_reviews WHERE action IN ('hidden','merged') "
        "GROUP BY batch ORDER BY batch DESC LIMIT ? OFFSET ?",
        (int(limit), int(offset))).fetchall()
    out = []
    for b in batches:
        ids = [int(r[0]) for r in conn.execute(
            "SELECT asset_id FROM duplicate_reviews WHERE batch=? ORDER BY asset_id",
            (b["batch"],))]
        out.append({"batch": int(b["batch"]), "keeper": b["keeper"], "action": b["action"],
                    "decided_at": b["at"], "decided_by": b["who"], "others": ids})
    return out, int(total or 0)
