"""Photo stacks in the family app (storage/stacks.py, static/js/stacks.js).

Off until ``photo_stacks`` is switched on (Settings → Advanced), when every
route answers 404 as if it did not exist.

``GET /api/stacks/<id>`` is the stack a photograph is in: every frame of it
this viewer may see, oldest first, which one is on top, and whether the
stack is shown apart. The frames come through ``db.query_assets`` with the
viewer's own limits, so a hidden or explicit frame, a folder somebody is
kept out of or somebody else's "Only to me" item never reaches anybody who
may not see it — a stack holds only photographs every viewer sees alike, and
this holds even if it did not.

``POST /api/stacks/<id>/top`` puts that photograph on top of its stack.
``POST /api/stacks/<id>/apart`` with ``{"apart": true}`` shows its stack as
separate photographs, ``false`` as one again. Both are for the household
(family and administrators), not guests: they change the timeline everybody
sees. Nothing is moved or deleted by either.
"""
from __future__ import annotations

from typing import Any

from flask import abort, jsonify

from ..server.auth import current_user
from ..storage import db, stacks
from ._body import json_object
from .api import _cfg, _conn, _roots, _thumb_version, _viewer, bp


def _switched_on() -> None:
    if not getattr(_cfg(), "photo_stacks", False):
        abort(404)


def _visible(conn, ids: list[int]) -> list[dict[str, Any]]:  # noqa: ANN001
    """Those of *ids* this viewer may see, as the gallery would show them."""
    if not ids:
        return []
    admin = bool(current_user().is_admin)
    rows, _ = db.query_assets(conn, _roots(), ids=ids, kinds=["picture"], limit=len(ids),
                              offset=0, with_total=False, include_nsfw=admin, **_viewer())
    return rows


def _stack_of(asset_id: int) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """The stack *asset_id* is in, its frames this viewer sees, and the photograph.

    404 when the photograph is not one this viewer may see or is in no stack.
    """
    conn = _conn()
    if not getattr(_cfg(), "replica_from", ""):
        stacks.refresh(conn)
    found = _visible(conn, [int(asset_id)])
    if not found:
        abort(404)
    key, ids = stacks.members(conn, asset_id)
    if not key:
        abort(404)
    frames = _visible(conn, ids)
    order = {aid: index for index, aid in enumerate(ids)}
    frames.sort(key=lambda row: order.get(int(row["id"]), 0))
    return key, frames, found[0]


@bp.get("/api/stacks/<int:asset_id>")
def stack(asset_id: int):
    _switched_on()
    conn = _conn()
    key, frames, _ = _stack_of(asset_id)
    choice = conn.execute("SELECT apart FROM stack_choices WHERE key = ?", (key,)).fetchone()
    top = next((int(r["id"]) for r in frames if int(r.get("stack_count") or 0) > 1), None)
    user = current_user()
    return jsonify({
        "apart": bool(choice and choice[0]),
        "top": top,
        "can_change": bool(user.id) and not user.is_guest,
        "items": [{
            "id": int(row["id"]),
            "date": row.get("date_key") or "",
            "thumb_v": _thumb_version(row),
            "width": row.get("width"),
            "height": row.get("height"),
            "sharpness": row.get("sharpness"),
        } for row in frames],
    })


def _may_change() -> None:
    user = current_user()
    if not user.id or user.is_guest:
        abort(403)


@bp.post("/api/stacks/<int:asset_id>/top")
def stack_top(asset_id: int):
    _switched_on()
    _may_change()
    key, frames, _ = _stack_of(asset_id)
    if len(frames) < 2:
        abort(404)
    stacks.choose_top(_conn(), key, asset_id, current_user().id)
    return jsonify({"ok": True, "top": int(asset_id)})


@bp.post("/api/stacks/<int:asset_id>/apart")
def stack_apart(asset_id: int):
    _switched_on()
    _may_change()
    key, frames, _ = _stack_of(asset_id)
    if len(frames) < 2:
        abort(404)
    apart = json_object().get("apart", True)
    if not isinstance(apart, bool):
        abort(400, description="Apart must be true or false.")
    stacks.set_apart(_conn(), key, apart, current_user().id)
    return jsonify({"ok": True, "apart": apart})
