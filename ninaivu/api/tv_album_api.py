"""The console's side of the TV album (ninaivu/server/tv_album.py).

Which album the televisions at home may show, on which port, whether it is
running and where a TV can be pointed by hand, and a new secret when the old
addresses should stop working. Console only, administrators only: choosing
what is shown to anybody on the home network with no sign-in is the
administrator's decision, like making a share link that never ends.
"""

from __future__ import annotations

from flask import current_app, jsonify

from ..server import auth
from ..server.auth import current_user, require_admin
from ..server.tv_album import TvAlbum, album_name
from ..storage import db
from ._body import json_object
from .admin_api import admin_bp


def _cfg():
    return current_app.config["MV_CONFIG"]


def _conn():
    return db.connect(_cfg().db_path)


def _tv() -> TvAlbum:
    services = current_app.config.get("MV_SERVICES")
    found = getattr(services, "tv_album", None)
    if found is None:
        # An app built without Services (a bare test app): one for this app.
        found = current_app.config.get("MV_TV_ALBUM")
        if found is None:
            found = current_app.config["MV_TV_ALBUM"] = TvAlbum(
                _cfg(), lambda: db.connect(_cfg().db_path))
    return found


def _whole(value, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{name} is a whole number")
    try:
        number = int(str(value).strip() or "0")
    except ValueError as exc:
        raise ValueError(f"{name} is a whole number") from exc
    if not low <= number <= high:
        raise ValueError(f"{name} is between {low} and {high}")
    return number


@admin_bp.get("/api/admin/tv-album")
@require_admin
def tv_album_status():
    """Which album, how many items it offers, the port, whether it is running,
    what is wrong if it is not, and the addresses a TV can be given by hand."""
    return jsonify(_tv().status())


@admin_bp.put("/api/admin/tv-album")
@require_admin
def tv_album_set():
    """Choose the album (``album_id``; 0 or null turns the TV album off) and,
    optionally, the ``port``. The server starts, moves or stops at once."""
    data = json_object()
    cfg = _cfg()
    album_id = cfg.tv_album
    port = cfg.tv_port
    try:
        if "album_id" in data:
            raw = data["album_id"]
            album_id = 0 if raw is None else _whole(raw, "album_id", 0, 2 ** 63 - 1)
        if "port" in data:
            port = _whole(data["port"], "port", 1024, 65535)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if album_id and album_name(_conn(), album_id) is None:
        return jsonify({"error": "There is no album with that number."}), 404
    if port in {int(getattr(cfg, "port", 0) or 0), int(getattr(cfg, "admin_port", 0) or 0)}:
        return jsonify({"error": "That port is already Ninaivu's own."}), 400
    changed = album_id != cfg.tv_album or port != cfg.tv_port
    tv = _tv()
    if changed:
        if album_id != cfg.tv_album:
            tv.forget_thumbnails()
        cfg.tv_album = album_id
        cfg.tv_port = port
        cfg.save()
        auth.audit(_conn(), current_user().id, "tv_album",
                   f"album {album_id} on port {port}" if album_id else "off")
    if album_id:
        if changed or not tv.running:
            tv.restart()
    else:
        tv.stop()
    return jsonify(tv.status())


@admin_bp.post("/api/admin/tv-album/secret")
@require_admin
def tv_album_new_secret():
    """A new secret: every address handed out before stops working, and the
    TVs at home find the new one in the next announcement."""
    tv = _tv()
    tv.new_secret()
    auth.audit(_conn(), current_user().id, "tv_album", "new secret")
    return jsonify(tv.status())
