""""Only to me": a family member keeping items out of everybody else's view.

Anybody signed in as a family member may take an item they can see out of the
family's gallery and keep it to themselves; it then appears for them under
"Only to me" in the sidebar and nowhere else. The storage side — how the item
is held hidden, and what it goes back to — is in ninaivu/storage/db.py
(:func:`db.set_private`, :func:`db.clear_private`).

The rules are three:

* making private is for items the caller can see now, and an item somebody
  else already keeps private is left alone;
* bringing an item back is for whoever made it private;
* nothing here takes an id it was not allowed to reach: every list is first
  narrowed by the same query the gallery uses.
"""

from __future__ import annotations

from flask import abort, jsonify

from ..server.auth import current_user, require_family
from ..storage import db
from ._body import json_object
from .api import _conn, _id_list, _roots, _viewer, bp


@bp.post("/api/private")
@require_family
def keep_private():
    """``{"ids": [...], "private": true|false}`` — keep these to me, or give
    them back to whoever could see them before."""
    data = json_object()
    ids = _id_list(data)
    wanted = data.get("private", True)
    if not isinstance(wanted, bool):
        abort(400, description="Private must be true or false.")
    if not ids:
        return jsonify({"updated": 0, "private": wanted})

    conn = _conn()
    user = current_user()
    viewer = _viewer()
    # What this caller may reach: the gallery's own query, which for ids also
    # finds the caller's own private items.
    allowed: list[int] = []
    for start in range(0, len(ids), 500):
        piece = ids[start:start + 500]
        rows, _ = db.query_assets(
            conn, _roots(), ids=piece, limit=len(piece), offset=0, columns=("id",),
            include_nsfw=user.is_admin, with_total=False, **viewer)
        allowed.extend(int(r["id"]) for r in rows)
    if not allowed:
        return jsonify({"updated": 0, "private": wanted})

    if wanted:
        updated = db.set_private(conn, allowed, user.id)
    else:
        # An administrator may bring back anybody's; everybody else, their own.
        updated = db.clear_private(conn, allowed, None if user.is_admin else user.id)
    return jsonify({
        "updated": updated,
        "private": wanted,
        "count": db.private_count(conn, _roots(), user.id, viewer["scope"]),
    })
