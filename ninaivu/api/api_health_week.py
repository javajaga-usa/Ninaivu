"""The weekly health digest (server/health_week.py), for administrators.

On the family API's blueprint, so the same answer reaches the console's
Overview and the top of the family app when an administrator is the one
signed in. A family member or a guest gets the refusal ``require_admin``
gives, and the app shows nothing.
"""
from __future__ import annotations

from flask import current_app, jsonify, request

from ..server.auth import require_admin
from .api import bp


@bp.get("/api/health/week")
@require_admin
def health_week():
    services = current_app.config["MV_SERVICES"]
    keeper = getattr(services, "health_week", None)
    if keeper is None:
        return jsonify({"error": "The weekly health digest is not running here."}), 404
    return jsonify(keeper.report(fresh=request.args.get("fresh") == "1"))
