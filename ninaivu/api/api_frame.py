"""The living photo frame (media/frame.py, static/js/frame.js).

Two routes, both off until ``photo_frame`` is switched on, when they answer
404 as if they did not exist. The batch is whatever the viewer may see, for
whoever the frame is signed in as, guests included: a frame in the hall shows
what that profile would see in the gallery, and nothing else.
"""

from __future__ import annotations

from flask import abort, jsonify, request

from ..media import frame
from ..server.auth import current_user
from ..storage import db
from .api import (_asset_file, _cfg, _conn, _guard, _ids_from_csv, _int_arg,
                  _roots, _viewer, _viewing_copy, bp)


def _switched_on() -> None:
    if not _cfg().photo_frame:
        abort(404)


@bp.get("/api/frame/next")
def frame_next():
    """The next photographs to show: ``n`` of them (12), shuffled with
    ``seed``, avoiding the ids in ``shown`` (the frame's recent history)."""
    _switched_on()
    shown = _ids_from_csv(request.args.get("shown", ""), 400)
    found = frame.batch(_conn(), _roots(), _viewer(), count=_int_arg("n", frame.BATCH),
                        seed=_int_arg("seed", 0), shown=shown)
    found["signed_in"] = bool(current_user().id)
    return jsonify(found)


@bp.get("/api/frame/picture/<int:asset_id>")
def frame_picture(asset_id: int):
    """One photograph at the size a screen needs, upright and with no
    metadata in it: the viewing copy the share page shows (at most 2560 px)."""
    _switched_on()
    row = _guard(db.get_asset(_conn(), asset_id))
    if (row.get("kind") or "") != "picture":
        abort(404)
    response = _viewing_copy(row, _asset_file(row), max_age=86400, turned=True)
    response.headers["Vary"] = "Cookie"
    return response
