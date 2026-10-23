"""Moments inside videos (storage/video_moments.py, media/video_moments.py).

Off until ``video_moments`` is switched on, when every route answers 404 as
if it did not exist. Anybody who may search the gallery may search inside
its videos, and is told only about the clips their own limits let them see,
exactly as the picture search is.

``GET /api/moments?q=<words>`` is the clips whose scenes best match the
words, best first, each with the seconds of its best scenes; ``ready`` says
whether the picture-search model is there to read the words, and ``clips``
and ``moments`` how much of the library has been looked into so far.
``GET /api/moments/<id>`` is one clip's scenes, the seconds each starts at.
``GET /api/moments/<id>/frame?at=<seconds>`` is a picture of one of them,
read from the clip by ffmpeg the first time it is asked for and kept.
"""
from __future__ import annotations

import os
from pathlib import Path

from flask import abort, jsonify, request, send_file

from ..media import media
from ..storage import db, video_moments
from .api import (_cfg, _conn, _engine, _guard, _roots, _text_vector, _thumb_version,
                  _viewer, bp)

#: A clip is offered when its best scene is at least this share of the best
#: clip's: a strip of the weakest matches is noise beside the grid.
NEAR_THE_BEST = 0.8
#: Words read of a search.
MAX_QUERY = 200
#: The longest side of a scene's picture, and how many are kept on disk.
FRAME_SIDE = 480
FRAMES_KEPT = 3000


def _switched_on() -> None:
    if not getattr(_cfg(), "video_moments", False):
        abort(404)


def _search_engine():
    engine = _engine()
    if engine is None or not getattr(engine, "semantic", False):
        return None
    return engine


@bp.get("/api/moments")
def moments_search():
    _switched_on()
    conn = _conn()
    text = " ".join(str(request.args.get("q") or "").split())[:MAX_QUERY]
    engine = _search_engine()
    done = video_moments.counts(conn)
    answer = {"ready": engine is not None, "clips": [], **{f"looked_{k}": v for k, v in done.items()}}
    if not text or engine is None:
        return jsonify(answer)
    vector = _text_vector(engine, text)
    if vector is None:
        return jsonify(answer)
    found = video_moments.search(conn, vector, str(getattr(engine, "model_id", "") or ""),
                                 min_score=float(getattr(engine, "search_floor", 0.15)))
    if not found:
        return jsonify(answer)
    # Narrowed to what this viewer may see, through the gallery's own query:
    # visibility, folders, dates, the bin and explicit content alike.
    wanted = [f["id"] for f in found]
    rows, _ = db.query_assets(conn, _roots(), ids=wanted, kinds=["video"],
                              limit=len(wanted), offset=0, with_total=False, **_viewer())
    allowed = {int(r["id"]): r for r in rows}
    # Then held to the best of what they may see: measured against a hidden
    # clip's score, a family member's own good matches were cut, and how
    # many were cut said something about what they cannot see.
    found = [f for f in found if int(f["id"]) in allowed]
    if not found:
        return jsonify(answer)
    best = found[0]["score"]
    found = [f for f in found if f["score"] >= best * NEAR_THE_BEST]
    clips = []
    for clip in found:
        row = allowed.get(int(clip["id"]))
        if row is None:
            continue
        clips.append({
            "id": int(row["id"]), "name": row.get("name") or Path(str(row["rel_path"])).name,
            "date": row.get("date_key") or "", "duration": float(row.get("duration") or 0),
            "thumb_v": _thumb_version(row), "score": clip["score"], "moments": clip["moments"],
        })
        if len(clips) >= video_moments.MAX_RESULTS:
            break
    answer["clips"] = clips
    return jsonify(answer)


@bp.get("/api/moments/<int:asset_id>")
def moments_of_clip(asset_id: int):
    _switched_on()
    conn = _conn()
    row = _guard(db.get_asset_brief(conn, asset_id))
    if row.get("kind") != "video":
        abort(404)
    return jsonify({"id": asset_id, "moments": video_moments.of_clip(conn, asset_id)})


def _frames_dir() -> Path:
    return Path(_cfg().thumbs_dir) / "moments"


def _keep_few(folder: Path) -> None:
    """The oldest pictures of scenes go once there are more than FRAMES_KEPT."""
    try:
        files = sorted(folder.rglob("*.jpg"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    for old in files[:max(0, len(files) - FRAMES_KEPT)]:
        try:
            old.unlink()
        except OSError:
            pass


@bp.get("/api/moments/<int:asset_id>/frame")
def moment_frame(asset_id: int):
    _switched_on()
    conn = _conn()
    row = _guard(db.get_asset_brief(conn, asset_id))
    if row.get("kind") != "video":
        abort(404)
    try:
        at = float(request.args.get("at", ""))
    except ValueError:
        abort(400, description="at must be a number of seconds.")
    # Only a scene that was found: not a way to have ffmpeg read any second
    # of any clip on request.
    known = [s for s in video_moments.of_clip(conn, asset_id) if abs(s - at) < 0.05]
    if not known:
        abort(404)
    at = known[0]
    folder = _frames_dir()
    name = f"{row.get('thumb') or asset_id}_{int(round(at * 100))}_{int(row.get('mtime') or 0)}.jpg"
    path = folder / name
    if not path.is_file():
        source = Path(str(row["root"])) / str(row["rel_path"])
        if not source.is_file():
            abort(404)
        image = media.extract_video_frame(source, at, max_side=FRAME_SIDE)
        if image is None:
            abort(404)
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_suffix(".part")
        image.save(part, "JPEG", quality=82)
        os.replace(part, path)
        _keep_few(folder)
    response = send_file(path, mimetype="image/jpeg", conditional=True, max_age=86400)
    response.headers["Cache-Control"] = "private, max-age=86400"
    return response
