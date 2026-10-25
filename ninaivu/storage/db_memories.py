"""On this day, the one photograph worth sending, and the map's points.
Part of :mod:`.db`, which re-exports every name.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Sequence

from .db import (  # noqa: F401
    roots_clause,
    row_to_dict,
    scope_clause,
    visibility_clause,
)


# ---------------------------------------------------------------------------
# Memories & Geo Explorer
# ---------------------------------------------------------------------------

#: "Taken on this month and day", for :func:`query_on_this_day` and
#: :func:`candidates_for_the_day`. ``date_key`` is the local day the scanner
#: settled on, and it decides whenever it is set. The timestamp is consulted
#: only for a row with no ``date_key``, only when it is a real capture time
#: (a dead clock is 0, which reads as 1 January 1970), and in local time:
#: ``captured_at`` is an instant made from the camera's local wall clock, so
#: read in UTC a photo taken at 2 a.m. in India was a memory of the day before
#: as well. ``mtime`` is never a capture date (scanner.capture_date).
_ON_DAY_SQL = ("(a.date_key LIKE ? OR (a.date_key = '' AND a.captured_at > 0 "
               "AND strftime('%m-%d', a.captured_at, 'unixepoch', 'localtime') = ?))")

#: The year a row was taken, by the same rules. ``SUBSTR('', 1, 4)`` is '' and
#: not NULL, so an undated row's year was '' and passed "before this year".
_YEAR_SQL = ("COALESCE(NULLIF(SUBSTR(a.date_key, 1, 4), ''), "
             "strftime('%Y', a.captured_at, 'unixepoch', 'localtime'))")


def query_on_this_day(
    conn: sqlite3.Connection,
    roots: Sequence[str] | str,
    *,
    month: int,
    day: int,
    viewer_id: int = 0,
    max_visibility: int = 1,
    scope: str | None = None,
    limit: int = 150,
) -> dict[str, Any]:
    """Return media items captured on this month and day across historical years."""
    roots_sql, roots_params = roots_clause("a", roots)
    where = [roots_sql, "a.trashed = 0", "a.nsfw = 0", visibility_clause("a", max_visibility),
             # A pair's other half is the same moment again: shown once.
             "a.live_clip = 0 AND a.raw_twin = 0"]
    params: list[Any] = [*roots_params, int(max_visibility)]

    scope_sql, scope_params = scope_clause("a", scope)
    if scope_sql:
        where.append(scope_sql)
        params.extend(scope_params)

    m_str = f"{month:02d}"
    d_str = f"{day:02d}"
    target_md = f"-{m_str}-{d_str}"

    where.append(_ON_DAY_SQL)
    params.extend([f"%{target_md}", f"{m_str}-{d_str}"])

    mine = "LEFT JOIN user_assets ua ON ua.asset_id = a.id AND ua.user_id = ?"
    join_params = [int(viewer_id)]
    where_sql = " AND ".join(where)

    projection = ("a.*, COALESCE(ua.favorite, 0) AS mine_favorite, "
                  "COALESCE(ua.rating, 0) AS mine_rating, "
                  + _YEAR_SQL + " AS year_str")

    rows = conn.execute(
        f"SELECT {projection} FROM assets a {mine} WHERE {where_sql} "
        f"ORDER BY COALESCE(a.captured_at, a.mtime) DESC LIMIT ?",
        (*join_params, *params, limit),
    ).fetchall()

    items = [row_to_dict(r) for r in rows]
    years_map: dict[str, list[dict[str, Any]]] = {}
    for idx, r in enumerate(rows):
        yr = str(r["year_str"]) if r["year_str"] else "Unknown"
        years_map.setdefault(yr, []).append(items[idx])

    return {
        "month": month,
        "day": day,
        "total": len(items),
        "items": items,
        "years": years_map,
    }


# ---------------------------------------------------------------------------
# Choosing one photograph worth sending
#
# "On this day" finds the candidates; this decides which single one a person
# should be shown. That is the whole difficulty. A day in this library offers
# a couple of hundred photographs across twenty years, and the largest trips
# run to two thousand frames — so a digest that sends a blurred burst frame,
# a screenshot or the ninth near-identical shot of the same moment teaches
# the household to ignore it, once, permanently.
#
# Nothing here is new analysis. The scan has already worked out which
# photographs have people in them, which are sharp, which are duplicates of
# each other and which are screenshots; this only reads those columns and
# puts them in an order. Ratings are deliberately not used: a household that
# has favourited four photographs out of two hundred thousand has told you
# nothing, and that silence is what a digest exists to fix.
# ---------------------------------------------------------------------------

#: What each signal is worth. Faces dominate on purpose — across this library
#: a little over half of everything has a person in it, and of the things
#: anybody wants sent on a Sunday morning, nearly all do.
PICK_WEIGHTS = {
    "face": 3.0,          # somebody is in it
    "faces_many": 1.5,    # a group: usually the picture of the occasion
    "sharp": 1.5,         # measured, not guessed
    "exposed": 0.5,       # not nearly black and not blown out
    "landscape": 0.2,     # reads better in a message than a tall crop
    "age": 2.0,           # see PICK_FORGOTTEN_YEARS
}

#: How far back a photograph has to be before it counts as fully forgotten.
#: The first version of this scored a flat bonus for "has faces and is sharp",
#: which on a real day tied a hundred and forty photographs at 6.2 and left
#: the choice between them to the sort order. Age is what actually separates
#: them: last year is a photograph you remember taking, and the point of a
#: digest is the ones you do not.
PICK_FORGOTTEN_YEARS = 15.0

#: Below this a photograph is soft enough that sending it is worse than
#: sending nothing. Measured at a fixed size by the quality pass, so one
#: threshold holds across cameras. The same default the scan flags "blurry"
#: at, because two thresholds for one idea is how they drift apart.
PICK_MIN_SHARPNESS = 45.0

#: Brightness is a mean on 0–1, not 0–255 (see `media.image_stats`, which
#: divides by 255). Getting that wrong is not a wrong answer but an empty
#: one: every photograph in the library sits inside 0.1 and 0.97, so a
#: 0–255 range refuses all of them. These are the scan's own `dark` and
#: `bright` thresholds.
PICK_BRIGHTNESS = (0.16, 0.86)


def pick_for_the_day(
    conn: sqlite3.Connection,
    roots: Sequence[str] | str,
    *,
    month: int,
    day: int,
    max_visibility: int = 1,
    scope: str | None = None,
    limit: int = 400,
) -> dict[str, Any] | None:
    """The one photograph from this day in past years worth sending, or None.

    None is a real answer and the caller must respect it: a day whose only
    photographs are screenshots, blurred, or hidden has nothing to say, and
    saying nothing is what keeps the household reading the ones that do
    arrive.

    Only pictures. A video cannot be embedded in a message, and a silent
    poster frame is a worse version of a photograph.
    """
    candidates = candidates_for_the_day(
        conn, roots, month=month, day=day,
        max_visibility=max_visibility, scope=scope, limit=limit)
    return candidates[0] if candidates else None


def candidates_for_the_day(
    conn: sqlite3.Connection,
    roots: Sequence[str] | str,
    *,
    month: int,
    day: int,
    max_visibility: int = 1,
    scope: str | None = None,
    limit: int = 400,
) -> list[dict[str, Any]]:
    """Every photograph from this day that could be sent, best first.

    Separate from :func:`pick_for_the_day` so that the console can show what
    the choice was made from, and so a test can assert on the order rather
    than on one winner.
    """
    roots_sql, roots_params = roots_clause("a", roots)
    where = [
        roots_sql,
        "a.trashed = 0",
        "a.nsfw = 0",
        # A message can hold a photograph and nothing else.
        "a.kind = 'picture'",
        # No thumbnail means nothing to embed, whatever else it has going
        # for it.
        "a.thumb IS NOT NULL",
        "a.thumb <> ''",
        visibility_clause("a", max_visibility),
    ]
    params: list[Any] = [*roots_params, int(max_visibility)]

    # The date limit of whoever it goes to, named here: the digest runs on its
    # own thread, or from the console, where there is no family member asking
    # and visibility_clause adds none. A photograph the family cannot open in
    # Ninaivu was emailed to them.
    from ..server import auth, date_policy                  # noqa: PLC0415
    role = auth.ROLE_ADMIN if max_visibility >= 2 else (
        auth.ROLE_FAMILY if max_visibility >= 1 else auth.ROLE_GUEST)
    where.append(date_policy.sql("a", date_policy.for_role(conn, role)))

    scope_sql, scope_params = scope_clause("a", scope)
    if scope_sql:
        where.append(scope_sql)
        params.extend(scope_params)

    target = f"{month:02d}-{day:02d}"
    where.append(_ON_DAY_SQL)
    params.extend([f"%-{target}", target])

    # This year is not a memory. The point is the years you have forgotten.
    where.append(_YEAR_SQL + " < ?")
    params.append(time.strftime("%Y"))

    rows = conn.execute(
        "SELECT a.*, "
        f"  {_YEAR_SQL} AS year_str, "
        "  (SELECT COUNT(*) FROM faces f WHERE f.asset_id = a.id) AS face_count "
        f"FROM assets a WHERE {' AND '.join(where)} "
        "ORDER BY COALESCE(a.captured_at, a.mtime) DESC LIMIT ?",
        (*params, int(limit)),
    ).fetchall()

    scored = []
    seen_groups: set[str] = set()
    for row in rows:
        item = row_to_dict(row)
        item["year"] = str(row["year_str"] or "")
        item["face_count"] = int(row["face_count"] or 0)
        score = _pick_score(item)
        if score is None:
            continue
        item["pick_score"] = round(score, 3)
        scored.append(item)

    scored.sort(key=lambda i: (-i["pick_score"], i.get("year") or ""))

    # One frame per burst. Duplicates are already grouped by the scan, and a
    # digest that offers the same moment twice has wasted the only two lines
    # anybody reads.
    spread = []
    for item in scored:
        group = item.get("dup_group") or ""
        if group and group in seen_groups:
            continue
        if group:
            seen_groups.add(group)
        spread.append(item)
    return spread


def _pick_score(item: dict[str, Any]) -> float | None:
    """How much this photograph is worth sending, or None to refuse it.

    Refusing is not scoring zero: a screenshot is not a weak memory, it is
    not a memory, and no amount of sharpness should let it through.
    """
    quality = item.get("quality")
    if isinstance(quality, str):
        try:
            quality = json.loads(quality)
        except ValueError:
            quality = []
    tags = {str(tag) for tag in (quality or [])}
    # The scan's own words for the things nobody wants sent to them.
    if tags & {"screenshot", "document", "screen"}:
        return None

    sharpness = item.get("sharpness")
    if sharpness is not None and float(sharpness) < PICK_MIN_SHARPNESS:
        return None
    brightness = item.get("brightness")
    if brightness is not None and not (
            PICK_BRIGHTNESS[0] <= float(brightness) <= PICK_BRIGHTNESS[1]):
        return None

    faces = int(item.get("face_count") or 0)
    score = 0.0
    if faces:
        score += PICK_WEIGHTS["face"]
    if faces > 1:
        # Two people is much more than one; six is not much more than five,
        # and beyond that it is a crowd nobody is the subject of.
        score += PICK_WEIGHTS["faces_many"] * min(1.0, (faces - 1) / 4.0)
    # Measured sharpness counts for something beyond the floor, but with a
    # ceiling: a very sharp photograph of nothing is still of nothing.
    # Sharpness runs from tens into the thousands in this library, so the
    # curve flattens early: past "clearly in focus" a sharper photograph is
    # not a better memory.
    if sharpness is not None:
        score += PICK_WEIGHTS["sharp"] * min(1.0, float(sharpness) / 300.0)
    if brightness is not None:
        middle = 1.0 - abs(float(brightness) - 0.5) / 0.5
        score += PICK_WEIGHTS["exposed"] * max(0.0, middle)
    width, height = item.get("width") or 0, item.get("height") or 0
    if width and height and width >= height:
        score += PICK_WEIGHTS["landscape"]
    year = item.get("year") or ""
    if year.isdigit():
        behind = int(time.strftime("%Y")) - int(year)
        score += PICK_WEIGHTS["age"] * min(1.0, behind / PICK_FORGOTTEN_YEARS)
    return score


def query_geo_points(
    conn: sqlite3.Connection,
    roots: Sequence[str] | str,
    *,
    viewer_id: int = 0,
    max_visibility: int = 1,
    scope: str | None = None,
    north: float | None = None,
    south: float | None = None,
    east: float | None = None,
    west: float | None = None,
    limit: int = 1500,
) -> list[dict[str, Any]]:
    """Return assets with GPS coordinates for map exploration."""
    roots_sql, roots_params = roots_clause("a", roots)
    where = [
        roots_sql,
        "a.trashed = 0",
        "a.nsfw = 0",
        "a.gps_lat IS NOT NULL",
        "a.gps_lon IS NOT NULL",
        visibility_clause("a", max_visibility),
        # Shown once on the map, as in the gallery.
        "a.live_clip = 0 AND a.raw_twin = 0",
    ]
    params: list[Any] = [*roots_params, int(max_visibility)]

    scope_sql, scope_params = scope_clause("a", scope)
    if scope_sql:
        where.append(scope_sql)
        params.extend(scope_params)

    if north is not None and south is not None:
        where.append("a.gps_lat BETWEEN ? AND ?")
        params.extend([min(south, north), max(south, north)])
    if east is not None and west is not None:
        if west <= east:
            where.append("a.gps_lon BETWEEN ? AND ?")
            params.extend([west, east])
        else:
            where.append("(a.gps_lon >= ? OR a.gps_lon <= ?)")
            params.extend([west, east])

    where_sql = " AND ".join(where)
    rows = conn.execute(
        f"SELECT a.id, a.filename, a.gps_lat, a.gps_lon, a.thumb, a.date_key, a.kind, "
        f"a.city, a.country, a.is_live, a.duration FROM assets a WHERE {where_sql} "
        f"ORDER BY COALESCE(a.captured_at, a.mtime) DESC LIMIT ?",
        (*params, limit),
    ).fetchall()

    return [
        {
            "id": int(r["id"]),
            "filename": r["filename"],
            "lat": float(r["gps_lat"]),
            "lon": float(r["gps_lon"]),
            "thumb": r["thumb"],
            "date": r["date_key"],
            "kind": r["kind"],
            "city": r["city"],
            "country": r["country"],
            "is_live": bool(r["is_live"]),
        }
        for r in rows
    ]
