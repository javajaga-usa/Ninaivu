"""Festival albums in the family app (media/festivals.py, static/js/festivals.js).

Off until ``festival_albums`` is switched on (Settings → Advanced), when
every route answers 404 as if it did not exist. Anybody who may open the
gallery may open it, guests too: an album names nobody, and every photograph
in it comes through ``db.query_assets`` with the viewer's own limits, so
hidden photographs, folders somebody is kept out of and other people's "Only
to me" items never count. An administrator sees everything the gallery
shows them, explicit photographs included.

``GET /api/festivals`` is every festival with the years found, each year's
count and day, a cover, and the festival's next day.
``GET /api/festivals/<key>`` is one festival with each year's best photographs
and every photograph's id, to open in the viewer.

The picture search, when there is one, decides which of a festival's days
were the festival (media/festivals.py); its words are turned into vectors
once and the answer is kept until the library or its search vectors change,
or for an hour.
"""
from __future__ import annotations

import logging
from typing import Any, Sequence

from flask import abort, jsonify

from .. import ai as ai_mod
from ..media import festival_calendar, festivals
from ..server.auth import current_user
from ..storage import db
from .api import _cfg, _conn, _engine, _roots, _text_vector, _thumb_version, _viewer, bp

log = logging.getLogger(__name__)

#: Seconds an answer is kept even when nothing it watches has moved:
#: favourites and quality flags move no counter.
KEEP_SECONDS = 3600


def _switched_on() -> None:
    if not getattr(_cfg(), "festival_albums", False):
        abort(404)


def _check(conn) -> festivals.Check | None:  # noqa: ANN001
    """The picture search as :data:`festivals.Check`, or None without one."""
    engine = _engine()
    np = getattr(ai_mod, "np", None)
    if engine is None or not getattr(engine, "semantic", False) or np is None:
        return None
    ids, buffer, dim, index = db.embedding_store(conn)
    if not ids or not dim:
        return None

    def vectors(texts: Sequence[str]):
        got = []
        for text in texts:
            try:
                vector = _text_vector(engine, text)
            except Exception:                                   # noqa: BLE001 - no picture search then
                log.info("the picture search could not read %r", text, exc_info=True)
                return None
            if vector is None:
                return None
            vector = np.asarray(vector, dtype="float32").reshape(-1)
            if vector.shape[0] != dim:                          # vectors of another model
                return None
            got.append(vector)
        return np.stack(got)

    ordinary = vectors(festivals.ORDINARY)
    if ordinary is None:
        return None
    matrix = ai_mod._matrix(ids, buffer, dim)                   # noqa: SLF001
    words: dict[str, Any] = {}

    def check(key: str, asset_ids: Sequence[int]) -> dict[int, float] | None:
        if key not in words:
            words[key] = vectors(festivals.FESTIVALS[key]["prompts"])
        festive = words[key]
        if festive is None:
            return None
        found = [(a, index[a]) for a in asset_ids if a in index]
        if not found:
            return {}
        rows = matrix[np.asarray([r for _, r in found], dtype=np.int64)]
        margin = (rows @ festive.T).max(axis=1) - (rows @ ordinary.T).max(axis=1)
        return {int(a): float(m) for (a, _), m in zip(found, margin)}

    return check


def _albums() -> dict[str, Any]:
    conn, roots, viewer = _conn(), _roots(), _viewer()
    user = current_user()
    explicit = bool(user.is_admin)
    engine = _engine()
    key = ("festival_albums", int(viewer["viewer_id"] or 0), int(viewer["max_visibility"]),
           viewer["scope"] or "", tuple(roots), explicit, getattr(engine, "model_id", "") or "",
           bool(getattr(engine, "semantic", False)), festival_calendar.FESTIVALS)
    return db.cached_aggregate(
        conn, key,
        lambda: festivals.albums(conn, roots, viewer, check=_check(conn), include_nsfw=explicit,
                                 version=_thumb_version),
        also=(db.EMBEDDINGS_GENERATION_KEY,), max_age=KEEP_SECONDS)


@bp.get("/api/festivals")
def festivals_list():
    _switched_on()
    return jsonify(festivals.summary(_albums()))


@bp.get("/api/festivals/<key>")
def festivals_one(key: str):
    _switched_on()
    if key not in festivals.FESTIVALS:
        abort(404)
    found = _albums()
    for festival in found["festivals"]:
        if festival["key"] == key:
            return jsonify({**festival, "checked": found["checked"], "today": found["today"]})
    abort(404)
