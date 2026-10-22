"""A life in photographs: one person's life told decade by decade.

Open Paati on the People page and Ninaivu tells her life from what the
library already knows about it: "The 1960s, aged 8 to 17: Madurai, with
Thatha from 1968", the best photographs of each part, where she was, who was
beside her, who joined her photographs for the first time (a husband, a son,
a granddaughter), the trips, and the stories the family told about those
photographs, her own voice first. The page (static/js/life-story.js) turns
every decade into a chapter, in Tamil or English, and ends with a book and a
film of her.

Nothing here reads a pixel or calls a model: it is counting over the index,
remembered until faces, trips or places change (api/api_life_story.py). A
short title and a few sentences for each chapter are written by the
on-device writing model when it is downloaded (:func:`model_messages`), from
these facts only, and kept (storage/life_story.py); without it the page
writes its own from the same facts.

Who sees what is never decided here. The person's photographs are read
through ``db.query_assets`` with the viewer's own limits, the check every
gallery listing goes through, so a hidden photograph, a folder somebody is
kept out of, or another person's "Only to me" items never count, and the
people a chapter may name are only those the viewer may see on the People
page (*names*). A guest, who is never told who is in a photograph, is not
given a life story at all (the routes are for family).
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter
from datetime import date
from typing import Any, Iterable, Sequence

from ..storage import db

#: The fewest dated photographs and videos of somebody before their life is
#: offered as a story: nine photographs are an album, not a life.
MIN_ITEMS = 12

#: A decade with fewer photographs than this joins the next one (the last
#: joins the one before): a chapter of two photographs says nothing.
MIN_CHAPTER = 4

#: The most of a person's items read. Far beyond anybody's in a family
#: library (the most-photographed child in 208,000 photographs is in about
#: 14,000); past it the life is told from an even share and says so.
MAX_ITEMS = 40000

#: Per chapter: photographs shown, places, people beside them, trips and
#: stories named.
PHOTOS, PLACES, WITH, TRIPS, STORIES = 9, 4, 5, 3, 3

#: The fewest photographs together before somebody counts as having joined
#: the person's life in a decade (one wedding guest is not a new chapter).
MIN_TOGETHER = 3

#: The most items handed to the scorer per chapter.
POOL = 600

_COLUMNS = ("id", "kind", "width", "height", "captured_at", "date_key", "city", "country",
            "sharpness", "brightness", "quality", "dup_group", "thumb", "occasion_id",
            "indexed_at", "rotation", "duration", "is_live")


def _chunks(ids: Sequence[int], size: int = 900):
    for start in range(0, len(ids), size):
        yield list(ids[start:start + size])


def _limits(viewer: dict[str, Any]) -> dict[str, Any]:
    return {"viewer_id": int(viewer.get("viewer_id") or 0),
            "max_visibility": int(viewer.get("max_visibility") or 0),
            "scope": viewer.get("scope") or None}


def _year(row: dict[str, Any]) -> int | None:
    key = str(row.get("date_key") or "")
    if len(key) >= 4 and key[:4].isdigit() and 1826 <= int(key[:4]) <= 2200:
        return int(key[:4])
    return None


def people(conn: sqlite3.Connection, roots: Sequence[str], viewer: dict[str, Any],
           names: dict[int, str]) -> list[dict[str, Any]]:
    """Everybody whose life there is enough to tell, for the picker: the
    named people this viewer may see, with their first and last year in the
    photographs this viewer may open, most photographed first."""
    if not names:
        return []
    limits = _limits(viewer)
    guard, params = db._face_visibility_sql(                         # noqa: SLF001
        "a", limits["max_visibility"], limits["scope"], roots)
    found = []
    for piece in _chunks(list(names), 500):
        marks = ",".join("?" * len(piece))
        found.extend(conn.execute(
            "SELECT f.person_id, COUNT(DISTINCT a.id) AS n, MIN(substr(a.date_key, 1, 4)) AS first, "
            "MAX(substr(a.date_key, 1, 4)) AS last FROM faces f JOIN assets a ON a.id = f.asset_id "
            f"WHERE {guard} AND a.date_key != '' AND f.person_id IN ({marks}) "
            "GROUP BY f.person_id", (*params, *piece)).fetchall())
    out = []
    for row in found:
        if int(row["n"]) < MIN_ITEMS:
            continue
        pid = int(row["person_id"])
        out.append({"id": pid, "name": names[pid], "photos": int(row["n"]),
                    "first": int(row["first"]) if str(row["first"] or "").isdigit() else None,
                    "last": int(row["last"]) if str(row["last"] or "").isdigit() else None})
    out.sort(key=lambda p: (-p["photos"], p["name"].casefold()))
    return out


def _decades(rows: Sequence[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """The rows in decades, a thin decade joined to its neighbour."""
    by: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        year = _year(row)
        if year is not None:
            by.setdefault(year // 10 * 10, []).append(row)
    groups = [by[d] for d in sorted(by)]
    merged: list[list[dict[str, Any]]] = []
    carry: list[dict[str, Any]] = []
    for group in groups:
        carry = carry + group
        if len(carry) >= MIN_CHAPTER:
            merged.append(carry)
            carry = []
    if carry:
        if merged:
            merged[-1] = merged[-1] + carry
        else:
            merged.append(carry)
    return merged


def _faces(conn: sqlite3.Connection, ids: Sequence[int]) -> list[sqlite3.Row]:
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


def _favourites(conn: sqlite3.Connection, ids: Sequence[int]) -> Counter[int]:
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


def _stories(conn: sqlite3.Connection, ids: Sequence[int]) -> dict[int, list[dict[str, Any]]]:
    """Every story told about these photographs, oldest first, with its words
    as far as anybody knows them."""
    from ..storage import story_words                               # noqa: PLC0415
    from .frame import _excerpt                                     # noqa: PLC0415
    rows: list[dict[str, Any]] = []
    for piece in _chunks(ids):
        marks = ",".join("?" * len(piece))
        try:
            rows.extend(dict(r) for r in conn.execute(
                "SELECT id, asset_id, speaker, text, duration, created_at FROM stories "
                f"WHERE asset_id IN ({marks}) ORDER BY created_at, id", piece))
        except sqlite3.OperationalError:
            return {}
    words = story_words.for_stories(conn, [r["id"] for r in rows])
    told: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        said = words.get(int(row["id"])) or {}
        told.setdefault(int(row["asset_id"]), []).append({
            "id": int(row["id"]), "asset": int(row["asset_id"]),
            "speaker": row.get("speaker") or "",
            "text": _excerpt(row.get("text") or said.get("words") or "", 240),
            "english": _excerpt(said.get("english") or "", 240),
            "duration": row.get("duration")})
    return told


def _score(row: dict[str, Any], faces: int, hearts: int, theirs: float) -> float | None:
    probe = dict(row)
    probe["year"] = str(row.get("date_key") or "")[:4]
    probe["face_count"] = faces
    score = db._pick_score(probe)                                    # noqa: SLF001
    if score is None:
        return None
    # A photograph where the person's own face is clear beats one where they
    # are a blur at the back; few faces beat a crowd for somebody's life.
    return (score + min(3, hearts) * 1.0 + (0.5 if row.get("favorite") else 0.0)
            + theirs * 1.5 - max(0, faces - 4) * 0.15)


def _shown(row: dict[str, Any], version) -> dict[str, Any]:  # noqa: ANN001
    return {"id": int(row["id"]), "kind": row.get("kind") or "", "date": row.get("date_key") or "",
            "width": row.get("width"), "height": row.get("height"),
            "thumb_v": version(row) if version else ""}


def _best(rows: Sequence[dict[str, Any]], face_count: Counter[int], hearts: Counter[int],
          theirs: dict[int, float], count: int) -> list[tuple[float, dict[str, Any]]]:
    """The best *count* photographs of *rows*, no two from the same day or
    burst while the chapter has more days than that."""
    pool = [r for r in rows if r.get("kind") == "picture" and r.get("thumb")]
    step = max(1, len(pool) // POOL)
    scored = []
    for row in pool[::step]:
        aid = int(row["id"])
        score = _score(row, face_count.get(aid, 0), hearts.get(aid, 0), theirs.get(aid, 0.0))
        if score is not None:
            scored.append((score, row))
    scored.sort(key=lambda pair: (-pair[0], str(pair[1].get("date_key") or "")))
    picked: list[tuple[float, dict[str, Any]]] = []
    days: set[str] = set()
    bursts: set[Any] = set()
    for pair in scored:
        row = pair[1]
        day, burst = str(row.get("date_key") or "")[:10], row.get("dup_group")
        if day in days or (burst and burst in bursts):
            continue
        picked.append(pair)
        days.add(day)
        if burst:
            bursts.add(burst)
        if len(picked) >= count:
            break
    if len(picked) < count:                     # a chapter of a few days: fill from them
        seen = {int(p[1]["id"]) for p in picked}
        picked.extend(p for p in scored if int(p[1]["id"]) not in seen)
        picked = picked[:count]
    return picked


def _relation(pid: int, other: int, links: Iterable[dict[str, Any]]) -> str:
    """What *other* is to *pid* in the family tree: parent, child, spouse,
    grandparent, grandchild, sibling, or '' when the tree does not say."""
    parents: dict[int, set[int]] = {}
    spouses: set[tuple[int, int]] = set()
    for link in links:
        a, b = int(link["person_a"]), int(link["person_b"])
        if link["kind"] == "parent":
            parents.setdefault(b, set()).add(a)
        elif link["kind"] == "spouse":
            spouses.add((min(a, b), max(a, b)))
    if (min(pid, other), max(pid, other)) in spouses:
        return "spouse"
    if other in parents.get(pid, set()):
        return "parent"
    if pid in parents.get(other, set()):
        return "child"
    if any(other in parents.get(p, set()) for p in parents.get(pid, set())):
        return "grandparent"
    if any(pid in parents.get(c, set()) for c in parents.get(other, set())):
        return "grandchild"
    if parents.get(pid, set()) & parents.get(other, set()):
        return "sibling"
    return ""


def life(conn: sqlite3.Connection, roots: Sequence[str], viewer: dict[str, Any], person_id: int, *,
         names: dict[int, str], faces: dict[int, int | None] | None = None,
         born: dict[int, int] | None = None, links: Sequence[dict[str, Any]] = (),
         version=None, today: date | None = None) -> dict[str, Any] | None:  # noqa: ANN001
    """Everything the person's life story says, for *viewer*, or None when
    there is not enough of it.

    *names* are the named people this viewer may see, the only ones a chapter
    may name, and must hold *person_id*; *faces* their best face to show;
    *born* the year each was born where the family has said; *links* the
    family tree relations between people this viewer may see; *version*
    gives a row's thumbnail version for its URL.
    """
    if person_id not in names:
        return None
    today = today or date.today()
    faces = faces or {}
    born = born or {}
    rows, _ = db.query_assets(
        conn, roots, kinds=["picture", "video"], person=person_id, sort="date_asc",
        limit=MAX_ITEMS, columns=_COLUMNS, with_total=False, **_limits(viewer))
    dated = [r for r in rows if _year(r) is not None]
    if len(dated) < MIN_ITEMS:
        return None
    ids = [int(r["id"]) for r in dated]
    face_rows = _faces(conn, ids)
    face_count = Counter(int(f["asset_id"]) for f in face_rows)
    theirs: dict[int, float] = {}
    beside: dict[int, set[int]] = {}
    for face in face_rows:
        pid = face["person_id"]
        aid = int(face["asset_id"])
        if pid is None:
            continue
        if int(pid) == person_id:
            theirs[aid] = max(theirs.get(aid, 0.0), float(face["quality"] or 0))
        elif int(pid) in names:
            beside.setdefault(aid, set()).add(int(pid))
    hearts = _favourites(conn, ids)
    stories = _stories(conn, ids)
    trips_seen = {int(o["id"]): o for o in db.list_occasions(
        conn, roots, limit=5000, with_place=True, **_limits(viewer))}
    year_born = born.get(person_id)

    # The first photograph each other person shares with this one: where it
    # falls is the chapter they joined.
    together: Counter[int] = Counter()
    first_with: dict[int, str] = {}
    for row in dated:
        for other in beside.get(int(row["id"]), ()):
            together[other] += 1
            first_with.setdefault(other, str(row.get("date_key") or ""))
    joined_in: dict[int, int] = {}

    chapters: list[dict[str, Any]] = []
    groups = _decades(dated)
    cover: tuple[float, dict[str, Any]] | None = None
    for index, group in enumerate(groups):
        years = sorted({_year(r) for r in group if _year(r) is not None})
        start, end = years[0] // 10 * 10, years[-1] // 10 * 10 + 9
        group_ids = [int(r["id"]) for r in group]
        picks = _best(group, face_count, hearts, theirs, PHOTOS)
        if picks and (cover is None or picks[0][0] > cover[0]):
            cover = picks[0]
        places: Counter[str] = Counter()
        for row in group:
            place = ", ".join(p for p in (row.get("city"), row.get("country")) if p)
            if place:
                places[place] += 1
        with_them: Counter[int] = Counter()
        for aid in group_ids:
            for other in beside.get(aid, ()):
                with_them[other] += 1
        new_people = []
        for other in sorted(first_with, key=lambda o: first_with[o]):
            year = int(first_with[other][:4]) if first_with[other][:4].isdigit() else None
            if year is None or not start <= year <= end or other in joined_in:
                continue
            relation = _relation(person_id, other, links)
            if together[other] < MIN_TOGETHER and not relation:
                continue
            joined_in[other] = index
            new_people.append({"id": other, "name": names[other], "face": faces.get(other),
                               "first": first_with[other], "relation": relation,
                               "born": born.get(other)})
        trips: Counter[int] = Counter(int(r["occasion_id"]) for r in group
                                      if r.get("occasion_id") and int(r["occasion_id"]) in trips_seen)
        chapter_trips = []
        for tid, n in sorted(trips.items(), key=lambda kv: (-kv[1], kv[0]))[:TRIPS]:
            trip = trips_seen[tid]
            chapter_trips.append({"id": tid, "title": trip.get("title") or "",
                                  "place": trip.get("place") or "",
                                  "days": int(trip.get("days") or 1), "items": n,
                                  "cover": trip.get("cover_id"),
                                  "started_at": trip.get("started_at")})
        told = [s for aid in group_ids for s in stories.get(aid, [])]
        own = names[person_id].casefold()
        told.sort(key=lambda s: (s["speaker"].casefold() != own, not s["text"]))
        shown = {int(p[1]["id"]) for p in picks}
        story_photos = {s["asset"] for s in told[:STORIES]}
        lookup = {int(r["id"]): r for r in group}
        chapters.append({
            "start": start, "end": end,
            "first_year": years[0], "last_year": years[-1],
            "ages": ([max(0, years[0] - year_born), max(0, years[-1] - year_born)]
                     if year_born else None),
            "items": len(group),
            "pictures": sum(1 for r in group if r.get("kind") == "picture"),
            "videos": sum(1 for r in group if r.get("kind") == "video"),
            "years": len(years),
            "photos": [_shown(p[1], version) for p in picks],
            "story_photos": [_shown(lookup[a], version) for a in story_photos
                             if a not in shown and a in lookup],
            "places": [{"place": p, "items": n} for p, n in places.most_common(PLACES)],
            "with": [{"id": o, "name": names[o], "face": faces.get(o), "photos": n,
                      "relation": _relation(person_id, o, links)}
                     for o, n in sorted(with_them.items(),
                                        key=lambda kv: (-kv[1], names[kv[0]].casefold()))[:WITH]],
            "new": new_people[:WITH],
            "born": [{"id": o, "name": names[o], "year": born[o], "relation": rel}
                     for o in names if o != person_id and o in born
                     and start <= born[o] <= end
                     and (rel := _relation(person_id, o, links)) in ("child", "grandchild")],
            "trips": chapter_trips,
            "stories": told[:STORIES],
            "stories_count": len(told),
        })

    first = dated[0]
    return {
        "person": {"id": person_id, "name": names[person_id], "face": faces.get(person_id),
                   "born": year_born, "photos": len(dated),
                   "undated": len(rows) - len(dated),
                   "first_year": _year(first), "last_year": _year(dated[-1]),
                   "capped": len(rows) >= MAX_ITEMS},
        "first": _shown(first, version),
        "cover": _shown(cover[1], version) if cover else None,
        "chapters": chapters,
        "this_year": today.year,
    }


# -- the writing model --------------------------------------------------------------

_RELATION = {"spouse": "husband or wife", "parent": "parent", "child": "child",
             "grandparent": "grandparent", "grandchild": "grandchild", "sibling": "brother or sister"}


def facts(found: dict[str, Any]) -> dict[str, Any]:
    """What the writing model is told about the life: years, places, names
    and what the family said, never a file, a path or a photograph."""
    person = found["person"]

    def who(p: dict[str, Any]) -> str:
        rel = _RELATION.get(p.get("relation") or "")
        return f"{p['name']} ({rel})" if rel else p["name"]

    chapters = []
    for c in found["chapters"]:
        one: dict[str, Any] = {
            "years": f"{c['first_year']}-{c['last_year']}" if c["first_year"] != c["last_year"]
            else str(c["first_year"]),
            "decade": f"{c['start']}s",
            "age": f"{c['ages'][0]} to {c['ages'][1]}" if c.get("ages") else None,
            "photos": c["items"],
            "places": [p["place"] for p in c["places"]],
            "often_with": [who(p) for p in c["with"]],
            "first_photographed_together": [who(p) for p in c["new"]],
            "born_this_decade": [f"{p['name']} ({_RELATION.get(p['relation'], '')}, {p['year']})"
                                 for p in c["born"]],
            "trips": [{"place": t["place"] or t["title"], "days": t["days"]} for t in c["trips"]],
            "stories_told": [{"by": s["speaker"] or None, "said": s["english"] or s["text"]}
                             for s in c["stories"] if s["text"] or s["english"]],
        }
        chapters.append({k: v for k, v in one.items() if v not in (None, [], "")})
    out = {"name": person["name"], "born": person.get("born"), "chapters": chapters}
    return {k: v for k, v in out.items() if v not in (None, [], "")}


def fingerprint(found: dict[str, Any], language: str) -> str:
    """Which facts some words were written from: words are kept for exactly
    those facts, so one viewer is never shown words written from photographs
    only another may see."""
    raw = json.dumps([language, facts(found)], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def model_messages(found: dict[str, Any], *, language: str, household: str = "") -> list[dict[str, str]]:
    """The writing model's instructions for the chapters' titles and words."""
    tongue = "Tamil (தமிழ்)" if language == "ta" else "English"
    count = len(found["chapters"])
    system = (
        f"You are Ninaivu, the {household + ' ' if household else ''}family's photo library, "
        f"running on their own computer. Write the life of {found['person']['name']} in "
        f"photographs as {count} short chapters, one for each chapter in the facts, in the same "
        f"order, in {tongue}. For each chapter give a title of at most eight words (for example "
        "\"Madurai, and the first scooter\") and two or three warm, plain sentences, the way a "
        "loving grandchild would tell it while turning the album's pages. Begin with one opening "
        "sentence about the whole life. Use ONLY the facts given; never invent events, feelings, "
        "jobs, names, places, dates or numbers, and never say two people married unless the facts "
        "call them husband or wife. You may quote a few words the family said. No emoji, no lists. "
        'Answer with JSON only: {"opening": "...", "chapters": [{"title": "...", "text": "..."}]}')
    user = "Facts (JSON):\n" + json.dumps(facts(found), ensure_ascii=False, separators=(",", ":"))
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _clean(text: Any, limit: int) -> str:
    text = " ".join(str(text or "").replace("*", "").replace("#", "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("। "), cut.rfind("! "))
    return cut[:end + 1] if end > 0 else cut.rsplit(" ", 1)[0] + "…"


def parsed(reply: str, chapters: int) -> dict[str, Any] | None:
    """The model's answer as ``{"opening", "chapters": [{"title", "text"}]}``
    with one entry for every chapter, or None when it did not answer so."""
    text = str(reply or "")
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("chapters"), list):
        return None
    written = []
    for item in data["chapters"][:chapters]:
        if not isinstance(item, dict):
            return None
        written.append({"title": _clean(item.get("title"), 80), "text": _clean(item.get("text"), 700)})
    if len(written) != chapters or not any(w["text"] for w in written):
        return None
    return {"opening": _clean(data.get("opening"), 400), "chapters": written}
