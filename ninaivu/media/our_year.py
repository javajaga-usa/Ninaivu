"""Our Year: a family's year in photographs, as a set of cards.

Open it on the first of January (or any day) and Ninaivu tells the household
the story of a year from what the library already knows: how many photographs
and videos were taken and on how many days, the month that was busiest and
the single day with the most, who appears most and who appears for the very
first time (a new baby, a new daughter-in-law), who is always in a photograph
together, the trips and the furthest place from home, the photographs the
family favourited and the stories they told about them, the camera that took
most of it, and one good photograph for every month. The page
(static/js/our-year.js) turns each of these into a card, in Tamil or English,
and ends with "Make a book" and "Make a film" of the year.

Nothing here reads a pixel or calls a model: it is counting over the index,
a second or two for a year of twenty thousand photographs, and remembered
until the library changes (``db.cached_aggregate``). A few warm sentences to
open with are written by the on-device writing model when it is downloaded
(:func:`model_messages`, api/api_our_year.py); without it the page writes its
own from the same facts.

Who sees what is never decided here. The year's photographs are read through
``db.query_assets`` with the viewer's own limits, the check every gallery
listing goes through, so a hidden photograph, a folder somebody is kept out
of, or another person's "Only to me" items never count. A guest, who is never
told who is in a photograph or where it was taken, is given neither people
nor places nor stories.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter
from datetime import date, datetime
from itertools import combinations
from typing import Any, Sequence

from ..storage import db
from . import occasions as occasions_mod

#: The fewest photographs and videos a year needs before it is offered: a
#: year with eight photographs has no story to tell in cards.
MIN_ITEMS = 20

#: The most of a year's items read. Far beyond any real family's year (the
#: busiest in a 208,000-photo library is about 19,000); past it the year is
#: told from the first this many and says so.
MAX_ITEMS = 60000

#: How many people, places and trips a card names.
TOP_PEOPLE, TOP_PLACES, TOP_TRIPS = 6, 5, 4

#: Who counts as "always together": named people in at least this many of
#: the same photographs.
MIN_TOGETHER = 3

#: The most items handed to the scorer for the month's photograph.
MONTH_POOL = 400

#: Columns read for every item of the year.
_COLUMNS = ("id", "kind", "width", "height", "captured_at", "date_key", "duration", "camera",
            "gps_lat", "gps_lon", "city", "country", "sharpness", "brightness", "quality",
            "dup_group", "thumb", "is_live", "occasion_id", "indexed_at", "rotation")


def _chunks(ids: Sequence[int], size: int = 900):
    for start in range(0, len(ids), size):
        yield list(ids[start:start + size])


def _limits(viewer: dict[str, Any]) -> dict[str, Any]:
    return {"viewer_id": int(viewer.get("viewer_id") or 0),
            "max_visibility": int(viewer.get("max_visibility") or 0),
            "scope": viewer.get("scope") or None}


def years(conn: sqlite3.Connection, roots: Sequence[str], viewer: dict[str, Any]) -> list[dict[str, Any]]:
    """The years this viewer can be told about, newest first, with how many
    photographs each holds."""
    limits = _limits(viewer)
    return [y for y in db.picture_years(conn, roots, max_visibility=limits["max_visibility"],
                                        scope=limits["scope"])
            if y["count"] >= MIN_ITEMS and 1826 <= y["year"] <= 2200]


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    return occasions_mod.haversine_km(a[0], a[1], b[0], b[1])


def _when(row: dict[str, Any]) -> float:
    return float(row.get("captured_at") or 0)


def _face_rows(conn: sqlite3.Connection, ids: Sequence[int]) -> list[sqlite3.Row]:
    rows: list[sqlite3.Row] = []
    for piece in _chunks(ids):
        marks = ",".join("?" * len(piece))
        try:
            rows.extend(conn.execute(
                "SELECT id, asset_id, person_id, quality FROM faces "
                f"WHERE asset_id IN ({marks}) AND source IN ('auto', 'confirmed', 'none')",
                piece).fetchall())
        except sqlite3.OperationalError:
            return []
    return rows


def _people(conn: sqlite3.Connection, roots: Sequence[str], viewer: dict[str, Any],
            faces: Sequence[sqlite3.Row], names: dict[int, str], year: int) -> dict[str, Any]:
    """Who appears most, who for the first time, and who is always together."""
    in_photos: dict[int, set[int]] = {}
    best: dict[int, tuple[float, int]] = {}
    by_asset: dict[int, set[int]] = {}
    for face in faces:
        pid = face["person_id"]
        if pid is None or int(pid) not in names:
            continue
        pid, aid = int(pid), int(face["asset_id"])
        in_photos.setdefault(pid, set()).add(aid)
        by_asset.setdefault(aid, set()).add(pid)
        quality = float(face["quality"] or 0)
        if pid not in best or quality > best[pid][0]:
            best[pid] = (quality, int(face["id"]))
    ranked = sorted(in_photos, key=lambda p: (-len(in_photos[p]), names[p].casefold()))

    def person(pid: int) -> dict[str, Any]:
        return {"id": pid, "name": names[pid], "photos": len(in_photos[pid]),
                "face": best[pid][1] if pid in best else None}

    # The first photograph of each of them anywhere in what this viewer may
    # see: a person whose first is in this year is new to the family's photos.
    first: dict[int, str] = {}
    if ranked:
        limits = _limits(viewer)
        guard, params = db._face_visibility_sql(                    # noqa: SLF001
            "a", limits["max_visibility"], limits["scope"], roots)
        for piece in _chunks(ranked, 500):
            marks = ",".join("?" * len(piece))
            for row in conn.execute(
                    "SELECT f.person_id, MIN(a.date_key) AS first FROM faces f "
                    f"JOIN assets a ON a.id = f.asset_id WHERE {guard} AND a.date_key != '' "
                    f"AND f.person_id IN ({marks}) GROUP BY f.person_id", (*params, *piece)):
                first[int(row["person_id"])] = str(row["first"] or "")
    new = [{**person(pid), "first": first[pid]} for pid in ranked
           if first.get(pid, "")[:4] == str(year)]
    new.sort(key=lambda p: p["first"])

    together: Counter[tuple[int, int]] = Counter()
    top = set(ranked[:12])
    for pids in by_asset.values():
        for pair in combinations(sorted(pids & top), 2):
            together[pair] += 1
    pair = None
    if together:
        (a, b), count = max(together.items(),
                            key=lambda kv: (kv[1], -min(ranked.index(kv[0][0]), ranked.index(kv[0][1]))))
        if count >= MIN_TOGETHER:
            pair = {"people": [person(a), person(b)], "photos": count}
    return {"top": [person(p) for p in ranked[:TOP_PEOPLE]], "named": len(ranked),
            "new": new[:TOP_PEOPLE], "together": pair}


def _places(rows: Sequence[dict[str, Any]], home: tuple[float, float] | None) -> dict[str, Any]:
    cities: Counter[str] = Counter()
    countries: set[str] = set()
    far: tuple[float, dict[str, Any]] | None = None
    for row in rows:
        city, country = row.get("city"), row.get("country")
        if city or country:
            cities[", ".join(p for p in (city, country) if p)] += 1
        if country:
            countries.add(str(country))
        lat, lon = row.get("gps_lat"), row.get("gps_lon")
        if home is not None and lat is not None and lon is not None and (lat or lon):
            try:
                km = _haversine_km(home, (float(lat), float(lon)))
            except (TypeError, ValueError):
                continue
            if math.isfinite(km) and (far is None or km > far[0]):
                far = (km, row)
    furthest = None
    if far is not None and far[0] >= 25:
        row = far[1]
        place = ", ".join(p for p in (row.get("city"), row.get("country")) if p)
        if place:
            furthest = {"place": place, "km": int(round(far[0])), "id": int(row["id"]),
                        "date": row.get("date_key") or ""}
    return {"top": [{"place": p, "items": n} for p, n in cities.most_common(TOP_PLACES)],
            "count": len(cities), "countries": sorted(countries), "furthest": furthest}


def _trips(conn: sqlite3.Connection, roots: Sequence[str], viewer: dict[str, Any], year: int,
           guest: bool) -> list[dict[str, Any]]:
    """The year's occasions of more than one day, the biggest first."""
    found = []
    for occasion in db.list_occasions(conn, roots, limit=5000, with_place=not guest,
                                      **_limits(viewer)):
        started = float(occasion.get("started_at") or 0)
        if not started or datetime.fromtimestamp(started).year != year:
            continue
        if int(occasion.get("days") or 1) < 2:
            continue
        found.append({"id": int(occasion["id"]), "title": occasion.get("title") or "",
                      "place": occasion.get("place") or "", "days": int(occasion.get("days") or 1),
                      "items": int(occasion.get("count") or 0),
                      "cover": occasion.get("cover_id"),
                      "started_at": started, "ended_at": occasion.get("ended_at")})
    found.sort(key=lambda o: (-o["days"] * max(1, o["items"]), o["started_at"]))
    return found


def _favourites(conn: sqlite3.Connection, ids: Sequence[int]) -> Counter[int]:
    """How many people in the household favourited each of *ids*."""
    hearts: Counter[int] = Counter()
    for piece in _chunks(ids):
        marks = ",".join("?" * len(piece))
        try:
            for row in conn.execute(
                    f"SELECT asset_id, COUNT(*) AS n FROM user_assets WHERE asset_id IN ({marks}) "
                    "AND favorite = 1 GROUP BY asset_id", piece):
                hearts[int(row["asset_id"])] = int(row["n"])
        except sqlite3.OperationalError:
            return Counter()
    return hearts


def _stories(conn: sqlite3.Connection, ids: Sequence[int]) -> dict[str, Any]:
    told: Counter[int] = Counter()
    speakers: set[str] = set()
    for piece in _chunks(ids):
        marks = ",".join("?" * len(piece))
        try:
            for row in conn.execute(
                    f"SELECT asset_id, speaker FROM stories WHERE asset_id IN ({marks})", piece):
                told[int(row["asset_id"])] += 1
                if row["speaker"]:
                    speakers.add(str(row["speaker"]))
        except sqlite3.OperationalError:
            return {"count": 0, "photos": 0, "speakers": [], "example": None}
    example = None
    if told:
        from . import frame                                          # noqa: PLC0415
        aid = told.most_common(1)[0][0]
        first = frame._first_story(conn, [aid]).get(aid)             # noqa: SLF001
        if first:
            example = {"id": aid, **first}
    return {"count": sum(told.values()), "photos": len(told), "speakers": sorted(speakers),
            "example": example}


def _score(row: dict[str, Any], faces: int, hearts: int) -> float | None:
    probe = dict(row)
    probe["year"] = str(row.get("date_key") or "")[:4]
    probe["face_count"] = faces
    score = db._pick_score(probe)                                    # noqa: SLF001
    if score is None:
        return None
    return score + min(3, hearts) * 1.0 + (0.5 if row.get("favorite") else 0.0)


def _shown(row: dict[str, Any], version) -> dict[str, Any]:  # noqa: ANN001
    return {"id": int(row["id"]), "kind": row.get("kind") or "", "date": row.get("date_key") or "",
            "width": row.get("width"), "height": row.get("height"),
            "thumb_v": version(row) if version else ""}


def summary(conn: sqlite3.Connection, roots: Sequence[str], viewer: dict[str, Any], year: int, *,
            names: dict[int, str], guest: bool = False,
            home: tuple[float, float] | None = None, version=None,  # noqa: ANN001
            today: date | None = None) -> dict[str, Any]:
    """Everything the year's cards say, for *viewer*.

    *names* are the named people this viewer may see (``list_people``), the
    only ones a card may name; *home* is the household's home, for the
    furthest place; *version* gives a row's thumbnail version for its URL.
    """
    today = today or date.today()
    limits = _limits(viewer)
    rows, _ = db.query_assets(
        conn, roots, kinds=["picture", "video"], date_from=f"{year:04d}-01-01",
        date_to=f"{year:04d}-12-31", sort="date_asc", limit=MAX_ITEMS, columns=_COLUMNS,
        with_total=False, **limits)
    rows = [r for r in rows if str(r.get("date_key") or "")[:4] == str(year)]
    pictures = [r for r in rows if r.get("kind") == "picture"]
    videos = [r for r in rows if r.get("kind") == "video"]
    ids = [int(r["id"]) for r in rows]

    months = [0] * 12
    days: Counter[str] = Counter()
    for row in rows:
        key = str(row.get("date_key") or "")
        if len(key) >= 10 and key[5:7].isdigit() and 1 <= int(key[5:7]) <= 12:
            months[int(key[5:7]) - 1] += 1
            days[key[:10]] += 1
    busiest_day = None
    if days:
        day, count = max(days.items(), key=lambda kv: (kv[1], kv[0]))
        on_day = [r for r in rows if str(r.get("date_key") or "")[:10] == day]
        trip = Counter(int(r["occasion_id"]) for r in on_day if r.get("occasion_id"))
        busiest_day = {"date": day, "items": count,
                       "occasion": trip.most_common(1)[0][0] if trip else None}

    faces = [] if guest else _face_rows(conn, [int(r["id"]) for r in pictures])
    face_count = Counter(int(f["asset_id"]) for f in faces)
    hearts = _favourites(conn, ids)

    # One photograph for each month that has any, and the best of the year
    # for the cover: the same judgement the digest and the frame use.
    by_month: dict[int, list[dict[str, Any]]] = {}
    for row in pictures:
        if row.get("thumb"):
            by_month.setdefault(int(str(row["date_key"])[5:7]), []).append(row)
    month_best: list[dict[str, Any]] = []
    cover: tuple[float, dict[str, Any]] | None = None
    for month in sorted(by_month):
        pool = by_month[month]
        step = max(1, len(pool) // MONTH_POOL)
        scored = []
        for row in pool[::step]:
            score = _score(row, face_count.get(int(row["id"]), 0), hearts.get(int(row["id"]), 0))
            if score is not None:
                scored.append((score, row))
        if not scored:
            continue
        score, row = max(scored, key=lambda pair: pair[0])
        month_best.append({**_shown(row, version), "month": month})
        if cover is None or score > cover[0]:
            cover = (score, row)

    loved = None
    if hearts:
        aid, n = max(hearts.items(), key=lambda kv: (kv[1], -kv[0]))
        row = next((r for r in rows if int(r["id"]) == aid), None)
        if row is not None:
            loved = {**_shown(row, version), "hearts": n}
    mine = sum(1 for r in rows if r.get("favorite"))

    previous = next((y["count"] for y in db.picture_years(
        conn, roots, max_visibility=limits["max_visibility"], scope=limits["scope"])
        if y["year"] == year - 1), None)
    cameras = Counter(str(r["camera"]).strip() for r in rows if (r.get("camera") or "").strip())
    camera = None
    if cameras:
        name, n = cameras.most_common(1)[0]
        camera = {"name": name, "items": n, "share": round(n / max(1, len(rows)), 3)}

    dated = [r for r in rows if _when(r)]
    first = min(dated, key=_when) if dated else (rows[0] if rows else None)
    last = max(dated, key=_when) if dated else (rows[-1] if rows else None)

    out: dict[str, Any] = {
        "year": year,
        "finished": year < today.year,
        "items": len(rows),
        "capped": len(rows) >= MAX_ITEMS,
        "pictures": len(pictures),
        "videos": len(videos),
        "live": sum(1 for r in pictures if r.get("is_live")),
        "video_minutes": int(round(sum(float(r.get("duration") or 0) for r in videos) / 60)),
        "days": len(days),
        "months": months,
        "busiest_month": (months.index(max(months)) + 1) if any(months) else None,
        "busiest_day": busiest_day,
        "previous_year": previous,
        "cover": _shown(cover[1], version) if cover else None,
        "month_photos": month_best,
        "first": _shown(first, version) if first else None,
        "last": _shown(last, version) if last else None,
        "favourites": {"mine": mine, "loved": loved,
                       "hearted": sum(1 for n in hearts.values() if n)},
        "camera": camera,
        "trips": [],
        "people": None,
        "places": None,
        "stories": None,
    }
    trips = _trips(conn, roots, viewer, year, guest)
    out["trips"] = trips[:TOP_TRIPS]
    out["trip_count"] = len(trips)
    out["trip_days"] = sum(t["days"] for t in trips)
    if not guest:
        out["people"] = _people(conn, roots, viewer, faces, names, year)
        out["places"] = _places(rows, home)
        out["stories"] = _stories(conn, ids)
    else:
        for trip in out["trips"]:
            trip["place"] = ""
    return out


# -- the writing model --------------------------------------------------------------

def facts(found: dict[str, Any]) -> dict[str, Any]:
    """What the writing model is told about the year: counts and names, never
    a file, a path or a photograph."""
    people = found.get("people") or {}
    places = found.get("places") or {}
    stories = found.get("stories") or {}
    month_names = ["January", "February", "March", "April", "May", "June", "July",
                   "August", "September", "October", "November", "December"]
    out: dict[str, Any] = {
        "year": found["year"],
        "year_is_over": found.get("finished"),
        "photos": found.get("pictures"), "videos": found.get("videos"),
        "days_with_photos": found.get("days"),
        "photos_the_year_before": found.get("previous_year"),
        "busiest_month": month_names[found["busiest_month"] - 1] if found.get("busiest_month") else None,
        "busiest_day": (found.get("busiest_day") or {}).get("date"),
        "trips": [{"place": t.get("place") or t.get("title"), "days": t["days"]}
                  for t in found.get("trips") or []],
        "people_most_in_photos": [p["name"] for p in people.get("top") or []],
        "new_to_the_family_photos": [p["name"] for p in people.get("new") or []],
        "always_together": [p["name"] for p in (people.get("together") or {}).get("people", [])],
        "places": [p["place"] for p in places.get("top") or []],
        "countries": places.get("countries") or [],
        "furthest_from_home": (places.get("furthest") or {}).get("place"),
        "voice_stories_told": stories.get("count") or 0,
    }
    return {k: v for k, v in out.items() if v not in (None, [], "")}


def model_messages(found: dict[str, Any], *, language: str, household: str = "") -> list[dict[str, str]]:
    """The writing model's instructions for the year's opening words."""
    tongue = "Tamil (தமிழ்)" if language == "ta" else "English"
    system = (
        f"You are Ninaivu, the {household + ' ' if household else ''}family's photo library, "
        "running on their own computer. Write the opening of the family's year in review: "
        f"three or four short, warm sentences in {tongue}, the way a loving relative would sum "
        "up the year looking through the album. Use ONLY the facts given; never invent events, "
        "feelings, names, places or numbers. Do not list every fact; choose the ones that make "
        "the year feel like theirs. No headings, no lists, no emoji.")
    user = "Facts (JSON):\n" + json.dumps(facts(found), ensure_ascii=False, separators=(",", ":"))
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def cleaned(text: str, limit: int = 900) -> str:
    """The model's words, one paragraph, without markdown, cut at a sentence."""
    text = " ".join(str(text or "").replace("*", "").replace("#", "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("। "), cut.rfind("! "))
    return cut[:end + 1] if end > 0 else cut.rsplit(" ", 1)[0] + "…"
