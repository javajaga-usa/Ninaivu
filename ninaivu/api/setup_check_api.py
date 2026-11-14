"""The Setup check page: is this computer set up so the household can use
Ninaivu? Ports, drives, Tailscale and permissions, each with what to do
(server/setup_check.py).

Console only, administrators only. It only looks: nothing is changed.
"""
from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

from ..server import setup_check
from ..server.auth import require_admin

setup_check_bp = Blueprint("setup_check", __name__)


@setup_check_bp.get("/api/admin/setup-check")
@require_admin
def setup_check_state():
    cfg = current_app.config["MV_CONFIG"]
    fresh = request.args.get("fresh") in ("1", "true", "yes")
    return jsonify(setup_check.cached(
        cfg, fresh=fresh,
        asked=current_app.config.get("MV_PORTS_ASKED") or {},
        scheme=current_app.config.get("MV_SCHEME") or "http"))
