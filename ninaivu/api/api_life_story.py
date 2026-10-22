"""A life in photographs in the family app (media/life_story.py,
static/js/life-story.js).

Off until ``life_story`` is switched on (Settings → Advanced), when every
route answers 404 as if it did not exist. For the family: a guest, who is
never told who is in a photograph, has no life to be told.

Everything is cut to what this viewer may see: the person's photographs come
through ``db.query_assets`` with the viewer's own limits, and only the people
they may see on the People page are named, offered or linked in the family
tree. Somebody who appears only in photographs this viewer cannot open has
no story here at all.

``GET /api/life`` is everybody whose life there is enough to tell, and
whether the chapters' words will be written by the on-device model.
``GET /api/life/<person>`` is everything the chapters say, with the words
already written for these facts if there are some.
``POST /api/life/<person>/words`` with ``{"language": "ta"|"en"}`` asks the
writing model for each chapter's title and sentences; without the model it
answers 503 and the page writes its own from the same facts. What it wrote is
kept (storage/life_story.py).
``PUT /api/life/<person>/born`` with ``{"year": 1948}`` (or null) keeps the
year somebody was born, the same year Then and now uses.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from flask import abort, current_app, jsonify, request

from .. import runtime
from ..media import life_story
from ..server.auth import current_user, require_family
from ..storage import db, family_tree, life_story as words_store, then_and_now as born_store
from ..words import Said
from ._body import json_object
from .api import _cfg, _conn, _roots, _thumb_version, _viewer, _viewer_limits, bp

log = logging.getLogger(__name__)

#: Seconds the writing model is given for a whole life (a few hundred words
#: in Tamil take Gemma 3 12B a minute or two), and how long to wait for it to
#: finish somebody else's call before the page keeps its own words.
WRITE_SECONDS = 300.0
HOST_WAIT = 10.0
#: Tokens per chapter, and for the opening; capped for a very long life.
TOKENS_PER_CHAPTER, TOKENS_OPENING, TOKENS_MOST = 220, 120, 2400

_writing = threading.BoundedSemaphore(1)


def _switched_on() -> None:
    if not getattr(_cfg(), "life_story", False):
        abort(404)


def _writer() -> tuple[bool, str]:
    if not runtime.enabled(_cfg()):
        return False, "the on-device AI engine is off (Advanced settings)"
    from ..runtime import llm                                   # noqa: PLC0415
    why = llm.ready()
    return why is None, why or ""


def _host():
    services = current_app.config.get("MV_SERVICES")
    return services.model_host() if services is not None and hasattr(services, "model_host") else None


def _people() -> dict[int, dict[str, Any]]:
    return {int(p["id"]): p for p in db.list_people(_conn(), _roots(), **_viewer_limits())
            if p.get("name")}


def _life(person_id: int) -> dict[str, Any]:
    conn, roots, viewer = _conn(), _roots(), _viewer()
    people = _people()
    if person_id not in people:
        abort(404)
    names = {pid: str(p["name"]) for pid, p in people.items()}
    faces = {pid: p.get("cover_face_id") for pid, p in people.items()}
    links = [r for r in family_tree.relations(conn)
             if int(r["person_a"]) in names and int(r["person_b"]) in names]
    born = born_store.born(conn, names)
    key = ("life_story", int(person_id), int(viewer["viewer_id"] or 0),
           int(viewer["max_visibility"]), viewer["scope"] or "", tuple(roots),
           tuple(sorted(names.items())), tuple(sorted(faces.items(), key=lambda kv: kv[0])),
           tuple(sorted(born.items())),
           tuple(sorted((int(r["person_a"]), int(r["person_b"]), r["kind"]) for r in links)))
    # Favourites and stories move no library counter, so a life is read
    # afresh after a few minutes even when nothing else changed.
    found = db.cached_aggregate(
        conn, key,
        lambda: life_story.life(conn, roots, viewer, person_id, names=names, faces=faces,
                                born=born, links=links, version=_thumb_version),
        also=(db.FACES_GENERATION_KEY, db.OCCASIONS_GENERATION_KEY, db.PLACES_GENERATION_KEY),
        max_age=300)
    if found is None:
        abort(404)
    return found


def _language(value: Any) -> str:
    return "ta" if value == "ta" else "en"


@bp.get("/api/life")
@require_family
def life_people():
    _switched_on()
    writes, why = _writer()
    people = _people()
    found = life_story.people(_conn(), _roots(), _viewer(),
                              {pid: str(p["name"]) for pid, p in people.items()})
    for person in found:
        person["face"] = people[person["id"]].get("cover_face_id")
    return jsonify({"people": found, "writer": writes, "why": why})


@bp.get("/api/life/<int:person_id>")
@require_family
def life_one(person_id: int):
    _switched_on()
    found = _life(person_id)
    language = _language(request.args.get("language"))
    written = words_store.get(_conn(), person_id, language,
                              life_story.fingerprint(found, language))
    writes, _ = _writer()
    return jsonify({**found, "written": written, "writer": writes})


@bp.post("/api/life/<int:person_id>/words")
@require_family
def life_words(person_id: int):
    """Each chapter's title and sentences, by the on-device writing model."""
    _switched_on()
    language = _language(json_object().get("language"))
    found = _life(person_id)
    facts = life_story.fingerprint(found, language)
    kept = words_store.get(_conn(), person_id, language, facts)
    if kept is not None:
        return jsonify(kept)
    writes, why = _writer()
    host = _host() if writes else None
    if host is None:
        return jsonify({"error": why or "The writing model is not ready."}), 503
    if not _writing.acquire(timeout=1.0):
        return jsonify({"error": "Ninaivu is writing for somebody else; try again in a moment."}), 429
    chapters = len(found["chapters"])
    try:
        reply = host.call("generate",
                          messages=life_story.model_messages(
                              found, language=language,
                              household=getattr(_cfg(), "house_name", "") or ""),
                          max_tokens=min(TOKENS_MOST, TOKENS_OPENING + TOKENS_PER_CHAPTER * chapters),
                          temperature=0.4, timeout=WRITE_SECONDS, wait=HOST_WAIT)
    except runtime.Unavailable:
        log.info("the writing model is not ready for a life story", exc_info=True)
        return jsonify({"error": "The writing model is not ready."}), 503
    except Exception:                                           # noqa: BLE001 - the page writes its own
        log.info("the writing model could not write a life story", exc_info=True)
        return jsonify({"error": "The writing model could not write this time."}), 503
    finally:
        _writing.release()
    written = life_story.parsed(reply.get("text") or "", chapters)
    if written is None:
        log.info("the writing model's life story was not in the asked-for shape")
        return jsonify({"error": "The writing model could not write this time."}), 503
    model = str(reply.get("model") or "")
    words_store.put(_conn(), person_id, language, facts, written, model)
    return jsonify({**written, "model": model, "written_at": time.time()})


@bp.put("/api/life/<int:person_id>/born")
@require_family
def life_born(person_id: int):
    """Keep the year somebody this viewer may see on the People page was born."""
    _switched_on()
    if person_id not in _people():
        abort(404)
    year = json_object().get("year")
    this_year = time.localtime().tm_year
    if year is not None and (isinstance(year, bool) or not isinstance(year, int)
                             or not born_store.EARLIEST <= year <= this_year):
        abort(400, description=Said("The year somebody was born has to be a year."))
    born_store.set_born(_conn(), person_id, year, current_user().id or None)
    return jsonify({"ok": True, "person_id": person_id, "year": year})
