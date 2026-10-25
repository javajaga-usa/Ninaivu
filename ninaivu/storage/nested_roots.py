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
from pathlib import Path
from typing import Any

from . import ask_family, db

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


class HeldOnTheSecondary(ValueError):
    """Originals of the folder going are kept only on the Secondary."""


def let_go_in(conn: sqlite3.Connection, root: str) -> int:
    """How many originals of *root* were let go of to free space, kept only
    on the Secondary of two computers (mugil/free.py)."""
    try:
        return int(conn.execute("SELECT COUNT(*) FROM mugil_let_go WHERE root=?",
                                (root,)).fetchone()[0])
    except sqlite3.OperationalError:
        return 0                          # an index made before the table was


def bring_back_first(cfg, conn: sqlite3.Connection, root: str) -> int:  # noqa: ANN001
    """Bring back to *root* the originals it let go of, before it is folded
    into another library folder; how many are still only on the Secondary.

    A let-go original is sent for by the library folder the Secondary keeps
    it under and its place there, and a fold moves the row to the other
    folder's name and place: folded while let go, it could never be sent for
    again, and the next scan found it missing and pruned its row."""
    if not let_go_in(conn, root):
        return 0
    from ..mugil import free                                 # noqa: PLC0415
    for (rel,) in conn.execute("SELECT rel_path FROM mugil_let_go WHERE root=?",
                               (root,)).fetchall():
        try:
            free.bring_back(cfg, conn, root, rel)
        except Exception:                                    # noqa: BLE001 - counted below
            log.warning("could not bring %s back to %s before folding it", rel, root,
                        exc_info=True)
    return let_go_in(conn, root)


def fold(conn: sqlite3.Connection, going: str, kept: str) -> dict[str, int]:
    """Move the rows of library folder *going* that *kept* also covers onto
    *kept*, keeping everything people did with them.

    One of the two must be inside the other. Rows of *going* outside *kept*
    are left alone, for the caller to deal with as for any folder removed.
    Returns ``{"merged": rows folded into a twin, "moved": rows re-keyed,
    "thumbs": thumbnail names nothing uses any more, for the caller to
    remove, "renamed": (asset id, old name, new name) of the thumbnails of
    rows re-keyed, for the caller to rename (reroot.rename_thumbs)}``.
    """
    below = inside(kept, going)
    above = inside(going, kept)
    if below is None and above is None:
        raise ValueError(f"{going} and {kept} are not one inside the other")
    if let_go_in(conn, going):
        # bring_back_first says why; the callers do it before anything changes.
        raise HeldOnTheSecondary(f"originals of {going} are kept only on the Secondary")

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
        renamed: list[tuple[int, str, str]] = []
        try:
            # Before the twins are merged: the rows going name the files.
            _carry_keyed_records(conn, going, kept, (covered, covered_args),
                                 (new_rel, new_rel_args))
            if merged:
                thumbs = _merge_twins(conn)
            renamed = _rename_thumbs(conn, kept)
            # No twin under *kept* yet: the same row, known by its place there.
            conn.execute(
                "UPDATE assets SET root=?, "
                "rel_path=(SELECT rel FROM temp.fold_pairs p WHERE p.old=assets.id), "
                "folder=(SELECT folder FROM temp.fold_pairs p WHERE p.old=assets.id) "
                "WHERE id IN (SELECT old FROM temp.fold_pairs WHERE new IS NULL)",
                (kept,))
            _carry_folder_rules(conn, going, kept, below, above)
            _carry_bin_and_uploads(conn, going, kept, below, above)
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
    return {"merged": int(merged), "moved": int(moved), "thumbs": thumbs,
            "renamed": renamed}


#: Tables keyed by (root, rel_path) that say something about a file: its
#: second copy, its off-site copy, that it was erased from the bin, an
#: administrator's decision on uploading it (storage/reroot.py has the same
#: list). Left on the folder going, the second copy and the off-site copy
#: copied the files moved again, and a restore from the second copy could
#: bring back a photograph somebody had erased (audit D-24).
_KEYED = ("mirror_copies", "offsite_copies", "erased", "cloud_approvals", "mugil_let_go")


def _carry_keyed_records(conn: sqlite3.Connection, going: str, kept: str,
                         covered: tuple[str, tuple[Any, ...]],
                         new_rel: tuple[str, tuple[Any, ...]]) -> None:
    """Re-key :data:`_KEYED`'s rows of *going* that *kept* covers (*covered*
    and *new_rel* are :func:`fold`'s SQL for which and where). Where *kept*
    has its own record for the file, its own stays. A second-copy record
    keeps the place its file is on the second disk (``stored_as``), under
    *going*'s name there."""
    from .reroot import KEYED_ROOT_TABLES                # noqa: PLC0415
    assert set(_KEYED) == set(KEYED_ROOT_TABLES), "keep the two lists together"
    where, where_args = covered
    rel, rel_args = new_rel
    tables = db._tables(conn)                               # noqa: SLF001
    for table in _KEYED:
        if table not in tables:
            continue
        if table == "mirror_copies":
            conn.execute(
                f"UPDATE OR IGNORE mirror_copies SET root=?, rel_path={rel}, "
                "stored_as=CASE WHEN stored_as='' THEN ? || rel_path ELSE stored_as END "
                f"WHERE root=? AND {where}",
                (kept, *rel_args, _mirror_folder(conn, going) + "/", going, *where_args))
        else:
            conn.execute(f"UPDATE OR IGNORE {table} SET root=?, rel_path={rel} "
                         f"WHERE root=? AND {where}", (kept, *rel_args, going, *where_args))
        conn.execute(f"DELETE FROM {table} WHERE root=? AND {where}", (going, *where_args))


def _mirror_folder(conn: sqlite3.Connection, root: str) -> str:
    """The folder the second copy keeps *root* under (storage/mirror.py's
    ``_library_folder``), from the library folders it has copied."""
    import hashlib                                           # noqa: PLC0415
    name = Path(root).name or "library"
    copied = {r[0] for r in conn.execute("SELECT DISTINCT root FROM mirror_copies")} | {root}
    if len([r for r in copied if (Path(r).name or "library") == name]) > 1:
        name += "-" + hashlib.sha1(str(root).encode()).hexdigest()[:6]  # noqa: S324
    return name


def _rename_thumbs(conn: sqlite3.Connection, kept: str) -> list[tuple[int, str, str]]:
    """The thumbnail names of the rows about to be re-keyed onto *kept*, as
    *kept* names them. A thumbnail is named after its folder and path, so
    each was made again by the next scan (audit D-24); the files are renamed
    by the caller instead, as moving a library does (reroot.rename_thumbs)."""
    from ..media.media import thumb_name                    # noqa: PLC0415
    moves = []
    for asset_id, rel, old in conn.execute(
            "SELECT p.old, p.rel, a.thumb FROM temp.fold_pairs p JOIN assets a ON a.id=p.old "
            "WHERE p.new IS NULL AND COALESCE(a.thumb, '') <> ''").fetchall():
        new = thumb_name(kept, rel)
        if new != old:
            moves.append((int(asset_id), str(old), new))
    conn.executemany("UPDATE assets SET thumb=? WHERE id=?",
                     [(new, asset_id) for asset_id, _, new in moves])
    return moves


def _under_kept(path: str, below: str | None, above: str | None) -> str | None:
    """*path*, relative to *going*, as it is spelled under *kept*; None when
    it is a part of *going* outside *kept*."""
    path = str(path or "").strip("/")
    if above is not None:
        return f"{above}/{path}" if path else above
    if path == below:
        return ""
    if path.startswith(below + "/"):
        return path[len(below) + 1:]
    return None


def _carry_bin_and_uploads(conn: sqlite3.Connection, going: str, kept: str,
                           below: str | None, above: str | None) -> None:
    """Re-key *going*'s bin entries and waiting uploads that *kept* covers.

    Left on *going* they name a library nobody lists: restoring one put its
    row (story and names too) under that folder and then walked all of it
    again, and an upload could no longer be approved (audit D-09). A bin
    entry's file stays where it is, in the bin it was put in
    (``recycle.in_its_bin``). An upload meant for the whole of an outer
    folder going is filed under *kept* instead.
    """
    try:
        entries = conn.execute(
            "SELECT id, rel_path FROM recycled WHERE root=?", (going,)).fetchall()
    except sqlite3.OperationalError:
        entries = []
    for entry_id, rel in entries:
        new_rel = _under_kept(rel, below, above)
        if new_rel:
            conn.execute("UPDATE recycled SET root=?, rel_path=? WHERE id=?",
                         (kept, new_rel, entry_id))
    try:
        uploads = conn.execute(
            "SELECT id, scope, record FROM pending_uploads WHERE root=? AND status='pending'",
            (going,)).fetchall()
    except sqlite3.OperationalError:
        uploads = []
    for upload_id, scope, raw in uploads:
        scope = str(scope or "").strip("/")
        new_scope = "" if (below is not None and not scope) else _under_kept(scope, below, above)
        if new_scope is None:
            continue                      # meant for a part of *going* outside *kept*
        try:
            record = json.loads(raw or "{}")
        except (TypeError, ValueError):
            continue
        place = record.get("_place") if isinstance(record, dict) else None
        if place is not None:
            new_place = _under_kept(place, below, above)
            if new_place is None:
                continue
            record["_place"] = new_place
        conn.execute("UPDATE pending_uploads SET root=?, scope=?, record=? WHERE id=?",
                     (kept, new_scope, json.dumps(record), upload_id))


def tidy_thumbnails(cfg: Any, folded: dict[str, Any]) -> None:
    """After :func:`fold`: remove the thumbnails nothing uses any more, and
    rename those of the rows re-keyed to the names their new folder gives
    them. A thumbnail that cannot be renamed is made again by the next scan."""
    from ..media import media                                # noqa: PLC0415
    from .reroot import rename_thumbs                        # noqa: PLC0415
    for base in folded.get("thumbs") or ():
        media.remove_thumbnails(cfg.thumbs_dir, base, cfg.thumb_sizes, cfg.thumb_format)
    if folded.get("renamed"):
        try:
            rename_thumbs(Path(cfg.thumbs_dir), folded["renamed"], dry_run=False)
        except OSError:
            log.warning("could not rename the thumbnails of the photographs moved "
                        "into another library folder; the next scan makes them again",
                        exc_info=True)


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

    # Two captions both typed by hand and different: both kept, the twin's
    # first, rather than the one going dropped without a word (D-06).
    joined = conn.execute(
        "UPDATE assets SET caption = caption || ' · ' || (SELECT g.caption FROM assets g "
        "JOIN temp.fold_pairs p ON p.old=g.id WHERE p.new=assets.id) "
        "WHERE caption_source='manual' AND COALESCE(caption, '') != '' AND id IN ("
        "SELECT p.new FROM temp.fold_pairs p JOIN assets g ON g.id=p.old "
        "WHERE g.caption_source='manual' AND COALESCE(g.caption, '') != '' "
        "AND g.caption != assets.caption)").rowcount
    if joined:
        log.info("fold: kept both hand-typed captions on %d photograph(s)", joined)
    for columns, source in (m for m in _MANUAL if m[1] != "caption_source"):
        # The rest cannot be joined: the twin's stands, and it is said.
        kept = conn.execute(
            f"SELECT COUNT(*) FROM assets k JOIN temp.fold_pairs p ON p.new=k.id "
            f"JOIN assets g ON g.id=p.old WHERE k.{source}='manual' AND g.{source}='manual' "
            + "AND (" + " OR ".join(f"k.{c} IS NOT g.{c}" for c in columns) + ")"
        ).fetchone()[0]
        if kept:
            log.info("fold: %d photograph(s) had %s set by hand in both folders; "
                     "the one in the folder kept stands", kept, columns[0])

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


#: SQL: whether face ``f`` has a "who is this?" question or answer on it.
_ASKED = ("f.id IN (SELECT face_id FROM ask_faces) "
          "OR f.id IN (SELECT face_id FROM ask_answers)")


def _asked_about(conn: sqlite3.Connection, face_id: int) -> bool:
    try:
        return conn.execute("SELECT 1 FROM ask_faces WHERE face_id=? UNION ALL "
                            "SELECT 1 FROM ask_answers WHERE face_id=? LIMIT 1",
                            (face_id, face_id)).fetchone() is not None
    except sqlite3.OperationalError:
        return False


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
        "  (SELECT 1 FROM face_rejections r WHERE r.face_id=f.id)"
        f"  OR {_ASKED})").fetchall()
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
            if face[1] is not None or _asked_about(conn, face[0]):
                conn.execute("UPDATE faces SET asset_id=? WHERE id=?", (face[5], face[0]))
            continue
        # A relative's answer waiting for a review goes with it (audit D-10).
        ask_family.move_face(conn, face[0], best[0])
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
