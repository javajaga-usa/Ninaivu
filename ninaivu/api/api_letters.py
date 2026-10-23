"""Letters read aloud in the family app (media/letters.py, runtime/voice.py,
static/js/letters.js).

Off until ``letters_aloud`` is switched on (Settings → Advanced), when every
route answers 404 as if it did not exist. For the family and administrators,
on any photograph they may see; a guest is never offered it.

``GET /api/asset/<id>/letter`` is the photograph's words, if it has some,
which voices are ready, and the reading kept of the words as they are.
``PUT /api/asset/<id>/letter`` with ``{"words", "language", "source"}`` keeps
the words (``source`` is ``read`` when they are what the text reader found
and nobody changed them); ``DELETE`` lets them and their reading go.
``POST /api/asset/<id>/letter/read`` reads the words on the photograph with
the text reader already in the app and answers with them, unsaved, for the
family to correct before they keep them. Never on a hidden photograph: what
an administrator hides is never read by AI (``db.AI_MAY_READ``), so its words
can only be typed.
``POST /api/asset/<id>/letter/voice`` reads the kept words aloud on the quick
model host, or answers at once with the reading already kept of them;
``GET /api/asset/<id>/letter/voice.wav`` is that reading.

The photograph is never written to. The reading is a WAV in the state
folder, named by the key of the words and the voice (media/letters.py), so
changing a word makes a new one; the old one is removed when it is.
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

from flask import abort, current_app, jsonify, send_file

from .. import runtime
from ..media import letters, media, upright
from ..runtime import voice as voice_mod
from ..server.auth import current_user, require_family
from ..storage import db, letters as letters_store
from ..words import said
from . import _long_calls
from ._body import json_object
from .api import _asset_file, _cfg, _conn, _guard, bp

log = logging.getLogger(__name__)

#: Seconds a letter may take to be read aloud, the voices' loading included
#: (a few pages of Tamil take the processor a minute or so).
SPEAK_SECONDS = 420.0
#: Seconds to wait for the quick host to finish somebody else's call.
HOST_WAIT = 30.0
#: The longest side the text reader is handed: it reduces the photograph to
#: media/letters.py LETTER_EDGE itself; this only bounds what is opened.
MAX_SIDE = 6000

_reading = threading.BoundedSemaphore(1)
_speaking = _long_calls.Limited(1)


def _switched_on() -> None:
    if not getattr(_cfg(), "letters_aloud", False):
        abort(404)
    if current_user().is_guest:
        abort(404)


def _picture(asset_id: int) -> dict[str, Any]:
    row = _guard(db.get_asset(_conn(), asset_id, current_user().id))
    if row.get("kind") != "picture" or row.get("trashed"):
        abort(404)
    return row


def _host():
    services = current_app.config.get("MV_SERVICES")
    if services is None:
        return None
    return services.quick_host() if hasattr(services, "quick_host") else services.model_host()


def folder(cfg: Any) -> Path:
    path = Path(cfg.state_dir) / "letters"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def reading_file(cfg: Any, asset_id: int, key: str) -> Path:
    return folder(cfg) / f"{int(asset_id)}-{key}.wav"


def _clear_readings(cfg: Any, asset_id: int, keep: str | None = None) -> None:
    for old in folder(cfg).glob(f"{int(asset_id)}-*.wav"):
        if keep is None or old.name != f"{int(asset_id)}-{keep}.wav":
            old.unlink(missing_ok=True)


def kept_reading(cfg: Any, conn: Any, asset_id: int) -> dict[str, Any] | None:
    """The reading kept of *asset_id*'s words as they are now, with its
    ``path``, or None: none was made, the words changed since, or the file
    is gone."""
    found = letters_store.get(conn, asset_id)
    if not found or not found["reading"]:
        return None
    reading = found["reading"]
    if reading.get("key") != letters.key(found["words"], found["language"]):
        return None
    path = reading_file(cfg, asset_id, str(reading["key"]))
    if not path.is_file():
        return None
    return {**reading, "path": str(path), "language": found["language"]}


def narration_for(cfg: Any, conn: Any, asset_id: int) -> dict[str, Any] | None:
    """A letter's reading as a memory film tells a story (media/films.py):
    its sound, length, language and the sentences as subtitles. None when
    the switch is off or there is no reading of the words as they are."""
    if not getattr(cfg, "letters_aloud", False):
        return None
    reading = kept_reading(cfg, conn, asset_id)
    if not reading:
        return None
    return {"path": reading["path"], "duration": float(reading.get("duration") or 0.0),
            "language": reading["language"],
            "segments": [{"start": s["start"], "end": s["end"], "text": s["text"]}
                         for s in reading.get("segments") or []]}


def _voices() -> dict[str, dict[str, Any]]:
    return {language: {"ready": (why := voice_mod.ready(language)) is None, "why": why or ""}
            for language in letters.LANGUAGES}


def _answer(asset_id: int) -> dict[str, Any]:
    cfg, conn = _cfg(), _conn()
    found = letters_store.get(conn, asset_id)
    reading = kept_reading(cfg, conn, asset_id)
    from ..media import ocr                                     # noqa: PLC0415
    return {
        "on": True,
        "words": found["words"] if found else "",
        "language": found["language"] if found else "",
        "source": found["source"] if found else "",
        "voices": _voices(),
        "reader": ocr.available(),
        # A hidden photograph is never read by AI; its words are typed.
        "readable": db.ai_may_read(conn, asset_id),
        "reading": {"audio": f"/api/asset/{asset_id}/letter/voice.wav?k={reading['key']}",
                    "segments": reading.get("segments") or [],
                    "duration": reading.get("duration") or 0} if reading else None,
    }


@bp.get("/api/asset/<int:asset_id>/letter")
@require_family
def letter_status(asset_id: int):
    _switched_on()
    _picture(asset_id)
    return jsonify(_answer(asset_id))


@bp.put("/api/asset/<int:asset_id>/letter")
@require_family
def letter_keep(asset_id: int):
    _switched_on()
    _picture(asset_id)
    data = json_object()
    words = letters.tidy(str(data.get("words") or ""))
    if not words:
        return jsonify({"error": said("There are no words to keep.")}), 400
    language = str(data.get("language") or "") or letters.language_of(words)
    if language not in letters.LANGUAGES:
        language = letters.language_of(words)
    source = "read" if data.get("source") == "read" else "typed"
    conn = _conn()
    letters_store.put(conn, asset_id, words, language, source, current_user().id)
    _clear_readings(_cfg(), asset_id, keep=letters.key(words, language))
    return jsonify(_answer(asset_id))


@bp.delete("/api/asset/<int:asset_id>/letter")
@require_family
def letter_remove(asset_id: int):
    _switched_on()
    _picture(asset_id)
    letters_store.remove(_conn(), asset_id)
    _clear_readings(_cfg(), asset_id)
    return jsonify(_answer(asset_id))


@bp.post("/api/asset/<int:asset_id>/letter/read")
@require_family
def letter_read(asset_id: int):
    """The words the text reader finds on the photograph, not kept."""
    _switched_on()
    row = _picture(asset_id)
    if int(row.get("visibility") or 0) >= 2:
        # Hidden is where a passport or a bank letter lives: AI never reads
        # it (hidden-not-read-by-AI), and the text reader is AI.
        return jsonify({"error": said("This photograph is hidden, so it is never read by AI. Its words can be typed instead.")}), 403
    from ..media import ocr                                     # noqa: PLC0415
    if not ocr.available():
        return jsonify({"error": said("The text reader is not installed (Reading the words in photographs, under Extras on the Home & extensions page). The words can be typed instead.")}), 409
    if not _reading.acquire(timeout=1.0):
        return jsonify({"error": said("Ninaivu is reading another letter; try again in a moment.")}), 429
    try:
        try:
            with media._open_oriented(_asset_file(row)) as opened:      # noqa: SLF001
                picture = upright.apply(opened.convert("RGB"), int(row.get("rotation") or 0))
                if max(picture.size) > MAX_SIDE:
                    picture.thumbnail((MAX_SIDE, MAX_SIDE))
        except (OSError, ValueError) as exc:
            log.info("photograph %s could not be opened to read: %s", asset_id, exc)
            return jsonify({"error": said("This photograph could not be opened.")}), 422
        found = letters.read_from_photo(picture)
    finally:
        _reading.release()
    return jsonify({"words": found["words"], "language": found["language"],
                    "lines": found["lines"]})


@bp.post("/api/asset/<int:asset_id>/letter/voice")
@require_family
def letter_voice(asset_id: int):
    """Read the kept words aloud, or answer with the reading already kept."""
    _switched_on()
    _picture(asset_id)
    cfg, conn = _cfg(), _conn()
    found = letters_store.get(conn, asset_id)
    if not found or not found["words"]:
        return jsonify({"error": said("Keep the words first, then they can be read aloud.")}), 409
    if kept_reading(cfg, conn, asset_id):
        return jsonify(_answer(asset_id))
    language = found["language"]
    if why := voice_mod.ready(language):
        return jsonify({"error": why}), 409
    host = _host()
    if host is None:
        return jsonify({"error": said("The on-device engine is not running.")}), 503
    sentences = letters.sentences(found["words"])
    if not sentences:
        return jsonify({"error": said("Nothing in these words can be read aloud.")}), 422
    if not _speaking.acquire(timeout=1.0):
        return jsonify({"error": said("Ninaivu is reading another letter aloud; try again in a moment.")}), 429
    key = letters.key(found["words"], language)
    target = reading_file(cfg, asset_id, key)
    pending = target.with_name(f".{target.stem}.{os.getpid()}.part.wav")
    try:
        try:
            made = host.call("speak", timeout=SPEAK_SECONDS, wait=HOST_WAIT,
                             sentences=sentences, language=language, out=str(pending))
        except runtime.HostBusy:
            return jsonify({"error": said("Ninaivu is busy; try again in a moment.")}), 429
        except runtime.Unavailable as exc:
            log.info("photograph %s found no voice: %s", asset_id, exc)
            return jsonify({"error": voice_mod.ready(language)
                            or said("the voice could not be loaded; see the log")}), 409
        except runtime.HostError as exc:
            log.warning("photograph %s could not be read aloud: %s", asset_id, exc)
            return jsonify({"error": said("These words could not be read aloud.")}), 500
        # The words may have been changed while they were being read.
        now = letters_store.get(conn, asset_id)
        if not now or letters.key(now["words"], now["language"]) != key:
            return jsonify({"error": said("The words were changed while they were read; try again.")}), 409
        os.replace(pending, target)
        letters_store.keep_reading(conn, asset_id, {
            "key": key, "duration": made.get("duration"), "segments": made.get("segments") or [],
            "voices": made.get("voices") or [language]})
        _clear_readings(cfg, asset_id, keep=key)
    finally:
        _speaking.release()
        pending.unlink(missing_ok=True)
    return jsonify(_answer(asset_id))


@bp.get("/api/asset/<int:asset_id>/letter/voice.wav")
@require_family
def letter_voice_file(asset_id: int):
    _switched_on()
    _picture(asset_id)
    reading = kept_reading(_cfg(), _conn(), asset_id)
    if not reading:
        abort(404)
    response = send_file(reading["path"], mimetype="audio/wav", conditional=True, max_age=0)
    response.headers["Cache-Control"] = "private, no-cache"
    return response
