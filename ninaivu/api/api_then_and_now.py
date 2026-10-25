"""Then and now in the family app (media/then_and_now.py, static/js/then-and-now.js).

Off until ``then_and_now`` is switched on (Settings → Advanced), when every
route answers 404 as if it did not exist.

Every photograph is found through ``db.query_assets`` with the viewer's own
limits, the check every gallery listing goes through, so a hidden photograph,
a folder somebody is kept out of, or another person's "Only to me" items are
never paired. Screenshots and documents are left out. A guest, who is never
told who is in a photograph or where it was taken, is given the same-place
pairs without the place's name, and no people.

``GET /api/then-and-now`` is every pair: ``places`` (the same place or view
years apart), ``people`` (each named person early and late) and ``same_age``
(a parent and their child at the same age), with the people of the family
tree whose year of birth is not known yet (``ask_born``).
``GET /api/then-and-now/today`` is the pairs whose older photograph was taken
this week of its year, for the strip above the gallery.
``PUT /api/then-and-now/born/<person>`` with ``{"year": 1968}`` (or null)
keeps the year somebody was born.
``POST /api/then-and-now/film`` makes a memory film of chosen pairs, side by
side (needs ``memory_films`` on too).
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from flask import abort, jsonify, make_response

from ..media import books as book_maker, films as maker, then_and_now as pairing
from ..server import features
from ..server.auth import current_user, require_family
from ..storage import db, family_tree, films as film_store, then_and_now as born_store
from ..words import Said, refusal
from ._body import json_object
from .api import _cfg, _conn, _roots, _safe_under, _thumb_version, _viewer, _viewer_limits, bp

log = logging.getLogger(__name__)

#: What is read of every photograph the viewer may see.
COLUMNS = ("id", "kind", "date_key", "captured_at", "mtime", "gps_lat", "gps_lon", "city",
           "country", "dup_group", "sharpness", "width", "height", "quality", "indexed_at",
           "rotation")
#: The most photographs looked through for the same-place pairs; past it, an
#: even share of the library (far beyond a family's: 208,000 is the largest).
MAX_ROWS = 400000
#: Named people paired with themselves (those in the most photographs), and
#: the most photographs of one person looked through.
MAX_PEOPLE = 24
PERSON_ROWS = 3000
#: Pairs on the strip above the gallery.
TODAY_PAIRS = 8
#: Pairs in one film.
MAX_FILM_PAIRS = 24

_working = threading.Lock()


def _switched_on() -> None:
    if not getattr(_cfg(), "then_and_now", False):
        abort(404)


def _query(**filters: Any) -> list[dict[str, Any]]:
    rows, _ = db.query_assets(_conn(), _roots(), kinds=["picture"], sort="date_asc",
                              offset=0, columns=COLUMNS, with_total=False, **_viewer(),
                              **filters)
    return rows


def _screens() -> set[int]:
    clause, params = db.screen_clause(_conn(), "a")
    return {int(r[0]) for r in _conn().execute(f"SELECT a.id FROM assets a WHERE {clause}",
                                               params)}


def _ranked(rows: list[dict[str, Any]], screens: set[int]) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        if int(row["id"]) in screens:
            continue
        row["rank"] = book_maker.score(row)
        out.append(row)
    return out


def _shown(row: dict[str, Any]) -> dict[str, Any]:
    out = {"id": int(row["id"]), "thumb_v": _thumb_version(row), "date": row.get("date_key") or "",
           "width": row.get("width"), "height": row.get("height")}
    if row.get("eyes"):
        out["eyes"] = row["eyes"]
    return out


def _told(pair: dict[str, Any], guest: bool) -> dict[str, Any]:
    out = {k: v for k, v in pair.items() if k not in ("then", "now")}
    out["then"], out["now"] = _shown(pair["then"]), _shown(pair["now"])
    if guest:
        out.pop("place", None)
    return out


def _person_rows(person_id: int, screens: set[int]) -> list[dict[str, Any]]:
    rows = _ranked(_query(person=person_id, limit=MAX_ROWS), screens)
    if len(rows) > PERSON_ROWS:
        step = len(rows) / PERSON_ROWS
        rows = [rows[int(i * step)] for i in range(PERSON_ROWS)]
    return rows


def _find(guest: bool) -> dict[str, Any]:
    """Every pair this viewer may be shown (worked out once, then kept)."""
    from .api_films import _person_faces                       # noqa: PLC0415

    conn = _conn()
    screens = _screens()
    rows = _ranked(_query(limit=MAX_ROWS), screens)
    store = db.embedding_store(conn)
    places = pairing.place_pairs(rows, store)
    taken = {int(p[side]["id"]) for p in places for side in ("then", "now")}
    views = pairing.view_pairs(rows, store, taken=taken)
    del rows
    found: dict[str, Any] = {
        "places": [_told(p, guest) for p in sorted(
            places + views, key=lambda p: (-p["years"], int(p["then"]["id"])))[:pairing.MAX_PAIRS]],
        "people": [], "same_age": [], "ask_born": []}
    if guest:
        return found

    people = {int(p["id"]): p for p in db.list_people(conn, _roots(), **_viewer_limits())
              if p.get("name")}
    kept: dict[int, tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]] = {}

    def of(pid: int) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
        if pid not in kept:
            rows = _person_rows(pid, screens)
            kept[pid] = (rows, _person_faces(pid, rows))
        return kept[pid]

    busiest = sorted(people.values(), key=lambda p: (-int(p.get("photo_count") or 0),
                                                     str(p["name"]).casefold()))
    for person in busiest[:MAX_PEOPLE]:
        rows, faces = of(int(person["id"]))
        pairs = pairing.person_pairs(rows, faces, person)
        if pairs:
            found["people"].append({"person": {"id": int(person["id"]), "name": person["name"],
                                               "face": person.get("cover_face_id")},
                                    "pairs": [_told(p, False) for p in pairs]})

    # Parent and child at the same age, for the family tree's parents.
    born = born_store.born(conn, people)
    asked: set[int] = set()
    for link in family_tree.relations(conn):
        a, b = int(link["person_a"]), int(link["person_b"])
        if link["kind"] != "parent" or a not in people or b not in people:
            continue
        missing = [p for p in (a, b) if p not in born]
        if missing:
            asked.update(missing)
            continue
        parent = {"id": a, "name": people[a]["name"], "born": born[a]}
        child = {"id": b, "name": people[b]["name"], "born": born[b]}
        pairs = pairing.same_age_pairs(parent, *of(a), child, *of(b))
        if pairs:
            found["same_age"].append({"parent": parent, "child": child,
                                      "pairs": [_told(p, False) for p in pairs]})
    found["ask_born"] = [{"id": pid, "name": people[pid]["name"],
                          "face": people[pid].get("cover_face_id")}
                         for pid in sorted(asked, key=lambda p: str(people[p]["name"]).casefold())]
    found["born"] = {str(pid): year for pid, year in born.items()}
    return found


def _pairs() -> dict[str, Any]:
    conn = _conn()
    viewer = _viewer()
    guest = bool(current_user().is_guest)
    key = ("then_and_now", int(viewer["viewer_id"] or 0), int(viewer["max_visibility"]),
           viewer["scope"] or "", tuple(_roots()), guest,
           db.visibility_clause("a", viewer["max_visibility"]),
           tuple(sorted(born_store.born(conn).items())),
           tuple((int(r["person_a"]), int(r["person_b"]), r["kind"])
                 for r in family_tree.relations(conn)))
    # One at a time: two people opening it at once would read the library twice.
    with _working:
        return db.cached_aggregate(
            conn, key, lambda: _find(guest),
            also=(db.FACES_GENERATION_KEY, db.EMBEDDINGS_GENERATION_KEY), max_age=3600)


@bp.get("/api/then-and-now")
def then_and_now_all():
    _switched_on()
    found = _pairs()
    return jsonify({**found, "films": features.on(_cfg(), "memory_films")
                    and not current_user().is_guest,
                    "min_years": pairing.MIN_YEARS})


def _today() -> tuple[int, int]:
    now = time.localtime()
    return now.tm_mon, now.tm_mday


@bp.get("/api/then-and-now/today")
def then_and_now_today():
    _switched_on()
    found = _pairs()
    every = [*found["places"], *(p for person in found["people"] for p in person["pairs"]),
             *(p for family in found["same_age"] for p in family["pairs"])]
    month, day = _today()
    return jsonify({"pairs": pairing.around_today(every, month, day)[:TODAY_PAIRS]})


@bp.put("/api/then-and-now/born/<int:person_id>")
@require_family
def then_and_now_born(person_id: int):
    """Keep the year somebody this viewer may see on the People page was born."""
    _switched_on()
    people = {int(p["id"]) for p in db.list_people(_conn(), _roots(), **_viewer_limits())}
    if person_id not in people:
        abort(404)
    year = json_object().get("year")
    this_year = time.localtime().tm_year
    if year is not None and (isinstance(year, bool) or not isinstance(year, int)
                             or not born_store.EARLIEST <= year <= this_year):
        abort(400, description="The year somebody was born has to be a year.")
    born_store.set_born(_conn(), person_id, year, current_user().id or None)
    return jsonify({"ok": True, "person_id": person_id, "year": year})


def _side(value: Any) -> tuple[int, int | None]:
    if not isinstance(value, dict):
        abort(400, description="Each pair is a then and a now.")
    asset, person = value.get("id"), value.get("person")
    if isinstance(asset, bool) or not isinstance(asset, int) or not 0 < asset < 2 ** 63:
        abort(400, description="Each pair is a then and a now.")
    if person is not None and (isinstance(person, bool) or not isinstance(person, int)
                               or not 0 < person < 2 ** 63):
        abort(400, description="Each pair is a then and a now.")
    return asset, person


@bp.post("/api/then-and-now/film")
@require_family
def then_and_now_film():
    """A memory film of the chosen pairs, side by side: a face held by the
    eyes where the pair is of a person, the whole view where it is of a place."""
    _switched_on()
    from . import api_films                                     # noqa: PLC0415
    if not features.on(_cfg(), "memory_films"):
        abort(404)
    data = json_object()
    raw = data.get("pairs")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_FILM_PAIRS:
        abort(400, description=Said("Choose 1 to {max_count} pairs.", max_count=MAX_FILM_PAIRS))
    sides = [(_side(p.get("then") if isinstance(p, dict) else None),
              _side(p.get("now") if isinstance(p, dict) else None)) for p in raw]
    size = data.get("size") or maker.DEFAULT_SIZE
    pace = data.get("pace") or "steady"
    look = data.get("look") or "natural"
    if size not in maker.SIZES or pace not in maker.PACES or look not in maker.LOOKS:
        abort(400, description="Unknown size, pace or look.")
    music_id, music_path = api_films._music(data.get("music"))  # noqa: SLF001

    # Only what this viewer may see, whatever ids were sent.
    wanted = sorted({a for pair in sides for a, _ in pair})
    found = {int(r["id"]): r for r in db.query_assets(
        _conn(), _roots(), kinds=["picture"], ids=wanted, limit=len(wanted), offset=0,
        with_total=False, **_viewer())[0]}
    people = {int(p["id"]) for p in db.list_people(_conn(), _roots(), **_viewer_limits())}
    faces: dict[int, dict[int, dict[str, Any]]] = {}

    def held(asset_id: int, person: int | None) -> dict[str, Any] | None:
        row = found.get(asset_id)
        if row is None:
            return None
        root = Path(row["root"]).resolve()
        path = (root / row["rel_path"]).resolve()
        if not _safe_under(path, root) or not path.is_file():
            return None
        eyes = None
        if person is not None and person in people:
            if person not in faces:
                faces[person] = api_films._person_faces(                # noqa: SLF001
                    person, [found[a] for a in wanted if a in found])
            eyes = (faces[person].get(asset_id) or {}).get("eyes")
        when = book_maker.when(row)
        label = str(pairing.year_of(row) or "")
        return api_films._held_item(row, path, eyes, label) if when else None  # noqa: SLF001

    items = []
    for (then_id, then_person), (now_id, now_person) in sides:
        then, now = held(then_id, then_person), held(now_id, now_person)
        if then and now:
            items.append({"id": then["id"], "kind": "pair", "then": then, "now": now})
    if not items:
        abort(409, description="The files of those moments cannot be reached.")
    title = api_films._text(data, "title", 120) or "Then and now"   # noqa: SLF001
    spec = {"title": title, "subtitle": api_films._text(data, "subtitle", 160),  # noqa: SLF001
            "closing": api_films._text(data, "closing", 120), "size": size,      # noqa: SLF001
            "subtitles": "none", "pace": pace, "look": look, "stories": False, "months": None,
            "items": items, "style": "then_and_now", "chapters": [],
            "music": {"path": music_path} if music_path else None}
    if maker.building():
        abort(409, description="A film is already being made. Try again when it is finished.")
    cfg = _cfg()
    if api_films._free_bytes(film_store.films_dir(cfg.state_dir)) < api_films.FILM_FREE_BYTES:  # noqa: SLF001
        abort(make_response(jsonify(refusal("There is not enough free space to make a film. "
                                            "Free at least 2 GB first.")), 507))
    conn = _conn()
    film_id = film_store.create(conn, owner_id=current_user().id, title=title,
                                subtitle=spec["subtitle"], source_kind="then_and_now",
                                source_id=None, size=size, subtitles="none", music_id=music_id,
                                item_ids=[n for i in items for n in (i["then"]["id"], i["now"]["id"])],
                                story_ids=[])
    try:
        maker.start(film_id, spec, db_path=cfg.db_path, state_dir=cfg.state_dir)
    except maker.Busy as exc:
        film_store.delete(conn, cfg.state_dir, film_store.get(conn, film_id))
        abort(409, description=str(exc))
    return jsonify(api_films._describe(film_store.get(conn, film_id))), 202  # noqa: SLF001
