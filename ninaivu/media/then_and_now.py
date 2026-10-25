"""Then and now: the same place, the same person, years apart.

Three kinds of pair, each an older photograph and a newer one:

* **The same place** (``place``): two photographs taken within a stone's
  throw of each other (the same small square of the map) at least
  :data:`MIN_YEARS` apart, that also *look* like the same view to the
  gallery's own picture search (SigLIP 2 or CLIP, the vectors every
  photograph already has): the temple tank in 1994 and last Pongal, the
  doorstep every Deepavali. The map alone would pair any two photographs
  taken at home; the look alone would pair any two sunsets.
* **The same view** (``view``): an old print scanned without a place, paired
  with a newer photograph that looks like the very same view, more strictly
  (:data:`VIEW_MATCH`), since nothing else says it is the same spot.
* **The same person** (``person``): a named person's early photographs
  against their late ones, the best faces of each
  (:func:`ninaivu.media.films.pick_then_and_now`), and **parent and child at
  the same age** (``same_age``): Amma at six beside Arun at six, from the
  family tree and the years they were born (storage/then_and_now.py).

Nothing here reads a pixel or calls a model: it compares vectors already in
memory (``db.embedding_store``) and dates and places already in the index, a
few seconds over a large library, remembered until the library changes. Which
photographs a viewer may see is decided by the caller, which hands only those
rows in (api/api_then_and_now.py).

The thresholds are a first guess from CLIP and SigLIP 2's behaviour on
photographs of the same scene (around 0.8 to 0.9) and of different scenes of
the same kind (around 0.5 to 0.7); they are worth tuning on the real library.
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Sequence

try:
    import numpy as np
except ImportError:                                          # pragma: no cover
    np = None  # type: ignore[assignment]

from . import films

#: The fewest years between the two photographs of a pair.
MIN_YEARS = 5
#: The side of a map square that counts as "the same place", in degrees of
#: latitude (about 170 metres); longitude is widened by the latitude.
CELL_DEGREES = 0.0015
#: How alike two photographs of the same map square must look.
PLACE_MATCH = 0.80
#: How alike two photographs must look to be paired with no place to go on.
VIEW_MATCH = 0.90
#: Above this, the two are the same photograph (a scan of a print, a copy).
SAME_PHOTO = 0.985
#: Two pairs whose photographs look at least this alike are the same pair
#: again (twenty pairs of the same beach); only the best is kept.
REPEAT = 0.93
#: The most photographs of one map square, and of one year without a
#: place, compared: the best of each year, shared out across the years.
PER_CELL = 160
PER_YEAR = 120
#: The most pairs of one map square (home has many good ones).
PER_PLACE = 2
#: The most pairs of each kind told.
MAX_PAIRS = 48
#: Parent and child at the same age: at most this many ages, and the oldest
#: age compared.
SAME_AGE_PAIRS = 6
OLDEST_AGE = 80
#: Without any search vectors, two photographs this close (km) and years
#: apart are paired by the map alone, the best of the first and last years.
MAP_ONLY_KM = 0.06


def year_of(row: dict[str, Any]) -> int:
    try:
        year = int(str(row.get("date_key") or "")[:4])
    except ValueError:
        return 0
    return year if 1826 <= year <= 2200 else 0


def _rank(row: dict[str, Any]) -> float:
    """How good a photograph is, as photo books judge it: worked out the first
    time it is asked for and kept on the row as ``rank``. Only the
    photographs of a square of the map, or of a person, are ever compared;
    scoring every photograph of the library first was a second of the first
    visit to Then and now."""
    if "rank" in row:
        return float(row["rank"])
    from . import books                                        # noqa: PLC0415
    row["rank"] = books.score(row)
    return row["rank"]


def _spread(rows: Sequence[dict[str, Any]], most: int) -> list[dict[str, Any]]:
    """At most *most* of *rows*: round by round, the best not yet taken of
    every year, so a year with a thousand photographs does not crowd out the
    year with three."""
    years: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        years.setdefault(year_of(row), []).append(row)
    for found in years.values():
        found.sort(key=lambda r: (-_rank(r), int(r["id"])))
    out: list[dict[str, Any]] = []
    while len(out) < most and any(years.values()):
        for year in sorted(years):
            if years[year] and len(out) < most:
                out.append(years[year].pop(0))
    return out


def _matrix(rows: Sequence[dict[str, Any]], store: tuple) -> tuple[list[dict[str, Any]], Any]:
    """The rows that have a search vector, and their vectors as unit rows."""
    ids, buffer, dim, index = store
    kept = [r for r in rows if int(r["id"]) in index]
    if not kept or not dim or np is None:
        return [], None
    whole = np.frombuffer(buffer, dtype=np.float32).reshape(-1, dim)
    vectors = whole[[index[int(r["id"])] for r in kept]].astype(np.float32, copy=True)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return kept, vectors / norms


def _cell(row: dict[str, Any]) -> tuple[int, int] | None:
    lat, lon = row.get("gps_lat"), row.get("gps_lon")
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lat) and math.isfinite(lon)) or (lat == 0 and lon == 0):
        return None
    wide = CELL_DEGREES / max(0.2, math.cos(math.radians(lat)))
    return int(math.floor(lat / CELL_DEGREES)), int(math.floor(lon / wide))


def _place_name(row: dict[str, Any]) -> str:
    return ", ".join(str(p) for p in (row.get("city"), row.get("country")) if p)


def _pair(kind: str, then: dict[str, Any], now: dict[str, Any], score: float,
          **more: Any) -> dict[str, Any]:
    return {"kind": kind, "then": then, "now": now, "years": year_of(now) - year_of(then),
            "score": round(float(score), 4), **more}


def _best_pairs(then_rows: Sequence[dict[str, Any]], then_v: Any,
                now_rows: Sequence[dict[str, Any]], now_v: Any, *, floor: float,
                min_years: int, most: int, distinct_years: bool = False) -> list[tuple[int, int, float]]:
    """The best (then, now) index pairs, by how alike they look plus a little
    for every year between them, each photograph used once, none a repeat of
    a pair already taken."""
    then_years = np.array([year_of(r) for r in then_rows])
    now_years = np.array([year_of(r) for r in now_rows])
    then_dup = [r.get("dup_group") for r in then_rows]
    now_dup = [r.get("dup_group") for r in now_rows]
    candidates: list[tuple[float, int, int, float]] = []
    step = 1024
    for start in range(0, len(then_rows), step):
        sims = then_v[start:start + step] @ now_v.T
        gap = now_years[None, :] - then_years[start:start + step, None]
        sims = np.where((gap >= min_years) & (sims < SAME_PHOTO), sims, -1.0)
        best = np.argmax(sims, axis=1)
        for k, j in enumerate(best):
            sim = float(sims[k, j])
            i = start + k
            if sim < floor:
                continue
            if then_dup[i] is not None and then_dup[i] == now_dup[j]:
                continue
            years = int(gap[k, j])
            candidates.append((sim + 0.004 * min(years, 30), i, int(j), sim))
    candidates.sort(key=lambda c: (-c[0], c[1], c[2]))
    taken: list[tuple[int, int, float]] = []
    used_then: set[int] = set()
    used_now: set[int] = set()
    used_years: set[int] = set()
    for _, i, j, sim in candidates:
        if len(taken) >= most:
            break
        if i in used_then or j in used_now:
            continue
        if distinct_years and (then_years[i] in used_years or now_years[j] in used_years):
            continue
        if any(float(then_v[i] @ then_v[a]) >= REPEAT or float(now_v[j] @ now_v[b]) >= REPEAT
               for a, b, _ in taken):
            continue
        taken.append((i, j, sim))
        used_then.add(i)
        used_now.add(j)
        used_years.update((int(then_years[i]), int(now_years[j])))
    return taken


def place_pairs(rows: Sequence[dict[str, Any]], store: tuple, *,
                min_years: int = MIN_YEARS, most: int = MAX_PAIRS) -> list[dict[str, Any]]:
    """The same place years apart: pairs of photographs from one small
    square of the map, at least *min_years* apart, that look alike.

    *rows* are the photographs the viewer may see (``id``, ``date_key``,
    ``gps_lat``, ``gps_lon``, ``city``, ``country``, ``dup_group`` and what
    :func:`ninaivu.media.books.score` reads); *store* is
    ``db.embedding_store``'s answer."""
    cells: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for row in rows:
        cell = _cell(row) if year_of(row) else None
        if cell is not None:
            cells.setdefault(cell, []).append(row)
    have_vectors = bool(store and store[0])
    found: list[dict[str, Any]] = []
    used: set[int] = set()
    for cell in sorted(cells):
        # The square and the eight around it, so two photographs a few steps
        # apart across a square's edge are still the same place; each pair is
        # found once, from whichever square comes first.
        here = [r for dy in (-1, 0, 1) for dx in (-1, 0, 1)
                for r in cells.get((cell[0] + dy, cell[1] + dx), ())
                if int(r["id"]) not in used]
        years = {year_of(r) for r in here}
        if not here or max(years) - min(years) < min_years \
                or not any(int(r["id"]) not in used for r in cells[cell]):
            continue
        pool = _spread(here, PER_CELL)
        if not have_vectors:
            made = _map_only(pool, min_years)
        else:
            kept, vectors = _matrix(pool, store)
            if len(kept) < 2:
                continue
            made = [_pair("place", kept[i], kept[j], sim,
                          place=_place_name(kept[j]) or _place_name(kept[i]))
                    for i, j, sim in _best_pairs(kept, vectors, kept, vectors, floor=PLACE_MATCH,
                                                 min_years=min_years, most=PER_PLACE,
                                                 distinct_years=True)]
        for pair in made:
            used.update((int(pair["then"]["id"]), int(pair["now"]["id"])))
        found += made
    found.sort(key=lambda p: (-p["score"] - 0.004 * min(p["years"], 30), int(p["then"]["id"])))
    return found[:most]


def _map_only(pool: Sequence[dict[str, Any]], min_years: int) -> list[dict[str, Any]]:
    """With no search vectors at all: the best photograph of the first year
    and of the last, when they are within :data:`MAP_ONLY_KM` of each other."""
    from . import occasions                                    # noqa: PLC0415
    first = min(year_of(r) for r in pool)
    last = max(year_of(r) for r in pool)
    early = sorted((r for r in pool if year_of(r) == first), key=lambda r: -_rank(r))
    late = sorted((r for r in pool if year_of(r) == last), key=lambda r: -_rank(r))
    for then in early[:5]:
        for now in late[:5]:
            km = occasions.haversine_km(float(then["gps_lat"]), float(then["gps_lon"]),
                                        float(now["gps_lat"]), float(now["gps_lon"]))
            if km <= MAP_ONLY_KM and last - first >= min_years:
                return [_pair("place", then, now, 0.0,
                              place=_place_name(now) or _place_name(then))]
    return []


def view_pairs(rows: Sequence[dict[str, Any]], store: tuple, *,
               min_years: int = MIN_YEARS, most: int = MAX_PAIRS,
               taken: Iterable[int] = ()) -> list[dict[str, Any]]:
    """The same view years apart with no place to go on: an older photograph
    without a place (a scanned print, an old camera) and a newer one, with or
    without a place, that look like the very same view."""
    if not store or not store[0] or np is None:
        return []
    skip = {int(i) for i in taken}
    dated = [r for r in rows if year_of(r) and int(r["id"]) not in skip]
    placeless = [r for r in dated if _cell(r) is None]
    if not placeless:
        return []
    then_rows, then_v = _matrix(_spread(placeless, PER_YEAR * 8), store)
    by_year: dict[int, list[dict[str, Any]]] = {}
    for row in dated:
        by_year.setdefault(year_of(row), []).append(row)
    pool = [r for year in sorted(by_year) for r in _spread(by_year[year], PER_YEAR)]
    now_rows, now_v = _matrix(pool, store)
    if not then_rows or not now_rows:
        return []
    found = []
    for i, j, sim in _best_pairs(then_rows, then_v, now_rows, now_v, floor=VIEW_MATCH,
                                 min_years=min_years, most=most):
        then, now = then_rows[i], now_rows[j]
        found.append(_pair("view", then, now, sim, place=_place_name(now)))
    return found


def person_pairs(rows: Sequence[dict[str, Any]], faces: dict[int, dict[str, Any]],
                 person: dict[str, Any], *, most: int = films.PAIRS) -> list[dict[str, Any]]:
    """One person years apart: their early photographs beside their late ones."""
    out = []
    for then, now in films.pick_then_and_now(rows, faces, most):
        out.append(_pair("person", _with_eyes(then, faces), _with_eyes(now, faces),
                         (then["face_score"] + now["face_score"]) / 2,
                         person={"id": int(person["id"]), "name": person["name"]}))
    return out


def _with_eyes(row: dict[str, Any], faces: dict[int, dict[str, Any]]) -> dict[str, Any]:
    face = faces.get(int(row["id"])) or {}
    return {**row, "eyes": face.get("eyes")}


def same_age_pairs(parent: dict[str, Any], parent_rows: Sequence[dict[str, Any]],
                   parent_faces: dict[int, dict[str, Any]], child: dict[str, Any],
                   child_rows: Sequence[dict[str, Any]], child_faces: dict[int, dict[str, Any]],
                   *, most: int = SAME_AGE_PAIRS) -> list[dict[str, Any]]:
    """A parent and their child at the same age, years apart: for every age
    there is a good photograph of both at, the best face of each.

    *parent* and *child* are ``{"id", "name", "born"}``; the age in a
    photograph is its year less the year they were born, right to within one.
    """
    born_parent, born_child = int(parent["born"]), int(child["born"])
    if born_child - born_parent < 12:
        return []

    def by_age(rows: Sequence[dict[str, Any]], faces: dict[int, dict[str, Any]],
               born: int) -> dict[int, dict[str, Any]]:
        best: dict[int, dict[str, Any]] = {}
        for item in films._held(rows, faces):                 # noqa: SLF001
            year = year_of(item)
            age = year - born if year else -1
            if 0 <= age <= OLDEST_AGE and (age not in best
                                           or item["face_score"] > best[age]["face_score"]):
                best[age] = item
        return best

    old = by_age(parent_rows, parent_faces, born_parent)
    young = by_age(child_rows, child_faces, born_child)
    ages = sorted(set(old) & set(young))
    if len(ages) > most:
        # Spread across the ages there are: a baby, a child, a grown-up.
        ages = [ages[round(k * (len(ages) - 1) / (most - 1))] for k in range(most)] if most > 1 \
            else ages[:1]
        ages = sorted(set(ages))
    out = []
    for age in ages:
        then, now = old[age], young[age]
        out.append(_pair("same_age", _with_eyes(then, parent_faces), _with_eyes(now, child_faces),
                         (then["face_score"] + now["face_score"]) / 2, age=age,
                         parent={"id": int(parent["id"]), "name": parent["name"]},
                         child={"id": int(child["id"]), "name": child["name"]}))
    return out


def around_today(pairs: Iterable[dict[str, Any]], month: int, day: int,
                 days: int = 3) -> list[dict[str, Any]]:
    """The pairs whose older photograph was taken within *days* of this
    month and day in its year: "this week, thirty years ago"."""
    from datetime import date                                   # noqa: PLC0415

    out = []
    for pair in pairs:
        key = str(pair["then"].get("date_key") or pair["then"].get("date") or "")
        try:
            then = date(2000, int(key[5:7]), int(key[8:10]))
            today = date(2000, month, day)
        except ValueError:
            continue
        apart = abs((then - today).days)
        if min(apart, 366 - apart) <= days:
            out.append(pair)
    out.sort(key=lambda p: (-p["years"], int(p["then"]["id"])))
    return out
