"""Our Year: the family's year in photographs (media/our_year.py,
static/js/our-year.js).

Off until ``our_year`` is switched on, when every route answers 404 as if it
did not exist. Anybody who may open the gallery may open it, guests too, and
each is told about the year only through their own limits; a guest is never
told who is in a photograph, where it was taken, or what was said about it.

``GET /api/our-year`` lists the years there is enough to tell, newest first,
and whether the opening words will be written by the on-device model.
``GET /api/our-year/<year>`` is everything the cards say.
``POST /api/our-year/<year>/words`` with ``{"language": "ta"|"en"}`` asks the
writing model for a few warm sentences to open with; without the model it
answers 503 and the page writes its own from the same facts. What the model
wrote is kept (in memory, until the library changes) so opening the year
again is instant.
"""
from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import Any

from flask import abort, current_app, jsonify

from .. import runtime
from ..media import our_year
from ..server.auth import current_user
from ..storage import db
from ._body import json_object
from .api import _cfg, _conn, _roots, _thumb_version, _viewer, bp

log = logging.getLogger(__name__)

#: Seconds the writing model is given, and how long the year's words wait for
#: it to finish another call before the page writes its own instead.
WRITE_SECONDS = 120.0
HOST_WAIT = 10.0
#: Words kept, for this many years and people at once.
_KEPT = 64
_words: "OrderedDict[tuple, dict[str, Any]]" = OrderedDict()
_words_lock = threading.Lock()
_writing = threading.BoundedSemaphore(2)


def _switched_on() -> None:
    if not getattr(_cfg(), "our_year", False):
        abort(404)


def _year(value: int) -> int:
    if not 1826 <= value <= 2200:
        abort(404)
    return value


def _writer() -> tuple[bool, str]:
    cfg = _cfg()
    if not runtime.enabled(cfg):
        return False, "the on-device AI engine is off (Advanced settings)"
    from ..runtime import llm                                   # noqa: PLC0415
    why = llm.ready()
    return why is None, why or ""


def _host():
    services = current_app.config.get("MV_SERVICES")
    return services.model_host() if services is not None and hasattr(services, "model_host") else None


def _home() -> tuple[float, float] | None:
    cfg = _cfg()
    if cfg.home_lat is None or cfg.home_lon is None:
        return None
    return float(cfg.home_lat), float(cfg.home_lon)


def _summary(year: int) -> dict[str, Any]:
    conn = _conn()
    roots = _roots()
    viewer = _viewer()
    user = current_user()
    guest = bool(user.is_guest)
    names: dict[int, str] = {}
    if not guest:
        names = {int(p["id"]): str(p["name"]) for p in db.list_people(
            conn, roots, max_visibility=viewer["max_visibility"], scope=viewer["scope"])
            if p.get("name")}
    home = _home()
    # The visibility clause carries the household's date limit, which moves no
    # library counter: a narrower limit is in force at once.
    key = ("our_year", int(year), int(viewer["viewer_id"] or 0), int(viewer["max_visibility"]),
           viewer["scope"] or "", tuple(roots), db.visibility_clause("a", viewer["max_visibility"]),
           guest, home,
           tuple(sorted(names.items())))
    # Favourites move no library counter, so the year is read afresh after a
    # few minutes even when nothing else changed.
    return db.cached_aggregate(
        conn, key,
        lambda: our_year.summary(conn, roots, viewer, year, names=names, guest=guest, home=home,
                                 version=_thumb_version),
        also=(db.FACES_GENERATION_KEY, db.OCCASIONS_GENERATION_KEY, db.PLACES_GENERATION_KEY),
        max_age=300)


@bp.get("/api/our-year")
def our_year_list():
    _switched_on()
    writes, why = _writer()
    viewer = _viewer()
    return jsonify({"years": our_year.years(_conn(), _roots(), viewer),
                    "writer": writes and not current_user().is_guest, "why": why})


@bp.get("/api/our-year/<int:year>")
def our_year_one(year: int):
    _switched_on()
    found = _summary(_year(year))
    if found["items"] < our_year.MIN_ITEMS:
        abort(404)
    return jsonify(found)


@bp.post("/api/our-year/<int:year>/words")
def our_year_words(year: int):
    """A few sentences to open the year with, by the on-device writing model."""
    _switched_on()
    user = current_user()
    if user.is_guest:
        abort(404)
    data = json_object()
    language = "ta" if data.get("language") == "ta" else "en"
    found = _summary(_year(year))
    if found["items"] < our_year.MIN_ITEMS:
        abort(404)
    facts = our_year.facts(found)
    key = (int(year), int(user.id or 0), language, repr(sorted(facts.items())))
    with _words_lock:
        kept = _words.get(key)
        if kept is not None:
            _words.move_to_end(key)
            return jsonify(kept)
    writes, why = _writer()
    host = _host() if writes else None
    if host is None:
        return jsonify({"error": why or "The writing model is not ready.", "written": ""}), 503
    if not _writing.acquire(timeout=1.0):
        return jsonify({"error": "Ninaivu is writing for somebody else; try again in a moment.",
                        "written": ""}), 429
    try:
        reply = host.call("generate",
                          messages=our_year.model_messages(
                              found, language=language,
                              household=getattr(_cfg(), "house_name", "") or ""),
                          max_tokens=300, temperature=0.4, timeout=WRITE_SECONDS, wait=HOST_WAIT)
    except runtime.Unavailable:
        log.info("the writing model is not ready for the year's words", exc_info=True)
        return jsonify({"error": "The writing model is not ready.", "written": ""}), 503
    except Exception:                                           # noqa: BLE001 - the page writes its own
        log.info("the writing model could not write the year's words", exc_info=True)
        return jsonify({"error": "The writing model could not write this time.", "written": ""}), 503
    finally:
        _writing.release()
    answer = {"written": our_year.cleaned(reply.get("text") or ""),
              "model": str(reply.get("model") or ""), "language": language}
    if answer["written"]:
        with _words_lock:
            _words[key] = answer
            while len(_words) > _KEPT:
                _words.popitem(last=False)
    return jsonify(answer)
