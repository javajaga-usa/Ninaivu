"""Library folders inside other library folders.

Nothing used to stop a household listing both ``/Volumes/SSD/Master`` and
``/Volumes/SSD/Master/Ninaivu Archive``: the Archive's default place is inside
the first library, and "Add a folder" took it as one more. Each library
folder is walked on its own, so every photograph in the inner one had two
rows, one under each folder, and what people did with it (a name, a
favourite, an album, a voice story) landed on whichever row they happened to
open. Removing either folder then deleted its rows and, by cascade,
everything on them: half of a family's work, with the button saying the
files are untouched (which they were).

:func:`fold` is what removing one of such a pair does now. Each row of the
folder going that the other folder also covers is folded into the other's
row for the same file, with everything people did with it; a row with no
twin there yet (that part was never walked from the other folder) is
re-keyed to the other folder instead. :func:`relation` is the check
"Add a folder" makes first.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from typing import Any

from . import db

log = logging.getLogger(__name__)

#: Columns of an asset that a person set by hand, each with the column that
#: says so. A manual value on the row going wins over an automatic one on the
#: row kept.
_MANUAL = (
    (("tags",), "tags_source"),
    (("caption",), "caption_source"),
    (("nsfw",), "nsfw_source"),
    (("rotation",), "rot_source"),
    (("captured_at", "date_key"), "date_source"),
)

#: Tables handled by hand below rather than by the generic re-pointing.
#: A storage-check record is the file's at the place it was checked; the twin
#: has its own, and the one going would only be a second history.
_BY_HAND = {"faces", "user_assets", "assets", "bitrot_records"}


def _norm(path: str) -> str:
    # normcase first: on Windows it also turns "/" into "\\", which would
    # undo the replace and leave no "/" to compare on.
    return os.path.normcase(str(path)).replace("\\", "/").rstrip("/")


def inside(child: str, parent: str) -> str | None:
    """The path of *child* below *parent*, spelled as *child* spells it, or
    None when it is not strictly inside."""
    c, p = _norm(child), _norm(parent)
    if not p or not c.startswith(p + "/"):
        return None
    return str(child).replace("\\", "/").rstrip("/")[len(p) + 1:]


def relation(path: str, roots: list[str]) -> tuple[list[str], list[str]]:
    """``(folders holding path, folders inside path)`` among *roots*."""
    around = [r for r in roots if inside(path, r) is not None]
    within = [r for r in roots if inside(r, path) is not None]
    return around, within


def _asset_references(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Every ``(table, column)`` that points at ``assets.id`` by foreign key,
    found rather than listed, so a table added later is carried too."""
    out = []
    for (table,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'").fetchall():
        if table in _BY_HAND:
            continue
        try:
            keys = conn.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
        except sqlite3.DatabaseError:
            continue
        for key in keys:
            if key[2] == "assets" and (key[4] in (None, "id")):
                out.append((table, key[3]))
    return out


def fold(conn: sqlite3.Connection, going: str, kept: str) -> dict[str, int]:
    """Move the rows of library folder *going* that *kept* also covers onto
    *kept*, keeping everything people did with them.

    One of the two must be inside the other. Rows of *going* outside *kept*
    are left alone, for the caller to deal with as for any folder removed.
    Returns ``{"merged": rows folded into a twin, "moved": rows re-keyed,
    "thumbs": thumbnail names nothing uses any more, for the caller to
    remove}``.
    """
    below = inside(kept, going)
    above = inside(going, kept)
    if below is None and above is None:
        raise ValueError(f"{going} and {kept} are not one inside the other")

    # The rel_path each row of *going* has under *kept*, as SQL on `assets`.
    if below is not None:
        prefix = below + "/"
        covered = "substr(rel_path, 1, ?) = ?"
        covered_args: tuple[Any, ...] = (len(prefix), prefix)
        new_rel = "substr(rel_path, ?)"
        new_rel_args: tuple[Any, ...] = (len(prefix) + 1,)
        new_folder = ("CASE WHEN folder = ? THEN '' ELSE substr(folder, ?) END")
        new_folder_args: tuple[Any, ...] = (below, len(prefix) + 1)
    else:
        covered = "1"
        covered_args = ()
        new_rel = "? || rel_path"
        new_rel_args = (above + "/",)
        new_folder = "CASE WHEN folder = '' THEN ? ELSE ? || folder END"
        new_folder_args = (above, above + "/")

    with db._write_lock:
        conn.execute("DROP TABLE IF EXISTS temp.fold_pairs")
        conn.execute("CREATE TEMP TABLE fold_pairs (old INTEGER PRIMARY KEY, "
                     "new INTEGER, rel TEXT NOT NULL, folder TEXT NOT NULL)")
        conn.execute(
            f"INSERT INTO temp.fold_pairs(old, rel, folder) "
            f"SELECT id, {new_rel}, {new_folder} FROM assets "
            f"WHERE root=? AND {covered}",
            (*new_rel_args, *new_folder_args, going, *covered_args))
        conn.execute(
            "UPDATE temp.fold_pairs SET new=(SELECT a.id FROM assets a "
            "WHERE a.root=? AND a.rel_path=temp.fold_pairs.rel)", (kept,))
        conn.execute("CREATE INDEX temp.fold_pairs_new ON fold_pairs(new)")
        merged = conn.execute(
            "SELECT COUNT(*) FROM temp.fold_pairs WHERE new IS NOT NULL").fetchone()[0]
        moved = conn.execute(
            "SELECT COUNT(*) FROM temp.fold_pairs WHERE new IS NULL").fetchone()[0]
        thumbs: list[str] = []
        try:
            if merged:
                thumbs = _merge_twins(conn)
            # No twin under *kept* yet: the same row, known by its place there.
            conn.execute(
                "UPDATE assets SET root=?, "
                "rel_path=(SELECT rel FROM temp.fold_pairs p WHERE p.old=assets.id), "
                "folder=(SELECT folder FROM temp.fold_pairs p WHERE p.old=assets.id) "
                "WHERE id IN (SELECT old FROM temp.fold_pairs WHERE new IS NULL)",
                (kept,))
            _carry_folder_rules(conn, going, kept, below, above)
            try:
                conn.execute(
                    "UPDATE bitrot_records SET root=?, rel_path=(SELECT a.rel_path "
                    "FROM assets a WHERE a.id=bitrot_records.asset_id) "
                    "WHERE asset_id IN (SELECT old FROM temp.fold_pairs WHERE new IS NULL)",
                    (kept,))
            except sqlite3.OperationalError:
                pass                      # never had a storage check
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.execute("DROP TABLE IF EXISTS temp.fold_pairs")
    log.info("folded %s into %s: %d merged with their twin, %d moved",
             going, kept, merged, moved)
    return {"merged": int(merged), "moved": int(moved), "thumbs": thumbs}


def _carry_folder_rules(conn: sqlite3.Connection, going: str, kept: str,
                        below: str | None, above: str | None) -> None:
    """Re-key *going*'s folder rules that cover part of *kept* onto *kept*.

    A rule is keyed by its library folder, so one left on *going* names a
    library that is no longer there: the photographs already hidden stay
    hidden, but the next one put in that folder is indexed under *kept*
    with no rule, and shown to the family (audit D-03). A rule on a folder
    above *kept* covers all of it, and becomes a rule on its top. Where
    *kept* has a rule of its own on the same folder, the more private wins.
    """
    try:
        rules = conn.execute(
            "SELECT folder, visibility, created_at, created_by FROM folder_rules "
            "WHERE root=?", (going,)).fetchall()
    except sqlite3.OperationalError:
        return                            # an index from before sign-in
    for folder, visibility, created_at, created_by in rules:
        folder = str(folder or "").strip("/")
        if above is not None:
            target = f"{above}/{folder}" if folder else above
        elif folder == below:
            target = ""
        elif folder.startswith(below + "/"):
            target = folder[len(below) + 1:]
        elif not folder or below.startswith(folder + "/"):
            target = ""                   # the rule covers all of *kept*
        else:
            continue                      # a part of *going* outside *kept*
        conn.execute(
            "INSERT INTO folder_rules(root, folder, visibility, created_at, created_by) "
            "VALUES(?,?,?,?,?) ON CONFLICT(root, folder) DO UPDATE SET "
            "visibility=MAX(visibility, excluded.visibility)",
            (kept, target, visibility, created_at, created_by))
        if above is not None or folder == below or folder.startswith(below + "/"):
            conn.execute("DELETE FROM folder_rules WHERE root=? AND folder=?",
                         (going, folder))


def _merge_twins(conn: sqlite3.Connection) -> list[str]:
    """Fold every row of ``temp.fold_pairs`` that has a twin into it, then
    delete it. Runs inside :func:`fold`'s lock and transaction. Returns the
    thumbnail names of the rows deleted that their twin does not use."""
    twin = "(SELECT new FROM temp.fold_pairs p WHERE p.old={col})"
    pairs = "SELECT old FROM temp.fold_pairs WHERE new IS NOT NULL"

    # What a person set by hand on the row going, where the twin has only
    # what a scan decided.
    for columns, source in _MANUAL:
        sets = ", ".join(f"{c}=(SELECT g.{c} FROM assets g JOIN temp.fold_pairs p "
                         f"ON p.old=g.id WHERE p.new=assets.id)" for c in (*columns, source))
        conn.execute(
            f"UPDATE assets SET {sets} WHERE {source} != 'manual' AND id IN ("
            f"SELECT p.new FROM temp.fold_pairs p JOIN assets g ON g.id=p.old "
            f"WHERE g.{source}='manual')")
    # Visibility: the more private of the two, and an "only to me" kept.
    conn.execute(
        "UPDATE assets SET "
        "visibility=MAX(visibility, (SELECT g.visibility FROM assets g JOIN temp.fold_pairs p "
        "  ON p.old=g.id WHERE p.new=assets.id)), "
        "vis_source=CASE WHEN vis_source='default' THEN (SELECT g.vis_source FROM assets g "
        "  JOIN temp.fold_pairs p ON p.old=g.id WHERE p.new=assets.id) ELSE vis_source END "
        "WHERE id IN (SELECT new FROM temp.fold_pairs WHERE new IS NOT NULL)")
    conn.execute(
        "UPDATE assets SET "
        + ", ".join(f"{c}=(SELECT g.{c} FROM assets g JOIN temp.fold_pairs p "
                    f"ON p.old=g.id WHERE p.new=assets.id)"
                    for c in ("private_to", "private_was", "private_was_src"))
        + " WHERE private_to IS NULL AND id IN (SELECT p.new FROM temp.fold_pairs p "
          "JOIN assets g ON g.id=p.old WHERE g.private_to IS NOT NULL)")

    # Favourites and stars: the more of each, then the rest moved across.
    conn.execute(
        "UPDATE user_assets SET "
        "favorite=MAX(favorite, (SELECT o.favorite FROM user_assets o JOIN temp.fold_pairs p "
        "  ON p.old=o.asset_id WHERE p.new=user_assets.asset_id AND o.user_id=user_assets.user_id)), "
        "rating=MAX(rating, (SELECT o.rating FROM user_assets o JOIN temp.fold_pairs p "
        "  ON p.old=o.asset_id WHERE p.new=user_assets.asset_id AND o.user_id=user_assets.user_id)) "
        "WHERE EXISTS (SELECT 1 FROM user_assets o JOIN temp.fold_pairs p ON p.old=o.asset_id "
        "  WHERE p.new=user_assets.asset_id AND o.user_id=user_assets.user_id)")
    conn.execute(f"UPDATE OR IGNORE user_assets SET asset_id={twin.format(col='asset_id')} "
                 f"WHERE asset_id IN ({pairs})")

    _merge_faces(conn)

    # Everything else that points at a photograph: stories, album places and
    # covers, capsules, descriptions, vectors... Where the twin already has
    # its own (one vector per photograph), the twin's stays.
    for table, column in _asset_references(conn):
        conn.execute(
            f'UPDATE OR IGNORE "{table}" SET "{column}"={twin.format(col=column)} '
            f'WHERE "{column}" IN ({pairs})')
    # Links shared to one photograph name it without a foreign key.
    try:
        conn.execute(f"UPDATE shares SET target_id={twin.format(col='target_id')} "
                     f"WHERE scope='asset' AND target_id IN ({pairs})")
    except sqlite3.OperationalError:
        pass
    for table in ("pending_uploads", "phone_backups"):
        try:
            conn.execute(f"UPDATE {table} SET asset_id={twin.format(col='asset_id')} "
                         f"WHERE asset_id IN ({pairs})")
        except sqlite3.OperationalError:
            pass

    _carry_id_lists(conn)

    thumbs = [r[0] for r in conn.execute(
        "SELECT g.thumb FROM temp.fold_pairs p JOIN assets g ON g.id=p.old "
        "JOIN assets t ON t.id=p.new WHERE g.thumb IS NOT NULL AND g.thumb != '' "
        "AND g.thumb != COALESCE(t.thumb, '')")]
    conn.execute(f"DELETE FROM assets WHERE id IN ({pairs})")
    return thumbs


#: Tables that name photographs in a JSON list rather than by foreign key,
#: which the generic re-pointing cannot see: a film or book made from the rows
#: going would answer "can no longer be seen" for photographs still in the
#: library (audit D-07).
_ID_LISTS = (("memory_films", "item_ids"), ("photo_books", "item_ids"))


def _carry_id_lists(conn: sqlite3.Connection) -> None:
    """Re-point the photographs named in :data:`_ID_LISTS` at their twins."""
    pairs = dict(conn.execute(
        "SELECT old, new FROM temp.fold_pairs WHERE new IS NOT NULL").fetchall())
    for table, column in _ID_LISTS:
        try:
            rows = conn.execute(f'SELECT id, "{column}" FROM "{table}"').fetchall()
        except sqlite3.OperationalError:
            continue                      # an index from before that feature
        for row_id, raw in rows:
            try:
                ids = json.loads(raw or "[]")
            except (TypeError, ValueError):
                continue
            if not isinstance(ids, list) or not any(i in pairs for i in ids):
                continue
            seen: set[Any] = set()
            carried = []
            for i in ids:
                i = pairs.get(i, i)
                if i not in seen:
                    seen.add(i)
                    carried.append(i)
            conn.execute(f'UPDATE "{table}" SET "{column}"=? WHERE id=?',
                         (json.dumps(carried), row_id))


def _merge_faces(conn: sqlite3.Connection) -> None:
    """Faces found on both rows are the same faces, found twice. A twin with
    none takes the row's faces whole; otherwise each name given on the row
    going is put on the twin's face in the same place, and a named face with
    no counterpart there is moved across rather than lost."""
    conn.execute(
        "UPDATE faces SET asset_id=(SELECT new FROM temp.fold_pairs p WHERE p.old=faces.asset_id) "
        "WHERE asset_id IN (SELECT p.old FROM temp.fold_pairs p WHERE p.new IS NOT NULL "
        "  AND NOT EXISTS (SELECT 1 FROM faces t WHERE t.asset_id=p.new))")
    named = conn.execute(
        "SELECT f.id, f.person_id, f.source, f.confidence, f.bbox, p.new "
        "FROM faces f JOIN temp.fold_pairs p ON p.old=f.asset_id "
        "WHERE p.new IS NOT NULL AND (f.person_id IS NOT NULL OR EXISTS "
        "  (SELECT 1 FROM face_rejections r WHERE r.face_id=f.id))").fetchall()
    for face in named:
        try:
            box = json.loads(face[4])
        except (TypeError, ValueError):
            box = None
        best, best_iou = None, 0.5
        if box:
            for other in conn.execute(
                    "SELECT id, person_id, bbox, source FROM faces WHERE asset_id=?",
                    (face[5],)):
                try:
                    score = db._box_iou(box, json.loads(other[2]))
                except (TypeError, ValueError):
                    continue
                if score >= best_iou:
                    best, best_iou = other, score
        if best is None:
            if face[1] is not None:
                conn.execute("UPDATE faces SET asset_id=? WHERE id=?", (face[5], face[0]))
            continue
        # A name a person confirmed wins over one the matcher guessed on the
        # twin; two different confirmed names keep the twin's (audit D-06).
        if face[1] is not None and (best[1] is None or (
                face[2] == "confirmed" and best[3] != "confirmed")):
            conn.execute("UPDATE faces SET person_id=?, source=?, confidence=? WHERE id=?",
                         (face[1], face[2], face[3], best[0]))
        conn.execute("UPDATE OR IGNORE face_rejections SET face_id=? WHERE face_id=?",
                     (best[0], face[0]))
        conn.execute("UPDATE people_clusters SET cover_face_id=? WHERE cover_face_id=?",
                     (best[0], face[0]))
