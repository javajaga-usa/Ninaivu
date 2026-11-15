"""What each person sees (server/privacy_view.py), for administrators.

One answer for the console's People page: every profile, with what the
family app would let them open and, for the rest, the first reason it would
not. Only read; a family member or a guest gets ``require_admin``'s refusal.
"""
from __future__ import annotations

from flask import current_app, jsonify

from ..server import privacy_view
from ..server.auth import require_admin
from .api import _conn, bp


@bp.get("/api/privacy/people")
@require_admin
def privacy_people():
    cfg = current_app.config["MV_CONFIG"]
    conn = _conn()
    people = privacy_view.everybody(conn, cfg)
    nobody = privacy_view.nobody_signed_in(conn, cfg)
    return jsonify({"people": people, "nobody": nobody,
                    "reasons": list(privacy_view.REASONS)})
