"""Finding the trips and festivals worth an album, from dates and places only.

The scan already cuts the library into occasions: runs of photographs with a
quiet gap between them (media/occasions.py). A trip is several of those away
from home, one after the other: three days in Ooty is a morning at the lake,
an afternoon in the tea gardens and a dinner, each an occasion of its own,
and nobody wants three albums. A festival is the few days around Deepavali or
Pongal in a year with photographs in them, as the festival albums find them
(features/festivals/media.py), the picture search confirming which days were
the festival where there is one.

Home is where most of the library was taken: the town with the most
photographs in its occasions. Occasions there, or with no place at all (a
camera without GPS), are not a trip.

Nothing here reads a pixel or writes anything; :mod:`.routes` asks it.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Sequence

from ...media import occasions as occasions_mod

DAY = 86400.0

#: Two occasions away from home this close together are one trip: a night's
#: sleep and a long drive.
TRIP_GAP = 36 * 3600.0
#: A trip is at least this many days ...
TRIP_MIN_DAYS = 2
#: ... and this many photographs.
TRIP_MIN_ITEMS = 10
#: A festival year needs this many photographs to be offered.
FESTIVAL_MIN_ITEMS = 6
#: A trip that ended less than this long ago may still be going on.
SETTLED = DAY


def home_place(occasions: Iterable[dict[str, Any]]) -> str | None:
    """The town most of the library was taken in, or None with no places."""
    counts: Counter[str] = Counter()
    for o in occasions:
        if o.get("place"):
            counts[str(o["place"])] += int(o.get("count") or 0)
    return counts.most_common(1)[0][0] if counts else None


def _trip_title(places: Sequence[str], started: float, ended: float) -> str:
    named = list(dict.fromkeys(places))
    where = named[0] if len(named) == 1 else (
        f"{named[0]} and {named[1]}" if len(named) == 2 else f"{named[0]}, {named[1]} and more")
    return f"{where}, {occasions_mod.date_range(started, ended)}"


def trips(occasions: Iterable[dict[str, Any]], *, now: float,
          home: str | None = None) -> list[dict[str, Any]]:
    """Runs of occasions away from home, oldest first, that are a trip.

    *occasions* are rows of the ``occasions`` table (``id``, ``key``,
    ``place``, ``started_at``, ``ended_at``, ``count``).
    """
    rows = sorted((dict(o) for o in occasions), key=lambda o: float(o.get("started_at") or 0))
    if home is None:
        home = home_place(rows)
    found: list[dict[str, Any]] = []
    run: list[dict[str, Any]] = []

    def close() -> None:
        if not run:
            return
        started = float(run[0]["started_at"])
        ended = max(float(o.get("ended_at") or o["started_at"]) for o in run)
        days = (occasions_mod._day(ended).date()                    # noqa: SLF001
                - occasions_mod._day(started).date()).days + 1      # noqa: SLF001
        count = sum(int(o.get("count") or 0) for o in run)
        if days >= TRIP_MIN_DAYS and count >= TRIP_MIN_ITEMS and ended < now - SETTLED:
            places = [str(o["place"]) for o in run]
            found.append({
                "key": f"trip-{run[0]['key']}", "kind": "trip",
                "title": _trip_title(places, started, ended),
                "places": list(dict.fromkeys(places)), "started_at": started, "ended_at": ended,
                "from": occasions_mod._day(started).date().isoformat(),    # noqa: SLF001
                "to": occasions_mod._day(ended).date().isoformat(),        # noqa: SLF001
                "days": days, "count": count, "occasions": [int(o["id"]) for o in run]})
        run.clear()

    for o in rows:
        place = o.get("place")
        if not place or place == home:
            close()
            continue
        if run and float(o["started_at"]) - max(
                float(r.get("ended_at") or r["started_at"]) for r in run) > TRIP_GAP:
            close()
        run.append(o)
    close()
    return found


def festivals(found: dict[str, Any]) -> list[dict[str, Any]]:
    """Each festival year with enough photographs, from the festival albums'
    answer (features/festivals/media.py ``albums``), oldest first."""
    out: list[dict[str, Any]] = []
    for festival in found.get("festivals", []):
        for year in festival.get("years", []):
            ids = [int(i) for i in year.get("ids", [])]
            if len(ids) < FESTIVAL_MIN_ITEMS:
                continue
            out.append({
                "key": f"festival-{festival['key']}-{year['year']}", "kind": "festival",
                "title": f"{festival['name']} {year['year']}", "festival": festival["key"],
                "year": int(year["year"]), "from": year.get("from"), "to": year.get("to"),
                "checked": bool(year.get("checked")), "count": len(ids), "ids": ids,
                "cover_ids": [int(p["id"]) for p in year.get("photos", [])[:4] if p.get("id")]})
    return out


def pending(candidates: Iterable[dict[str, Any]], decided: dict[str, Any]) -> list[dict[str, Any]]:
    """The candidates nobody has answered yet, newest first."""
    waiting = [c for c in candidates if c["key"] not in decided]
    return sorted(waiting, key=lambda c: str(c.get("from") or ""), reverse=True)
