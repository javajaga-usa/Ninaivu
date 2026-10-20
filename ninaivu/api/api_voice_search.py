"""Speaking to search: a few seconds said aloud, written down here, searched.

Off until ``voice_search`` is switched on, when every route answers 404 as if
it did not exist. Family members and administrators may speak; guests type.

``POST /api/voice-search`` takes the recording itself as the body (the
browser's own WebM, Ogg or MP4, at most :data:`MAX_BYTES`). It is written to
a private file, handed to the quick model host to be written down by the
voice-story model (runtime/speech.py, Whisper, Tamil or English), and the
file is removed whatever happens: a search spoken aloud is never kept. The
answer is what was heard and the words to search with: the words as said,
or for a Tamil search its English beside them when the model can translate,
because the picture search understands English best and a name or a place
is read from English by the gallery's phrase rules.

``GET /api/voice-search`` says whether it can work here and if not why, so
the page can say so before anybody speaks.
"""
from __future__ import annotations

import logging
import os
import secrets
import threading
from pathlib import Path
from typing import Any

from flask import abort, current_app, jsonify

from .. import runtime
from ..runtime import speech
from ..server.auth import current_user, require_family
from ..storage import stories
from ..words import said
from ._body import read_at_most
from .api import _cfg, bp

log = logging.getLogger(__name__)

#: The largest recording: half a minute of a phone's AAC is well under this.
MAX_BYTES = 4 * 1024 * 1024
#: The longest a spoken search is written down for, in seconds.
HEAR_SECONDS = 90.0
#: Seconds a search waits for the quick host to finish somebody else's.
HOST_WAIT = 30.0
#: Searches written down at once. One host writes one at a time; more than
#: this waiting is a household talking faster than it can listen.
_hearing = threading.BoundedSemaphore(3)


def _switched_on() -> None:
    if not getattr(_cfg(), "voice_search", False):
        abort(404)
    if current_user().is_guest:
        abort(404)


def ready() -> str | None:
    """Why a spoken search cannot be written down here, or None."""
    from ..runtime.transcriber import ready as words_ready     # noqa: PLC0415
    return words_ready()


def _host():
    services = current_app.config.get("MV_SERVICES")
    if services is None:
        return None
    return services.quick_host() if hasattr(services, "quick_host") else services.model_host()


def _folder() -> Path:
    folder = Path(_cfg().state_dir) / "voice-search"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    return folder


def query_for(heard: dict[str, Any]) -> str:
    """The words to search with: English when a search said in another
    language came with its English, otherwise what was said."""
    text = str(heard.get("text") or "").strip()
    english = str(heard.get("english") or "").strip()
    if english and (heard.get("language") or "en") != "en":
        return english
    return text


@bp.get("/api/voice-search")
@require_family
def voice_search_status():
    _switched_on()
    why = ready()
    return jsonify({"on": True, "ready": why is None, "why": why or "",
                    "max_bytes": MAX_BYTES})


@bp.post("/api/voice-search")
@require_family
def voice_search():
    _switched_on()
    why = ready()
    if why:
        return jsonify({"error": why}), 409
    data = read_at_most(MAX_BYTES, "A spoken search can be at most half a minute.")
    if not data:
        return jsonify({"error": "Nothing was heard. Press the microphone and speak."}), 400
    found = stories.sniff(data[:64])
    if found is None:
        return jsonify({"error": "That is not a sound recording."}), 415
    host = _host()
    if host is None:
        return jsonify({"error": "The on-device engine is not running."}), 503
    if not _hearing.acquire(timeout=1.0):
        return jsonify({"error": "Ninaivu is listening to other searches; try again in a moment."}), 429
    path = _folder() / f"{secrets.token_hex(12)}{stories.AUDIO_TYPES[found]}"
    try:
        with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out:
            out.write(data)
        from ..media import media                                 # noqa: PLC0415
        heard = host.call(
            "transcribe", timeout=HEAR_SECONDS, wait=HOST_WAIT, path=str(path),
            ffmpeg=media.FFMPEG,
            languages=speech.languages(getattr(_cfg(), "speech_languages", "ta,en")),
            translate=True)
    except runtime.HostBusy:
        return jsonify({"error": "Ninaivu is busy listening; try again in a moment."}), 429
    except runtime.Unavailable as exc:
        # The host's own words stay in the log; the person gets a fixed sentence.
        log.info("a spoken search found no speech model: %s", exc)
        return jsonify({"error": ready() or said("no speech model is downloaded (AI models page)")}), 409
    except runtime.HostError as exc:
        log.warning("a spoken search could not be written down: %s", exc)
        return jsonify({"error": "That could not be made out. Try again, a little closer."}), 422
    finally:
        _hearing.release()
        path.unlink(missing_ok=True)
    query = query_for(heard)
    return jsonify({"text": str(heard.get("text") or ""),
                    "english": str(heard.get("english") or ""),
                    "language": str(heard.get("language") or ""),
                    "query": query, "took": heard.get("took")})
