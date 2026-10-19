"""The living photo frame: which photographs a frame on the wall shows next.

A tablet in the kitchen, or a television's browser, signed in as somebody in
the family, opens Ninaivu at ``/#frame`` and drifts through the library full
screen (static/js/frame.js). This decides what it drifts through, a dozen at a
time, and what it says about each one.

A frame that shows the library in date order shows last week for a month. One
that shows it at random shows a blurred burst frame, a receipt and the ninth
copy of the same sunset. What a frame on a wall is for is the photograph
nobody has looked at in fifteen years, so each batch is mixed from four pools:

* **This day in past years**, whenever the library has any: the strongest
  pull a photograph has is "fourteen years ago today".
* **Photographs somebody told a story about**: the family already said these
  matter, and the frame shows the first words of the story under them.
* **Favourites** of whoever the frame is signed in as.
* **Everything else**, shuffled with a seed the frame keeps, so one evening
  does not repeat itself.

Every candidate goes through the same judgement the weekly digest uses to pick
one photograph worth sending (``db._pick_score``): screenshots, documents,
blurred and badly exposed frames are refused outright, and one frame of each
burst is kept. Only pictures: a frame is for looking, not for sound.

Who sees what is never decided here. Every pool is narrowed by
``db.query_assets`` with the viewer's own limits (visibility, the folder they
are kept to, the dates they may see, their "Only to me" items), which is the
check every gallery listing goes through.
"""

from __future__ import annotations

import json
import random
import sqlite3
import time
from datetime import date
from typing import Any, Sequence

from ..storage import db

#: Photographs in one batch. The frame asks for the next batch before the
#: last one runs out; at a minute each this is a quarter of an hour.
BATCH = 12

#: The most one batch may ask for.
MAX_BATCH = 40

#: How many of each pool are read before scoring. Generous, because scoring
#: refuses a good share of a real library (screenshots, receipts, bursts).
POOL = {"day": 240, "stories": 120, "favourites": 120, "shuffle": 360}

#: Of a batch, at most this share is this day in past years: a day with four
#: hundred photographs of one wedding should not be the whole evening.
DAY_SHARE = 0.5

#: Of a batch, at most this share has a story.
STORY_SHARE = 0.25

#: Of a batch, at most this share is the viewer's favourites.
FAVOURITE_SHARE = 0.2

#: Columns read for every candidate: what scoring and the caption need.
_COLUMNS = ("id", "kind", "width", "height", "captured_at", "date_key", "city", "country",
            "sharpness", "brightness", "quality", "dup_group", "rotation", "thumb",
            "blurhash", "color", "caption", "is_live")


def _limits(viewer: dict[str, Any]) -> dict[str, Any]:
    return {"viewer_id": int(viewer.get("viewer_id") or 0),
            "max_visibility": int(viewer.get("max_visibility") or 0),
            "scope": viewer.get("scope") or None}


def _narrow(conn: sqlite3.Connection, roots: Sequence[str], ids: Sequence[int],
            viewer: dict[str, Any]) -> list[dict[str, Any]]:
    """The rows of *ids* this viewer may see, as the gallery would list them."""
    found: list[dict[str, Any]] = []
    ids = list(dict.fromkeys(int(i) for i in ids))
    for start in range(0, len(ids), 500):
        piece = ids[start:start + 500]
        rows, _ = db.query_assets(conn, roots, ids=piece, kinds=["picture"], limit=len(piece),
                                  columns=_COLUMNS, with_total=False, **_limits(viewer))
        found.extend(rows)
    return found


def _on_this_day_ids(conn: sqlite3.Connection, roots: Sequence[str], today: date,
                     limit: int) -> list[int]:
    """Pictures taken on this month and day in an earlier year. Narrowed to
    what the viewer may see afterwards, by :func:`_narrow`."""
    roots_sql, roots_params = db.roots_clause("a", roots)
    target = f"{today.month:02d}-{today.day:02d}"
    rows = conn.execute(
        f"SELECT a.id FROM assets a WHERE {roots_sql} AND a.kind = 'picture' "
        "AND a.trashed = 0 AND a.nsfw = 0 AND a.thumb IS NOT NULL AND a.thumb <> '' "
        f"AND {db._ON_DAY_SQL} AND {db._YEAR_SQL} < ? "        # noqa: SLF001
        "ORDER BY RANDOM() LIMIT ?",
        (*roots_params, f"%-{target}", target, str(today.year), int(limit))).fetchall()
    return [int(r[0]) for r in rows]


def _story_ids(conn: sqlite3.Connection, limit: int) -> list[int]:
    try:
        rows = conn.execute("SELECT DISTINCT asset_id FROM stories ORDER BY RANDOM() LIMIT ?",
                            (int(limit),)).fetchall()
    except sqlite3.OperationalError:
        return []                                       # an index from before stories
    return [int(r[0]) for r in rows]


def _year(item: dict[str, Any]) -> int | None:
    key = str(item.get("date_key") or "")
    if len(key) >= 4 and key[:4].isdigit():
        return int(key[:4])
    when = item.get("captured_at")
    if when and float(when) > 0:
        return time.localtime(float(when)).tm_year
    return None


def _score(item: dict[str, Any], faces: int, today: date) -> float | None:
    """The digest's judgement of one photograph (None refuses it), with a
    little more for a photograph that is somebody's favourite or has a story."""
    probe = dict(item)
    year = _year(item)
    probe["year"] = str(year) if year else ""
    probe["face_count"] = faces
    score = db._pick_score(probe)                       # noqa: SLF001
    if score is None:
        return None
    if item.get("favorite"):
        score += 1.0
    if year and year < today.year and str(item.get("date_key") or "")[5:10] == \
            f"{today.month:02d}-{today.day:02d}":
        score += 1.0
    return score


def _screen(item: dict[str, Any]) -> bool:
    """Whether the scan called this a screenshot, a document or a screen."""
    quality = item.get("quality")
    if isinstance(quality, str):
        try:
            quality = json.loads(quality)
        except ValueError:
            quality = []
    return bool({str(tag) for tag in (quality or [])} & {"screenshot", "document", "screen"})


def _faces(conn: sqlite3.Connection, ids: Sequence[int]) -> dict[int, list[dict[str, Any]]]:
    """Every face in each of these, with the name of whoever it is, if known."""
    found: dict[int, list[dict[str, Any]]] = {}
    ids = list(ids)
    for start in range(0, len(ids), 500):
        piece = ids[start:start + 500]
        marks = ",".join("?" * len(piece))
        try:
            rows = conn.execute(
                "SELECT f.asset_id, f.bbox, f.quality, p.name AS person_name "
                "FROM faces f LEFT JOIN people_clusters p ON p.id = f.person_id "
                f"WHERE f.asset_id IN ({marks}) AND f.source IN ('auto', 'confirmed', 'none')",
                piece).fetchall()
        except sqlite3.OperationalError:
            return {}
        for row in rows:
            found.setdefault(int(row["asset_id"]), []).append(dict(row))
    return found


def _focus(item: dict[str, Any], faces: list[dict[str, Any]]) -> list[float] | None:
    """Where the frame's slow zoom should drift towards: the middle of the
    faces, as fractions of the picture. None for the middle of the picture.

    A face's box is in the pixels of the picture as stored; a picture turned
    by hand afterwards has its boxes in the other orientation, so it is left
    to drift towards the middle rather than towards somebody's elbow."""
    width, height = int(item.get("width") or 0), int(item.get("height") or 0)
    if not faces or not width or not height or int(item.get("rotation") or 0) % 360:
        return None
    xs, ys, weights = 0.0, 0.0, 0.0
    for face in faces:
        try:
            x, y, w, h = (float(v) for v in json.loads(face["bbox"])[:4])
        except (TypeError, ValueError, KeyError):
            continue
        weight = max(1.0, w * h)
        xs += (x + w / 2) * weight
        ys += (y + h / 2) * weight
        weights += weight
    if not weights:
        return None
    fx, fy = xs / weights / width, ys / weights / height
    if not (0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0):
        return None
    return [round(fx, 3), round(fy, 3)]


def _first_story(conn: sqlite3.Connection, ids: Sequence[int]) -> dict[int, dict[str, Any]]:
    """The first story told about each of these: who told it, and what they
    said as far as anybody knows (the words typed beside it, or as the
    on-device engine wrote them down)."""
    from ..storage import story_words                   # noqa: PLC0415
    ids = list(ids)
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    try:
        rows = conn.execute(
            "SELECT id, asset_id, speaker, text, created_at FROM stories "
            f"WHERE asset_id IN ({marks}) ORDER BY created_at, id", ids).fetchall()
    except sqlite3.OperationalError:
        return {}
    first: dict[int, dict[str, Any]] = {}
    for row in rows:
        first.setdefault(int(row["asset_id"]), dict(row))
    words = story_words.for_stories(conn, [s["id"] for s in first.values()])
    told: dict[int, dict[str, Any]] = {}
    for asset_id, story in first.items():
        said = words.get(int(story["id"])) or {}
        told[asset_id] = {
            "speaker": story.get("speaker") or "",
            "text": _excerpt(story.get("text") or said.get("words") or ""),
            "english": _excerpt(said.get("english") or ""),
            "language": said.get("language") or "",
        }
    return told


def _excerpt(text: str, limit: int = 180) -> str:
    """The start of what was said, cut at a word where it is long."""
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return (cut or text[:limit]) + "…"


def _take(pool: list[dict[str, Any]], count: int, chosen: list[dict[str, Any]],
          seen: set[int], groups: set[str]) -> None:
    """Up to *count* from *pool*, best first, skipping anything already in
    the batch or another frame of a burst already in it."""
    taken = 0
    for item in pool:
        if taken >= count:
            return
        if int(item["id"]) in seen:
            continue
        group = item.get("dup_group") or ""
        if group and group in groups:
            continue
        chosen.append(item)
        seen.add(int(item["id"]))
        if group:
            groups.add(group)
        taken += 1


def batch(conn: sqlite3.Connection, roots: Sequence[str], viewer: dict[str, Any], *,
          count: int = BATCH, seed: int = 0, shown: Sequence[int] = (),
          today: date | None = None) -> dict[str, Any]:
    """The next *count* photographs for the frame, mixed and in the order to
    show them, each with what the frame says about it.

    *seed* fixes the shuffle for the evening; *shown* is what the frame has
    shown lately, which this batch avoids where the library is big enough.
    """
    today = today or date.today()
    count = max(1, min(int(count), MAX_BATCH))
    rng = random.Random(f"{seed}:{len(shown)}")
    avoid = {int(i) for i in shown}

    day = _narrow(conn, roots, _on_this_day_ids(conn, roots, today, POOL["day"]), viewer)
    told = _narrow(conn, roots, _story_ids(conn, POOL["stories"]), viewer)
    favourites: list[dict[str, Any]] = []
    if viewer.get("viewer_id"):
        favourites, _ = db.query_assets(
            conn, roots, kinds=["picture"], favorites=True, sort="random", limit=POOL["favourites"],
            columns=_COLUMNS, with_total=False, **_limits(viewer))
    shuffled, _ = db.query_assets(
        conn, roots, kinds=["picture"], sort="random", seed=int(seed) or None,
        offset=(len(shown) // count) * POOL["shuffle"] if seed else 0,
        limit=POOL["shuffle"], columns=_COLUMNS, with_total=False, **_limits(viewer))
    if seed and not shuffled:
        # Round the whole library: start the shuffle again from the top.
        shuffled, _ = db.query_assets(
            conn, roots, kinds=["picture"], sort="random", seed=int(seed), limit=POOL["shuffle"],
            columns=_COLUMNS, with_total=False, **_limits(viewer))

    every = {int(i["id"]): i for pool in (day, told, favourites, shuffled) for i in pool}
    faces = _faces(conn, list(every))

    def ranked(pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
        scored = []
        for item in pool:
            if not item.get("thumb"):
                continue
            score = _score(item, len(faces.get(int(item["id"]), [])), today)
            if score is None:
                continue
            # A little chance in the order, so the best photograph of a day
            # is not first on every batch, and fresh ones before repeats.
            fresh = 0.0 if int(item["id"]) in avoid else 3.0
            scored.append((score + fresh + rng.random() * 1.5, item))
        scored.sort(key=lambda pair: -pair[0])
        return [item for _, item in scored]

    chosen: list[dict[str, Any]] = []
    seen: set[int] = set()
    groups: set[str] = set()
    _take(ranked(day), max(1, int(count * DAY_SHARE)), chosen, seen, groups)
    _take(ranked(told), max(1, int(count * STORY_SHARE)), chosen, seen, groups)
    _take(ranked(favourites), max(1, int(count * FAVOURITE_SHARE)), chosen, seen, groups)
    _take(ranked(shuffled), count - len(chosen), chosen, seen, groups)
    if len(chosen) < count:
        # A small library: anything good enough, repeats allowed last.
        _take(ranked(list(every.values())), count - len(chosen), chosen, seen, groups)
    if not chosen:
        # Nothing passed: a library of soft scans, say. Better a soft
        # photograph on the wall than a blank one; never a screenshot.
        lenient = [i for i in every.values() if i.get("thumb") and not _screen(i)]
        rng.shuffle(lenient)
        _take(lenient, count, chosen, seen, groups)
    rng.shuffle(chosen)

    ids = [int(i["id"]) for i in chosen]
    stories = _first_story(conn, ids)
    return {
        "items": [_public(item, faces.get(int(item["id"]), []), stories.get(int(item["id"])), today)
                  for item in chosen],
        "today": today.isoformat(),
    }


def _public(item: dict[str, Any], faces: list[dict[str, Any]], story: dict[str, Any] | None,
            today: date) -> dict[str, Any]:
    """What the frame is told about one photograph."""
    asset_id = int(item["id"])
    year = _year(item)
    on_this_day = bool(year and year < today.year and
                       str(item.get("date_key") or "")[5:10] == f"{today.month:02d}-{today.day:02d}")
    names = []
    for face in sorted(faces, key=lambda f: -(f.get("quality") or 0)):
        name = (face.get("person_name") or "").strip()
        if name and name not in names:
            names.append(name)
    turned = int(item.get("rotation") or 0) % 180 == 90
    width, height = int(item.get("width") or 0), int(item.get("height") or 0)
    return {
        "id": asset_id,
        "src": f"/api/frame/picture/{asset_id}",
        "thumb": f"/api/thumb/{asset_id}?s=640",
        "width": height if turned else width,
        "height": width if turned else height,
        "color": item.get("color") or "",
        "date_key": item.get("date_key") or "",
        "captured_at": item.get("captured_at"),
        "year": year,
        "years_ago": (today.year - year) if year else None,
        "on_this_day": on_this_day,
        "place": ", ".join(p for p in (item.get("city"), item.get("country")) if p),
        "people": names[:4],
        "more_people": max(0, len(names) - 4),
        "focus": _focus(item, faces),
        "story": story,
        "favorite": bool(item.get("favorite")),
    }
