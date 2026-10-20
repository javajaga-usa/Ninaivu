"""Colour for black-and-white photographs, made on this computer.

Off until ``colour_photos`` is switched on, when every route answers 404 as if
it did not exist. Family members and administrators may colour a photograph
they can see; guests may not.

``GET /api/asset/<id>/colour`` says what the viewer may offer for one
photograph: whether it looks black and white (a grey or a single-tint
picture, read from its preview: media/onnx_tools.py), whether the colouring
model is ready, the photograph it is a colour copy of, and the colour copies
already made of it (storage/colour_copies.py).

``POST /api/asset/<id>/colour`` colours it and answers with the result as a
JPEG, upright, which nothing keeps: the page shows it beside the original and,
if the family wants it, saves it through the photo editor's own route
(``/api/asset/<id>/edited-copy?made=colour``), as a copy beside the original.
The original is never written to. The photograph is turned upright and
handed to the quick model host as a private file (runtime/colour.py), and
both files are removed as soon as the answer is read.

``GET /api/asset/<id>/colour/upright`` is the photograph as the browser sees
it, turned upright, for the original's half of the viewer's Before and after
(a colour copy is upright already).
"""
from __future__ import annotations

import io
import logging
import os
import secrets
import threading
from pathlib import Path

from flask import abort, current_app, jsonify, send_file
from PIL import Image

from .. import runtime
from ..media import media, onnx_tools, upright
from ..runtime import colour as colour_mod
from ..server.auth import current_user, require_family
from ..storage import colour_copies, db
from .api import _asset_file, _cfg, _conn, _guard, _viewing_copy, bp

log = logging.getLogger(__name__)

#: The longest side handed to the model host. The model sees 512 pixels
#: whatever it is given; the rest is the photograph's own lightness, so this
#: is only how large the copy can be (16 megapixels, under the edited copy's
#: 24).
MAX_SIDE = 4096
#: Seconds one photograph may take, loading the model included.
COLOUR_SECONDS = 180.0
#: Seconds to wait for the quick host to finish somebody else's call.
HOST_WAIT = 30.0
#: Photographs coloured at once.
_colouring = threading.BoundedSemaphore(2)


def _switched_on() -> None:
    if not getattr(_cfg(), "colour_photos", False):
        abort(404)
    if current_user().is_guest:
        abort(404)


def _picture(asset_id: int) -> dict:
    row = _guard(db.get_asset(_conn(), asset_id, current_user().id))
    if row.get("kind") != "picture" or row.get("trashed"):
        abort(404)
    return row


def _visible(asset_id: int | None) -> int | None:
    """*asset_id* if this viewer may see it, else None."""
    if not asset_id:
        return None
    try:
        _guard(db.get_asset(_conn(), asset_id, current_user().id))
    except Exception:                                           # noqa: BLE001 - a 404 or 403
        return None
    return int(asset_id)


def _looks_black_and_white(row: dict) -> bool:
    cfg = _cfg()
    if not row.get("thumb"):
        return False
    for size in sorted(cfg.thumb_sizes):
        path = cfg.thumbs_dir / media.thumb_file(row["thumb"], size, cfg.thumb_format)
        if path.is_file():
            try:
                with Image.open(path) as preview:
                    return onnx_tools.looks_black_and_white(preview)
            except (OSError, ValueError):
                return False
    return False


def _host():
    services = current_app.config.get("MV_SERVICES")
    if services is None:
        return None
    return services.quick_host() if hasattr(services, "quick_host") else services.model_host()


def _folder() -> Path:
    folder = Path(_cfg().state_dir) / "colour"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    return folder


@bp.get("/api/asset/<int:asset_id>/colour")
@require_family
def colour_status(asset_id: int):
    _switched_on()
    row = _picture(asset_id)
    conn = _conn()
    why = colour_mod.ready()
    source = _visible(colour_copies.source_of(conn, asset_id))
    copies = [c for c in colour_copies.copies_of(conn, asset_id) if _visible(c)]
    return jsonify({"on": True, "ready": why is None, "why": why or "",
                    # A colour copy is not offered colour again.
                    "mono": source is None and _looks_black_and_white(row),
                    "source": source, "copies": copies,
                    "before": f"/api/asset/{source}/colour/upright" if source else None})


@bp.post("/api/asset/<int:asset_id>/colour")
@require_family
def colour_photo(asset_id: int):
    _switched_on()
    row = _picture(asset_id)
    if why := colour_mod.ready():
        return jsonify({"error": why}), 409
    host = _host()
    if host is None:
        return jsonify({"error": "The on-device engine is not running."}), 503
    if not _colouring.acquire(timeout=1.0):
        return jsonify({"error": "Ninaivu is colouring other photographs; try again in a moment."}), 429
    token = secrets.token_hex(12)
    given, made = _folder() / f"{token}.png", _folder() / f"{token}.jpg"
    try:
        try:
            with media._open_oriented(_asset_file(row)) as opened:      # noqa: SLF001
                picture = upright.apply(opened.convert("RGB"), int(row.get("rotation") or 0))
                if max(picture.size) > MAX_SIDE:
                    picture.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
                with open(os.open(given, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out:
                    picture.save(out, "PNG", compress_level=1)
        except (OSError, ValueError, Image.DecompressionBombError):
            return jsonify({"error": "This photograph could not be opened."}), 422
        try:
            host.call("colour", timeout=COLOUR_SECONDS, wait=HOST_WAIT,
                      path=str(given), out=str(made))
        except runtime.HostBusy:
            return jsonify({"error": "Ninaivu is busy; try again in a moment."}), 429
        except runtime.Unavailable as exc:
            return jsonify({"error": str(exc)}), 409
        except runtime.HostError as exc:
            log.warning("photograph %s could not be coloured: %s", asset_id, exc)
            return jsonify({"error": "This photograph could not be coloured."}), 500
        coloured = io.BytesIO(made.read_bytes())
    finally:
        _colouring.release()
        given.unlink(missing_ok=True)
        made.unlink(missing_ok=True)
    response = send_file(coloured, mimetype="image/jpeg", max_age=0)
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.get("/api/asset/<int:asset_id>/colour/upright")
@require_family
def colour_upright(asset_id: int):
    """The photograph as the browser sees it, turned upright: the original's
    half of the viewer's Before and after."""
    _switched_on()
    row = _picture(asset_id)
    return _viewing_copy(row, _asset_file(row), max_age=3600, turned=True)
