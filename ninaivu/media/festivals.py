"""Festival albums: every Deepavali, every Pongal, side by side.

Nobody tags a photograph "Deepavali 1994", and nobody has to. The festival's
day each year is worked out from the Tamil calendar (media/festival_calendar.py),
the photographs of the few days around it are read from the index, and the
picture search confirms which of those days were the festival: lamps on the
doorstep, kolam, golu dolls on their steps, sugarcane and the Pongal pot.
A day with no such photograph (an office outing that fell on Deepavali) is
left out; a year where none of it was the festival has no page.

Each festival becomes one album that grows every year: each year a column
with its best photographs, the years side by side from the first to now
(static/js/festivals.js), and a book of all of them (api/api_books.py).

The picture search is a comparison, not a threshold: a photograph belongs to
the festival when it looks more like the festival's words than like any of a
few words for an ordinary photograph (:data:`ORDINARY`), so it works the same
for the CLIP and SigLIP models, whose scores sit on different scales. Without
the picture search, or before the photographs have been read by it, a year is
told from its dates alone, and says so.

Who sees what is never decided here: every photograph comes through
``db.query_assets`` with the viewer's own limits, the check every gallery
listing goes through, so hidden photographs, folders somebody is kept out of
and other people's "Only to me" items are never counted. Nothing names
anybody, so guests may open it too, through their own limits.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date
from typing import Any, Callable, Sequence

from ..storage import db
from . import festival_calendar as calendar

#: What each festival is called (English; the page says it in Tamil too), and
#: the words the picture search looks for in its photographs.
FESTIVALS: dict[str, dict[str, Any]] = {
    "pongal": {"name": "Pongal", "prompts": (
        "a clay pot of pongal rice boiling over", "sugarcane stalks at a festival",
        "a colourful kolam drawn on the ground", "decorated cows with painted horns",
        "a family cooking pongal outdoors over a fire")},
    "puthandu": {"name": "Tamil New Year", "prompts": (
        "a tray of fruits, flowers and a mirror for the new year",
        "a festive meal served on a banana leaf", "a kolam at a doorstep",
        "a family in new traditional clothes at a temple")},
    "krishna": {"name": "Krishna Jayanthi", "prompts": (
        "small footprints drawn in rice flour on the floor", "a baby Krishna idol with flowers",
        "a child dressed as Krishna with a peacock feather", "a pot of butter and sweets for a puja")},
    "vinayagar": {"name": "Vinayagar Chathurthi", "prompts": (
        "a Ganesha idol decorated with flowers", "a clay statue of an elephant-headed god",
        "modak sweets offered at a puja", "an umbrella over a small Ganesha idol")},
    "navaratri": {"name": "Navaratri", "prompts": (
        "golu dolls arranged on tiered steps", "a display of many small dolls on steps",
        "a decorated goddess idol for Navaratri", "books and musical instruments at a Saraswati puja",
        "women in silk saris visiting a golu")},
    "deepavali": {"name": "Deepavali", "prompts": (
        "small clay oil lamps glowing", "fireworks lighting up the night sky",
        "a child holding a sparkler", "a colourful rangoli with lamps",
        "a plate of Deepavali sweets", "a family in new clothes bursting crackers")},
    "karthigai": {"name": "Karthigai Deepam", "prompts": (
        "rows of lit clay lamps on a doorstep at night", "many small oil lamps glowing in the dark",
        "lamps around a kolam", "a hilltop beacon fire at night")},
    "christmas": {"name": "Christmas", "prompts": (
        "a decorated Christmas tree with lights", "Christmas decorations and a star",
        "Santa Claus", "Christmas presents under a tree", "a Christmas cake")},
}

#: Words for an ordinary photograph: a festival's photograph has to look more
#: like the festival than like any of these.
ORDINARY = (
    "an ordinary everyday photograph", "people standing and smiling for a photo",
    "a person at work in an office", "a street with cars", "a landscape with trees and sky",
    "a room in a house", "a screenshot of a phone screen", "a page of a document",
    "food on a plate in a restaurant", "a child playing at home",
)

#: The columns read for each photograph.
_COLUMNS = ("id", "kind", "width", "height", "captured_at", "date_key", "date_source",
            "sharpness", "brightness", "quality", "dup_group", "thumb", "indexed_at",
            "rotation", "duration", "is_live")

#: The most photographs read for one festival in one year.
MAX_YEAR_ITEMS = 800
#: Photographs shown in each year's column.
PHOTOS = 6
#: Without the picture search, the fewest photographs in a festival's days
#: before the year is offered on its dates alone: two snaps of a car park on
#: 31 October are not Deepavali.
MIN_BY_DATE = 4
#: A date the camera did not record (the file's own times) can be the day it
#: was copied, and is no evidence of a festival.
_GUESSED = {"mtime", "created", "none", ""}

#: A check that scores photographs for a festival: given the festival's key
#: and some asset ids, how much more each looks like the festival than like
#: an ordinary photograph (above 0 is the festival), for the ids that have
#: been read by the picture search; or None when there is no picture search.
Check = Callable[[str, Sequence[int]], "dict[int, float] | None"]


def _limits(viewer: dict[str, Any]) -> dict[str, Any]:
    return {"viewer_id": int(viewer.get("viewer_id") or 0),
            "max_visibility": int(viewer.get("max_visibility") or 0),
            "scope": viewer.get("scope") or None}


def _flags(row: dict[str, Any]) -> set[str]:
    quality = row.get("quality")
    if isinstance(quality, str):
        try:
            quality = json.loads(quality)
        except ValueError:
            quality = []
    return {str(t) for t in (quality or [])}


def _shown(row: dict[str, Any], version) -> dict[str, Any]:  # noqa: ANN001
    return {"id": int(row["id"]), "kind": row.get("kind") or "", "date": row.get("date_key") or "",
            "width": row.get("width"), "height": row.get("height"),
            "thumb_v": version(row) if version else ""}


def _best(rows: Sequence[dict[str, Any]], margins: dict[int, float], count: int
          ) -> list[dict[str, Any]]:
    """The best *count* pictures: the most festive first, weighed with the
    picking score every book and memory uses; no two from one burst."""
    scored = []
    for row in rows:
        if row.get("kind") != "picture" or not row.get("thumb"):
            continue
        score = db._pick_score(dict(row, year=str(row.get("date_key") or "")[:4]))  # noqa: SLF001
        if score is None:
            continue
        scored.append((score + margins.get(int(row["id"]), 0.0) * 40.0, row))
    scored.sort(key=lambda pair: (-pair[0], int(pair[1]["id"])))
    picked: list[dict[str, Any]] = []
    bursts: set[Any] = set()
    for _, row in scored:
        burst = row.get("dup_group")
        if burst and burst in bursts:
            continue
        picked.append(row)
        if burst:
            bursts.add(burst)
        if len(picked) >= count:
            break
    return picked


def _one_year(rows: list[dict[str, Any]], key: str, check: Check | None
              ) -> tuple[list[dict[str, Any]], dict[int, float], bool] | None:
    """The year's photographs of the festival, how festive each looked, and
    whether the picture search confirmed them; None when it was not the
    festival at all."""
    rows = [r for r in rows if str(r.get("date_source") or "") not in _GUESSED
            and not _flags(r) & {"screenshot", "document", "screen"}]
    if not rows:
        return None
    margins = check(key, [int(r["id"]) for r in rows]) if check is not None else None
    if not margins:
        # Nothing here has been read by the picture search (or there is none):
        # the dates alone, when there are enough photographs on them.
        return (rows, {}, False) if len(rows) >= MIN_BY_DATE else None
    festive_days = {str(r.get("date_key") or "") for r in rows if margins.get(int(r["id"]), -1.0) > 0}
    if not festive_days:
        return None
    # The whole of each day that had the festival in it: the family photograph
    # in new clothes belongs as much as the lamps behind it. Photographs the
    # picture search has not read yet join on their dates.
    kept = [r for r in rows if str(r.get("date_key") or "") in festive_days
            or int(r["id"]) not in margins]

    def order(row: dict[str, Any]) -> tuple[int, float, int]:
        margin = margins.get(int(row["id"]), -1.0)
        if margin > 0:                                 # the most festive first
            return 0, -margin, int(row["id"])
        return 1, float(row.get("captured_at") or 0), int(row["id"])
    kept.sort(key=order)
    return kept, margins, True


def albums(conn: sqlite3.Connection, roots: Sequence[str], viewer: dict[str, Any], *,
           check: Check | None = None, include_nsfw: bool = False, version=None,  # noqa: ANN001
           today: date | None = None) -> dict[str, Any]:
    """Every festival album this viewer may see, oldest year first.

    *check* is the picture search (see :data:`Check`); *version* gives a
    row's thumbnail version for its URL.
    """
    today = today or date.today()
    limits = _limits(viewer)
    # Only the years with photographs in them: a camera clock set to 1904
    # is one year to read, not a century.
    years_seen = sorted(y["year"] for y in db.picture_years(
        conn, roots, max_visibility=limits["max_visibility"], scope=limits["scope"])
        if 1826 <= y["year"] <= today.year)
    out: list[dict[str, Any]] = []
    checked_any = False
    for key in calendar.FESTIVALS:
        years = []
        for year in years_seen:
            day, first, last = calendar.days(key, year)
            if first > today:
                continue
            rows, _ = db.query_assets(
                conn, roots, kinds=["picture", "video"], sort="date_asc",
                date_from=first.isoformat(), date_to=last.isoformat(),
                limit=MAX_YEAR_ITEMS, columns=_COLUMNS, with_total=False,
                include_nsfw=include_nsfw, **limits)
            found = _one_year(rows, key, check)
            if found is None:
                continue
            kept, margins, checked = found
            checked_any = checked_any or checked
            best = _best(kept, margins, PHOTOS)
            years.append({
                "year": year, "day": day.isoformat(),
                "from": first.isoformat(), "to": last.isoformat(),
                "items": len(kept),
                "pictures": sum(1 for r in kept if r.get("kind") == "picture"),
                "videos": sum(1 for r in kept if r.get("kind") == "video"),
                "checked": checked,
                "photos": [_shown(r, version) for r in best],
                "ids": [int(r["id"]) for r in kept],
            })
        upcoming = calendar.days(key, today.year)[0]
        if upcoming < today:
            upcoming = calendar.days(key, today.year + 1)[0]
        cover = None
        for entry in reversed(years):
            if entry["photos"]:
                cover = entry["photos"][0]
                break
        out.append({"key": key, "index": index_of(key), "name": FESTIVALS[key]["name"], "years": years,
                    "first": years[0]["year"] if years else None,
                    "last": years[-1]["year"] if years else None,
                    "items": sum(y["items"] for y in years),
                    "cover": cover, "next": upcoming.isoformat()})
    return {"festivals": out, "checked": checked_any, "today": today.isoformat()}


def index_of(key: str) -> int:
    """The festival's number, from 1: what a book of it is asked for by."""
    return calendar.FESTIVALS.index(key) + 1


def key_of(index: int) -> str | None:
    """The festival numbered *index* (see :func:`index_of`), or None."""
    return calendar.FESTIVALS[index - 1] if 1 <= index <= len(calendar.FESTIVALS) else None


def summary(found: dict[str, Any]) -> dict[str, Any]:
    """The albums without their photographs' ids, for the list of festivals."""
    return {**found, "festivals": [
        {**f, "years": [{k: v for k, v in y.items() if k not in ("ids", "photos")}
                        for y in f["years"]]} for f in found["festivals"]]}


def ids_of(found: dict[str, Any], key: str) -> list[int]:
    """Every photograph of the festival *key*, oldest year first."""
    for festival in found["festivals"]:
        if festival["key"] == key:
            return [i for y in festival["years"] for i in y["ids"]]
    return []
