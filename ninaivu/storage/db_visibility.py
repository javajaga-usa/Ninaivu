"""Who sees what: visibility levels, items kept private, folder rules and
their undo history, screenshots and flagged photographs hidden from the
family, and what the AI may read. Part of :mod:`.db`, which re-exports every
name; import them through ``db``, never on their own.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Sequence

from . import db as _db  # noqa: F401
from .db import (  # noqa: F401
    ADMIN_ONLY_KINDS,
    _write_lock,
    cached_aggregate,
    folder_clause,
    get_meta,
    roots_clause,
    scope_clause,
)


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

    An item somebody keeps "Only to me" is held the same way: no bulk change
    shows it again (the ``keep_private_hidden`` trigger is the backstop).
    """
    if int(visibility) >= 2:
        return ""
    floor = " AND private_to IS NULL"
    if ADMIN_ONLY_KINDS:
        names = ",".join("'%s'" % k for k in ADMIN_ONLY_KINDS)
        floor += f" AND kind NOT IN ({names})"
    return floor


# ---------------------------------------------------------------------------
# "Only to me"
# ---------------------------------------------------------------------------

#: The visibility source of an item its owner keeps to themselves.
PRIVATE_SOURCE = "private"


def set_private(conn: sqlite3.Connection, asset_ids: Sequence[int],
                user_id: int) -> int:
    """Take these items out of everybody else's view and keep them for *user_id*.

    Each item's visibility is held at hidden while it is private, and what it
    was is kept to put back. An item already private to somebody is left as it
    is: one person cannot take another's private item. The other half of a
    live photo or a RAW+JPEG pair goes with it. Returns how many changed.
    """
    if not asset_ids or not user_id:
        return 0
    ids = with_live_clips(conn, asset_ids)
    changed = 0
    with _write_lock:
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            marks = ",".join("?" * len(chunk))
            cur = conn.execute(
                f"UPDATE assets SET private_to=?, private_was=visibility, "
                f"private_was_src=vis_source, visibility=2, vis_source=? "
                f"WHERE id IN ({marks}) AND private_to IS NULL",
                (int(user_id), PRIVATE_SOURCE, *chunk))
            changed += max(0, cur.rowcount)
        conn.commit()
    if changed:
        forget_ai_reading(conn, ids=list(ids))
    return changed


def clear_private(conn: sqlite3.Connection, asset_ids: Sequence[int],
                  user_id: int | None) -> int:
    """Give these items back their visibility from before they were made private.

    *user_id* is the owner; None releases anybody's (an administrator). An
    item whose visibility was changed again while it was private — an
    administrator hid its folder, say — keeps that later decision rather than
    the one from before. Returns how many changed.
    """
    if not asset_ids:
        return 0
    ids = with_live_clips(conn, asset_ids)
    changed = 0
    with _write_lock:
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            marks = ",".join("?" * len(chunk))
            owner = "" if user_id is None else " AND private_to = ?"
            owner_params = [] if user_id is None else [int(user_id)]
            cur = conn.execute(
                f"UPDATE assets SET "
                f"visibility = CASE WHEN vis_source = ? "
                f"  THEN COALESCE(private_was, 1) ELSE visibility END, "
                f"vis_source = CASE WHEN vis_source = ? "
                f"  THEN COALESCE(private_was_src, 'manual') ELSE vis_source END, "
                f"private_to = NULL, private_was = NULL, private_was_src = NULL "
                f"WHERE id IN ({marks}) AND private_to IS NOT NULL{owner}",
                (PRIVATE_SOURCE, PRIVATE_SOURCE, *chunk, *owner_params))
            changed += max(0, cur.rowcount)
        conn.commit()
    return changed


def private_count(conn: sqlite3.Connection, roots: Sequence[str] | str,
                  user_id: int, scope: str | None = None) -> int:
    """How many items *user_id* keeps "Only to me", counted as the gallery
    lists them (a live photo or a RAW+JPEG shot once)."""
    if not user_id:
        return 0
    roots_sql, roots_params = roots_clause("a", roots)
    where = [roots_sql, "a.private_to = ?", "a.trashed = 0",
             "a.live_clip = 0", "a.raw_twin = 0"]
    params: list[Any] = [*roots_params, int(user_id)]
    scope_sql, scope_params = scope_clause("a", scope)
    if scope_sql:
        where.append(scope_sql)
        params.extend(scope_params)
    return int(conn.execute(
        f"SELECT COUNT(*) n FROM assets a WHERE {' AND '.join(where)}",
        params).fetchone()["n"])


def with_live_clips(conn: sqlite3.Connection, asset_ids: Sequence[int]) -> list[int]:
    """*asset_ids*, and the other half of every pair among them.

    A live photo is two rows: the still, and its clip (``live_clip=1``), which
    the household reaches by holding the still. Hiding, flagging or deleting
    the still has to take its clip with it: on its own the still went, and the
    clip stayed in every listing and played the same moment, with sound.

    A RAW taken with a JPEG is the same: the JPEG is what is shown, and the
    RAW (``raw_twin=1``) goes wherever the JPEG goes, so hiding a photograph
    does not leave its RAW visible to whoever can download it.
    """
    ids = [int(i) for i in asset_ids]
    if not ids:
        return ids
    found = companion_map(conn, ids)
    clips = [c for i in ids for c in found.get(i, ())]
    seen = set(ids)
    return ids + [c for c in dict.fromkeys(clips) if c not in seen]


def companion_map(conn: sqlite3.Connection, asset_ids: Sequence[int]) -> dict[int, list[int]]:
    """``still id -> [its clip and RAW ids]`` for those of *asset_ids* that
    have them (see :func:`with_live_clips`)."""
    ids = list(dict.fromkeys(int(i) for i in asset_ids))
    out: dict[int, list[int]] = {}
    # Matched as spelt first, through the (root, rel_path) index: compared
    # without case, each 500 ids read the whole library (a second for every
    # 5,000 photographs hidden). The case-blind match is only for a pair the
    # scan has not yet repaired the spelling of.
    pairs = (("live_video_path", "s.is_live = 1 AND c.live_clip = 1"),
             ("raw_path", "c.raw_twin = 1"))
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        placeholders = ",".join("?" * len(chunk))
        for column, condition in pairs:
            found = conn.execute(
                f"SELECT s.id sid, c.id cid FROM assets s CROSS JOIN assets c "
                f"ON c.root = s.root AND c.rel_path = s.{column} "
                f"WHERE s.id IN ({placeholders}) AND COALESCE(s.{column}, '') <> '' "
                f"AND {condition}", chunk).fetchall()
            for r in found:
                out.setdefault(int(r["sid"]), []).append(int(r["cid"]))
            matched = {int(r["sid"]) for r in found}
            for r in conn.execute(
                    f"SELECT s.id FROM assets s WHERE s.id IN ({placeholders}) "
                    f"AND COALESCE(s.{column}, '') <> ''", chunk).fetchall():
                if int(r["id"]) in matched:
                    continue
                for c in conn.execute(
                        f"SELECT c.id cid FROM assets s JOIN assets c ON c.root = s.root "
                        f"AND c.rel_path = s.{column} COLLATE NOCASE "
                        f"WHERE s.id = ? AND {condition}", (int(r["id"]),)):
                    out.setdefault(int(r["id"]), []).append(int(c["cid"]))
    return out


#: The same thing under the name it now deserves.
with_companions = with_live_clips


def raw_twin_of(conn: sqlite3.Connection, asset: dict[str, Any]) -> dict[str, Any] | None:
    """The RAW row paired with *asset*, or None."""
    path = asset.get("raw_path")
    if not path:
        return None
    return _companion(conn, asset["root"], path, "raw_twin = 1 AND trashed = 0")


def _companion(conn: sqlite3.Connection, root: str, path: str,
               condition: str) -> dict[str, Any] | None:
    """The row at *path* in *root* meeting *condition*, spelt as stored first.

    The scan writes the companion's path exactly as the folder spells it, so
    the exact match finds it through the (root, rel_path) index. Compared
    without case, as it was, SQLite could not use that index and read every
    row of the library: 25 ms a photograph on 210,000, five seconds for a
    page of On This Day. The case-blind match stays as a fallback for a row
    the scan has not yet repaired.
    """
    row = conn.execute(
        f"SELECT * FROM assets WHERE root = ? AND rel_path = ? AND {condition}",
        (root, path)).fetchone()
    if row is None:
        row = conn.execute(
            f"SELECT * FROM assets WHERE root = ? AND rel_path = ? COLLATE NOCASE "
            f"AND {condition}", (root, path)).fetchone()
    return dict(row) if row else None


def live_clip_of(conn: sqlite3.Connection, asset: dict[str, Any]) -> dict[str, Any] | None:
    """The clip row of a live photo, or None."""
    path = asset.get("live_video_path")
    if not (asset.get("is_live") and path):
        return None
    # A clip in the bin is not played from the still, nor a row that is not
    # a video at all.
    return _companion(conn, asset["root"], path, "kind = 'video' AND trashed = 0")


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
            restored += _refollow_folder_rules(conn, batch_id, batch["root"],
                                               batch["folder"] or "")
        conn.execute("UPDATE visibility_batches SET undone_at=? WHERE id=?",
                     (time.time(), batch_id))
        conn.commit()
    # Undoing can hide things again; what AI made of them while they were
    # visible goes, as it does whenever something is hidden.
    if batch["scope"] == "folder":
        forget_ai_reading(conn, root=batch["root"])
    else:
        ids = [int(r[0]) for r in conn.execute(
            "SELECT asset_id FROM visibility_undo WHERE batch_id=?", (batch_id,))]
        for at in range(0, len(ids), 500):
            forget_ai_reading(conn, ids=ids[at:at + 500])
    return {"ok": True, "batch_id": batch_id, "restored": restored,
            "folder": batch["folder"], "visibility": int(batch["visibility"])}


def _refollow_folder_rules(conn: sqlite3.Connection, batch_id: int, root: str,
                           folder: str) -> int:
    """Files that came into an undone folder change's folder after it was made.

    The snapshot holds only the files there at the time. Files scanned in
    since took the rule being undone, and kept it: a folder opened to the
    family by mistake and put back stayed open for every photograph that had
    arrived in between. Each is set again by the rules as they now stand.
    Called holding ``_write_lock``. Returns how many changed.
    """
    where, params = _scope_sql(root, folder)
    rows = conn.execute(
        f"SELECT id, folder, kind, visibility FROM assets WHERE {where} "
        "AND vis_source='folder' AND id NOT IN "
        "(SELECT asset_id FROM visibility_undo WHERE batch_id=?)",
        [*params, batch_id]).fetchall()
    rules = folder_rules(conn, root)
    changes = []
    for row in rows:
        found = folder_rule_for(rules, row["folder"] or "")
        level, source = (found[0], "folder") if found else (1, "default")
        if row["kind"] in ADMIN_ONLY_KINDS and level < 2:
            level, source = 2, "kind"
        if level != int(row["visibility"]) or source != "folder":
            changes.append((level, source, int(row["id"])))
    conn.executemany("UPDATE assets SET visibility=?, vis_source=? WHERE id=?", changes)
    return len(changes)


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
    # An item held while an administrator screens it (features/screening/storage.py)
    # is not hidden yet: Ignore puts it back as it was, reading and all.
    where = ["visibility >= 2", "kind != 'audio'",
             "COALESCE(vis_source, '') != 'screening'"]
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
        "OR EXISTS (SELECT 1 FROM descriptions d WHERE d.asset_id=assets.id) "
        "OR EXISTS (SELECT 1 FROM video_moments_done m WHERE m.asset_id=assets.id) "
        "OR EXISTS (SELECT 1 FROM letter_words l WHERE l.asset_id=assets.id "
        "AND l.source = 'read') "
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
        _db.forget_embeddings()
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
    # The features' readings too, in order (the readings_forgotten hook,
    # ninaivu/features): the on-device engine's descriptions, the moments
    # found inside its video, and the words the text reader read off a
    # letter; words somebody typed are theirs, and stay as a caption does.
    from .. import features                           # noqa: PLC0415
    features.call("readings_forgotten", conn, found)
    # In pieces: a whole library folder set to admins only is tens of
    # thousands of ids, past what one statement may be given.
    for at in range(0, len(found), 500):
        part = found[at:at + 500]
        marks = ",".join("?" * len(part))
        conn.execute(
            "UPDATE assets SET ocr_text=NULL, ocr_version=0, ai_version=0, "
            # How much it looks like people in only their underwear is read
            # off the vector that goes below. A flag it gave stays: hidden
            # now, it is still explicit should it be shown again.
            "revealing=0, revealing_version=0, "
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


def search_shield_clause(conn: sqlite3.Connection, alias: str,
                         near_explicit: float = 0.0,
                         revealing: float = 0.0) -> tuple[str, list[Any]]:
    """SQL keeping a family member's search off what is nearly hidden.

    Search is where the few items the rules missed come to the top: words
    like "screenshot" or an adult word rank exactly those first, out of a
    library where nobody would come across them by scrolling. So a family
    member's search leaves out every screenshot and document
    (:func:`screen_clause`), shown to the family by hand or not, and every
    item the explicit check scored at *near_explicit* or over, short of the
    line that flags it, and every picture of people in nothing but their
    underwear scored at *revealing* or over (media/revealing.py). A verdict
    an administrator gave by hand (``nsfw_source='manual'``) is theirs and
    stands against both scores.
    """
    dot = f"{alias}." if alias else ""
    screen_sql, params = screen_clause(conn, alias)
    clause = f"NOT {screen_sql}"
    if near_explicit > 0:
        clause += f" AND ({dot}nsfw_source='manual' OR {dot}nsfw_score < ?)"
        params = [*params, float(near_explicit)]
    if revealing > 0:
        clause += f" AND ({dot}nsfw_source='manual' OR {dot}revealing < ?)"
        params = [*params, float(revealing)]
    return clause, params


def screens_not_explicit(conn: sqlite3.Connection, root: str, *,
                         ids: Sequence[int] | None = None) -> int:
    """Screenshots and documents are hidden, not explicit.

    Explicit is for adult content only. The content check used to call a
    good many documents and screenshots explicit (see ``ai.NSFW_PROMPTS``),
    which put a passport or a chat screenshot under Explicit on the Folders
    screen. Every picture :func:`screen_clause` calls a screenshot or document
    loses a flag the model gave it, and is hidden the way the screenshot rule
    hides one, whatever that rule's setting: it was out of the family app as
    explicit and stays out of it as hidden. A flag an administrator set by
    hand is theirs and stays, and so does the model's on a file that is not
    hidden after this (one shown to the family file by file): explicit is
    what keeps it out of the family app. *ids* narrows it to items just tagged.
    Returns how many were changed.
    """
    from ..media import screens                      # noqa: PLC0415

    clause, params = screen_clause(conn)
    where = f"root=? AND nsfw=1 AND nsfw_source<>'manual' AND {clause}"
    args: list[Any] = [str(root), *params]
    if ids is not None:
        if not ids:
            return 0
        where += f" AND id IN ({','.join('?' * len(ids))})"
        args += [int(i) for i in ids]
    found = [int(r[0]) for r in conn.execute(f"SELECT id FROM assets WHERE {where}", args)]
    if not found:
        return 0
    sources = ",".join("'%s'" % s for s in screens.REPLACEABLE_SOURCES)
    hidden: list[int] = []
    changed = 0
    with _write_lock:
        for at in range(0, len(found), 500):
            chunk = found[at:at + 500]
            marks = ",".join("?" * len(chunk))
            hidden += [int(r[0]) for r in conn.execute(
                f"SELECT id FROM assets WHERE id IN ({marks}) AND visibility < 2 "
                f"AND vis_source IN ({sources})", chunk)]
            conn.execute(
                f"UPDATE assets SET visibility=2, vis_source='{SCREEN_SOURCE}' "
                f"WHERE id IN ({marks}) AND visibility < 2 AND vis_source IN ({sources})",
                chunk)
            # Only what is hidden now loses the flag. A file somebody showed
            # to the family by hand (``vis_source='item'``) is not hidden
            # above, and without its flag it would reach the family app.
            changed += conn.execute(
                f"UPDATE assets SET nsfw=0 WHERE id IN ({marks}) AND visibility >= 2",
                chunk).rowcount
        conn.commit()
    for at in range(0, len(hidden), 500):
        forget_ai_reading(conn, ids=hidden[at:at + 500])
    return changed


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


def store_revealing_scores(conn: sqlite3.Connection,
                           scores: Sequence[tuple[int, float]]) -> None:
    """Record how much each picture looks like people in only their
    underwear, with this version (media/revealing.py)."""
    from ..media import revealing                    # noqa: PLC0415

    if not scores:
        return
    with _write_lock:
        conn.executemany(
            "UPDATE assets SET revealing=?, revealing_version=? WHERE id=?",
            [(round(float(score), 4), revealing.VERSION, int(asset_id))
             for asset_id, score in scores])
        conn.commit()


#: ``nsfw_source`` of a picture flagged explicit by the underwear check
#: (:func:`flag_revealing`), so a higher line set since, or the check turned
#: off, can take back exactly the flags it gave (:func:`unflag_revealing`).
#: Like ``auto``, it is Ninaivu's own verdict and the tagger's may replace it.
REVEALING_SOURCE = "underwear"


def flag_revealing(conn: sqlite3.Connection, root: str, line: float, *,
                   ids: Sequence[int] | None = None) -> int:
    """Flag as explicit every picture of people in only their underwear
    scored at *line* or over, so it is out of the family app altogether, the
    gallery as well as search (media/revealing.py).

    A verdict an administrator gave by hand is theirs and stays, and so does
    the content check's own flag. A screenshot or document already hidden is
    left alone: hidden is what keeps those out, and :func:`screens_not_explicit`
    would only take the flag off again. *ids* narrows it to items just judged.
    Returns how many were flagged.
    """
    if line <= 0:
        return 0
    screen_sql, screen_params = screen_clause(conn)
    where = ("root=? AND kind IN ('picture', 'video') AND revealing >= ? AND nsfw=0 "
             f"AND nsfw_source<>'manual' AND NOT (visibility >= 2 AND {screen_sql})")
    args: list[Any] = [str(root), float(line), *screen_params]
    if ids is not None:
        if not ids:
            return 0
        where += f" AND id IN ({','.join('?' * len(ids))})"
        args += [int(i) for i in ids]
    with _write_lock:
        flagged = [int(r[0]) for r in conn.execute(f"SELECT id FROM assets WHERE {where}", args)]
        for at in range(0, len(flagged), 500):
            chunk = flagged[at:at + 500]
            conn.execute(
                f"UPDATE assets SET nsfw=1, nsfw_source='{REVEALING_SOURCE}' "
                f"WHERE id IN ({','.join('?' * len(chunk))})", chunk)
        conn.commit()
    return len(flagged)


def unflag_revealing(conn: sqlite3.Connection, root: str, line: float,
                     content_line: float) -> int:
    """Take back the underwear check's flags that its line no longer gives:
    every photo or video it flagged that scores under *line*, or all of them when
    *line* is 0 (the check off). One the content check would flag on its own
    (``nsfw_score`` at *content_line* or over) keeps its flag. A live clip or
    RAW that was flagged with its photograph is unflagged with it, unless its
    own score or an administrator says otherwise. Returns how many pictures."""
    from ..media import revealing                    # noqa: PLC0415

    # Only a picture judged by this version: one whose reading was forgotten
    # (hidden, storage forget_ai_reading) has no score to go by.
    where = (f"root=? AND kind IN ('picture', 'video') AND nsfw=1 AND nsfw_source='{REVEALING_SOURCE}' "
             "AND revealing_version=? AND (nsfw_score IS NULL OR nsfw_score < ?)")
    args: list[Any] = [str(root), revealing.VERSION, float(content_line)]
    if line > 0:
        where += " AND revealing < ?"
        args.append(float(line))
    found = [int(r[0]) for r in conn.execute(f"SELECT id FROM assets WHERE {where}", args)]
    if not found:
        return 0
    companions = companion_map(conn, found)
    others = list(dict.fromkeys(c for i in found for c in companions.get(i, ())))
    with _write_lock:
        for at in range(0, len(found), 500):
            chunk = found[at:at + 500]
            conn.execute(
                "UPDATE assets SET nsfw=0, nsfw_source='auto' "
                f"WHERE id IN ({','.join('?' * len(chunk))})", chunk)
        for at in range(0, len(others), 500):
            chunk = others[at:at + 500]
            conn.execute(
                "UPDATE assets SET nsfw=0 WHERE nsfw=1 AND nsfw_source<>'manual' "
                "AND (nsfw_score IS NULL OR nsfw_score < ?) "
                f"AND id IN ({','.join('?' * len(chunk))})", [float(content_line), *chunk])
        conn.commit()
    return len(found)


def flag_explicit_scores(conn: sqlite3.Connection, root: str, line: float, *,
                         ids: Sequence[int] | None = None) -> int:
    """Flag as explicit every photo and video the content check scored at
    *line* or over and left unflagged: scored under a higher line, before
    it was lowered. A verdict an administrator gave by hand stays, hidden
    items are left as they are (a screenshot is hidden, not explicit), and
    nothing is unflagged here. Returns how many were flagged.
    """
    if line <= 0:
        return 0
    from ..media.scanner import AI_VERSION            # noqa: PLC0415

    # Only a score the current model gave: a change of model leaves the old
    # one's scores, on its own scale (CLIP's run far over SigLIP's line),
    # until the picture is tagged again.
    where = ("root=? AND kind IN ('picture', 'video') AND nsfw=0 AND nsfw_score >= ? "
             "AND nsfw_source<>'manual' AND visibility < 2 AND ai_version >= ?")
    args: list[Any] = [str(root), float(line), AI_VERSION]
    if ids is not None:
        if not ids:
            return 0
        where += f" AND id IN ({','.join('?' * len(ids))})"
        args += [int(i) for i in ids]
    with _write_lock:
        flagged = conn.execute(f"UPDATE assets SET nsfw=1 WHERE {where}", args).rowcount
        conn.commit()
    return flagged


def unflag_explicit_scores(conn: sqlite3.Connection, root: str, line: float,
                           revealing_line: float) -> int:
    """Take back the content check's flags that its line no longer gives:
    every photo or video it flagged whose score, from the current model, is
    under *line* — a line raised since it was flagged. An administrator's
    verdict and the underwear check's flags are their own, and one the
    underwear check would flag (``revealing`` at *revealing_line* or over,
    when that check is on) keeps its flag. A live clip or RAW flagged with
    its photograph is unflagged with it, unless its own score or an
    administrator says otherwise. Returns how many were unflagged.
    """
    from ..media import revealing                    # noqa: PLC0415
    from ..media.scanner import AI_VERSION            # noqa: PLC0415

    if line <= 0:
        return 0
    where = ("root=? AND kind IN ('picture', 'video') AND nsfw=1 AND nsfw_source='auto' "
             "AND nsfw_score < ? AND ai_version >= ? AND visibility < 2 "
             "AND live_clip=0 AND raw_twin=0")
    args: list[Any] = [str(root), float(line), AI_VERSION]
    if revealing_line > 0:
        where += " AND NOT (revealing_version=? AND revealing >= ?)"
        args += [revealing.VERSION, float(revealing_line)]
    found = [int(r[0]) for r in conn.execute(f"SELECT id FROM assets WHERE {where}", args)]
    if not found:
        return 0
    companions = companion_map(conn, found)
    others = list(dict.fromkeys(c for i in found for c in companions.get(i, ())))
    with _write_lock:
        for at in range(0, len(found), 500):
            chunk = found[at:at + 500]
            conn.execute(f"UPDATE assets SET nsfw=0 WHERE id IN ({','.join('?' * len(chunk))})",
                         chunk)
        for at in range(0, len(others), 500):
            chunk = others[at:at + 500]
            conn.execute(
                "UPDATE assets SET nsfw=0 WHERE nsfw=1 AND nsfw_source='auto' "
                "AND (nsfw_score IS NULL OR nsfw_score < ?) "
                f"AND id IN ({','.join('?' * len(chunk))})", [float(line), *chunk])
        conn.commit()
    return len(found)


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


def _without_rule(conn: sqlite3.Connection, root: str, folder: str
                  ) -> list[tuple[int, str, int, int, str]]:
    """What every file the rule on *folder* set would go back to without it.

    Only files that took their level from a folder rule move. Anything decided
    item by item, hidden on disk, kept "Only to me", held back as a screenshot
    or by its kind keeps its level: taking a rule away is not a decision about
    those. Each file goes to the rule that covers it once this one is gone (a
    rule further up, or one further down that still stands), or to the default.
    Rows are ``(level, source, id, level before, source before)``.
    """
    where, params = _scope_sql(root, folder)
    rules = [r for r in folder_rules(conn, root) if (r["folder"] or "") != folder]
    out = []
    for row in conn.execute(
            f"SELECT id, folder, kind, visibility, vis_source FROM assets "
            f"WHERE {where} AND vis_source='folder'", params):
        found = folder_rule_for(rules, row["folder"] or "")
        level, source = (found[0], "folder") if found else (1, "default")
        if row["kind"] in ADMIN_ONLY_KINDS and level < 2:
            level, source = 2, "kind"
        out.append((level, source, int(row["id"]),
                    int(row["visibility"]), str(row["vis_source"])))
    return out


def preview_clear_folder_rule(conn: sqlite3.Connection, root: str,
                              folder: str) -> dict[str, Any]:
    """What taking the rule off *folder* would do: how many files move, and
    how many of them become visible to more people (``exposed``)."""
    folder = (folder or "").strip().strip("/")
    moves = _without_rule(conn, root, folder)
    exposed = sum(1 for level, _, _, before, _ in moves if level < before)
    rule = conn.execute("SELECT visibility FROM folder_rules WHERE root=? AND folder=?",
                        (root, folder)).fetchone()
    return {"folder": folder, "has_rule": rule is not None,
            "rule": int(rule["visibility"]) if rule else None,
            "total": len(moves), "exposed": exposed,
            "hidden_by_the_filesystem": 0, "decided_individually": 0,
            "child_rules": 0}


def clear_folder_rule(conn: sqlite3.Connection, root: str, folder: str,
                      user_id: int | None = None) -> int:
    """Take the rule off *folder*, and put its files back as if it was never set.

    Deleting the rule alone left every file it had hidden still hidden, with
    nothing on the Visibility tab saying why: "stop hiding this folder" did
    nothing anybody could see. The files that took their level from this rule
    now follow whatever rule still covers them (see :func:`_without_rule`).
    Recorded in the undo history like any folder change. Returns how many
    files changed.
    """
    folder = (folder or "").strip().strip("/")
    prior = conn.execute("SELECT visibility FROM folder_rules WHERE root=? AND folder=?",
                         (root, folder)).fetchone()
    moves = _without_rule(conn, root, folder)
    changes = [(level, source, asset_id) for level, source, asset_id, before, was in moves
               if level != before or source != was]
    with _write_lock:
        if prior is not None or changes:
            # What the folder now resolves to, for the history's one line.
            rules = [r for r in folder_rules(conn, root) if (r["folder"] or "") != folder]
            found = folder_rule_for(rules, folder)
            cur = conn.execute(
                "INSERT INTO visibility_batches(created_at, created_by, scope, root, "
                "folder, visibility, exposed, had_rule, prior_rule, affected) "
                "VALUES(?,?,'folder',?,?,?,?,?,?,?)",
                (time.time(), user_id, root, folder, found[0] if found else 1,
                 sum(1 for level, _, _, before, _ in moves if level < before),
                 1 if prior else 0, int(prior["visibility"]) if prior else None,
                 len(changes)))
            batch_id = int(cur.lastrowid)
            conn.executemany(
                "INSERT INTO visibility_undo(batch_id, asset_id, visibility, vis_source) "
                "VALUES(?,?,?,?)",
                [(batch_id, asset_id, before, was)
                 for level, source, asset_id, before, was in moves
                 if level != before or source != was])
        conn.execute("DELETE FROM folder_rules WHERE root=? AND folder=?", (root, folder))
        conn.executemany("UPDATE assets SET visibility=?, vis_source=? WHERE id=?", changes)
        conn.commit()
    _trim_undo_history(conn)
    hidden_now = [asset_id for level, _, asset_id in changes if level >= 2]
    for at in range(0, len(hidden_now), 500):
        forget_ai_reading(conn, ids=hidden_now[at:at + 500])
    return len(changes)


def folder_counts(conn: sqlite3.Connection, root: str) -> tuple[int, list[dict[str, Any]]]:
    """How many items one library folder holds outside the bin, and how many
    each folder in it holds (``folder``, ``n``; the top level left out).

    Remembered until the library changes, and handed to every caller as the
    same object, so it must only be read: the console's folder pickers ask
    for it on each open, and read and copied afresh it was most of a second
    on a library of 210,000 items.
    """
    return cached_aggregate(conn, ("folder_counts", root), lambda: (
        int(conn.execute("SELECT COUNT(*) FROM assets WHERE root=? AND trashed=0",
                         (root,)).fetchone()[0] or 0),
        [dict(entry) for entry in conn.execute(
            "SELECT folder, COUNT(*) n FROM assets WHERE root=? AND trashed=0 "
            "AND folder != '' GROUP BY folder ORDER BY folder", (root,))],
    ), shared=True)


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
