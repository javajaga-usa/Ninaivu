"""Writes to ``assets``: upsert, update, delete-missing, and the scan runs.
Part of :mod:`.db`, which re-exports every name.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any, Iterable, Sequence

from . import roots as roots_kit
from .db import AI_MAY_READ, _tables, _write_lock  # noqa: F401

log = logging.getLogger(__name__)

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
        "SELECT id, root, rel_path, size, thumb FROM assets WHERE root=?" + within,
        (root, *params)).fetchall()
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

    # Gone from the disk and forgotten here: recorded as removed on purpose,
    # as emptying the bin records it, so the Secondary's bring-in does not
    # bring back a photograph somebody removed in Finder (storage/on_purpose.py).
    from .on_purpose import record_erased                 # noqa: PLC0415
    with _write_lock:
        record_erased(conn, stale)
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
        # And the second copy's, which keeps the file where it was on its disk.
        from . import mirror                                   # noqa: PLC0415
        mirror.note_move(conn, root, old_rel, new_rel)
        conn.commit()
    return gone


#: How long the total of a word search is kept (query_assets).
WORDS_TOTAL_SECONDS = 30.0


