"""Occasions (the albums made by themselves), albums and duplicate groups:
the index's side of them. Part of :mod:`.db`, which re-exports every name.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Sequence

from . import db as _db
from .db import (  # noqa: F401
    ALBUMS_GENERATION_KEY,
    OCCASIONS_GENERATION_KEY,
    cached_aggregate,
    roots_clause,
    row_to_dict,
    scope_clause,
    visibility_clause,
    visible_to,
)


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
    with _db._write_lock:
        existing: dict[str, int] = {}
        stored: dict[int, tuple] = {}
        for r in conn.execute(
                "SELECT id, key, title, place, started_at, ended_at, days, count "
                "FROM occasions WHERE root=?", (root,)):
            existing[r["key"]] = int(r["id"])
            stored[int(r["id"])] = (r["title"], r["place"], r["started_at"],
                                    r["ended_at"], r["days"], r["count"])
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
                # Most occasions are as they were; writing each back anyway
                # was twenty thousand statements under the write lock.
                if stored.get(occasion_id) != fields:
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


def occasion_inputs(conn: sqlite3.Connection, root: str) -> str:
    """A short fingerprint of everything :func:`rebuild_occasions` reads.

    Which photographs are grouped (not in the bin, not flagged, not hidden),
    when each was taken, where, and its town. Reading it is one pass in
    SQLite with no rows handed to Python: about a tenth of a rebuild on a
    200,000-photograph library. The sum is weighted by id, so two
    photographs swapping their times still changes it.
    """
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(MAX(id), 0), COALESCE(SUM(("
        " (id * 2654435761"
        "  + CAST(COALESCE(captured_at, mtime, 0) * 1000 AS INTEGER) * 31"
        "  + CAST(COALESCE(gps_lat, 0) * 1000000 AS INTEGER) * 10007"
        "  + CAST(COALESCE(gps_lon, 0) * 1000000 AS INTEGER) * 7919"
        "  + COALESCE(length(city) * 65599 + unicode(city) * 131"
        "             + unicode(substr(city, -1)), 0)"
        " ) % 2147483647) * ((id % 97) + 1)), 0) "
        "FROM assets WHERE root=? AND trashed=0 AND nsfw=0 AND visibility < 2",
        (root,)).fetchone()
    return f"{row[0]}:{row[1]}:{row[2]}"


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
    params = (*roots_params, int(max_visibility), *scope_params, int(limit))
    def grouped() -> list[dict[str, Any]]:
        return [dict(r) for r in conn.execute(
            f"SELECT o.id, o.key, o.title, o.place, o.started_at, o.ended_at, "
            # The cover is the newest item that has a thumbnail. Plain MAX(id)
            # picked an MP3 for an administrator's occasion, which has no
            # picture and was a broken image asked for on every visit.
            f"o.days, COUNT(a.id) AS visible, "
            f"MAX(CASE WHEN a.thumb IS NOT NULL THEN a.id END) AS cover_id "
            f"FROM occasions o JOIN assets a ON a.occasion_id = o.id "
            f"WHERE {guard} GROUP BY o.id "
            f"ORDER BY o.started_at DESC LIMIT ?", params)]

    # Read on every visit to the gallery, and a group over every item in an
    # occasion: remembered until the occasions or what this viewer may see
    # change.
    rows = cached_aggregate(conn, ("occasions", guard, params), grouped,
                            also=(OCCASIONS_GENERATION_KEY,))

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
    with _db._write_lock:
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
    # Counted as the album lists its contents: a pair's other half is not
    # listed there, so counted it promised photographs the album never showed.
    where = [roots_sql, visibility_clause("a", max_visibility), "a.trashed = 0", "a.nsfw = 0",
             "a.live_clip = 0", "a.raw_twin = 0"]
    visible = list(params) + [int(max_visibility)]
    if scope_sql:
        where.append(scope_sql)
        visible += scope_params
    seen = " AND ".join(w for w in where if w)

    # Two grouped passes over the album items for every album at once: what
    # the viewer sees of each (count, newest addition for a cover, earliest
    # date) and each album's whole date (the figure :func:`album_date`
    # gives). Four correlated subqueries per album took over half a second
    # once one album held fifty thousand photographs, on every album list.
    # The two passes still read every item of every album, 0.45 s on 208,000
    # photographs with 100,000 in albums, so they are remembered until a
    # photograph joins or leaves an album or the library changes.
    def passes() -> tuple[dict[int, tuple], dict[int, Any]]:
        shown = {int(r["album_id"]): (r["n"], r["shown_date"], r["cover"])
                 for r in conn.execute(
                     f"SELECT ai.album_id, COUNT(*) AS n, "
                     f"MIN(NULLIF(a.date_key, '')) AS shown_date, "
                     f"ai.asset_id AS cover, MAX(ai.added_at) AS newest "
                     f"FROM album_items ai JOIN assets a ON a.id = ai.asset_id "
                     f"WHERE {seen} GROUP BY ai.album_id", visible)}
        whole = {int(r[0]): r[1] for r in conn.execute(
            "SELECT ai.album_id, MIN(NULLIF(a.date_key, '')) FROM album_items ai "
            "JOIN assets a ON a.id = ai.asset_id WHERE a.trashed = 0 GROUP BY ai.album_id")}
        return shown, whole

    shown, whole = cached_aggregate(conn, ("albums", seen, tuple(visible)), passes,
                                    also=(ALBUMS_GENERATION_KEY,), shared=True)
    rows = []
    for album in conn.execute("SELECT id, name, created_at, cover_id, created_by "
                              "FROM albums ORDER BY name COLLATE NOCASE"):
        d = dict(album)
        mine = shown.get(int(d["id"]))
        d["n"] = int(mine[0]) if mine else 0
        # SQLite gives the bare column from the row MAX() chose: the newest.
        d["effective_cover_id"] = mine[2] if mine else None
        d["date_key"] = whole.get(int(d["id"])) or ""
        d["shown_date"] = (mine[1] if mine else None) or ""
        rows.append(d)
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
        # Who may open the album is decided by its whole date, as before; the
        # date shown is the earliest of what this viewer can see. Shown whole,
        # it told a family member the date of a photograph only an
        # administrator may see.
        if not date_policy.allows(d):
            continue
        d["date_key"] = d.pop("shown_date")
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
    with _db._write_lock:
        conn.executemany(
            "INSERT OR IGNORE INTO album_items(album_id, asset_id, added_at) "
            "VALUES(?,?,?)",
            [(album_id, a, now) for a in asset_ids],
        )
        conn.commit()


def album_remove(conn: sqlite3.Connection, album_id: int, asset_ids: Sequence[int]) -> None:
    with _db._write_lock:
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
    with _db._write_lock:
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


def earliest_date(conn: sqlite3.Connection, asset_ids: Sequence[int]) -> str:
    """The earliest date among these items, or ''. For an album as one viewer
    sees it: :func:`album_date` counts everything in it, hidden or not."""
    ids = [int(i) for i in asset_ids]
    found: list[str] = []
    for start in range(0, len(ids), 500):
        piece = ids[start:start + 500]
        row = conn.execute(
            "SELECT MIN(NULLIF(date_key, '')) FROM assets WHERE id IN (%s)"
            % ",".join("?" * len(piece)), piece).fetchone()
        if row and row[0]:
            found.append(row[0])
    return min(found) if found else ""


def get_album(conn: sqlite3.Connection, album_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id, name, created_at, cover_id, created_by FROM albums WHERE id=?",
        (album_id,)
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["date_key"] = _db.album_date(conn, album_id)
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
    with _db._write_lock:
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
             + " AND a.dup_group IS NOT NULL"
             # Never a pair's other half: see Scanner._link_duplicates.
             + " AND a.live_clip=0 AND a.raw_twin=0")
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
