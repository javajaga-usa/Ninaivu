"""The library's totals, facets, timeline and years. Part of :mod:`.db`, which
re-exports every name.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Sequence

from . import db as _db  # noqa: I001
from .db import (  # noqa: F401
    EMBEDDINGS_GENERATION_KEY,
    TAGS_GENERATION_KEY,
    cached_aggregate,
    private_count,
    roots_clause,
    row_to_dict,
    scope_clause,
    visibility_clause,
)


def library_stats(conn: sqlite3.Connection, roots: Sequence[str] | str | None = None, *,
                  viewer_id: int = 0, max_visibility: int = 1,
                  scope: str | None = None, gate: Any = None) -> dict[str, Any]:
    """Counts for the sidebar, limited to what this viewer may see: *gate*'s
    (a ``server.visibility.Viewer``) when it is given."""
    if gate is not None:
        viewer_id, max_visibility = gate.viewer_id, gate.max_visibility
        gate_sql, gate_params = gate.sql("a")
        where = [gate_sql, "a.trashed = 0"]
        params: list[Any] = list(gate_params)
        # The caller's own "Only to me" count is by folder and sub-folder.
        roots, scope = gate.roots, gate.scope
        roots_sql, roots_params = roots_clause("a", roots)
        if int(max_visibility) < 2:
            where.append("a.nsfw = 0")
    else:
        # One alias (`a`) everywhere, so the scope clause can be reused verbatim.
        roots_sql, roots_params = roots_clause("a", roots if roots is not None else [])
        where = [roots_sql, "a.trashed = 0", visibility_clause("a", max_visibility)]
        params = [*roots_params, int(max_visibility)]
        # Flagged content is admin-only in every listing (query_assets, and the
        # API's per-item guard); counting it for anybody else said how much of it
        # there was, and in which years and folders.
        if int(max_visibility) < 2:
            where.append("a.nsfw = 0")
        scope_sql, scope_params = scope_clause("a", scope)
        if scope_sql:
            where.append(scope_sql)
            params.extend(scope_params)
    where_sql = " AND ".join(where)

    def whole_library() -> dict[str, Any]:
        row = conn.execute(
            # A live photo counts once, as the photograph it is. Its clip is the
            # other half of that same thing, not a video of its own — counting it
            # again put 5,674 three-second fragments into the video total on the
            # library this was found on, twenty-eight per cent of it.
            # A RAW shot with a JPEG is the same photograph again, so it is
            # not counted either; its bytes are, because they are on disk.
            f"SELECT COALESCE(SUM(a.live_clip=0 AND a.raw_twin=0), 0) n, "
            f"COALESCE(SUM(a.size),0) bytes, "
            f"SUM(a.kind='picture' AND a.raw_twin=0) pictures, "
            f"SUM(a.kind='video' AND a.live_clip=0) videos, "
            f"SUM(a.is_live=1) live, "
            f"SUM(a.kind='audio') audio, SUM(a.nsfw=1) nsfw, "
            f"SUM(a.visibility=0) public, SUM(a.visibility=2) hidden, "
            f"MIN(a.date_key) first_date, MAX(a.date_key) last_date "
            f"FROM assets a WHERE {where_sql}",
            params,
        ).fetchone()
        dupes = conn.execute(
            f"SELECT COUNT(DISTINCT a.dup_group) n FROM assets a "
            f"WHERE {where_sql} AND a.dup_group IS NOT NULL "
            f"AND a.live_clip=0 AND a.raw_twin=0", params,
        ).fetchone()["n"]
        return {**dict(row), "dupes": dupes}

    # The same for everybody with this viewer's limits, and unchanged until the
    # library is: remembered. The two below are not — one is this viewer's
    # own, the other is written by the tagger, not by a scan.
    row = cached_aggregate(conn, ("library_stats", where_sql, tuple(params)),
                           whole_library)
    dupes = row["dupes"]

    # Written by the tagger, which the library's number does not follow, so
    # kept until the analysed items' own number moves. Counted within what
    # this viewer may see, like every other number here: it was counted by
    # folder alone, so a member was told how many items were analysed among
    # the private, hidden and flagged ones too (decided 9 November 2026).
    embedded = cached_aggregate(
        conn, ("embedded", where_sql, tuple(params)),
        lambda: conn.execute(
            f"SELECT COUNT(*) n FROM embeddings e JOIN assets a ON a.id = e.asset_id "
            f"WHERE {where_sql}", params,
        ).fetchone()["n"], also=(EMBEDDINGS_GENERATION_KEY,))

    # The family app's Documents view, for an administrator: the family is
    # shown none of these (the hiding rule), so they are not counted for
    # anybody else. Remembered with the rest until the library changes.
    documents = 0
    if int(max_visibility) >= 2:
        screens_sql, screens_params = _db.screen_clause(conn, "a")
        documents = cached_aggregate(
            conn, ("documents", where_sql, tuple(params), screens_sql, tuple(screens_params)),
            lambda: conn.execute(
                f"SELECT COUNT(*) n FROM assets a WHERE {where_sql} AND {screens_sql} "
                f"AND a.live_clip = 0 AND a.raw_twin = 0", [*params, *screens_params],
            ).fetchone()["n"],
            # The screen check's scores are written by the tagger as well.
            max_age=600)

    # From this person's favourites outward, not from the library inward.
    # SQLite chose to walk every visible item and look each one up among the
    # favourites — sixty milliseconds on 100,000 items for a count that is
    # usually a few hundred. CROSS JOIN is SQLite's way of fixing the order.
    favorites = conn.execute(
        f"SELECT COUNT(*) n FROM user_assets ua "
        f"CROSS JOIN assets a ON a.id = ua.asset_id "
        f"WHERE ua.user_id = ? AND ua.favorite = 1 AND {where_sql}",
        (int(viewer_id), *params),
    ).fetchone()["n"]

    return {
        "count": row["n"] or 0,
        "bytes": row["bytes"] or 0,
        "pictures": row["pictures"] or 0,
        "videos": row["videos"] or 0,
        #: Photographs that came with a clip. Not added to the totals — each
        #: one is already counted among the pictures — it is how many of them
        #: move when you hold them.
        "live": row["live"] or 0,
        "audio": row["audio"] or 0,
        "favorites": favorites or 0,
        "nsfw": row["nsfw"] or 0,
        "public": row["public"] or 0,
        "hidden": row["hidden"] or 0,
        "duplicate_groups": dupes or 0,
        "documents": documents or 0,
        "embedded": embedded or 0,
        # This viewer's own "Only to me" items. Nobody else's: the number is
        # theirs alone, as their favourites are.
        "private": private_count(conn, roots, viewer_id, scope),
        "first_date": row["first_date"] or "",
        "last_date": row["last_date"] or "",
    }


def facets(conn: sqlite3.Connection, roots: Sequence[str] | str | None = None,
           limit: int = 40, *, max_visibility: int = 1, scope: str | None = None,
           gate: Any = None) -> dict[str, Any]:
    """Tag cloud, folder list, camera list and per-year counts.

    Everything here leaks information about the library, so it is filtered by
    the same visibility and scope rules as the gallery itself: those of
    *gate* (a ``server.visibility.Viewer``) when it is given.
    """
    if gate is not None:
        gate_sql, base = gate.sql("")
        max_visibility = gate.max_visibility
        guard = gate_sql + " AND trashed=0 AND live_clip=0 AND raw_twin=0"
        if int(max_visibility) < 2:
            guard += " AND nsfw=0"
    else:
        roots_sql, roots_params = roots_clause("assets", roots if roots is not None else [])
        scope_sql, scope_params = scope_clause("assets", scope)
        guard = (roots_sql.replace("assets.", "") + " AND trashed=0 AND "
                 + "live_clip=0 AND raw_twin=0 AND "
                 + visibility_clause("", max_visibility))
        # As in library_stats: below an admin, flagged content is not counted
        # in the folder, camera and year lists either — only the tags left it out.
        if int(max_visibility) < 2:
            guard += " AND nsfw=0"
        if scope_sql:
            guard += " AND " + scope_sql.replace("assets.", "")
        base = [*roots_params, int(max_visibility), *scope_params]

    # The tag cloud reads tags, which have a counter of their own. Worked out
    # once at the longest length anybody asks for and cut to each caller's:
    # the sidebar's forty and the search box's two hundred were two reads of
    # every row's tags, and the first letter typed after a start (or after
    # the tagger moved anything) waited 1.2 s on 208,000 items for the second.
    kept = max(int(limit), FACETS_KEPT)
    found = cached_aggregate(conn, ("facets", guard, tuple(base), kept),
                             lambda: _db._facets(conn, guard, base, kept),
                             also=(TAGS_GENERATION_KEY,))
    if kept == int(limit):
        return found
    return {key: (value if key == "years" else value[:max(0, int(limit))])
            for key, value in found.items()}


#: The most facets of each kind anybody asks for (/api/suggest).
FACETS_KEPT = 200


def _facets(conn: sqlite3.Connection, guard: str, base: list[Any],
            limit: int) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for (raw,) in conn.execute(
        f"SELECT tags FROM assets WHERE {guard} AND nsfw=0 AND tags != '[]'", base
    ):
        try:
            for tag in json.loads(raw or "[]"):
                counts[tag] = counts.get(tag, 0) + 1
        except (TypeError, ValueError):
            continue
    top_tags = sorted(counts.items(), key=lambda kv: -kv[1])[:limit]

    # Folders, cameras and years in one pass over the library rather than
    # three: grouped by all three at once, then summed each way here. The
    # groups number in the thousands where the rows number in the hundreds
    # of thousands, and it was the three reads of every row that cost.
    folders: dict[str, int] = {}
    cameras: dict[str, int] = {}
    years: dict[str, int] = {}
    for folder, camera, year, n in conn.execute(
        f"SELECT folder, camera, substr(date_key,1,4) y, COUNT(*) n FROM assets "
        f"WHERE {guard} GROUP BY folder, camera, y", base,
    ):
        folders[folder] = folders.get(folder, 0) + n
        if camera:
            cameras[camera] = cameras.get(camera, 0) + n
        if year:
            years[year] = years.get(year, 0) + n

    def top(counted: dict[str, int]) -> list[tuple[str, int]]:
        return sorted(counted.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]

    return {
        "tags": [{"name": t, "count": c} for t, c in top_tags],
        "folders": [{"name": f, "count": n} for f, n in top(folders)],
        "cameras": [{"name": c, "count": n} for c, n in top(cameras)],
        "years": [{"year": y, "count": n}
                  for y, n in sorted(years.items(), reverse=True)],
    }


def timeline(conn: sqlite3.Connection, roots: Sequence[str] | str | None = None, *,
             max_visibility: int = 1, scope: str | None = None,
             gate: Any = None) -> list[dict[str, Any]]:
    """How many items each day holds, for the caller's limits: *gate*'s (a
    ``server.visibility.Viewer``) when it is given."""
    if gate is not None:
        gate_sql, gate_params = gate.sql("")
        guard = (gate_sql + " AND trashed=0 AND nsfw=0 AND live_clip=0 AND raw_twin=0"
                 " AND date_key != ''")
        params = tuple(gate_params)
    else:
        roots_sql, roots_params = roots_clause("assets", roots if roots is not None else [])
        scope_sql, scope_params = scope_clause("assets", scope)
        guard = (roots_sql.replace("assets.", "")
                 + " AND trashed=0 AND nsfw=0 AND live_clip=0 AND raw_twin=0 AND "
                 + visibility_clause("", max_visibility) + " AND date_key != ''")
        if scope_sql:
            guard += " AND " + scope_sql.replace("assets.", "")
        params = (*roots_params, int(max_visibility), *scope_params)

    def days() -> list[dict[str, Any]]:
        rows = conn.execute(
            f"SELECT date_key, COUNT(*) n FROM assets WHERE {guard} "
            f"GROUP BY date_key ORDER BY date_key DESC", params,
        ).fetchall()
        return [{"date": r["date_key"], "count": r["n"]} for r in rows]

    return cached_aggregate(conn, ("timeline", guard, params), days)


def picture_years(conn: sqlite3.Connection, roots: Sequence[str] | str | None = None, *,
                  max_visibility: int = 1,
                  scope: str | None = None, gate: Any = None) -> list[dict[str, Any]]:
    """How many photographs this viewer can see in each year, newest first.

    The same limits as :func:`timeline`, for photographs only: what the
    ready-made books (features/books/routes.py) offer a "best of the year" for.
    *gate*, a ``server.visibility.Viewer``, is the caller's limits when given.
    """
    if gate is not None:
        gate_sql, gate_params = gate.sql("")
        guard = (gate_sql + " AND trashed=0 AND nsfw=0 AND live_clip=0 AND raw_twin=0"
                 " AND kind='picture' AND date_key != ''")
        params = tuple(gate_params)
    else:
        roots_sql, roots_params = roots_clause("assets", roots if roots is not None else [])
        scope_sql, scope_params = scope_clause("assets", scope)
        guard = (roots_sql.replace("assets.", "")
                 + " AND trashed=0 AND nsfw=0 AND live_clip=0 AND raw_twin=0 AND "
                 + "kind='picture' AND "
                 + visibility_clause("", max_visibility) + " AND date_key != ''")
        if scope_sql:
            guard += " AND " + scope_sql.replace("assets.", "")
        params = (*roots_params, int(max_visibility), *scope_params)

    def years() -> list[dict[str, Any]]:
        rows = conn.execute(
            f"SELECT substr(date_key, 1, 4) y, COUNT(*) n FROM assets WHERE {guard} "
            f"GROUP BY y ORDER BY y DESC", params,
        ).fetchall()
        return [{"year": int(r["y"]), "count": r["n"]} for r in rows
                if str(r["y"]).isdigit()]

    return cached_aggregate(conn, ("picture_years", guard, params), years)
