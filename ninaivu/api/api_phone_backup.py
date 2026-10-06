"""Phone backup: the family app's routes. See ninaivu/media/phone_backup.py.

Family members and administrators may back up; guests may not, the same as
uploading. A phone only ever sees its own person's files — every row is looked
up by the signed-in profile as well as by its id.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from flask import abort, current_app, jsonify, request
from werkzeug.utils import secure_filename

from ..media import phone_backup
from ..storage import roots as roots_kit
from ..utils.filenames import safe_filename
from ..server.auth import current_user, require_family
from ._body import json_object, read_at_most
from .api import UPLOAD_EXTENSIONS, _cfg, _conn, _roots, _safe_under, _viewer, bp

#: A phone's id, as the app makes it: random, and nothing else.
_DEVICE_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

#: Why the indexer stands down while a batch of backups is filed.
REASON = "filing phone backups"


def _device_id(value: Any) -> str:
    text = str(value or "")
    if not _DEVICE_ID.match(text):
        abort(400, description="This phone's backup id is missing or malformed.")
    return text


def _ready():
    conn = _conn()
    phone_backup.init_schema(conn)
    return conn


def _destination() -> tuple[str, str]:
    """The library and folder a family upload goes to — as for /api/upload."""
    cfg = _cfg()
    roots = _roots()
    if not roots:
        abort(403, description="Your assigned library is no longer available")
    root = cfg.active_root if cfg.active_root in roots else roots[0]
    if not roots_kit.root_present(root):
        # Not merely a folder at the path: an unplugged drive's mount point is
        # one, and backups written there were hidden when the drive came back.
        abort(409, description="The library folder is unavailable")
    scope = _viewer()["scope"] or ""
    if not _safe_under(Path(root) / scope, Path(root)):
        abort(403, description="Backups must stay inside your library")
    return root, scope


def _supported(name: str) -> str | None:
    safe = _backup_name(name)
    if not safe or safe.startswith(".") or Path(safe).suffix.lower() not in UPLOAD_EXTENSIONS:
        return None
    return safe


def _backup_name(name: str) -> str:
    """The name a phone's file is filed and remembered under.

    The name is part of the fingerprint a phone is recognised by
    (``phone_backup.fingerprint``), so whatever name the old reduction to ASCII
    gave a usable result for keeps it — "café.jpg" is still filed as
    "cafe.jpg", and "my photo.jpg" as "my_photo.jpg". Changing either would make
    every phone send again everything with such a name. Only where that
    reduction left nothing usable — a name in Tamil alone came out as "jpg"
    and was called unsupported — is the name kept with its letters
    (``utils/filenames.py``).
    """
    legacy = secure_filename(Path(name).name) or ""
    if Path(legacy).stem and Path(legacy).suffix.lower() in UPLOAD_EXTENSIONS:
        return legacy
    return safe_filename(name)


def _refused(exc: phone_backup.BackupError):
    body: dict[str, Any] = {"error": str(exc), "status": exc.status}
    if exc.offset is not None:
        body["offset"] = exc.offset
    return jsonify(body), exc.status


@bp.post("/api/phone-backup/check")
@require_family
def phone_backup_check():
    """Which of these files are already safe, part-way here, or new."""
    data = json_object()
    device_id = _device_id(data.get("device_id"))
    files = data.get("files")
    if not isinstance(files, list) or len(files) > phone_backup.MAX_CHECK:
        abort(400, description=f"Send up to {phone_backup.MAX_CHECK:,} files at a time.")
    offered = []
    for item in files:
        if not isinstance(item, dict):
            abort(400, description="Each file is an object.")
        offered.append({"name": _supported(str(item.get("name") or "")) or "",
                        "size": _whole(item.get("size")),
                        "modified": _stamp(item.get("modified"))})
    conn = _ready()
    answers = phone_backup.check(conn, current_user().id, device_id, offered)
    for answer, item in zip(answers, offered):
        if not item["name"]:
            answer["state"] = "unsupported"
    return jsonify({"files": answers,
                    "summary": phone_backup.summary(conn, current_user().id, device_id)})


@bp.post("/api/phone-backup/files")
@require_family
def phone_backup_begin():
    """Start (or find) one file, and say which byte to send next."""
    data = json_object()
    device_id = _device_id(data.get("device_id"))
    name = _supported(str(data.get("name") or ""))
    if name is None:
        abort(400, description="Ninaivu takes photographs, video and audio. "
                               "That file type is not one of them.")
    _destination()
    try:
        answer = phone_backup.begin(
            _ready(), _cfg(), current_user().id, device_id,
            str(data.get("device") or "")[:80], name=name,
            size=_whole(data.get("size")), modified=_stamp(data.get("modified")))
    except phone_backup.BackupError as exc:
        return _refused(exc)
    return jsonify(answer)


@bp.put("/api/phone-backup/files/<int:row_id>")
@require_family
def phone_backup_piece(row_id: int):
    """One piece of a file, at ``?offset=``. The body is the bytes."""
    offset = request.args.get("offset", type=int)
    if offset is None or offset < 0:
        abort(400, description="Say where this piece goes with ?offset=.")
    # Before the body is read: the app-wide limit is for whole uploads, and
    # a piece that size would be held in memory only to be refused. A piece
    # sent without a declared length is read no further than the limit.
    if (request.content_length or 0) > phone_backup.MAX_PIECE_BYTES:
        return _refused(phone_backup.BackupError("That piece is too large.", 413))
    root, scope = _destination()
    piece = read_at_most(phone_backup.MAX_PIECE_BYTES, "That piece is too large.")
    try:
        answer = phone_backup.receive(_ready(), _cfg(), current_user().id, row_id,
                                      offset, piece, root=root, scope=scope,
                                      max_visibility=_viewer()["max_visibility"])
    except phone_backup.BackupError as exc:
        return _refused(exc)
    return jsonify(answer)


@bp.post("/api/phone-backup/finish")
@require_family
def phone_backup_finish():
    """A batch is done. File it now if this person's backups need no review."""
    data = json_object()
    device_id = _device_id(data.get("device_id"))
    conn = _ready()
    cfg = _cfg()
    user = current_user()
    result: dict[str, Any] = {"approved": 0, "failed": 0, "needs_review": True}
    if user.is_admin or cfg.phone_backup_trusted:
        result = {**file_now(conn, cfg, user.id, user.id), "needs_review": False}
    return jsonify({**result, "summary": phone_backup.summary(conn, user.id, device_id)})


@bp.get("/api/phone-backup/status")
@require_family
def phone_backup_status():
    raw = request.args.get("device_id", "")
    device_id = _device_id(raw) if raw else ""
    return jsonify(phone_backup.summary(_ready(), current_user().id, device_id))


def file_now(conn, cfg, user_id: int, reviewer: int) -> dict[str, Any]:
    """Approve this person's staged backups, the indexer stood down once for
    the lot. Left in the queue if something is moving files right now."""
    from .admin_api import _moving_files, resume_after        # noqa: PLC0415

    scanner = current_app.config.get("MV_SCANNER")
    if scanner is None:
        return phone_backup.approve_finished(conn, cfg, user_id, reviewer)
    if _moving_files(scanner):
        return {"approved": 0, "failed": 0, "later": True}
    scanner.defer(REASON)
    scanner.stop(join=True)
    landed: set[str] = set()
    try:
        return phone_backup.approve_finished(conn, cfg, user_id, reviewer, landed=landed)
    finally:
        resume_after(scanner, REASON, landed)


def _whole(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        abort(400, description="A file's size must be a whole number of bytes.")
    if number < 0:
        abort(400, description="A file's size must be a whole number of bytes.")
    return number


def _stamp(value: Any) -> float:
    """Seconds since 1970. A phone gives milliseconds; either is taken."""
    try:
        number = float(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if number > 1e11:
        number /= 1000.0
    return number if 0 < number < 4e9 else 0.0
