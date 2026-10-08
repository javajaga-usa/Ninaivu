"""Scan old prints: the family app's side of ninaivu/media/print_scan.py.

Three steps, because there is a person in the middle of them:

1. ``POST /api/prints/detect`` takes the photographs of the prints, finds the
   prints in them and answers with a small picture of each, plus a guess at
   the decade where the picture-search model is installed. The photographs
   are held in a private folder under Ninaivu's state directory meanwhile,
   for the person who sent them only.
2. The person unticks what is not a print, chooses the clean-up and says when
   the prints are from.
3. ``POST /api/prints/save`` develops and files the ticked prints as a
   background job (media/jobs.py), polled at ``/api/prints/jobs/<id>``.

The paths are ``/api/prints/…`` rather than ``/api/scan…``: everything under
``/api/scan`` is library management and belongs to the console alone
(tests/test_known_defects.py), and this belongs to the family.

Who may do what is what uploading already settled: a family member or an
administrator, never a guest; into a library folder they were assigned and
inside the part of it they were given. An administrator's prints go into the
library at once, as an edited copy does; a family member's wait in the review
queue, as every upload of theirs does.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from flask import abort, jsonify, request

from ..media import jobs, print_scan
from ..media.jobs import JobError
from ..server.auth import current_user, require_family
from ..utils.filenames import safe_filename
from ._body import json_object
from .api import UPLOAD_EXTENSIONS, _cfg, _engine, _roots, _safe_under, _viewer, bp


def _where() -> tuple[str, str]:
    """The library and the folder in it a scan goes to — as for /api/upload."""
    cfg = _cfg()
    roots = _roots()
    if not roots:
        abort(403, description="Your assigned library is no longer available")
    root = cfg.active_root if cfg.active_root in roots else roots[0]
    if not Path(root).is_dir():
        abort(409, description="The library folder is unavailable")
    scope = _viewer()["scope"] or ""
    if not _safe_under(Path(root) / scope, Path(root)):
        abort(403, description="Upload folder must stay inside your library")
    return root, scope


def _capabilities() -> dict:
    user = current_user()
    tools = print_scan.tools_available()
    return {
        "restore": tools["restore"],
        "upscale": tools["upscale"],
        "year_guess": print_scan.year_model_ready(_engine()),
        "needs_review": not user.is_admin,
        "max_photos": print_scan.MAX_PHOTOS,
        "max_photo_mb": print_scan.MAX_PHOTO_BYTES // (1024 * 1024),
        "folder": print_scan.FOLDER,
    }


@bp.get("/api/prints/capabilities")
@require_family
def prints_capabilities():
    """What the scan screen can offer here: which clean-up models are
    installed, whether a decade can be guessed, and whether the prints will
    wait for an administrator."""
    _where()
    return jsonify(_capabilities())


def _data_url(data: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")


@bp.post("/api/prints/detect")
@require_family
def prints_detect():
    """Find the prints in the photographs sent, and hold the photographs."""
    _where()
    limit = print_scan.MAX_PHOTOS * print_scan.MAX_PHOTO_BYTES + 1024 * 1024
    if request.content_length and request.content_length > limit:
        abort(413, description="Send fewer photographs at a time.")
    files = [f for f in request.files.getlist("photos") if f and f.filename]
    if not files:
        abort(400, description="Choose or take a photograph of the prints.")
    if len(files) > print_scan.MAX_PHOTOS:
        abort(400, description=f"Up to {print_scan.MAX_PHOTOS} photographs at a time.")

    cfg = _cfg()
    user = current_user()
    token, folder = print_scan.new_session(cfg, user.id)
    meta = {"owner": user.id, "photos": []}
    answer = []
    previews = []
    engine = _engine()
    try:
        for index, upload in enumerate(files):
            name = safe_filename(upload.filename) or f"photo-{index + 1}.jpg"
            suffix = Path(name).suffix.lower()
            if suffix not in UPLOAD_EXTENSIONS:
                abort(400, description=f"{name} is not a photograph.")
            stored = folder / f"{index}{suffix}"
            with stored.open("xb") as output:
                # Read in pieces and counted, so a body that lied about its
                # length is still stopped at the limit.
                written = 0
                while chunk := upload.stream.read(1024 * 1024):
                    written += len(chunk)
                    if written > print_scan.MAX_PHOTO_BYTES:
                        abort(413, description=f"{name} is too large.")
                    output.write(chunk)
            try:
                photo = print_scan.open_photo(stored)
            except (OSError, ValueError, SyntaxError):
                abort(400, description=f"{name} could not be opened as a photograph.")
            seen = print_scan.examine(photo, engine)
            meta["photos"].append({"file": stored.name, "name": name,
                                   "prints": [item.as_dict() for item in seen]})
            answer.append({"name": name, "prints": [
                {"index": i, "whole": item.found.whole,
                 "width": item.image.width, "height": item.image.height,
                 "thumb": _data_url(print_scan.preview(item.image))}
                for i, item in enumerate(seen)]})
            previews.extend(item.image for item in seen)
        print_scan.save_session(folder, meta)
    except BaseException:
        import shutil                                       # noqa: PLC0415
        shutil.rmtree(folder, ignore_errors=True)
        raise
    guess = print_scan.guess_decade(previews[:24], engine)
    return jsonify({"session": token, "photos": answer, "guess": guess,
                    "capabilities": _capabilities()})


def _picks(raw, meta) -> list[tuple[int, int]]:
    photos = meta.get("photos") or []
    if not isinstance(raw, list) or not raw or len(raw) > print_scan.MAX_PHOTOS * print_scan.MAX_PRINTS:
        abort(400, description="Tick at least one print to save.")
    picks = []
    for item in raw:
        if (not isinstance(item, list) or len(item) != 2
                or not all(isinstance(n, int) and not isinstance(n, bool) for n in item)):
            abort(400, description="Tick at least one print to save.")
        photo, spot = item
        if not 0 <= photo < len(photos) or not 0 <= spot < len(photos[photo]["prints"]):
            abort(400, description="That print is not in this batch.")
        if (photo, spot) not in picks:
            picks.append((photo, spot))
    return picks


@bp.post("/api/prints/save")
@require_family
def prints_save():
    """Develop and file the ticked prints, in the background."""
    data = json_object()
    library, scope = _where()
    cfg = _cfg()
    user = current_user()
    held = print_scan.load_session(cfg, data.get("session"), user.id)
    if held is None:
        abort(404, description="This batch has expired. Choose the photographs again.")
    folder, meta = held
    picks = _picks(data.get("picks"), meta)
    try:
        when = print_scan.parse_when(data.get("date"))
    except ValueError as error:
        abort(400, description=str(error))
    tools = print_scan.tools_available()
    factor = data.get("upscale") or 0
    if factor not in (0, 2, 4):
        abort(400, description="Upscale is 2 or 4 times.")
    options = print_scan.Options(
        fix_colours=bool(data.get("fix_colours", True)),
        # Asked for and not installed is not an error worth a refusal: the
        # screen disables both, and a stale screen still gets its prints.
        restore_faces=bool(data.get("restore_faces")) and tools["restore"],
        upscale=factor if tools["upscale"] else 0,
        keep_original=bool(data.get("keep_original", True)),
    )
    user_id, is_admin = user.id, user.is_admin

    def work(report):
        from ..storage import db                            # noqa: PLC0415

        saver = print_scan.Saver(db.connect(cfg.db_path), cfg, library, scope or None,
                                 user_id, is_admin)
        try:
            outcome = print_scan.run_batch(folder, meta, picks, options, when, saver, report)
        finally:
            # This thread's connection, which ends with it.
            db.close_all()
        return json.dumps(outcome).encode("utf-8")

    try:
        job_id = jobs.start(user_id, "prints", work)
    except JobError as error:
        return jsonify(error=str(error)), 503
    return jsonify(id=job_id, status=jobs.status(job_id, user_id)), 202


@bp.get("/api/prints/jobs/<job_id>")
@require_family
def prints_job(job_id: str):
    state = jobs.status(job_id, current_user().id)
    if state is None or state.get("kind") != "prints":
        abort(404)
    return jsonify(state)


@bp.get("/api/prints/jobs/<job_id>/result")
@require_family
def prints_job_result(job_id: str):
    state = jobs.status(job_id, current_user().id)
    if state is None or state.get("kind") != "prints":
        abort(404)
    result = jobs.take_result(job_id, current_user().id)
    if result is None:
        abort(404)
    return jsonify(json.loads(result))
