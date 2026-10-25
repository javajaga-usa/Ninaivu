"""Young faces, older names in the family app (media/young_faces.py,
static/js/young-faces.js).

Off until ``young_faces`` is switched on (Settings → Advanced), when every
route answers 404 as if it did not exist. For family members and
administrators: a guest is never told who is in a photograph.

Everything is read with the viewer's own limits. The unnamed faces are those
in photographs this viewer may open (hidden ones and other people's "Only to
me" items never, explicit ones only for an administrator, who sees
everything); a person is described only by faces confirmed in photographs the
viewer may open, so "her earliest named photograph, 1975" never points at
one they cannot see; and only people the viewer may see on the People page
are suggested at all. Screenshots and documents are left out.

Saying yes names the face through :meth:`FaceIndexer.confirm`, the call the
console's review queue and the "Who is this?" answers make, so the person's
likeness is rebuilt from it. That widens who may name a face the way the
"Who is this?" review does (api_ask_family.py): a family member may name only
a face on a photograph they can open, only as somebody they can already see
on the People page, and only a face nobody has named. Saying no is kept as
"not this person" (``face_rejections``) and the face is not suggested as them
again. To ask a relative with no account, the page makes an ordinary "Who is
this?" link for the faces (api_ask_family.py), which shows the faces and
never the name Ninaivu thought of.

``GET /api/young-faces`` is the suggestions, grouped by person, with the
people whose year of birth would help (``ask_born``).
``POST /api/young-faces/<face>/yes`` and ``/no`` with ``{"person_id": 7}``.
``PUT /api/young-faces/born/<person>`` with ``{"year": 1954}`` (or null),
the same year Then and now and A life in photographs keep.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

import numpy as np
from flask import abort, jsonify

from ..media import facematch, faces as faces_mod, young_faces as guess
from ..media.faceindex import CLUSTER_MIN_QUALITY
from ..media.life_story import _relation
from ..server.auth import current_user, require_family
from ..storage import db, family_tree, then_and_now as born_store
from ._body import json_object
from .api import _cfg, _conn, _guard, _roots, _thumb_version, _viewer, _viewer_limits, bp

log = logging.getLogger(__name__)

#: Unnamed faces read at a time: a piece of the library's faces, never all of
#: their vectors at once (ArcFace's are 2 KB each), and each piece scored
#: against every person's young faces (up to 16 each) in one product.
CHUNK = 5000
#: The columns read of each suggested photograph.
COLUMNS = ("id", "kind", "date_key", "captured_at", "mtime", "width", "height", "rotation",
           "indexed_at")

_working = threading.Lock()


def _switched_on() -> None:
    if not getattr(_cfg(), "young_faces", False):
        abort(404)


def _family_only() -> None:
    _switched_on()
    if current_user().is_guest:
        abort(404)


def _guard_sql() -> tuple[str, list[Any]]:
    """Photographs this viewer may open, as SQL on ``a``: the People page's
    rule (db._face_visibility_sql), with explicit photographs for an
    administrator, who sees everything."""
    limits = _viewer_limits()
    roots_sql, params = db.roots_clause("a", _roots())
    where = [roots_sql, db.visibility_clause("a", limits["max_visibility"]), "a.trashed = 0",
             "a.kind = 'picture'"]
    params = [*params, int(limits["max_visibility"])]
    scope_sql, scope_params = db.scope_clause("a", limits["scope"])
    if scope_sql:
        where.append(scope_sql)
        params.extend(scope_params)
    if not current_user().is_admin:
        where.append("a.nsfw = 0")
    screens, screen_params = db.screen_clause(_conn(), "a")
    where.append(f"NOT ({screens})")
    params.extend(screen_params)
    return " AND ".join(where), params


def _people() -> dict[int, dict[str, Any]]:
    return {int(p["id"]): p for p in db.list_people(_conn(), _roots(), **_viewer_limits())
            if p.get("name")}


def _find() -> dict[str, Any]:
    """Every suggestion this viewer may be shown (worked out once, then kept)."""
    conn = _conn()
    people = _people()
    model = db.face_model_in_use(conn)
    dim = faces_mod.embed_dim_of(model)
    rules = facematch.thresholds_for(model)
    born = born_store.born(conn, people)
    guard, params = _guard_sql()

    known: dict[int, guess.Known] = {}
    for row in conn.execute(
            "SELECT f.id, f.person_id, f.embedding, a.date_key, a.date_source "
            "FROM faces f JOIN assets a ON a.id = f.asset_id "
            f"WHERE {guard} AND f.source = 'confirmed' AND f.person_id IS NOT NULL "
            "ORDER BY f.quality DESC", params):
        pid = int(row["person_id"])
        if pid not in people:
            continue
        vector = faces_mod.unpack(row["embedding"], dim)
        if vector is None:
            continue
        person = known.setdefault(pid, guess.Known(pid, born=born.get(pid)))
        person.vectors.append(vector)
        person.years.append(guess.year_of(row["date_key"], row["date_source"]))
        person.face_ids.append(int(row["id"]))
    refs = guess.references(known.values())
    del known
    found: dict[str, Any] = {"people": [], "ask_born": [], "faces": 0}
    if not refs:
        return found

    # Only faces that could be suggested as anybody: dated before somebody's
    # earliest named photograph, or not dated at all.
    trusted = ",".join("?" * len(guess.TRUSTED_DATES))
    newest = guess.cutoff(refs)
    cursor = conn.execute(
        "SELECT f.id, f.asset_id, f.embedding, f.quality, a.date_key, a.date_source "
        "FROM faces f JOIN assets a ON a.id = f.asset_id "
        f"WHERE {guard} AND f.person_id IS NULL AND f.thumb IS NOT NULL AND f.quality >= ? "
        "AND NOT EXISTS (SELECT 1 FROM faces_ignored i WHERE i.face_id = f.id) "
        f"AND (a.date_source NOT IN ({trusted}) OR CAST(substr(a.date_key, 1, 4) AS INTEGER) <= ?)",
        (*params, CLUSTER_MIN_QUALITY, *guess.TRUSTED_DATES, newest))
    kept: list[guess.Unnamed] = []
    looked = 0
    while True:
        rows = cursor.fetchmany(CHUNK)
        if not rows:
            break
        faces = []
        for row in rows:
            vector = faces_mod.unpack(row["embedding"], dim)
            if vector is not None:
                faces.append(guess.Unnamed(int(row["id"]), int(row["asset_id"]),
                                           facematch.unit(vector),
                                           guess.year_of(row["date_key"], row["date_source"]),
                                           float(row["quality"] or 0)))
        looked += len(faces)
        if faces:
            mask = guess.near(np.vstack([f.vector for f in faces]), refs, rules)
            kept.extend(f for f, keep in zip(faces, mask) if keep)
    found["faces"] = looked

    assets = sorted({f.asset_id for f in kept})
    present: dict[int, set[int]] = {}
    for start in range(0, len(assets), 900):
        part = assets[start:start + 900]
        for row in conn.execute(
                "SELECT asset_id, person_id FROM faces WHERE person_id IS NOT NULL "
                f"AND asset_id IN ({','.join('?' * len(part))})", part):
            present.setdefault(int(row[0]), set()).add(int(row[1]))
    links = [r for r in family_tree.relations(conn)
             if int(r["person_a"]) in people and int(r["person_b"]) in people]
    suggestions = guess.suggest(
        kept, refs, rules=rules, present=present, blocked=db.rejections(conn),
        relation=lambda pid, other: _relation(pid, other, links))

    # The photographs, read again through the gallery's own query.
    rows = {}
    wanted = sorted({s["asset_id"] for s in suggestions})
    for start in range(0, len(wanted), 900):
        part = wanted[start:start + 900]
        for row in db.query_assets(conn, _roots(), kinds=["picture"], ids=part, limit=len(part),
                                   offset=0, columns=COLUMNS, with_total=False, **_viewer())[0]:
            rows[int(row["id"])] = row
    by_ref = {r.person_id: r for r in refs}
    grouped: dict[int, list[dict[str, Any]]] = {}
    for item in suggestions:
        row = rows.get(item["asset_id"])
        if row is None:
            continue
        item = {**item, "photo": {"id": int(row["id"]), "thumb_v": _thumb_version(row),
                                  "date": row.get("date_key") or "",
                                  "width": row.get("width"), "height": row.get("height")},
                "family": [{**f, "name": people[f["id"]]["name"]} for f in item["family"]]}
        if item["also"] is not None:
            item["also"] = {"id": item["also"], "name": people[item["also"]]["name"],
                            "face": by_ref[item["also"]].face_id}
        grouped.setdefault(item["person_id"], []).append(item)
    for pid, items in sorted(grouped.items(), key=lambda g: (-len(g[1]), str(people[g[0]]["name"]).casefold())):
        ref = by_ref[pid]
        found["people"].append({
            "person": {"id": pid, "name": people[pid]["name"], "born": born.get(pid),
                       "face": people[pid].get("cover_face_id"), "earliest": ref.earliest,
                       "earliest_face": ref.face_id},
            "faces": items})
    found["ask_born"] = [{"id": pid, "name": people[pid]["name"],
                          "face": people[pid].get("cover_face_id")}
                         for pid in grouped if pid not in born]
    return found


def _suggestions() -> dict[str, Any]:
    conn = _conn()
    viewer = _viewer()
    guard, params = _guard_sql()
    # Keyed on what the viewer may see, not on who they are: everybody with
    # the same limits shares one reading of every face (the guard and the
    # gallery query narrow by those limits alone; a viewer's own "Only to me"
    # photographs are outside the guard).
    key = ("young_faces", guard, tuple(params), tuple(_roots()),
           int(viewer["max_visibility"]), viewer["scope"] or "",
           bool(current_user().is_admin),
           tuple(sorted(born_store.born(conn).items())),
           tuple((int(r["person_a"]), int(r["person_b"]), r["kind"])
                 for r in family_tree.relations(conn)))
    # One at a time: two people opening it at once would read every face twice.
    with _working:
        return db.cached_aggregate(conn, key, _find, also=(db.FACES_GENERATION_KEY,),
                                   max_age=3600)


@bp.get("/api/young-faces")
@require_family
def young_faces_all():
    _family_only()
    started = time.monotonic()
    found = _suggestions()
    log.debug("young faces: %d people in %.1f s", len(found["people"]), time.monotonic() - started)
    return jsonify({**found, "min_gap": guess.MIN_GAP})


def _face_and_person(face_id: int) -> tuple[dict[str, Any], int]:
    """The unnamed face, on a photograph this viewer may open, and the person
    they mean, whom they may see on the People page; 404 otherwise."""
    data = json_object()
    person_id = data.get("person_id")
    if isinstance(person_id, bool) or not isinstance(person_id, int) or not 0 < person_id < 2 ** 63:
        abort(400, description="person_id must be a number.")
    conn = _conn()
    face = db.get_face(conn, face_id)
    if not face:
        abort(404)
    _guard(db.get_asset(conn, int(face["asset_id"]), current_user().id))
    if person_id not in _people():
        abort(404)
    if face.get("person_id") is not None:
        abort(409, description="This face has been named since.")
    return face, person_id


@bp.post("/api/young-faces/<int:face_id>/yes")
@require_family
def young_faces_yes(face_id: int):
    """It is them: the face is named, as a "Who is this?" answer would."""
    from .api_faces import _face_indexer                       # noqa: PLC0415

    _family_only()
    face, person_id = _face_and_person(face_id)
    result = _face_indexer().confirm(_conn(), int(face["id"]), person_id)
    return jsonify({"ok": True, "face": result})


@bp.post("/api/young-faces/<int:face_id>/no")
@require_family
def young_faces_no(face_id: int):
    """It is not them: never suggested as them again."""
    from .api_faces import _face_indexer                       # noqa: PLC0415

    _family_only()
    face, person_id = _face_and_person(face_id)
    _face_indexer().reject(_conn(), int(face["id"]), person_id)
    return jsonify({"ok": True})


@bp.put("/api/young-faces/born/<int:person_id>")
@require_family
def young_faces_born(person_id: int):
    """Keep the year somebody this viewer may see on the People page was born."""
    _family_only()
    if person_id not in _people():
        abort(404)
    year = json_object().get("year")
    this_year = time.localtime().tm_year
    if year is not None and (isinstance(year, bool) or not isinstance(year, int)
                             or not born_store.EARLIEST <= year <= this_year):
        abort(400, description="The year somebody was born has to be a year.")
    born_store.set_born(_conn(), person_id, year, current_user().id or None)
    return jsonify({"ok": True, "person_id": person_id, "year": year})
