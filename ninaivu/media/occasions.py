"""Auto-albums: the runs of photographs that were one occasion.

Nobody tags a holiday while they are on it. What a camera roll actually
records is bursts — a birthday, a weekend away, an afternoon in the garden —
separated by hours or days of nothing. That gap is the signal, and it needs no
model to read: sort by when the shutter went, and cut wherever the library
went quiet for long enough.

Location sharpens it where a camera recorded any. Two runs an hour apart in
the same town are one afternoon; two runs an hour apart four hundred
kilometres apart are the flight there and the arrival, and should not share a
title.

Everything here is a pure function over metadata. No pixels are read, nothing
is written, and the whole thing is cheap enough to rebuild from scratch after
every scan — which is what happens, because an occasion that gains a
photograph tomorrow should quietly grow rather than fork into a second album.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Sequence

from ..archive.dates import from_timestamp

#: Mean earth radius, kilometres.
_EARTH_KM = 6371.0

_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points, in kilometres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * _EARTH_KM * math.asin(min(1.0, math.sqrt(a)))


def _field(row: Any, name: str) -> Any:
    """One accessor for both shapes a caller might pass.

    Rows arrive either as ``sqlite3.Row`` straight off a query or as plain
    dicts from a test. ``Row`` has no ``.get()``, so neither shape can be
    assumed.
    """
    try:
        return row[name]
    except (KeyError, IndexError):
        return None


def _when(row: Any) -> float:
    """When the shutter went, falling back the way the rest of Ninaivu does."""
    return float(_field(row, "captured_at") or _field(row, "mtime") or 0.0)


def _coords(row: Any) -> tuple[float, float] | None:
    lat, lon = _field(row, "gps_lat"), _field(row, "gps_lon")
    if lat is None or lon is None:
        return None
    return float(lat), float(lon)


def split(rows: Sequence[Any], *, gap_seconds: float,
          radius_km: float = 0.0) -> list[list[Any]]:
    """Cut a time-ordered sequence wherever the occasion changed.

    ``rows`` must already be sorted oldest first. A run is broken by a silence
    longer than ``gap_seconds``, or — when both sides carry coordinates and
    ``radius_km`` is set — by a jump further than that.

    A photograph with no coordinates never breaks a run. Most libraries are
    mostly scans and old compacts; treating "no GPS" as "somewhere else" would
    shatter exactly the archives that most need grouping.
    """
    runs: list[list[Any]] = []
    current: list[Any] = []
    previous_when = 0.0
    previous_at: tuple[float, float] | None = None

    for row in rows:
        when = _when(row)
        here = _coords(row)
        if current:
            moved = (radius_km > 0 and here and previous_at
                     and haversine_km(*previous_at, *here) > radius_km)
            if when - previous_when > gap_seconds or moved:
                runs.append(current)
                current = []
        current.append(row)
        previous_when = when
        if here:
            previous_at = here

    if current:
        runs.append(current)
    return runs


def _day(ts: float) -> datetime:
    """The local day a capture time falls on.

    ``captured_at`` is a real instant made from the camera's local wall time
    (archive/dates.py ``to_timestamp``), so it is read back in local time too.
    Read in UTC, a photo taken at 2 a.m. in India was titled the day before.
    ``from_timestamp`` also copes with the years before 1970 Windows refuses.
    """
    return from_timestamp(ts)


def date_range(started: float, ended: float) -> str:
    """A date range a person would write down, not an ISO interval."""
    a, b = _day(started), _day(ended)
    if (a.year, a.month, a.day) == (b.year, b.month, b.day):
        return f"{a.day} {_MONTHS[a.month - 1]} {a.year}"
    if a.year != b.year:
        return (f"{a.day} {_MONTHS[a.month - 1]} {a.year} – "
                f"{b.day} {_MONTHS[b.month - 1]} {b.year}")
    if a.month != b.month:
        return (f"{a.day} {_MONTHS[a.month - 1]} – "
                f"{b.day} {_MONTHS[b.month - 1]} {b.year}")
    return f"{a.day}–{b.day} {_MONTHS[a.month - 1]} {a.year}"


def _place(run: Sequence[Any]) -> str | None:
    """The one place the whole run agrees on, or nothing.

    A single dissenting city means the run crossed somewhere, and a title that
    names only half a trip is worse than a title that names none of it.
    """
    seen = set()
    for row in run:
        city = _field(row, "city")
        if city:
            seen.add(str(city))
        if len(seen) > 1:
            return None
    return next(iter(seen)) if seen else None


def describe(run: Sequence[Any]) -> dict[str, Any]:
    """Everything about a run that is worth storing.

    The key is the first capture time, so an occasion keeps its identity
    across rebuilds — and an earlier photograph found later grows the same
    occasion rather than founding a rival one.
    """
    times = sorted(_when(row) for row in run)
    started, ended = times[0], times[-1]
    place = _place(run)
    dates = date_range(started, ended)
    return {
        "key": f"e{int(started)}",
        "title": f"{place}, {dates}" if place else dates,
        "place": place,
        "started_at": started,
        "ended_at": ended,
        "days": (_day(ended).date() - _day(started).date()).days + 1,
        "count": len(run),
    }


def group(rows: Sequence[Any], *, gap_seconds: float, radius_km: float = 0.0,
          min_items: int = 1) -> list[tuple[dict[str, Any], list[Any]]]:
    """Split, describe, and drop the runs too small to be an occasion.

    Two photographs of a parking sign are not a trip. ``min_items`` is what
    keeps the auto-album list something a person can actually read.
    """
    out = []
    for run in split(rows, gap_seconds=gap_seconds, radius_km=radius_km):
        if len(run) >= min_items:
            out.append((describe(run), run))
    return out
