"""Where the photographs were taken: the map's questions, and locations filled in.

The map asked for up to 1,500 photographs with a location and drew one pin
each. This library has 23,776, so it drew a sixteenth of them, pins in a busy
place stacked on one another, and a click opened one photograph. These answer
the map instead:

* :func:`clusters` groups what is on screen into cells a pin's width across at
  the map's zoom, each with its count, a cover and the place it is in, so every
  photograph is on the map and a phone draws a few hundred markers, not
  thousands;
* :func:`places` lists the places, country then city, with their counts and
  extent, and the years there are photographs with locations for;
* :func:`photo_ids` gives the photographs of one cell or one place, for the
  viewer;
* :func:`trail` walks a period's photographs in the order they were taken and
  folds those close together into stops: a trip, drawn as a route.

Every one applies the viewer's visibility ceiling and folder scope, as every
read in Ninaivu does, and the same filters: a year, a person, a place.

Most photographs have no location, often because one camera on a trip had GPS
and another had not. :func:`location_suggestions` finds those taken within an
hour or so of a photograph that has one, and :func:`apply_locations` writes it,
in the index only, marked ``location_inferred`` so :func:`undo_locations` can
take every one of them back. A suggested location is never used to suggest
another.
"""
from __future__ import annotations

import bisect
import sqlite3
from typing import Any, Sequence

from . import db

#: How wide a group is on screen: pins nearer than this merge into one.
CLUSTER_PX = 64
MAX_ZOOM = 20
#: Consecutive photographs this close together are one stop on a trip.
STOP_KM = 2.0
#: How many photographs one "view these" answers with, at most.
PHOTO_LIMIT = 5000
#: Capture times good enough to place a photograph by: the camera's own clock,
#: or a video's. A date read from a file name or its modification time is not.
TIMED = ("exif", "container")
#: The windows a household may choose for a suggestion, in hours.
WINDOWS = (0.5, 1.0, 2.0)


def cell_degrees(zoom: int) -> float:
    """A group's width in degrees at this zoom: CLUSTER_PX of a 256-px tile."""
    zoom = max(0, min(MAX_ZOOM, int(zoom)))
    return 360.0 / (2 ** zoom) * CLUSTER_PX / 256.0


#: How far a group's own bounds are widened when it is opened: its corners are
#: its photographs, and a comparison at the last decimal must not lose them.
EDGE = 1e-9


def _wrap(lon: float) -> float:
    """Into -180..180. Only when outside it: the arithmetic moved 77.59 to
    77.59000000000003, and a group of one photograph found none."""
    lon = float(lon)
    return lon if -180.0 <= lon <= 180.0 else ((lon + 180.0) % 360.0) - 180.0


def _filters(roots: Sequence[str] | str, *, max_visibility: int = 1, scope: str | None = None,
             viewer_id: int = 0, bounds: tuple[float, float, float, float] | None = None,
             year: int | None = None, person: int | None = None,
             place: tuple[str, str] | None = None) -> tuple[str, list[Any]]:
    """The WHERE for every map question. *bounds* is (south, north, west, east)."""
    roots_sql, params = db.roots_clause("a", roots)
    where = [roots_sql, "a.trashed = 0", "a.nsfw = 0", "a.gps_lat IS NOT NULL",
             "a.gps_lon IS NOT NULL", db.visibility_clause("a", max_visibility)]
    params.append(int(max_visibility))
    scope_sql, scope_params = db.scope_clause("a", scope)
    if scope_sql:
        where.append(scope_sql)
        params.extend(scope_params)
    if bounds is not None:
        south, north, west, east = (float(v) for v in bounds)
        where.append("a.gps_lat BETWEEN ? AND ?")
        params.extend([min(south, north) - EDGE, max(south, north) + EDGE])
        west, east = west - EDGE, east + EDGE
        # A map panned round the world reports longitudes past 180. The whole
        # width is no filter at all; otherwise wrap, and across the date line
        # the range is the two ends.
        if east - west < 360:
            w, e = _wrap(west), _wrap(east)
            if w <= e:
                where.append("a.gps_lon BETWEEN ? AND ?")
            else:
                where.append("(a.gps_lon >= ? OR a.gps_lon <= ?)")
            params.extend([w, e])
    if year:
        where.append("a.date_key BETWEEN ? AND ?")
        params.extend([f"{int(year):04d}-01-01", f"{int(year):04d}-12-31"])
    if person:
        # IN rather than a correlated EXISTS, as in db.query_assets: this
        # person's faces read once, not a probe for every photograph.
        where.append("a.id IN (SELECT asset_id FROM faces WHERE person_id = ?)")
        params.append(int(person))
    if place is not None:
        where.append("COALESCE(a.country, '') = ? AND COALESCE(a.city, '') = ?")
        params.extend([place[0] or "", place[1] or ""])
    return " AND ".join(where), params


#: The id of a group's cover: its newest picture with a thumbnail, else its newest item.
_COVER = ("COALESCE(MAX(CASE WHEN a.kind = 'picture' AND a.thumb IS NOT NULL "
          "THEN a.id END), MAX(a.id))")


def _named(conn: sqlite3.Connection, ids: list[int]) -> dict[int, tuple[str, str]]:
    """``id -> (city, country)`` for the given covers."""
    found: dict[int, tuple[str, str]] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        for row in conn.execute(
                f"SELECT id, city, country FROM assets WHERE id IN ({','.join('?' * len(chunk))})",
                chunk):
            found[int(row[0])] = (row[1] or "", row[2] or "")
    return found


def clusters(conn: sqlite3.Connection, roots: Sequence[str] | str, *, zoom: int,
             **limits: Any) -> list[dict[str, Any]]:
    """What is on screen, grouped a pin's width at a time.

    The whole world — what the map opens on — reads every located photograph
    whichever index it takes, so it is remembered as the place list is. A
    view with bounds is a different question on every pan and is not kept.
    """
    if limits.get("bounds") is None:
        where, params = _filters(roots, **limits)
        also = (db.PLACES_GENERATION_KEY,)
        if limits.get("person"):
            also += (db.FACES_GENERATION_KEY,)
        return db.cached_aggregate(
            conn, ("geo_clusters", int(zoom), where, tuple(params)),
            lambda: _clusters(conn, zoom, roots, **limits), also=also)
    return _clusters(conn, zoom, roots, **limits)


def _clusters(conn: sqlite3.Connection, zoom: int, roots: Sequence[str] | str,
              **limits: Any) -> list[dict[str, Any]]:
    cell = cell_degrees(zoom)
    where, params = _filters(roots, **limits)
    rows = conn.execute(
        "SELECT CAST((a.gps_lat + 90) / ? AS INTEGER) gy, "
        "CAST((a.gps_lon + 180) / ? AS INTEGER) gx, COUNT(*) n, "
        "AVG(a.gps_lat) lat, AVG(a.gps_lon) lon, MIN(a.gps_lat) s, MAX(a.gps_lat) nn, "
        f"MIN(a.gps_lon) w, MAX(a.gps_lon) e, {_COVER} cover "
        f"FROM assets a WHERE {where} GROUP BY gy, gx",
        (cell, cell, *params)).fetchall()
    names = _named(conn, [int(r["cover"]) for r in rows])
    out = []
    for r in rows:
        city, country = names.get(int(r["cover"]), ("", ""))
        out.append({
            "lat": float(r["lat"]), "lon": float(r["lon"]), "count": int(r["n"]),
            "cover": int(r["cover"]), "city": city, "country": country,
            "bounds": [float(r["s"]), float(r["nn"]), float(r["w"]), float(r["e"])],
        })
    return out


def places(conn: sqlite3.Connection, roots: Sequence[str] | str,
           **limits: Any) -> dict[str, Any]:
    """Every place there are photographs from, most photographed first, with
    the years there are any for (whatever year is chosen, so it can change).

    A group over every located photograph, asked for each time the map opens:
    remembered until the library, a location or a cover changes — and the
    faces, when it is one person's places.
    """
    where, params = _filters(roots, **limits)
    anywhere = {k: v for k, v in limits.items() if k != "year"}
    where_all, params_all = _filters(roots, **anywhere)
    also = (db.PLACES_GENERATION_KEY,)
    if limits.get("person"):
        also += (db.FACES_GENERATION_KEY,)
    return db.cached_aggregate(
        conn, ("places", where, tuple(params), where_all, tuple(params_all)),
        lambda: _places(conn, where, params, where_all, params_all), also=also)


def _places(conn: sqlite3.Connection, where: str, params: list[Any],
            where_all: str, params_all: list[Any]) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT COALESCE(a.country, '') country, COALESCE(a.city, '') city, COUNT(*) n, "
        "MIN(a.gps_lat) s, MAX(a.gps_lat) nn, MIN(a.gps_lon) w, MAX(a.gps_lon) e, "
        f"{_COVER} cover FROM assets a WHERE {where} "
        "GROUP BY COALESCE(a.country, ''), COALESCE(a.city, '') ORDER BY n DESC",
        params).fetchall()
    found = [{
        "country": r["country"], "city": r["city"], "count": int(r["n"]), "cover": int(r["cover"]),
        "bounds": [float(r["s"]), float(r["nn"]), float(r["w"]), float(r["e"])],
    } for r in rows]
    years = [int(r[0]) for r in conn.execute(
        f"SELECT DISTINCT substr(a.date_key, 1, 4) y FROM assets a WHERE {where_all} "
        "AND a.date_key GLOB '[12][0-9][0-9][0-9]-*' ORDER BY y DESC", params_all)]
    extent = None
    if found:
        extent = [min(p["bounds"][0] for p in found), max(p["bounds"][1] for p in found),
                  min(p["bounds"][2] for p in found), max(p["bounds"][3] for p in found)]
    return {"places": found, "years": years, "total": sum(p["count"] for p in found),
            "bounds": extent}


def photo_ids(conn: sqlite3.Connection, roots: Sequence[str] | str, *,
              limit: int = PHOTO_LIMIT, **limits: Any) -> tuple[list[int], int]:
    """The photographs of one group or place, newest first, and how many there are."""
    where, params = _filters(roots, **limits)
    total = int(conn.execute(f"SELECT COUNT(*) FROM assets a WHERE {where}", params).fetchone()[0])
    ids = [int(r[0]) for r in conn.execute(
        f"SELECT a.id FROM assets a WHERE {where} "
        "ORDER BY COALESCE(a.captured_at, a.mtime) DESC LIMIT ?", (*params, int(limit)))]
    return ids, total


def trail(conn: sqlite3.Connection, roots: Sequence[str] | str, *, limit: int = 20000,
          **limits: Any) -> list[dict[str, Any]]:
    """A period's photographs in the order they were taken, as stops on a route."""
    from ..media.occasions import haversine_km

    where, params = _filters(roots, **limits)
    rows = conn.execute(
        "SELECT a.id, a.gps_lat, a.gps_lon, a.date_key, a.city, a.country, a.kind "
        f"FROM assets a WHERE {where} "
        "ORDER BY COALESCE(a.captured_at, a.mtime), a.id LIMIT ?", (*params, int(limit))).fetchall()
    stops: list[dict[str, Any]] = []
    for r in rows:
        lat, lon = float(r["gps_lat"]), float(r["gps_lon"])
        here = stops[-1] if stops else None
        if here is not None and haversine_km(here["lat"], here["lon"], lat, lon) <= STOP_KM:
            here["ids"].append(int(r["id"]))
            here["last"] = r["date_key"] or here["last"]
            if here["cover_kind"] != "picture" and r["kind"] == "picture":
                here["cover"], here["cover_kind"] = int(r["id"]), "picture"
            continue
        stops.append({"lat": lat, "lon": lon, "first": r["date_key"] or "",
                      "last": r["date_key"] or "", "city": r["city"] or "",
                      "country": r["country"] or "", "ids": [int(r["id"])],
                      "cover": int(r["id"]), "cover_kind": r["kind"]})
    for stop in stops:
        stop["count"] = len(stop["ids"])
        stop.pop("cover_kind")
    return stops


# -- locations filled in ---------------------------------------------------------

def _suggest(conn: sqlite3.Connection, roots: Sequence[str] | str,
             max_hours: float) -> list[tuple[dict[str, Any], dict[str, Any], float]]:
    """``(photograph, the located one nearest in time, minutes apart)`` for each
    photograph without a location taken within *max_hours* of one with."""
    roots_sql, params = db.roots_clause("a", roots)
    anchors = [dict(r) for r in conn.execute(
        "SELECT a.id, a.captured_at, a.gps_lat, a.gps_lon, a.city, a.country FROM assets a "
        f"WHERE {roots_sql} AND a.trashed = 0 AND a.gps_lat IS NOT NULL "
        "AND a.gps_lon IS NOT NULL AND a.captured_at IS NOT NULL "
        "AND a.date_source = 'exif' AND a.location_inferred = 0 ORDER BY a.captured_at", params)]
    if not anchors:
        return []
    times = [float(a["captured_at"]) for a in anchors]
    marks = ",".join("?" * len(TIMED))
    limit = float(max_hours) * 3600.0
    found = []
    for row in conn.execute(
            "SELECT a.id, a.captured_at, a.date_key, a.kind FROM assets a "
            f"WHERE {roots_sql} AND a.trashed = 0 AND a.gps_lat IS NULL "
            f"AND a.captured_at IS NOT NULL AND a.date_source IN ({marks}) "
            "AND a.kind IN ('picture', 'video')", (*params, *TIMED)):
        when = float(row["captured_at"])
        at = bisect.bisect_left(times, when)
        gap, best = min((abs(times[j] - when), j) for j in (at - 1, at) if 0 <= j < len(times))
        if gap <= limit:
            found.append((dict(row), anchors[best], gap / 60.0))
    return found


def location_suggestions(conn: sqlite3.Connection, roots: Sequence[str] | str, *,
                         max_hours: float = 1.0) -> dict[str, Any]:
    """The suggestions, a day and a place at a time, newest day first."""
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for photo, anchor, minutes in _suggest(conn, roots, max_hours):
        key = (photo["date_key"] or "", anchor["city"] or "", anchor["country"] or "")
        group = groups.setdefault(key, {
            "date": key[0], "city": key[1], "country": key[2], "ids": [],
            "lat": float(anchor["gps_lat"]), "lon": float(anchor["gps_lon"]),
            "anchor": int(anchor["id"]), "max_minutes": 0.0})
        group["ids"].append(int(photo["id"]))
        group["max_minutes"] = max(group["max_minutes"], round(minutes, 1))
    rows = sorted(groups.values(), key=lambda g: (g["date"], g["city"]), reverse=True)
    for group in rows:
        group["count"] = len(group["ids"])
    roots_sql, params = db.roots_clause("a", roots)
    inferred = conn.execute(
        f"SELECT COUNT(*) FROM assets a WHERE {roots_sql} AND a.location_inferred = 1",
        params).fetchone()[0]
    return {"groups": rows, "total": sum(g["count"] for g in rows), "inferred": int(inferred)}


def apply_locations(conn: sqlite3.Connection, roots: Sequence[str] | str, *,
                    max_hours: float = 1.0, ids: Sequence[int] | None = None) -> int:
    """Give photographs the location of the one nearest in time. Worked out
    again here rather than taken from the request, so only a suggestion that
    still stands is written. Returns how many were."""
    wanted = None if ids is None else {int(i) for i in ids}
    changes = [(float(anchor["gps_lat"]), float(anchor["gps_lon"]), anchor["city"],
                anchor["country"], int(photo["id"]))
               for photo, anchor, _ in _suggest(conn, roots, max_hours)
               if wanted is None or int(photo["id"]) in wanted]
    # Under the lock every other writer takes, so the backup's snapshot never
    # copies the index half-way through this.
    with db._write_lock:                                  # noqa: SLF001
        conn.executemany(
            "UPDATE assets SET gps_lat = ?, gps_lon = ?, city = ?, country = ?, "
            "location_inferred = 1 WHERE id = ? AND gps_lat IS NULL", changes)
        conn.commit()
    return len(changes)


def undo_locations(conn: sqlite3.Connection, roots: Sequence[str] | str) -> int:
    """Take back every location Ninaivu filled in. Returns how many."""
    roots_sql, params = db.roots_clause("a", roots)
    ids = [int(r[0]) for r in conn.execute(
        f"SELECT a.id FROM assets a WHERE {roots_sql} AND a.location_inferred = 1", params)]
    with db._write_lock:                                  # noqa: SLF001
        conn.executemany(
            "UPDATE assets SET gps_lat = NULL, gps_lon = NULL, city = NULL, country = NULL, "
            "location_inferred = 0 WHERE id = ?", [(i,) for i in ids])
        conn.commit()
    return len(ids)
