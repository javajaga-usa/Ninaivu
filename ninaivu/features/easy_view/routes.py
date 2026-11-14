"""Easy view in the family app (features/easy_view/media.py, static/js/easy-view.js).

Off until ``easy_view`` is switched on (Settings → Advanced), when every
route answers 404 as if it did not exist. For family members and
administrators; a guest is never offered it.

``GET /api/easy-view`` says whether the person signed in has easy view on,
and which voices the home computer has. ``PUT /api/easy-view`` with
``{"on": true|false}`` turns it on or off for themselves.

``GET /api/easy-view/people`` (administrators) lists the profiles that have
it, and ``PUT /api/easy-view/people/<id>`` with ``{"on"}`` turns it on or off
for one of them: how a grandparent who never opens a menu gets it.

``GET /api/asset/<id>/told?language=ta`` is what easy view says about a
photograph the person may see: who the family named in it, where and when it
was taken, and a caption the family wrote, as short sentences (nothing is
guessed and no model looks at the photograph). ``POST
/api/asset/<id>/told/voice`` with ``{"language"}`` reads them aloud in the
voice letters are read in, on the quick model host, or answers at once with
the reading already made of the same sentences; ``GET
/api/asset/<id>/told/voice.wav?language=…`` is that reading. When the home
computer has no voice for the language, the page uses the device's own.

The photograph is never written to. A reading is a small WAV in the state
folder, named by the photograph and the key of the sentences, so a changed
name or caption makes a new one; at most :data:`KEEP` are kept.
"""
from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path
from typing import Any

from flask import abort, current_app, jsonify, request, send_file

from ... import runtime
from ...api import _long_calls
from ...api._body import json_object
from ...api.api import _cfg, _conn, _gate, bp
from ...runtime import voice as voice_mod
from ...server import auth
from ...server.auth import current_user, require_admin, require_family
from ...storage import db
from ...words import said
from ..photo_talk import media as photo_talk
from . import media as told, storage as easy_store

log = logging.getLogger(__name__)

#: Seconds a photograph may take to be told aloud, the voice's loading included.
SPEAK_SECONDS = 180.0
#: Seconds to wait for the quick host to finish somebody else's call.
HOST_WAIT = 20.0
#: Readings kept in the state folder; the oldest go first.
KEEP = 400

_speaking = _long_calls.Limited(1)


def _switched_on() -> None:
    if not getattr(_cfg(), "easy_view", False):
        abort(404)
    if current_user().is_guest:
        abort(404)


def _item(asset_id: int) -> dict[str, Any]:
    row = _gate(db.get_asset(_conn(), asset_id, current_user().id))
    if row.get("trashed"):
        abort(404)
    return row


def _host():
    services = current_app.config.get("MV_SERVICES")
    if services is None:
        return None
    return services.quick_host() if hasattr(services, "quick_host") else services.model_host()


def _voices() -> dict[str, dict[str, Any]]:
    return {lang: {"ready": (why := voice_mod.ready(lang)) is None, "why": why or ""}
            for lang in told.LANGUAGES}


def folder(cfg: Any) -> Path:
    path = Path(cfg.state_dir) / "easy-view"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def reading_file(cfg: Any, asset_id: int, key: str) -> Path:
    return folder(cfg) / f"{int(asset_id)}-{key}.wav"


def _keep_few(cfg: Any) -> None:
    """Let the oldest readings go once there are more than :data:`KEEP`."""
    try:
        found = sorted(folder(cfg).glob("*.wav"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    for old in found[:max(0, len(found) - KEEP)]:
        with contextlib.suppress(OSError):
            old.unlink()


def _wanted(value: Any = None) -> str:
    return told.language(value or request.args.get("language") or current_user().language)


def _said(asset_id: int, lang: str) -> tuple[dict[str, Any], list[str]]:
    row = _item(asset_id)
    names = photo_talk.names_in(db.faces_for_asset(_conn(), asset_id))
    return row, told.sentences(row, names=names, lang=lang)


def _answer(asset_id: int, lang: str) -> dict[str, Any]:
    _, sentences = _said(asset_id, lang)
    key = told.key(sentences, lang)
    made = reading_file(_cfg(), asset_id, key).is_file()
    return {
        "language": lang, "sentences": sentences, "key": key,
        "voice": _voices()[lang],
        "audio": f"/api/asset/{asset_id}/told/voice.wav?language={lang}&k={key}" if made else None,
    }


@bp.get("/api/easy-view")
@require_family
def easy_view_mine():
    _switched_on()
    return jsonify({"on": easy_store.is_on(_conn(), current_user().id), "voices": _voices()})


@bp.put("/api/easy-view")
@require_family
def easy_view_set_mine():
    _switched_on()
    data = json_object()
    if not isinstance(data.get("on"), bool):
        return jsonify({"error": said("On must be true or false.")}), 400
    user = current_user()
    easy_store.set_on(_conn(), user.id, data["on"], user.id)
    return jsonify({"on": data["on"], "voices": _voices()})


@bp.get("/api/easy-view/people")
@require_admin
def easy_view_people():
    _switched_on()
    conn = _conn()
    users = auth.list_users(conn)
    return jsonify({"on": sorted(easy_store.on_for(conn, (u.id for u in users)))})


@bp.put("/api/easy-view/people/<int:user_id>")
@require_admin
def easy_view_set_person(user_id: int):
    _switched_on()
    data = json_object()
    if not isinstance(data.get("on"), bool):
        return jsonify({"error": said("On must be true or false.")}), 400
    conn = _conn()
    if auth.get_user(conn, user_id) is None:
        abort(404)
    easy_store.set_on(conn, user_id, data["on"], current_user().id)
    return jsonify({"id": user_id, "on": data["on"]})


@bp.get("/api/asset/<int:asset_id>/told")
@require_family
def easy_view_told(asset_id: int):
    _switched_on()
    return jsonify(_answer(asset_id, _wanted()))


@bp.post("/api/asset/<int:asset_id>/told/voice")
@require_family
def easy_view_told_voice(asset_id: int):
    """Tell the photograph aloud, or answer with the reading already made."""
    _switched_on()
    cfg = _cfg()
    lang = _wanted(json_object().get("language"))
    _, sentences = _said(asset_id, lang)
    key = told.key(sentences, lang)
    target = reading_file(cfg, asset_id, key)
    if target.is_file():
        return jsonify(_answer(asset_id, lang))
    if why := voice_mod.ready(lang):
        return jsonify({"error": why}), 409
    host = _host()
    if host is None:
        return jsonify({"error": said("The on-device engine is not running.")}), 503
    if not _speaking.acquire(timeout=1.0):
        return jsonify({"error": said("Ninaivu is reading something else aloud; try again in a moment.")}), 429
    pending = target.with_name(f".{target.stem}.{os.getpid()}.part.wav")
    try:
        try:
            host.call("speak", timeout=SPEAK_SECONDS, wait=HOST_WAIT,
                      sentences=[{"text": s, "pause": 0.35} for s in sentences],
                      language=lang, out=str(pending))
        except runtime.HostBusy:
            return jsonify({"error": said("Ninaivu is busy; try again in a moment.")}), 429
        except runtime.Unavailable as exc:
            log.info("photograph %s found no voice: %s", asset_id, exc)
            return jsonify({"error": voice_mod.ready(lang)
                            or said("the voice could not be loaded; see the log")}), 409
        except runtime.HostError as exc:
            log.warning("photograph %s could not be told aloud: %s", asset_id, exc)
            return jsonify({"error": said("This photograph could not be told aloud.")}), 500
        os.replace(pending, target)
        _keep_few(cfg)
    finally:
        _speaking.release()
        pending.unlink(missing_ok=True)
    return jsonify(_answer(asset_id, lang))


@bp.get("/api/asset/<int:asset_id>/told/voice.wav")
@require_family
def easy_view_told_file(asset_id: int):
    _switched_on()
    lang = _wanted()
    _, sentences = _said(asset_id, lang)
    path = reading_file(_cfg(), asset_id, told.key(sentences, lang))
    if not path.is_file():
        abort(404)
    response = send_file(str(path), mimetype="audio/wav", conditional=True, max_age=0)
    response.headers["Cache-Control"] = "private, no-cache"
    return response
