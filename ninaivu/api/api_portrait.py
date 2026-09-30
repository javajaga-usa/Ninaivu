"""Finding the people in a photograph, for the Skin and Hair retouching tools.

The editors work in the browser, on pixels that never leave it. What they cannot
do there is the one thing that needs a model: say where the faces are. This is
that one request. The browser sends the picture it is showing, the household's
own face detector finds the faces, and what comes back is a description — a
handful of numbers per person and three small pictures that say, pixel by pixel,
which of them is skin, which is hair and which is a bindi. Nothing is stored and
nothing is sent anywhere else.

What the maps mean, and how they are found, is in :mod:`ninaivu.media.portrait`.
"""

from __future__ import annotations

import base64
import io
import logging
import threading

from flask import jsonify, request
from PIL import Image

from ..media import face_parser, portrait
from ..media.faces import pil_to_bgr
from ..server.auth import require_family
from .api import bp
from .api_faces import _face_indexer

log = logging.getLogger("ninaivu.portrait")

#: Looking at a large photograph takes a few hundred megabytes while it is done, and
#: a few seconds of a processor. Two at once is what a household does; more than that
#: wait for their turn rather than filling the machine.
_SLOTS = threading.BoundedSemaphore(2)

#: A picture to analyse is at most this many megapixels as it arrives. The
#: editor sends what it is showing, already reduced; a bigger one is a mistake
#: or a mischief, and decoding it could fill the machine's memory.
MAX_PIXELS = 60_000_000


def _png(array) -> str:
    buffer = io.BytesIO()
    Image.fromarray(array, mode="RGB").save(buffer, format="PNG", compress_level=3)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


@bp.post("/api/portrait/analyse")
@require_family
def portrait_analyse():
    """Every face in the picture sent as the request body, and what each needs."""
    if not portrait.available():
        return jsonify(error="Finding faces needs OpenCV, which this installation does not have."), 501

    body = request.get_data(cache=False)
    if not body:
        return jsonify(error="No picture was sent."), 400

    engine = _face_indexer().engine
    if not engine.available:
        return jsonify(error="Finding faces needs the face model. Download it under AI models, "
                             "or paint the area by hand.", needs="faces"), 503

    try:
        with Image.open(io.BytesIO(body)) as source:
            if source.width * source.height > MAX_PIXELS:
                return jsonify(error="That picture is too large to look at."), 413
            source.draft("RGB", (portrait.WORK_SIZE, portrait.WORK_SIZE))
            picture = source.convert("RGB")
        picture.thumbnail((portrait.WORK_SIZE, portrait.WORK_SIZE), Image.Resampling.LANCZOS)
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        return jsonify(error=f"That picture could not be read: {exc}"), 400

    bgr = pil_to_bgr(picture)
    located = engine.locate(bgr, max_dim=portrait.WORK_SIZE)
    if not located:
        return jsonify(size=list(picture.size), faces=[], maps=None,
                       reason="No face was found in this photograph.")

    try:
        with _SLOTS:
            analysis = portrait.analyse(bgr[:, :, ::-1].copy(), located, parser=face_parser.get())
    except Exception:                                   # noqa: BLE001
        log.exception("portrait analysis failed")
        return jsonify(error="Something went wrong looking at the faces. You can still paint "
                             "the area by hand."), 500

    if not any(face.get("usable") for face in analysis.faces):
        return jsonify(size=list(analysis.size), faces=analysis.faces, maps=None,
                       reason="The faces in this photograph are too small or turned away to work on.")

    maps = {name: _png(array) for name, array in portrait.pack_planes(analysis).items()}
    return jsonify(size=list(analysis.size), faces=analysis.faces, maps=maps,
                   cast=analysis.cast, engine=portrait.engine_of(analysis))
