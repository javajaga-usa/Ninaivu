"""Talk about this photo: ask the open photograph a question, answered here.

Off until ``photo_talk`` is switched on, when every route answers 404 as if
it did not exist. Family members and administrators may ask about a
photograph they can see; guests, who are never told who is in a photograph
or where it was taken, may not.

``GET /api/asset/<id>/talk`` says whether this photograph can be talked
about: the on-device engine and the model with its vision part ready, and the
photograph one AI may read. A hidden photograph (``db.AI_MAY_READ``: hidden
by an administrator, by its folder, or kept "Only to me") is never shown to
the model, nor is one marked explicit, even to the administrator who can see
it; the viewer then does not offer it.

``POST /api/asset/<id>/talk`` with ``{"question": "…", "turns": [{"q", "a"}]}``
turns the photograph upright, writes it as a small private JPEG for the model
host, asks (runtime/llm.py ``look``, media/photo_talk.py) and removes the
file as soon as the answer is back. Neither the question nor the answer is
kept anywhere; the page holds the talk while it is open.
"""
from __future__ import annotations

import contextlib
import logging
import os
import secrets
import time
from pathlib import Path

from flask import abort, current_app, jsonify
from PIL import Image

from .. import runtime
from ..media import media, photo_talk, upright
from ..server import features
from ..server.auth import current_user, require_family
from ..storage import db
from ..storage import descriptions as descriptions_mod
from ..words import Said, said
from . import _long_calls
from ._body import json_object
from .api import _asset_file, _cfg, _conn, _guard, bp

log = logging.getLogger(__name__)

#: The longest side of the picture the model is shown. Gemma 3 sees 896
#: pixels a side whatever it is given; a little more keeps small writing
#: legible through the resize.
MAX_SIDE = 1024
#: Seconds an answer may take, the model's loading aside (the host adds its
#: own allowance the first time).
LOOK_SECONDS = 150.0
#: Seconds to wait for the model host to finish another call.
HOST_WAIT = 15.0
#: Questions answered at once.
_asking = _long_calls.Limited(2)


def _switched_on() -> None:
    if not features.on(_cfg(), "photo_talk"):
        abort(404)
    if current_user().is_guest:
        abort(404)


def _picture(asset_id: int) -> dict:
    row = _guard(db.get_asset(_conn(), asset_id, current_user().id))
    if row.get("kind") != "picture" or row.get("trashed"):
        abort(404)
    return row


def _kept_from_ai(row: dict) -> str | None:
    """Why this photograph is not shown to the model, or None."""
    if int(row.get("visibility") or 0) >= 2:
        return said("This photograph is hidden, so it is never shown to the AI.")
    if row.get("nsfw"):
        return said("This photograph is marked explicit, so it is never shown to the AI.")
    return None


def _ready() -> str | None:
    """Why nothing can be asked on this computer now, or None."""
    if not runtime.enabled(_cfg()):
        return said("the on-device AI engine is off (Advanced settings)")
    from ..runtime import llm                                   # noqa: PLC0415
    return llm.vision_ready()


def _host():
    services = current_app.config.get("MV_SERVICES")
    return services.model_host() if services is not None and hasattr(services, "model_host") else None


#: Seconds after which a picture left in the talk folder is a leftover.
LEFTOVER_SECONDS = 3600


def _folder() -> Path:
    folder = Path(_cfg().state_dir) / "talk"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Each picture is removed as soon as its answer is back; one left behind
    # by a crash or a power cut mid-answer goes now, rather than staying
    # beside the index for ever (it may be of a family member's own photo).
    stale = time.time() - LEFTOVER_SECONDS
    try:
        for old in folder.glob("*.jpg"):
            with contextlib.suppress(OSError):
                if old.stat().st_mtime < stale:
                    old.unlink()
    except OSError:
        pass
    return folder


@bp.get("/api/asset/<int:asset_id>/talk")
@require_family
def talk_status(asset_id: int):
    _switched_on()
    row = _picture(asset_id)
    kept = _kept_from_ai(row)
    why = _ready()
    return jsonify({"on": True, "allowed": kept is None, "kept": kept or "",
                    "ready": why is None, "why": why or "",
                    "max_question": photo_talk.MAX_QUESTION})


@bp.post("/api/asset/<int:asset_id>/talk")
@require_family
def talk(asset_id: int):
    _switched_on()
    row = _picture(asset_id)
    if kept := _kept_from_ai(row):
        return jsonify({"error": kept}), 403
    data = json_object()
    question = str(data.get("question") or "").strip()
    if not question:
        return jsonify({"error": "Type a question first."}), 400
    if len(question) > photo_talk.MAX_QUESTION:
        return jsonify({"error": Said("A question can be at most {max} characters.",
                                      max=photo_talk.MAX_QUESTION)}), 400
    if why := _ready():
        return jsonify({"error": why}), 409
    host = _host()
    if host is None:
        return jsonify({"error": "The on-device engine is not running."}), 503
    if not _asking.acquire(timeout=1.0):
        return jsonify({"error": "Ninaivu is answering other questions; ask again in a moment."}), 429
    token = secrets.token_hex(12)
    shown = _folder() / f"{token}.jpg"
    try:
        try:
            with media._open_oriented(_asset_file(row)) as opened:      # noqa: SLF001
                picture = upright.apply(opened.convert("RGB"), int(row.get("rotation") or 0))
                picture.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
                with open(os.open(shown, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out:
                    picture.save(out, "JPEG", quality=90)
        except (OSError, ValueError, Image.DecompressionBombError):
            return jsonify({"error": "This photograph could not be opened."}), 422
        conn = _conn()
        cfg = _cfg()
        language = photo_talk.language_of(question)
        known = photo_talk.notes(row, names=photo_talk.names_in(db.faces_for_asset(conn, asset_id)),
                                 description=descriptions_mod.get(conn, asset_id))
        messages = photo_talk.model_messages(
            question, language=language, known=known,
            turns=photo_talk.earlier(data.get("turns")),
            household=getattr(cfg, "house_name", "") or "")
        try:
            reply = host.call("look", timeout=LOOK_SECONDS, wait=HOST_WAIT, path=str(shown),
                              messages=messages, max_tokens=photo_talk.ANSWER_TOKENS,
                              temperature=0.2)
        except runtime.HostBusy:
            return jsonify({"error": "Ninaivu is busy; ask again in a moment."}), 429
        except runtime.Unavailable as exc:
            # The host's own words stay in the log; the person gets a fixed sentence.
            log.info("photograph %s could not be looked at: %s", asset_id, exc)
            return jsonify({"error": _ready() or said(
                "the vision part of the writing model is not downloaded (AI models page)")}), 409
        except runtime.HostError as exc:
            log.warning("photograph %s could not be talked about: %s", asset_id, exc)
            return jsonify({"error": "No answer came this time. Ask again in a moment."}), 500
    finally:
        _asking.release()
        shown.unlink(missing_ok=True)
    answer = photo_talk.cleaned(str(reply.get("text") or ""))
    if not answer:
        return jsonify({"error": "No answer came this time. Ask again in a moment."}), 500
    return jsonify({"answer": answer, "language": language,
                    "model": str(reply.get("model") or ""),
                    "took": reply.get("took")})
