"""Words spoken in videos (storage/video_words.py, media/video_words.py).

Off until ``video_words`` is switched on, when every route answers 404 as if
it did not exist. What was said in a video is for whoever may open the video,
as a voice story is for whoever may open its photograph: every answer goes
through the gallery's own limits.

``GET /api/video-words?q=<words>`` is the videos with a line holding every
word, most lines first, each with its first few matching lines (start, end,
the words as said and in English); ``ready`` says whether this computer can
listen at all, and ``looked_clips`` and ``looked_lines`` how much of the
library has been listened to so far.
``GET /api/video-words/<id>`` is every line of one video, for the viewer.
``GET /api/video-words/<id>.vtt?lang=spoken|english`` is the same as WebVTT
subtitles, for the viewer's ``<track>``.
``GET /api/video-words/<id>/frame?at=<seconds>`` is a picture of the video
where one of its lines starts, read by ffmpeg the first time and kept.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from flask import Response, abort, jsonify, request, send_file

from ..media import media
from ..server import features
from ..storage import db, video_words
from .api import _cfg, _conn, _guard, _roots, _thumb_version, _viewer, bp
from .api_video_moments import _keep_few

#: Words read of a search.
MAX_QUERY = 200
#: The longest side of a line's picture.
FRAME_SIDE = 480


def _switched_on() -> None:
    if not features.on(_cfg(), "video_words"):
        abort(404)


def _ready() -> bool:
    if not features.on(_cfg(), "ai_runtime") or not media.FFMPEG:
        return False
    from ..runtime.transcriber import ready                  # noqa: PLC0415
    return ready() is None


def _video(asset_id: int) -> dict:
    row = _guard(db.get_asset_brief(_conn(), asset_id))
    if row.get("kind") != "video":
        abort(404)
    return row


@bp.get("/api/video-words")
def video_words_search():
    _switched_on()
    conn = _conn()
    text = " ".join(str(request.args.get("q") or "").split())[:MAX_QUERY]
    done = video_words.counts(conn)
    answer = {"ready": _ready(), "clips": [], **{f"looked_{k}": v for k, v in done.items()}}
    found = video_words.search(conn, text) if text else []
    if not found:
        return jsonify(answer)
    # Narrowed to what this viewer may see, through the gallery's own query:
    # visibility, folders, dates, the bin and explicit content alike.
    wanted = [f["id"] for f in found]
    rows, _ = db.query_assets(conn, _roots(), ids=wanted, kinds=["video"],
                              limit=len(wanted), offset=0, with_total=False, **_viewer())
    allowed = {int(r["id"]): r for r in rows}
    clips = []
    for clip in found:
        row = allowed.get(int(clip["id"]))
        if row is None:
            continue
        clips.append({
            "id": int(row["id"]), "name": row.get("name") or Path(str(row["rel_path"])).name,
            "date": row.get("date_key") or "", "duration": float(row.get("duration") or 0),
            "thumb_v": _thumb_version(row), "hits": clip["hits"], "lines": clip["lines"],
        })
        if len(clips) >= video_words.MAX_RESULTS:
            break
    answer["clips"] = clips
    return jsonify(answer)


@bp.get("/api/video-words/<int:asset_id>")
def video_words_of_clip(asset_id: int):
    _switched_on()
    _video(asset_id)
    lines = video_words.of_clip(_conn(), asset_id)
    languages = sorted({line["language"] for line in lines if line["language"]})
    return jsonify({"id": asset_id, "lines": lines, "languages": languages,
                    "english": any(line["english"] for line in lines)})


@bp.get("/api/video-words/<int:asset_id>.vtt")
def video_words_subtitles(asset_id: int):
    _switched_on()
    _video(asset_id)
    which = "english" if request.args.get("lang") == "english" else "spoken"
    body = video_words.vtt(video_words.of_clip(_conn(), asset_id), which)
    response = Response(body, mimetype="text/vtt")
    response.headers["Cache-Control"] = "private, no-cache"
    return response


@bp.get("/api/video-words/<int:asset_id>/frame")
def video_words_frame(asset_id: int):
    _switched_on()
    row = _video(asset_id)
    try:
        at = float(request.args.get("at", ""))
    except ValueError:
        abort(400, description="at must be a number of seconds.")
    # Only where a line starts: not a way to have ffmpeg read any second of
    # any video on request.
    known = [line["at"] for line in video_words.of_clip(_conn(), asset_id)
             if abs(line["at"] - at) < 0.05]
    if not known:
        abort(404)
    at = known[0]
    folder = Path(_cfg().thumbs_dir) / "words"
    name = f"{row.get('thumb') or asset_id}_{int(round(at * 100))}_{int(row.get('mtime') or 0)}.jpg"
    path = folder / name
    if not path.is_file():
        source = Path(str(row["root"])) / str(row["rel_path"])
        if not source.is_file():
            abort(404)
        # A second in: the speaker's face rather than the frame before.
        image = media.extract_video_frame(source, at + 0.5, max_side=FRAME_SIDE)
        if image is None:
            abort(404)
        path.parent.mkdir(parents=True, exist_ok=True)
        # A name of its own: two people opening the same scene at once each
        # wrote the same ".part", and one moved the other's away mid-write.
        part = path.with_name(f".{path.stem}.{os.getpid()}.{threading.get_ident()}.part")
        try:
            image.save(part, "JPEG", quality=82)
            os.replace(part, path)
        finally:
            part.unlink(missing_ok=True)
        _keep_few(folder)
    response = send_file(path, mimetype="image/jpeg", conditional=True, max_age=86400)
    response.headers["Cache-Control"] = "private, max-age=86400"
    return response
