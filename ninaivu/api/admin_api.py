"""Endpoints that exist only on the admin console (port 3000).

Nothing here is mounted on the family app, so these paths are not merely
forbidden there — they are absent. A 404 on the home port is the correct
answer, and it is the one the router gives.
"""

from __future__ import annotations

import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from flask import Blueprint, abort, current_app, g, has_app_context, jsonify, request, send_file

from .. import COPYRIGHT, LICENCE, __version__
from ..server import auth
from ..storage import db
from ..media import media
from ..media.scanner import Scanner
from ..server.config import Config, clean_home_name, house_name
from ..server.auth import ROLE_LABELS, VIS_NAMES, current_user, require_admin
from ._body import json_object
from ..words import said

admin_bp = Blueprint("admin", __name__)

#: What the console's switch turns the keyframe pass back on to, and the most
#: moments it can be asked for. Both come from the scanner rather than being
#: written down again here: it samples KEYFRAME_POINTS, and a number larger
#: than it has points for would quietly sample that many anyway.
KEYFRAME_POINTS = Scanner.KEYFRAME_POINTS
DEFAULT_VIDEO_KEYFRAMES = Config.video_keyframes

#: Passes a scan can make that a household can turn on and off from the
#: console. All three are off by default and none of them had a switch — only a
#: start-up flag, or config.json by hand — so on the library they were found
#: on, 21,887 photographs with coordinates had never been named and a text
#: reader that had just been installed was never going to be asked to read.
SCAN_PASSES = ("place_names", "ocr_enabled", "faces_enabled")


def _cfg():
    return current_app.config["MV_CONFIG"]


def _conn():
    return db.connect(_cfg().db_path)


def _scanner():
    return current_app.config["MV_SCANNER"]


@admin_bp.get("/api/admin/date-policy")
@require_admin
def date_policy_get():
    from ..server import date_policy
    return jsonify(date_policy.read(_conn()))


@admin_bp.get("/api/admin/settings/all")
@require_admin
def settings_all():
    """Every setting, in its group, with its meaning and default: the
    console's Advanced page. See server/settings_groups.py."""
    from ..server import settings_groups                     # noqa: PLC0415
    return jsonify(settings_groups.describe(_cfg()))


@admin_bp.post("/api/admin/settings/all")
@require_admin
def settings_all_change():
    """Change any setting by name. Checked before anything is written, so a
    request with one bad value changes nothing at all. Settings that describe
    how this process was started are refused: they are set on the command
    line or in the environment, and this would only pretend to change them."""
    from ..server import settings_groups                     # noqa: PLC0415
    cfg = _cfg()
    data = json_object()
    changes = data.get("settings") if isinstance(data.get("settings"), dict) else data
    try:
        changed = settings_groups.apply(cfg, changes)
    except settings_groups.BadValue as exc:
        return jsonify({"error": str(exc)}), 400
    if changed:
        cfg.save()
        auth.audit(_conn(), current_user().id, "settings", ", ".join(changed))
        # What the dedicated pages do after the same change.
        if any(name.startswith(("notify_", "digest_")) for name in changed):
            current_app.config["MV_NOTIFY"] = None          # rebuilt on next use
        if "hide_screens" in changed:
            threading.Thread(target=_scanner().apply_screen_rule,
                             name="ninaivu-screens", daemon=True).start()
        if "digest_enabled" in changed and (keeper := _digest_keeper()) is not None:
            # As the digest page does: on now, not at the next restart.
            keeper.start() if cfg.digest_enabled else keeper.stop(timeout=1.0)
        if set(changed) & {"cloud_kinds", "cloud_max_mb", "cloud_approval_mb",
                           "cloud_skip_folders", "cloud_skip_words"}:
            # As Mugil's page does: set aside what the rules now hold, and
            # release what they no longer do, now rather than file by file.
            current_app.config["MV_SERVICES"].cloud.apply_rules()
        if "backup_every_hours" in changed and (
                backups := current_app.config.get("MV_BACKUPS")) is not None:
            # Off to on: the keeper never started its loop, so start it now
            # (a no-op when it is running; it reads the interval each round).
            backups.start()
    return jsonify({"ok": True, "changed": changed,
                    "restart": sorted(set(changed) & RESTART_SETTINGS)})


#: Settings a running server does not pick up until it starts again.
RESTART_SETTINGS = frozenset({
    "workers", "thumb_sizes", "thumb_format", "network_access", "tailnet_https",
    "ai_engine", "clip_model", "clip_pretrained", "ai_gpu", "ai_models_dir",
    "extensions", "proxy_cache_mb", "hardware_tier", "ai_enabled", "allowed_hosts",
    # Read when the console's address is chosen at start-up (__main__.py).
    "console_on_network",
})


def _image_model_state() -> dict[str, Any]:
    from ..media import components                            # noqa: PLC0415
    try:
        described = components.describe("image-model")
    except Exception:                                           # noqa: BLE001
        return {"present": False, "installing": False}
    return {"present": bool(described.get("present")),
            "installing": bool(described.get("installing"))}


@admin_bp.get("/api/admin/first-day")
@require_admin
def first_day():
    """Where the first-day walk-through stands, so the console knows whether
    to open it and what each step can already tick off."""
    cfg = _cfg()
    conn = _conn()
    roots = cfg.libraries or ([cfg.active_root] if cfg.active_root else [])
    people = conn.execute("SELECT COUNT(*) FROM users WHERE role != 'admin' AND active=1").fetchone()[0]
    from ..media import model_catalog                     # noqa: PLC0415
    return jsonify({
        "done": bool(cfg.first_day_done),
        "library": {"chosen": bool(roots), "root": roots[0] if roots else ""},
        "people": int(people),
        "ai": {key: bool(getattr(cfg, key, False)) for key in ("faces_enabled", "place_names", "ocr_enabled")},
        "faces_model": model_catalog.installed("faces"),
        # Search by description: offered here rather than installed by the
        # launcher at the first start (see launcher/start.py --ai).
        "image_model": _image_model_state(),
        "backup": {"enabled": bool(getattr(cfg, "cloud_enabled", False))},
    })


@admin_bp.post("/api/admin/first-day")
@require_admin
def first_day_done():
    """Finished, or skipped: either way it does not open again."""
    cfg = _cfg()
    cfg.first_day_done = True
    cfg.save()
    return jsonify({"done": True})


@admin_bp.get("/api/admin/attention")
@require_admin
def attention():
    """What is waiting for a person, counted, for the top of the Overview.

    One request for the four queues the console keeps, so the first screen can
    say "3 uploads, 2 groups of faces, nothing else" instead of asking the
    administrator to open each page to find out. Each count links to its page.
    """
    from ..utils import logs
    conn = _conn()
    user = current_user()

    def count(sql, *params):
        try:
            return int(conn.execute(sql, params).fetchone()[0])
        except sqlite3.Error:
            return 0

    uploads = count("SELECT COUNT(*) FROM pending_uploads WHERE status='pending'")
    turns = count("SELECT COUNT(*) FROM orientation_proposals WHERE status='pending'")
    cfg = _cfg()
    roots = cfg.libraries or ([cfg.active_root] if cfg.active_root else [])
    try:
        groups = len(db.list_unnamed_clusters(conn, roots, max_visibility=user.max_visibility,
                                              min_size=3, limit=200)) if roots else 0
    except sqlite3.Error:
        groups = 0
    problems = len(logs.recent(50))
    items = [
        {"key": "uploads", "count": uploads, "page": "uploads",
         "title": said("Uploads awaiting approval"),
         "detail": said("Photographs the family sent, waiting to be filed or refused.")},
        {"key": "faces", "count": groups, "page": "faces",
         "title": said("Groups of faces without a name"),
         "detail": said("Name one face and the rest of its group follow.")},
        {"key": "straighten", "count": turns, "page": "straighten",
         "title": said("Photographs that may be on their side"),
         "detail": said("Suggested quarter turns, strongest first; approve or dismiss.")},
        {"key": "problems", "count": problems, "page": "health",
         "title": said("Things that went wrong since the last start"),
         "detail": said("Warnings and errors from the log, newest first.")},
    ]
    return jsonify(items=items, total=sum(i["count"] for i in items))


@admin_bp.get("/api/admin/uploads")
@require_admin
def pending_uploads():
    import json
    rows = _conn().execute(
        "SELECT p.*, u.display_name uploader FROM pending_uploads p "
        "LEFT JOIN users u ON u.id=p.uploaded_by WHERE p.status='pending' ORDER BY p.id DESC"
    ).fetchall()
    items = []
    for row in rows:
        record = json.loads(row["record"])
        # A derivative knows its folder already; an ordinary upload is filed by
        # date. Say which, and say where, rather than leaving the queue to guess.
        place = record.get("_place")
        destination = f"{row['root']}/{place}" if place is not None else (
            f"{row['root']}{'/' + row['scope'] if row['scope'] else ''}/year/month/day")
        items.append({"id": row["id"], "filename": row["filename"],
                      "uploader": row["uploader"] or "Former family member",
                      "uploaded_at": row["uploaded_at"], "creation_date": record.get("date_key"),
                      "date_source": record.get("date_source"), "kind": record["kind"],
                      "size": record["size"], "has_thumb": bool(record.get("thumb")),
                      "scope": row["scope"], "library": row["root"],
                      "edited_from": record.get("_edited_from"),
                      "files_by_date": place is None, "destination": destination})
    return jsonify(items=items, total=len(items))


@admin_bp.get("/api/admin/uploads/<int:upload_id>/preview")
@require_admin
def pending_upload_preview(upload_id):
    import json
    import mimetypes
    from ..media import upload_review
    from .api import INLINE_TYPES
    upload = upload_review.get(_conn(), upload_id)
    if not upload or upload["status"] != "pending":
        abort(404)
    cfg = _cfg()
    record = json.loads(upload["record"])
    if request.args.get("thumb") and record.get("thumb"):
        path = cfg.thumbs_dir / media.thumb_file(record["thumb"], cfg.thumb_sizes[0], cfg.thumb_format)
    else:
        path = upload_review.source_path(cfg, upload)
    if not path.is_file():
        abort(404)
    mime = mimetypes.guess_type(path.name)[0]
    response = send_file(path, mimetype=mime if mime in INLINE_TYPES else "application/octet-stream",
                         as_attachment=mime not in INLINE_TYPES, conditional=True)
    response.headers["Cache-Control"] = "private, no-store"
    return response


@admin_bp.post("/api/admin/uploads/<int:upload_id>/approve")
@require_admin
def approve_upload(upload_id):
    from ..media import date_edit, upload_review
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or set(data) - {"creation_date"}:
        return jsonify(error="Provide an optional creation_date in YYYY-MM-DD format."), 400
    if "creation_date" in data:
        try:
            date_edit.parse_date(data["creation_date"])
        except (ValueError, TypeError):
            return jsonify(error="Use a creation date in YYYY-MM-DD format."), 400
    scanner = _scanner()
    landed = []
    with date_edit.edit_lock:
        if _moving_files(scanner):
            return jsonify(error="Wait for the current library operation to finish."), 409
        scanner.defer("approving a family upload")
        scanner.stop(join=True)
        try:
            if scanner.running:
                return jsonify(error="The scanner is still stopping. Try again shortly."), 409
            item = upload_review.approve(_conn(), _cfg(), upload_id, current_user().id,
                                         data.get("creation_date"))
            landed.append(item.get("root"))
            auth.audit(_conn(), current_user().id, "approve_upload", f"upload {upload_id} -> asset {item['id']}")
            return jsonify(ok=True, item=item)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except OSError as exc:
            return jsonify(error=str(exc)), 409
        finally:
            resume_after(scanner, "approving a family upload", landed)


@admin_bp.post("/api/admin/uploads/<int:upload_id>/reject")
@require_admin
def reject_upload(upload_id):
    """Refuse an upload: it is not filed, and the uploaded file is deleted."""
    from ..media import date_edit, upload_review
    # The approval's lock: refusing an upload being approved at that moment
    # would delete the file under it.
    with date_edit.edit_lock:
        try:
            done = upload_review.reject(_conn(), _cfg(), upload_id, current_user().id)
        except LookupError:
            abort(404)
        except ValueError as exc:
            return jsonify(error=str(exc)), 409
    auth.audit(_conn(), current_user().id, "reject_upload",
               f"upload {upload_id} ({done['filename']}) deleted, not filed")
    return jsonify(ok=True, **done)


def resume_after(scanner, reason: str, roots=()) -> None:
    """Let the indexer carry on after a change that stood it down.

    A scan the change interrupted carries on. Otherwise only the library
    folders the change wrote into are scanned: every one of them used to be,
    and on a USB drive that was four minutes to index the one photograph just
    approved. Every folder is watched afterwards either way.
    """
    if scanner.resume(reason) or not _cfg().watch:
        return
    wanted = sorted({str(root) for root in roots if root})
    if wanted:
        scanner.start(wanted)
    else:
        scanner.watch()


def _moving_files(scanner) -> bool:
    """Whether something that moves files around the library is under way.

    A consolidation or another approval is: approving now would move files
    under it. A straightening pass or a storage check also holds the indexer
    down, but only reads — an approval does not have to wait hours for one.
    """
    from ..media.scanner import READ_ONLY_CLAIMS           # noqa: PLC0415
    held = scanner.deferred or ""
    return bool(held) and not all(
        claim in READ_ONLY_CLAIMS for claim in held.split("; "))


@admin_bp.post("/api/admin/date-policy")
@require_admin
def date_policy_save():
    import json
    from ..server import date_policy
    try:
        policy = date_policy.validate(request.get_json(silent=True))
    except (ValueError, TypeError):
        return jsonify(error="Use a YYYY-MM-DD cutoff and before, after or all for every role."), 400
    db.set_meta(_conn(), date_policy.KEY, json.dumps(policy))
    auth.audit(_conn(), current_user().id, "date_policy", json.dumps(policy))
    return jsonify(policy)


@admin_bp.patch("/api/admin/assets/<int:asset_id>/creation-date")
@require_admin
def change_creation_date(asset_id):
    from ..media import date_edit
    data = request.get_json(silent=True)
    try:
        if not isinstance(data, dict) or set(data) != {"creation_date"}:
            raise ValueError("Provide a creation_date in YYYY-MM-DD format.")
        when = date_edit.parse_date(data["creation_date"])
    except (ValueError, TypeError) as exc:
        return jsonify(error=str(exc)), 400
    # Changing the date moves the file to that day's folder, which a read-only
    # library — an NTFS drive on a Mac — cannot do. Say so before trying.
    from ..storage import new_files
    asset = db.get_asset(_conn(), asset_id)
    if asset and (why := new_files.read_only_reason(asset["root"])):
        return jsonify(error=why), 409
    scanner = _scanner()
    landed = []
    with date_edit.edit_lock:
        if _moving_files(scanner):
            return jsonify(error="Wait for the current library operation to finish."), 409
        scanner.defer("updating a file creation date")
        scanner.stop(join=True)
        try:
            if scanner.running:
                return jsonify(error="The scanner is still stopping. Try again shortly."), 409
            cfg = _cfg()
            asset = date_edit.relocate(_conn(), asset_id, when,
                                      cfg.libraries or [cfg.active_root],
                                      archive_path=cfg.state_dir / "archive.db")
            landed.append(asset.get("root"))
            auth.audit(_conn(), current_user().id, "creation_date",
                       f"asset {asset_id}: {asset['date_key']} -> {asset['rel_path']}")
            return jsonify(ok=True, item=asset)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except OSError as exc:
            return jsonify(error=str(exc)), 409
        finally:
            resume_after(scanner, "updating a file creation date", landed)


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------

def _recipe() -> str:
    """The thumbnail settings token, cached on the app.

    The gallery puts the same token in its URLs, so both faces of the app name
    the same picture the same way and share one cached copy of it.
    """
    cached = current_app.config.get("NINAIVU_THUMB_RECIPE")
    if not cached:
        cfg = _cfg()
        cached = media.thumb_recipe(cfg.thumb_sizes, cfg.thumb_format,
                                    cfg.thumb_quality)
        current_app.config["NINAIVU_THUMB_RECIPE"] = cached
    return cached


@admin_bp.get("/api/admin/backups")
@require_admin
def backups():
    """What has been kept of the index, and when."""
    keeper = current_app.config.get("MV_BACKUPS")
    if keeper is None:
        return jsonify({"enabled": False})
    state = keeper.state()
    state["enabled"] = state["every_hours"] > 0
    return jsonify(state)


@admin_bp.post("/api/admin/backups/now")
@require_admin
def backup_now():
    """Make one immediately. The schedule keeps running either way."""
    keeper = current_app.config.get("MV_BACKUPS")
    if keeper is None:
        return jsonify({"error": "Backups are not configured."}), 400
    made = keeper.run()
    if made is None:
        return jsonify({"error": keeper.last_error
                        or "A backup is already being written."}), 409
    return jsonify({"ok": True, **keeper.state()})


@admin_bp.post("/api/admin/backups/verify")
@require_admin
def verify_backup():
    """Rehearse restoring the newest bundle, now, without touching live state."""
    keeper = current_app.config.get("MV_BACKUPS")
    if keeper is None:
        return jsonify({"error": "Backups are not configured."}), 400
    result = keeper.verify_latest()
    if result is None:
        return jsonify({"error": "A backup is being written. Check again when it finishes."}), 409
    return jsonify({"ok": True, "verified": result, **keeper.state()})


@admin_bp.get("/api/admin/problems")
@require_admin
def problems():
    """The last warnings and errors this run has produced.

    In memory, not from the file: what belongs on a screen is what has gone
    wrong since the machine was last started, and reading a rotating log back
    to answer that would be a worse answer more slowly.
    """
    from ..utils import logs                                  # noqa: PLC0415

    limit = max(1, min(50, request.args.get("limit", 12, type=int) or 12))
    return jsonify({
        "problems": logs.recent(limit),
        "file": str(_cfg().state_dir / logs.LOG_NAME),
    })


@admin_bp.post("/api/admin/problems/clear")
@require_admin
def clear_problems():
    """Acknowledge them. The file keeps everything; this is the screen."""
    from ..utils import logs                                  # noqa: PLC0415

    logs.forget()
    return jsonify({"ok": True})


@admin_bp.get("/api/admin/overview")
@require_admin
def overview():
    """Everything the console's landing page shows, in one request."""
    cfg = _cfg()
    conn = _conn()
    engine = current_app.config.get("MV_ENGINE")

    people = auth.list_users(conn)
    by_role: dict[str, int] = {}
    for person in people:
        if person.active:
            by_role[person.role] = by_role.get(person.role, 0) + 1

    scheme = current_app.config.get("MV_SCHEME") or ("https" if cfg.port == 443 else "http")
    hostnames = current_app.config.get("MV_HOSTNAMES", {})
    family_host = hostnames.get("family") or _display_host(cfg)
    default_port = 443 if scheme == "https" else 80
    suffix = "" if cfg.port == default_port else f":{cfg.port}"
    home_url = f"{scheme}://{family_host}{suffix}"

    payload: dict[str, Any] = {
        "app": {
            "version": __version__,
            "copyright": COPYRIGHT,
            "licence": LICENCE,
            "home_url": home_url,
            # The port matters more than the URL: bound to 0.0.0.0 the server
            # cannot know which of its addresses this browser used, so the page
            # composes the link from its own hostname and this.
            "home_port": cfg.port,
            "admin_port": cfg.admin_port,
            "scheme": scheme,
            "hostnames": hostnames,
            "open_browsing": cfg.open_browsing,
            "nsfw_filter": cfg.nsfw_filter,
            "hide_screens": cfg.hide_screens,
            "watch": cfg.watch,
            "video_keyframes": cfg.video_keyframes,
            **{key: getattr(cfg, key) for key in SCAN_PASSES},
            "house_name": cfg.house_name,
            "house_name_effective": house_name(cfg),
            "remote_access": cfg.remote_access,
            "remote_networks": list(cfg.remote_networks or []),
            "remote_hostname": cfg.remote_hostname,
            "update_check": cfg.update_check,
            "outside_ai_for_family": bool(getattr(cfg, "outside_ai_for_family", False)),
        },
        "library": {
            "root": cfg.active_root,
            "roots": cfg.roots,
            "folders": _library_summaries(conn, cfg),
            "locked": cfg.lock_roots,
        },
        "scan": _scanner().progress.snapshot(),
        "ai": engine.info if engine is not None else {"engine": "loading"},
        "capabilities": {
            "ffmpeg": bool(media.FFMPEG),
            "heif": media.HEIF_OK,
            "opencv": media.cv2 is not None,
        },
        "people": {
            "total": len([p for p in people if p.active]),
            "by_role": {role: by_role.get(role, 0) for role in auth.ROLES},
            "disabled": len([p for p in people if not p.active]),
            # People, not sessions. Every phone, tablet and browser tab that
            # signed in keeps its own session, so counting sessions said
            # "20 signed in" in a house with four profiles. The anonymous
            # "just looking" visitor has no session row at all, so it is
            # neither counted here nor needs excluding.
            "signed_in": conn.execute(
                "SELECT COUNT(DISTINCT user_id) n FROM sessions WHERE expires_at > ?",
                (time.time(),),
            ).fetchone()["n"],
            # Kept for anything that still reads it: how many sessions are open.
            "sessions": conn.execute(
                "SELECT COUNT(*) n FROM sessions WHERE expires_at > ?",
                (time.time(),),
            ).fetchone()["n"],
        },
    }

    if cfg.active_root:
        payload["stats"] = db.library_stats(
            conn, cfg.active_root, viewer_id=current_user().id,
            max_visibility=auth.VIS_HIDDEN, scope=None,
        )
        payload["rules"] = [
            {**rule, "visibility_name": VIS_NAMES[int(rule["visibility"])]}
            for rule in db.folder_rules(conn, cfg.active_root)
        ]
    return jsonify(payload)


def _assigned_within(assignment: str | None, root: str) -> bool:
    """Whether a profile's assignment is *root* itself or a folder inside it.

    Compared on path components, never as a string prefix: ``/data/kids`` does
    not contain ``/data/kids-private``.
    """
    if not assignment:
        return False
    return bool(auth.resolve_library(assignment, [root])[0])


def _library_summaries(conn, cfg) -> list[dict[str, Any]]:
    """Each configured library folder, with what is indexed inside it."""
    out = []
    for root in cfg.roots:
        path = Path(root)
        exists = False
        try:
            exists = path.is_dir()
        except OSError:
            pass
        row = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(size),0) bytes FROM assets "
            "WHERE root=? AND trashed=0", (root,),
        ).fetchone()
        assigned = sum(
            1 for user in conn.execute(
                "SELECT library FROM users WHERE active=1 AND library IS NOT NULL")
            if _assigned_within(user["library"], root))
        out.append({
            "path": root,
            "name": path.name or root,
            "exists": exists,
            "count": row["n"] or 0,
            "bytes": row["bytes"] or 0,
            "assigned": assigned,
            "active": root == cfg.active_root,
        })
    return out


def _display_host(cfg) -> str:
    return "localhost" if cfg.host in ("0.0.0.0", "127.0.0.1", "::") else cfg.host


# ---------------------------------------------------------------------------
# Folder tree with visibility
# ---------------------------------------------------------------------------

_MATERIALIZED = "MATERIALIZED " if sqlite3.sqlite_version_info >= (3, 35, 0) else ""

#: What the Folders screen can narrow to: everything, one kind, or the
#: screenshots and documents (media/screens.py).
FOLDER_FILTERS = ("all", "picture", "video", "audio", "screen")


@admin_bp.get("/api/admin/folder")
@require_admin
def folder_detail():
    """One folder: what is directly inside it, and what is hidden in there.

    The tree view answers "how is the library arranged". This answers the
    question an admin actually arrives with — *what is in this folder, and
    which of it is private* — which needs the items themselves, not counts.
    """
    cfg = _cfg()
    conn = _conn()
    root = cfg.active_root
    if not root:
        return jsonify({"folder": "", "children": [], "items": [], "counts": {},
                        "kinds": {}, "show": "all"})

    folder = (request.args.get("folder") or "").strip().strip("/")
    prefix = f"{folder}/" if folder else ""
    show = request.args.get("show") or "all"
    if show not in FOLDER_FILTERS:
        show = "all"

    # What the filter keeps, and what counts as a screenshot or document, for
    # every row once. The same expressions give the chips their numbers, so
    # counting all five kinds costs no more passes over the table than one.
    screen_sql, screen_params = db.screen_clause(conn)
    if show == "screen":
        keep_sql, keep_params = screen_sql, screen_params
        row_keep, row_keep_params = "screen", []
    elif show == "all":
        keep_sql, keep_params = "1", []
        row_keep, row_keep_params = "1", []
    else:
        keep_sql, keep_params = "kind=?", [show]
        row_keep, row_keep_params = "kind=?", [show]

    # Immediate subfolders, each with the counts for everything beneath it, so
    # a folder whose contents are all private says so before it is opened.
    within, within_params = db.folder_clause("folder", folder) if folder else ("1=1", [])
    # MATERIALIZED, so each row is judged once. Left to itself SQLite folds
    # the inner query into the outer one and copies the screenshot test into
    # every SUM that reads it — six times per row, seconds at the library root.
    # SQLite before 3.35 does not know the word; the answer is the same there.
    rows = conn.execute(
        f"WITH marked AS {_MATERIALIZED}(SELECT folder, kind, visibility, size, "
        f"captured_at, ({screen_sql}) screen FROM assets "
        f"WHERE root=? AND trashed=0 AND {within}) "
        "SELECT folder, SUM(keep) n, SUM(keep AND visibility=0) public, "
        "SUM(keep AND visibility=1) family, SUM(keep AND visibility=2) hidden, "
        "COALESCE(SUM(CASE WHEN keep THEN size END),0) bytes, "
        "MAX(CASE WHEN keep THEN captured_at END) newest, "
        "COUNT(*) k_all, SUM(kind='picture') k_picture, SUM(kind='video') k_video, "
        "SUM(kind='audio') k_audio, SUM(screen) k_screen "
        f"FROM (SELECT *, ({row_keep}) keep FROM marked) GROUP BY folder",
        (*screen_params, root, *within_params, *row_keep_params),
    ).fetchall()

    children: dict[str, dict[str, Any]] = {}
    here = {"n": 0, "public": 0, "family": 0, "hidden": 0, "bytes": 0}
    kinds = {key: 0 for key in FOLDER_FILTERS}
    for row in rows:
        for key in FOLDER_FILTERS:
            kinds[key] += int(row[f"k_{key}"] or 0)
        rest = (row["folder"] or "")[len(prefix):] if prefix else (row["folder"] or "")
        if not rest:
            for key in ("n", "public", "family", "hidden"):
                here[key] += int(row[key] or 0)
            here["bytes"] += int(row["bytes"] or 0)
            continue
        if not row["n"]:
            continue                 # nothing of this kind in there
        name = rest.split("/", 1)[0]
        node = children.setdefault(name, {
            "name": name, "path": f"{prefix}{name}",
            "n": 0, "public": 0, "family": 0, "hidden": 0, "bytes": 0,
            "newest": None, "cover": None,
        })
        for key in ("n", "public", "family", "hidden"):
            node[key] += int(row[key] or 0)
        node["bytes"] += int(row["bytes"] or 0)
        if row["newest"] and (node["newest"] is None or row["newest"] > node["newest"]):
            node["newest"] = row["newest"]

    # A picture for each subfolder, preferring one anybody may see so the
    # screen does not become a wall of private photographs.
    for node in children.values():
        beneath, beneath_params = db.folder_clause("folder", node["path"])
        cover = conn.execute(
            "SELECT id, indexed_at, rotation FROM assets WHERE root=? AND trashed=0 "
            f"AND thumb IS NOT NULL AND {beneath} AND ({keep_sql}) "
            "ORDER BY visibility ASC, captured_at DESC LIMIT 1",
            (root, *beneath_params, *keep_params)).fetchone()
        node["cover"] = cover["id"] if cover else None
        # The cover's thumbnail version, for the same cache reason as
        # everywhere else a thumbnail URL is built.
        node["cover_v"] = media.thumb_version(cover, _recipe()) if cover else ""

    items = [dict(r) for r in conn.execute(
        "SELECT id, filename, kind, visibility, vis_source, size, captured_at, "
        "date_key, thumb, width, height, duration, indexed_at, rotation "
        "FROM assets "
        f"WHERE root=? AND trashed=0 AND folder=? AND ({keep_sql}) "
        "ORDER BY captured_at DESC, filename LIMIT 600",
        (root, folder, *keep_params)).fetchall()]
    for item in items:
        # `is None`, not `or`: public is 0, and `0 or 1` called it "family".
        level = 1 if item["visibility"] is None else int(item["visibility"])
        item["visibility_name"] = VIS_NAMES.get(level, "family")
        # The thumbnail's version. Same shape as api._thumb_version, and for
        # the same reason: content is the file plus the turn applied to it.
        item["thumb_v"] = media.thumb_version(item, _recipe())
        item.pop("indexed_at", None)

    rule = db.folder_rule_for(db.folder_rules(conn, root), folder)
    parts = [p for p in folder.split("/") if p]
    trail = [{"name": Path(root).name or root, "path": ""}]
    for depth, part in enumerate(parts):
        trail.append({"name": part, "path": "/".join(parts[:depth + 1])})

    return jsonify({
        "root": root,
        "folder": folder,
        "trail": trail,
        "children": sorted(children.values(), key=lambda c: c["name"].lower()),
        "items": items,
        "counts": here,
        "rule": {"visibility": rule[0], "at": rule[1]} if rule else None,
        "total_hidden": sum(c["hidden"] for c in children.values()) + here["hidden"],
        # This folder and everything beneath it, whatever the filter.
        "kinds": kinds,
        "show": show,
    })


# -- locations filled in --------------------------------------------------------
#
# Photographs without a location that were taken within an hour or so of one
# with, given its location, in the index only (storage/geo.py).

def _location_libraries() -> list[str]:
    cfg = _cfg()
    return cfg.libraries or ([cfg.active_root] if cfg.active_root else [])


def _location_window(value: Any) -> float:
    from ..storage import geo

    try:
        hours = float(value)
    except (TypeError, ValueError):
        abort(400, description="Choose how close in time: half an hour, an hour or two hours")
    if hours not in geo.WINDOWS:
        abort(400, description="Choose how close in time: half an hour, an hour or two hours")
    return hours


@admin_bp.get("/api/admin/locations")
@require_admin
def location_suggestions():
    from ..storage import geo

    hours = _location_window(request.args.get("hours", 1))
    return jsonify({"hours": hours, "windows": list(geo.WINDOWS),
                    **geo.location_suggestions(_conn(), _location_libraries(), max_hours=hours)})


@admin_bp.post("/api/admin/locations/apply")
@require_admin
def location_apply():
    """Give the suggested location to all of them, or to the ones listed."""
    from ..storage import geo

    data = json_object()
    hours = _location_window(data.get("hours", 1))
    ids = None
    if not data.get("all"):
        ids = data.get("ids")
        if (not isinstance(ids, list) or not ids or len(ids) > 5000
                or not all(isinstance(i, int) for i in ids)):
            abort(400, description="Say which photographs (up to 5000 at a time), or all of them")
    done = geo.apply_locations(_conn(), _location_libraries(), max_hours=hours, ids=ids)
    auth.audit(_conn(), current_user().id, "locations_filled_in",
               f"{done} photographs, within {hours:g} h of one with a location")
    return jsonify({"ok": True, "applied": done})


@admin_bp.post("/api/admin/locations/undo")
@require_admin
def location_undo():
    """Take back every location Ninaivu filled in."""
    from ..storage import geo

    done = geo.undo_locations(_conn(), _location_libraries())
    auth.audit(_conn(), current_user().id, "locations_undone", f"{done} photographs")
    return jsonify({"ok": True, "undone": done})


@admin_bp.get("/api/admin/folders")
@require_admin
def folders():
    """The library's folder tree, each node with its counts and setting."""
    cfg = _cfg()
    conn = _conn()
    root = cfg.active_root
    if not root:
        return jsonify({"folders": [], "rules": []})

    rows = db.cached_aggregate(conn, ("folder_visibility", root), lambda: [
        dict(row) for row in conn.execute(
            "SELECT folder, COUNT(*) n, SUM(visibility=0) public, "
            "SUM(visibility=1) family, SUM(visibility=2) hidden "
            "FROM assets WHERE root=? AND trashed=0 GROUP BY folder",
            (root,),
        )])

    totals: dict[str, dict[str, int]] = {}
    for row in rows:
        parts = [p for p in (row["folder"] or "").split("/") if p]
        for depth in range(1, len(parts) + 1):
            prefix = "/".join(parts[:depth])
            bucket = totals.setdefault(
                prefix, {"count": 0, "public": 0, "family": 0, "hidden": 0})
            bucket["count"] += row["n"]
            bucket["public"] += row["public"] or 0
            bucket["family"] += row["family"] or 0
            bucket["hidden"] += row["hidden"] or 0

    rules = {r["folder"]: int(r["visibility"]) for r in db.folder_rules(conn, root)}

    return jsonify({
        "folders": [
            {
                "path": path,
                "name": path.split("/")[-1],
                "depth": path.count("/"),
                **counts,
                # The explicit rule on this exact folder, if any …
                "rule": VIS_NAMES.get(rules[path]) if path in rules else None,
                # … and what it actually resolves to, inherited or not.
                "effective": VIS_NAMES.get(
                    db.visibility_for_folder(
                        [{"folder": f, "visibility": v} for f, v in rules.items()],
                        path,
                    ) if rules else None
                ),
            }
            for path, counts in sorted(totals.items())
        ],
        "root_rule": VIS_NAMES.get(rules.get("")) if "" in rules else None,
        "rules": [{"folder": f, "visibility": VIS_NAMES[v]} for f, v in rules.items()],
    })


def _asset_abs_path(asset: dict[str, Any]) -> Path | None:
    """An asset's real file on disk, refusing anything outside its root."""
    try:
        root = Path(asset["root"]).resolve()
        path = (root / asset["rel_path"]).resolve()
    except (KeyError, OSError, ValueError):
        return None
    try:
        same_root = os.path.commonpath([path, root]) == str(root)
    except ValueError:
        same_root = False
    if not same_root or not path.is_file():
        return None
    return path


@admin_bp.post("/api/admin/reveal/<int:asset_id>")
@require_admin
def reveal(asset_id: int):
    """Open the operating system's own file manager, with this file selected.

    Only meaningful when the browser and the server are the same computer —
    Ninaivu is often a NAS in a closet nobody is sitting in front of — so this
    is loopback-only on top of the admin session the console already
    requires, the same guard :func:`shutdown` uses below. Anywhere else the
    honest answer is "can't", not a server reaching for a window nobody can
    see.
    """
    if not from_this_computer():
        return jsonify({"error": "Ninaivu's file browser only opens on the "
                                  "computer the server is running on."}), 409

    conn = _conn()
    asset = db.get_asset(conn, asset_id)
    if not asset:
        return jsonify({"error": "not found"}), 404
    path = _asset_abs_path(asset)
    if path is None:
        return jsonify({"error": "that file is not on disk any more"}), 404

    try:
        if sys.platform.startswith("win"):
            # A string, not a list. Given a list, Python quotes the whole
            # argument, "/select,E:\Archive\lesson 7.mp4",
            # and Explorer does not recognise a quoted switch: it ignores it
            # and opens Documents. It wants the quotes around the path alone.
            subprocess.Popen(f'explorer /select,"{path}"')
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)])
        else:
            # No file manager verb for "select this one file" is universal
            # across Linux desktops, so the containing folder is the honest
            # fallback.
            subprocess.Popen(["xdg-open", str(path.parent)])
    except OSError as exc:
        return jsonify({"error": f"could not open a file manager: {exc}"}), 500

    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Where the space goes
# ---------------------------------------------------------------------------
#
# The Large files page lists files; this says where the bulk is — a year of
# camcorder tapes, one folder of films — which is what a household deciding
# what to leave out of the backup wants, and the index can answer it in one pass.

#: Rows shown for the breakdowns that have a long tail.
STORAGE_TOP = 12


@admin_bp.get("/api/admin/storage-report")
@require_admin
def storage_report():
    """The library's size by year, kind, camera and folder, and how much of
    each is in the cloud backup."""
    cfg = _cfg()
    conn = _conn()
    from ..cloud import store                               # noqa: PLC0415
    store.init_schema(conn)
    roots = cfg.roots or ([cfg.active_root] if cfg.active_root else [])
    empty = {"files": 0, "bytes": 0, "backed_up_bytes": 0, "by_kind": [], "by_year": [],
             "by_camera": [], "by_folder": [], "by_type": []}
    if not roots:
        return jsonify(empty)
    marks = ",".join("?" * len(roots))
    # One read of the index, joined to the record of uploads, grouped five
    # ways below. `up` is the file's size when it has gone up, else 0.
    base = (f"FROM assets a LEFT JOIN cloud_uploads c ON c.root = a.root "
            f"AND c.rel_path = a.rel_path AND c.state = 'done' "
            f"WHERE a.trashed = 0 AND a.root IN ({marks})")

    def grouped(expression: str, limit: int | None = None,
                order: str = "bytes DESC") -> list[dict[str, Any]]:
        rows = conn.execute(
            f"SELECT {expression} AS label, COUNT(*) AS files, "
            f"COALESCE(SUM(a.size), 0) AS bytes, "
            f"COALESCE(SUM(CASE WHEN c.id IS NULL THEN 0 ELSE a.size END), 0) AS backed_up_bytes "
            f"{base} GROUP BY label ORDER BY {order}", roots).fetchall()
        out = [{"label": str(r["label"] or ""), "files": int(r["files"]),
                "bytes": int(r["bytes"]), "backed_up_bytes": int(r["backed_up_bytes"])}
               for r in rows]
        if limit and len(out) > limit:
            rest = out[limit:]
            out = out[:limit] + [{
                "label": f"{len(rest):,} others", "rest": True,
                "files": sum(r["files"] for r in rest),
                "bytes": sum(r["bytes"] for r in rest),
                "backed_up_bytes": sum(r["backed_up_bytes"] for r in rest)}]
        return out

    by_kind = grouped("a.kind")
    return jsonify({
        "files": sum(r["files"] for r in by_kind),
        "bytes": sum(r["bytes"] for r in by_kind),
        "backed_up_bytes": sum(r["backed_up_bytes"] for r in by_kind),
        "by_kind": by_kind,
        "by_year": grouped("substr(a.date_key, 1, 4)", order="label DESC"),
        "by_camera": grouped("COALESCE(NULLIF(a.camera, ''), '')", STORAGE_TOP),
        # The first folder of the path inside the library.
        "by_folder": grouped(
            "CASE WHEN instr(a.rel_path, '/') > 0 "
            "THEN substr(a.rel_path, 1, instr(a.rel_path, '/') - 1) ELSE '' END", STORAGE_TOP),
        "by_type": grouped("LOWER(a.ext)", STORAGE_TOP),
    })


# ---------------------------------------------------------------------------
# Largest files — a worklist, not a verdict
# ---------------------------------------------------------------------------
#
# Size alone is only a proxy for "this doesn't belong in a personal library" —
# a long family video is large too. `no_camera` narrows the guess using
# metadata Ninaivu already extracts (nothing new is detected here): no EXIF
# camera, and for video, a runtime well past what a phone clip usually is.
# It is a badge, never a filter, for the same reason an automatic face match
# is "suggested" rather than "decided" — the human still looks at each row.

#: Below this, "largest files" stops being a useful worklist and starts being
#: noise — most personal libraries have plenty of ordinary photos and short
#: clips well under this line.
DEFAULT_LARGE_FILE_FLOOR = 500 * 1024 * 1024

#: A video this long is not a phone clip, whatever else is true about it.
LONG_VIDEO_SECONDS = 15 * 60


def _not_camera_shaped(row: dict[str, Any]) -> bool:
    if row.get("camera") or row.get("date_source") != "mtime":
        return False
    if row.get("kind") == "video":
        return bool(row.get("duration") and float(row["duration"]) >= LONG_VIDEO_SECONDS)
    return True


@admin_bp.get("/api/admin/large-files")
@require_admin
def large_files():
    """The biggest files in the library nobody has said "keep" to yet."""
    cfg = _cfg()
    conn = _conn()
    libraries = cfg.libraries or ([cfg.active_root] if cfg.active_root else [])
    if not libraries:
        return jsonify({"items": [], "total": 0, "floor_mb": DEFAULT_LARGE_FILE_FLOOR // (1024 * 1024)})

    try:
        floor = int(float(request.args.get("min_mb", 0)) * 1024 * 1024)
    except (TypeError, ValueError, OverflowError):     # "inf" floats, then overflows
        floor = 0
    if floor <= 0:
        floor = DEFAULT_LARGE_FILE_FLOOR
    limit = max(1, min(500, request.args.get("limit", 200, type=int) or 200))

    rows, total = db.query_assets(
        conn, libraries, max_visibility=2, min_size=floor,
        exclude_reviewed_large=True, sort="size_desc", limit=limit)

    items = []
    for row in rows:
        items.append({
            "id": row["id"],
            "filename": row["filename"],
            "folder": row["folder"],
            "kind": row["kind"],
            "size": row["size"],
            "duration": row["duration"],
            "captured_at": row["captured_at"],
            "camera": row["camera"],
            "thumb": bool(row.get("thumb")),
            "thumb_v": media.thumb_version(row, _recipe()) if row.get("thumb") else "",
            "no_camera": _not_camera_shaped(row),
        })
    return jsonify({
        "items": items,
        "total": total,
        "floor_mb": floor // (1024 * 1024),
    })


@admin_bp.post("/api/admin/large-files/keep")
@require_admin
def keep_large_files():
    """Stop asking about these on the largest-files screen.

    Nothing about the files themselves changes — see
    :func:`db.mark_large_files_reviewed`. Deleting instead is the ordinary
    ``/api/delete`` (password, moved to the bin, not erased) — this route
    only ever dismisses.
    """
    data = json_object()
    ids = data.get("ids")
    if (not isinstance(ids, list) or not ids or len(ids) > 5000
            or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids)):
        return jsonify({"error": "Send a list of up to 5000 item ids."}), 400
    conn = _conn()
    kept = db.mark_large_files_reviewed(conn, ids)
    return jsonify({"kept": kept})


# ---------------------------------------------------------------------------
# Role preview — "show me what a guest sees"
# ---------------------------------------------------------------------------

@admin_bp.get("/api/admin/preview")
@require_admin
def preview():
    """Count and sample what a given role (or profile) would actually see.

    This is the check an admin wants before handing out a login: not "what did
    I configure" but "what will they get".
    """
    from .api import _as_id
    cfg = _cfg()
    conn = _conn()
    libraries = cfg.libraries or ([cfg.active_root] if cfg.active_root else [])
    if not libraries:
        return jsonify({"total": 0, "items": []})

    person_id = _as_id(request.args.get("person"))
    if person_id is not None:
        person = auth.get_user(conn, person_id)
        if person is None:
            return jsonify({"error": "No such profile."}), 404
        role = person.role
        label = person.display_name
        # Resolve exactly as a real request would: their assigned folder wins,
        # the legacy scope column is the fallback, admins see everything.
        if person.is_admin:
            roots, scope = libraries, None
        else:
            roots, scope = auth.resolve_library(person.assigned_library, libraries)
            scope = scope or (person.effective_scope or None)
        assignment = person.assigned_library
    else:
        role = request.args.get("role", auth.ROLE_GUEST)
        if role not in auth.ROLES:
            return jsonify({"error": "Unknown role."}), 400
        label = ROLE_LABELS[role]
        assignment = request.args.get("library") or None
        roots, scope = auth.resolve_library(assignment, libraries)
        scope = scope or auth.normalise_scope(request.args.get("scope"))

    limits = {
        "viewer_id": 0,
        "max_visibility": auth.max_visibility_for(role),
        "scope": scope,
    }
    g.date_preview_role = role
    try:
        rows, total = db.query_assets(conn, roots, limit=24, offset=0, **limits)
        stats = db.library_stats(conn, roots, **limits)
    finally:
        g.pop("date_preview_role", None)
    return jsonify({
        "as": label,
        "role": role,
        "role_label": ROLE_LABELS[role],
        "scope": scope,
        "library": assignment,
        "folders": roots,
        "total": total,
        "stats": stats,
        "items": [
            {
                "id": row["id"],
                "name": row["filename"],
                "kind": row["kind"],
                "folder": row["folder"],
                "has_thumb": bool(row["thumb"]),
                # Thumbnails are cached `immutable`; this is what changes when
                # one is rewritten. See _thumb_version in api.py.
                "thumb_v": media.thumb_version(row, _recipe()),
                "visibility": VIS_NAMES[int(row["visibility"])],
            }
            for row in rows
        ],
    })


# ---------------------------------------------------------------------------
# Library folders
# ---------------------------------------------------------------------------

@admin_bp.get("/api/admin/libraries")
@require_admin
def libraries():
    cfg = _cfg()
    return jsonify({"folders": _library_summaries(_conn(), cfg)})


@admin_bp.delete("/api/admin/libraries")
@require_admin
def remove_library():
    """Stop indexing a library folder. Files on disk are never touched."""
    cfg = _cfg()
    conn = _conn()
    path = str(request.args.get("path", "")).strip()
    if not path:
        return jsonify({"error": "Which folder?"}), 400

    assigned = [row for row in conn.execute(
        "SELECT id, display_name, library FROM users "
        "WHERE active=1 AND library IS NOT NULL").fetchall()
        if _assigned_within(row["library"], path)]
    if assigned and not request.args.get("force"):
        names = ", ".join(r["display_name"] for r in assigned[:5])
        return jsonify({
            "error": f"{names} {'is' if len(assigned) == 1 else 'are'} assigned "
                     f"to that folder. Reassign them first, or confirm to remove "
                     f"it anyway: they will see nothing until they are reassigned.",
            "assigned": [r["display_name"] for r in assigned],
        }), 409

    listed = list(cfg.roots)
    if not cfg.remove_library(path):
        return jsonify({"error": "That folder is not in the library."}), 404
    # The index knows a folder by the name it was listed under, which need not
    # be the name asked for ("/photos/" for "/photos", or a link to it):
    # deleting by the request's spelling left every row of the folder behind.
    removed = [root for root in listed if root not in cfg.roots]

    # Assignments inside the removed folder are deliberately left in place.
    # An assignment that matches no library resolves to *nothing*
    # (auth.resolve_library), whereas clearing it would mean "every library" —
    # removing a child's folder must never hand them the whole house.
    conn.executemany("DELETE FROM assets WHERE root=?", [(root,) for root in removed])
    conn.commit()
    cfg.save()
    auth.audit(conn, current_user().id, "remove_library", path)
    return jsonify({"ok": True, "folders": _library_summaries(conn, cfg)})


@admin_bp.post("/api/admin/libraries/active")
@require_admin
def set_active_library():
    """Choose which folder the console's Visibility tab operates on."""
    cfg = _cfg()
    data = json_object()
    path = str(data.get("path", "")).strip()
    if path not in cfg.roots:
        return jsonify({"error": "That folder is not in the library."}), 404
    cfg.active_root = path
    cfg.save()
    return jsonify({"ok": True, "root": cfg.active_root})


@admin_bp.get("/api/admin/assignable")
@require_admin
def assignable():
    """Folders an admin can hand to a profile: every library folder, plus
    every indexed folder inside one, as absolute paths."""
    cfg = _cfg()
    conn = _conn()
    out: list[dict[str, Any]] = []
    for root in cfg.roots:
        total, rows = db.cached_aggregate(conn, ("assignable", root), lambda root=root: (
            conn.execute("SELECT COUNT(*) n FROM assets WHERE root=? AND trashed=0",
                         (root,)).fetchone()["n"] or 0,
            [dict(entry) for entry in conn.execute(
                "SELECT folder, COUNT(*) n FROM assets WHERE root=? AND trashed=0 "
                "AND folder != '' GROUP BY folder", (root,))],
        ))
        out.append({
            "path": root,
            "label": Path(root).name or root,
            "depth": 0,
            "count": total,
            "is_root": True,
        })
        totals: dict[str, int] = {}
        for entry in rows:
            parts = [p for p in entry["folder"].split("/") if p]
            for depth in range(1, len(parts) + 1):
                prefix = "/".join(parts[:depth])
                totals[prefix] = totals.get(prefix, 0) + entry["n"]
        for prefix, count in sorted(totals.items()):
            out.append({
                "path": f"{root}/{prefix}".replace("//", "/"),
                "label": prefix.split("/")[-1],
                "depth": prefix.count("/") + 1,
                "count": count,
                "is_root": False,
            })
    return jsonify({"folders": out})


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@admin_bp.post("/api/admin/settings")
@require_admin
def settings():
    cfg = _cfg()
    data = json_object()
    changed = []

    # A switch in the console, a number everywhere else. Sampling a clip at
    # several moments is the most expensive pass a scan has — five decodes and
    # five passes through the model per file, which on a processor is hours for
    # a few thousand videos — so a household has to be able to say no to it
    # without hand-editing config.json. Off does not mean videos go untagged:
    # the tagging pass reads their poster frame like any other thumbnail. It
    # means one moment instead of five.
    #
    # Read before anything is written, so a number that makes no sense leaves
    # the whole request alone rather than saving the switches sent with it.
    keyframes = None
    if "video_keyframes" in data:
        wanted = data["video_keyframes"]
        if isinstance(wanted, bool):
            wanted = DEFAULT_VIDEO_KEYFRAMES if wanted else 0
        try:
            keyframes = max(0, min(len(KEYFRAME_POINTS), int(wanted)))
        except (TypeError, ValueError, OverflowError):   # Infinity, 1e400
            return jsonify({"error": "video_keyframes must be a number"}), 400

    for key in ("open_browsing", "nsfw_filter", "hide_screens", "watch", "ai_enabled",
                "ai_gpu", "update_check", "straighten_auto", "straighten_auto_apply",
                "straighten_requires_face",
                "outside_ai_for_family",
                *SCAN_PASSES):
        if key in data:
            setattr(cfg, key, bool(data[key]))
            changed.append(key)
    if "hide_screens" in changed:
        # Hiding a few thousand rows, or scoring vectors tagging already made,
        # is seconds rather than instant; the switch answers straight away.
        threading.Thread(target=_scanner().apply_screen_rule,
                         name="ninaivu-screens", daemon=True).start()
    if keyframes is not None:
        cfg.video_keyframes = keyframes
        changed.append("video_keyframes")
    if "house_name" in data:
        # The name everyone starts with. Family members may keep their own
        # instead, so this is a default rather than a decree.
        cfg.house_name = clean_home_name(data["house_name"])
        changed.append("house_name")
    # Remote access (server/remote.py): which provider, and what it needs.
    if "remote_access" in data:
        from ..server import remote                          # noqa: PLC0415
        value = str(data["remote_access"] or "auto").strip().lower()
        if value not in remote.PROVIDERS:
            return jsonify({"error": "remote_access must be one of "
                                     + ", ".join(remote.PROVIDERS)}), 400
        cfg.remote_access = value
        changed.append("remote_access")
    if "remote_networks" in data:
        import ipaddress                                     # noqa: PLC0415
        nets = data["remote_networks"]
        if isinstance(nets, str):
            nets = [n for n in (p.strip() for p in nets.split(",")) if n]
        if not isinstance(nets, list) or not all(isinstance(n, str) for n in nets):
            return jsonify({"error": "remote_networks is a list of address ranges"}), 400
        try:
            nets = [str(ipaddress.ip_network(n.strip(), strict=False)) for n in nets]
        except ValueError as exc:
            return jsonify({"error": f"remote_networks: {exc}"}), 400
        cfg.remote_networks = nets
        changed.append("remote_networks")
    if "remote_hostname" in data:
        host = str(data["remote_hostname"] or "").strip().lower()
        if host and (len(host) > 253 or any(c.isspace() or c in "/\\@:" for c in host)):
            return jsonify({"error": "remote_hostname is a host name"}), 400
        cfg.remote_hostname = host
        changed.append("remote_hostname")
    if changed:
        cfg.save()
        auth.audit(_conn(), current_user().id, "settings", ", ".join(changed))
    return jsonify({
        "ok": True,
        "changed": changed,
        "settings": {
            "open_browsing": cfg.open_browsing,
            "nsfw_filter": cfg.nsfw_filter,
            "hide_screens": cfg.hide_screens,
            "watch": cfg.watch,
            "ai_enabled": cfg.ai_enabled,
            "ai_gpu": cfg.ai_gpu,
            "video_keyframes": cfg.video_keyframes,
            **{key: getattr(cfg, key) for key in SCAN_PASSES},
            "house_name": cfg.house_name,
            "house_name_effective": house_name(cfg),
            "remote_access": cfg.remote_access,
            "remote_networks": list(cfg.remote_networks or []),
            "remote_hostname": cfg.remote_hostname,
            "update_check": cfg.update_check,
        },
    })


# ---------------------------------------------------------------------------
# Extensions
# ---------------------------------------------------------------------------
#
# The optional pieces outside the core's promise (ninaivu/extensions.py).
# Every one is off until an administrator turns it on here, and the answer
# says, for each, whether anything leaves this computer when it is on and
# where it goes. A change takes effect at the next start.

@admin_bp.get("/api/admin/extensions")
@require_admin
def extensions_listing():
    from .. import extensions
    return jsonify(extensions=extensions.listing(_cfg()),
                   active=[e.name for e in extensions.active(_cfg())])


@admin_bp.post("/api/admin/extensions")
@require_admin
def extensions_switch():
    from .. import extensions
    data = json_object()
    name = data.get("name")
    on = data.get("enabled")
    if not isinstance(name, str) or not name.strip() or not isinstance(on, bool):
        return jsonify(error="Send the extension's name and whether it is on."), 400
    name = name.strip()
    if on and name not in extensions.discover():
        return jsonify(error=f"{name} is not installed on this machine."), 404
    cfg = _cfg()
    extensions.set_enabled(cfg, name, on)
    cfg.save()
    auth.audit(_conn(), current_user().id, "settings",
               f"turned the {name} extension {'on' if on else 'off'}")
    return jsonify(extensions=extensions.listing(cfg),
                   active=[e.name for e in extensions.active(cfg)],
                   restart_needed=True)


# ---------------------------------------------------------------------------
# Ninaivu 5.0: Bitrot & Integrity Scrubber
# ---------------------------------------------------------------------------

import hashlib

_SCRUBBER_RUNNING = False
_SCRUBBER_LOCK = threading.Lock()
_SCRUBBER_PROGRESS = {"total": 0, "processed": 0, "running": False,
                      **{state: 0 for state in db.BITROT_STATES}}

def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(1048576):
            digest.update(chunk)
    return digest.hexdigest()


def _classify(previous: dict | None, actual_hash: str,
              stat_result) -> tuple[str, str | None]:
    """Decide what this file's new fingerprint means.

    Returns the status and the hash it was compared against.

    The whole value of the scrubber is in this function, and specifically in
    the middle case. A changed hash on its own says nothing: an edit and bit
    rot look identical if you only look at the bytes. What separates them is
    the file's modification time and size — the user's photo editor moves
    both, and decay on disk moves neither. So a different hash beside an
    untouched mtime and size is the one thing nothing legitimate produces,
    and that is what gets reported.
    """
    if previous is None or not previous.get("actual_hash"):
        return "baseline", None

    expected = str(previous["actual_hash"])
    if actual_hash == expected:
        return "verified", expected

    # The bytes differ. Was the file touched?
    old_mtime = previous.get("file_mtime")
    old_size = previous.get("file_size")
    if old_mtime is None or old_size is None:
        # Recorded before Ninaivu kept the file's shape, so the question cannot
        # be answered. Re-baseline rather than raise an alarm we cannot stand
        # behind; the next pass has what it needs.
        return "baseline", expected

    touched = (abs(float(old_mtime) - stat_result.st_mtime) > 0.001
               or int(old_size) != int(stat_result.st_size))
    return ("changed" if touched else "corrupt"), expected


def notifier():
    """The process's notifier, rebuilt whenever the settings change."""
    from ..utils import notify  # noqa: PLC0415

    existing = current_app.config.get("MV_NOTIFY")
    if existing is None:
        existing = notify.from_config(_cfg())
        current_app.config["MV_NOTIFY"] = existing
    return existing


def notify_event(event: str, summary: str, detail: str = "") -> None:
    """Report something, from anywhere, without ever raising.

    The caller is normally a background thread in the middle of doing the
    thing being reported, so a broken webhook must not become a broken
    scrubber.
    """
    try:
        notifier().send(event, summary, detail)
    except Exception as exc:  # noqa: BLE001
        # Never raised to the caller, but not swallowed either: otherwise a
        # broken webhook stays broken, unnoticed, for as long as nobody wonders
        # why the notifications stopped.
        import logging
        logging.getLogger(__name__).warning(
            "notification %r could not be sent: %s: %s", event, type(exc).__name__, exc)


def _notification_settings() -> dict[str, Any]:
    """The notifier's state, and what the console's form was saved with.

    The form used to open empty and send every field when saved, so changing
    one tick box wiped the webhook address and the whole email setup — and
    with them the warnings about failing backups and disks. The password is
    never sent back: only whether one is saved.
    """
    cfg = _cfg()
    return {**notifier().snapshot(), "form": {
        "webhook": getattr(cfg, "notify_webhook", "") or "",
        "smtp_host": getattr(cfg, "notify_smtp_host", "") or "",
        "smtp_port": int(getattr(cfg, "notify_smtp_port", 587) or 587),
        "smtp_user": getattr(cfg, "notify_smtp_user", "") or "",
        "smtp_to": getattr(cfg, "notify_smtp_to", "") or "",
        "smtp_password_saved": bool(getattr(cfg, "notify_smtp_password", "")),
    }}


@admin_bp.get("/api/admin/notifications")
@require_admin
def notification_settings():
    return jsonify(_notification_settings())


@admin_bp.post("/api/admin/notifications")
@require_admin
def save_notification_settings():
    from ..utils import notify  # noqa: PLC0415

    cfg = _cfg()
    data = json_object()
    # Check the numbers before changing anything, so a typo is a 400 rather
    # than a 500 that has already half-applied the other settings.
    try:
        if "smtp_port" in data:
            port = int(data["smtp_port"] or 587)
            if not 0 < port < 65536:
                raise ValueError
        if "quiet_hours" in data:
            quiet = max(0, int(float(data["quiet_hours"] or 0) * 3600))
    except (TypeError, ValueError, OverflowError):
        return jsonify({"error": "The SMTP port and quiet hours must be numbers."}), 400
    events = data.get("events") or []
    if not isinstance(events, list):
        return jsonify({"error": "events must be a list."}), 400
    # The same rules the Advanced page applies to these two (an http(s) address,
    # one of the known formats), so the two pages cannot save different things.
    from ..server import settings_groups  # noqa: PLC0415
    for short in ("webhook", "webhook_format"):
        if short in data and str(data[short]).strip():
            try:
                settings_groups.coerce(f"notify_{short}", str(data[short]).strip())
            except settings_groups.BadValue as exc:
                return jsonify({"error": str(exc)}), 400
    fields = {
        "notify_webhook": str,
        "notify_webhook_format": str,
        "notify_smtp_host": str,
        "notify_smtp_user": str,
        "notify_smtp_password": str,
        "notify_smtp_to": str,
    }
    changed = []
    for key, cast in fields.items():
        short = key.replace("notify_", "")
        if short in data:
            value = cast(data[short]).strip()
            if key == "notify_smtp_password" and not value:
                continue                    # left blank: keep the saved one
            setattr(cfg, key, value)
            changed.append(short)
    if "smtp_port" in data:
        cfg.notify_smtp_port = port
        changed.append("smtp_port")
    if "smtp_tls" in data:
        cfg.notify_smtp_tls = bool(data["smtp_tls"])
        changed.append("smtp_tls")
    if "events" in data:
        wanted = [e for e in events if isinstance(e, str) and e in notify.EVENTS]
        cfg.notify_events = wanted
        changed.append("events")
    if "quiet_hours" in data:
        cfg.notify_quiet_seconds = quiet
        changed.append("quiet_hours")

    if changed:
        cfg.save()
        current_app.config["MV_NOTIFY"] = None      # rebuilt on next use
        auth.audit(_conn(), current_user().id, "notifications", ", ".join(changed))
    return jsonify({"ok": True, "changed": changed,
                    "settings": _notification_settings()})


@admin_bp.post("/api/admin/notifications/test")
@require_admin
def test_notification():
    """Prove it reaches somebody, ignoring the quiet window."""
    result = notifier().test()
    return jsonify(result)


# ---------------------------------------------------------------------------
# The weekly photograph
#
# Deliberately not folded into the notification settings above. Those go to
# whoever looks after the machine and promise never to contain a photograph,
# a path or a name; this goes to the household and is a photograph of them.
# The mail server is shared because nobody should type it twice.
# ---------------------------------------------------------------------------

def _digest_keeper():
    services = current_app.config.get("MV_SERVICES")
    return getattr(services, "digest", None) if services else None


def _digest_settings() -> dict[str, Any]:
    from ..utils import digest as digest_kit                # noqa: PLC0415

    cfg = _cfg()
    keeper = _digest_keeper()
    sender = digest_kit.from_config(cfg)
    return {
        "enabled": bool(getattr(cfg, "digest_enabled", False)),
        "to": getattr(cfg, "digest_to", "") or "",
        "weekday": int(getattr(cfg, "digest_weekday", digest_kit.DEFAULT_WEEKDAY)),
        "hour": int(getattr(cfg, "digest_hour", digest_kit.DEFAULT_HOUR)),
        "link": getattr(cfg, "digest_link", "") or "",
        # Without a mail server there is nothing to turn on, and saying so is
        # better than a switch that silently does nothing.
        "has_mail_server": bool(getattr(cfg, "notify_smtp_host", "")),
        "ready": sender.configured,
        "last_sent": db.get_meta(_conn(), digest_kit.ALREADY_SENT_KEY) or "",
        "last_error": getattr(keeper, "last_error", None) if keeper else None,
    }


@admin_bp.get("/api/admin/digest")
@require_admin
def digest_settings():
    return jsonify(_digest_settings())


@admin_bp.post("/api/admin/digest")
@require_admin
def save_digest_settings():
    cfg = _cfg()
    data = json_object()
    try:
        weekday = int(data.get("weekday", cfg.digest_weekday))
        hour = int(data.get("hour", cfg.digest_hour))
        if not 0 <= weekday <= 6 or not 0 <= hour <= 23:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        return jsonify({"error": "The day must be 0–6 and the hour 0–23."}), 400

    changed = []
    for key in ("to", "link"):
        if key in data:
            setattr(cfg, f"digest_{key}", str(data[key] or "").strip())
            changed.append(key)
    if "enabled" in data:
        cfg.digest_enabled = bool(data["enabled"])
        changed.append("enabled")
    if "weekday" in data:
        cfg.digest_weekday = weekday
        changed.append("weekday")
    if "hour" in data:
        cfg.digest_hour = hour
        changed.append("hour")
    if changed:
        cfg.save()
        auth.audit(_conn(), current_user().id, "digest", ", ".join(changed))
        keeper = _digest_keeper()
        if keeper is not None:
            # Turning it on should not wait for the next restart.
            keeper.start() if cfg.digest_enabled else keeper.stop(timeout=1.0)
    return jsonify({"ok": True, "changed": changed, "settings": _digest_settings()})


@admin_bp.get("/api/admin/digest/preview")
@require_admin
def digest_preview():
    """What would go out today, without sending it.

    "Untestable until Sunday" is how a weekly thing ships broken.
    """
    from ..utils import digest as digest_kit                # noqa: PLC0415

    cfg = _cfg()
    roots = cfg.roots or ([cfg.active_root] if cfg.active_root else [])
    built = digest_kit.build(cfg, _conn(), roots)
    if not built["ready"]:
        return jsonify({"ready": False, "reason": built["reason"]})
    item = built["item"]
    return jsonify({
        "ready": True,
        "subject": built["subject"],
        "asset_id": item.get("id"),
        "year": item.get("year"),
        "faces": item.get("face_count"),
        "others": built["others"],
        "has_picture": built["has_picture"],
    })


@admin_bp.post("/api/admin/digest/send")
@require_admin
def digest_send_now():
    """Send one now, whatever day it is. Still refuses an empty day."""
    keeper = _digest_keeper()
    if keeper is None:
        return jsonify({"error": "The digest is not running on this server."}), 503
    result = keeper.run(force=True)
    auth.audit(_conn(), current_user().id, "digest_sent",
               result.get("subject") or result.get("reason") or "")
    return jsonify(result)


#: How a storage check is remembered across a restart (storage/resume.py).
SCRUBBER_RESUME = "scrubber"
#: Files checked between notes of how far the check has got.
SCRUBBER_CHECKPOINT = 200


def start_scrubber_job(db_path: Path | str, after_id: int = 0,
                       scanner=None, notify=None,
                       on_done: Callable[[], None] | None = None) -> bool:
    """Start the storage check in the background. False if one is running.

    *after_id* carries a check on from the last file it had reached, which is
    how start-up picks up one a restart interrupted. *on_done* is called when
    a check reaches the end (ninaivu/storage/repair.py repairs what it found).

    *notify(event, summary, detail)* is how the findings are reported. The
    check runs on a thread of its own, outside any request, where
    ``notify_event`` cannot see the app's settings; so a start from a request
    carries the app along, and start-up passes its own notifier.
    """
    global _SCRUBBER_RUNNING
    if notify is None and has_app_context():
        app = current_app._get_current_object()

        def notify(event: str, summary: str, detail: str = "") -> None:
            with app.app_context():
                notify_event(event, summary, detail)
    with _SCRUBBER_LOCK:
        if _SCRUBBER_RUNNING:
            return False
        _SCRUBBER_RUNNING = True
    def run() -> None:
        if scanner is None:
            finished = _run_scrubber(db_path, int(after_id or 0), notify=notify)
        else:
            from ..media.scanner import CLAIM_STORAGE_CHECK    # noqa: PLC0415
            with scanner.held(CLAIM_STORAGE_CHECK):
                finished = _run_scrubber(db_path, int(after_id or 0),
                                         workload=getattr(scanner, "workload", None),
                                         notify=notify)
        if finished and on_done is not None:
            try:
                on_done()
            except Exception:                                # noqa: BLE001
                import logging
                logging.getLogger(__name__).exception("after the storage check")

    threading.Thread(target=run, name="ninaivu-scrubber", daemon=True).start()
    return True


def _run_scrubber(db_path: Path | str, after_id: int = 0, workload=None, notify=None) -> bool:
    """Read every file and compare it with its fingerprint. True when it
    reached the end, rather than stopping early.

    *workload* (ninaivu/server/workload.py) is asked before each file, so a
    check that reads the whole library makes way for somebody watching a
    video from it.
    """
    global _SCRUBBER_RUNNING, _SCRUBBER_PROGRESS
    from ..storage import resume                          # noqa: PLC0415

    # Everything from the first line is under the ``try``: the running flag was
    # set by the caller, and only the ``finally`` clears it. When the set-up
    # below raised — "database is locked" while a scan wrote, the realistic
    # case — the thread died with the flag still up, and every later Start
    # answered "already running" until Ninaivu was restarted.
    try:
        conn = db.connect(db_path)
        # In id order, so "how far it got" is one number. A check started again
        # after a restart reads the whole library afresh and would otherwise take
        # every file again from the first.
        rows = conn.execute(
            "SELECT id, root, rel_path, size FROM assets WHERE trashed=0 AND id > ? "
            "ORDER BY id", (after_id,)).fetchall()
        already = conn.execute(
            "SELECT COUNT(*) FROM assets WHERE trashed=0 AND id <= ?",
            (after_id,)).fetchone()[0] if after_id else 0
        total = len(rows) + int(already)
        _SCRUBBER_PROGRESS.update(
            total=total, processed=int(already), verified=0, corrupt=0, missing=0,
            baseline=0, changed=0, unreadable=0, running=True, held="")
        resume.want(conn, SCRUBBER_RESUME, {"after_id": after_id})

        def say(reason):
            _SCRUBBER_PROGRESS["held"] = reason or ""

        last_commit = time.monotonic()
        for index, r in enumerate(rows, start=1):
            if workload is not None:
                workload.wait_turn("check", on_hold=say)
            asset_id = int(r["id"])
            root = r["root"]
            rel_path = r["rel_path"]
            full_path = Path(root) / rel_path
            previous = db.last_bitrot_fingerprint(conn, asset_id)

            # Committed about once a second rather than once per file: on a
            # library of small photographs the commit, not the hashing, was
            # most of each file's cost. Not less often than that, because the
            # open transaction holds the write lock while the next file is
            # being read, and a run of large videos would hold it for minutes.
            if not full_path.is_file():
                db.record_bitrot_check(conn, asset_id, root, rel_path,
                                       previous.get("actual_hash") if previous else None,
                                       None, "missing", commit=False)
                _SCRUBBER_PROGRESS["missing"] += 1
            else:
                try:
                    stat_result = full_path.stat()
                    actual_hash = _hash_file(full_path)
                except OSError:
                    db.record_bitrot_check(conn, asset_id, root, rel_path,
                                           previous.get("actual_hash") if previous else None,
                                           None, "unreadable", commit=False)
                    _SCRUBBER_PROGRESS["unreadable"] += 1
                else:
                    status, expected = _classify(previous, actual_hash, stat_result)
                    db.record_bitrot_check(
                        conn, asset_id, root, rel_path, expected, actual_hash,
                        status, stat_result.st_mtime, stat_result.st_size, commit=False)
                    _SCRUBBER_PROGRESS[status] = _SCRUBBER_PROGRESS.get(status, 0) + 1
            _SCRUBBER_PROGRESS["processed"] += 1
            if time.monotonic() - last_commit > 1.0:
                conn.commit()
                last_commit = time.monotonic()
            if index % SCRUBBER_CHECKPOINT == 0:
                resume.want(conn, SCRUBBER_RESUME, {"after_id": asset_id})
        conn.commit()
        resume.done(conn, SCRUBBER_RESUME)
        _report_scrubber_findings(notify)
        return True
    except Exception:                                    # noqa: BLE001
        import logging
        logging.getLogger(__name__).exception("the storage check stopped early")
        return False
    finally:
        with _SCRUBBER_LOCK:
            _SCRUBBER_RUNNING = False
            _SCRUBBER_PROGRESS["running"] = False


def _report_scrubber_findings(notify=None) -> None:
    """Tell somebody if the pass found anything worth acting on.

    Only the states that mean something is wrong, and only when there are any.
    A pass that verifies everything is the normal case and sends nothing —
    a notification that arrives every night is one nobody reads.
    """
    notify = notify or notify_event
    corrupt = _SCRUBBER_PROGRESS.get("corrupt", 0)
    missing = _SCRUBBER_PROGRESS.get("missing", 0)
    unreadable = _SCRUBBER_PROGRESS.get("unreadable", 0)

    if corrupt:
        notify(
            "integrity",
            f"{corrupt} file(s) changed on disk",
            f"An integrity pass found {corrupt} file(s) whose contents differ "
            "from the last check while the file itself was not edited. That is "
            "what bit rot looks like. Check the Storage integrity panel, and "
            "restore those files from a backup.")
    if missing or unreadable:
        notify(
            "missing",
            f"{missing + unreadable} file(s) could not be read",
            f"{missing} indexed file(s) are no longer at their path and "
            f"{unreadable} could not be opened. A disconnected drive explains "
            "both, and so does a deletion outside Ninaivu.")


@admin_bp.post("/api/admin/scrubber/start")
@require_admin
def start_scrubber():
    if not start_scrubber_job(_cfg().db_path, scanner=_scanner()):
        return jsonify({"ok": False, "message": "Scrubber is already running", "progress": _SCRUBBER_PROGRESS})
    return jsonify({"ok": True, "message": "Scrubber started", "progress": _SCRUBBER_PROGRESS})


@admin_bp.get("/api/admin/scrubber/status")
@require_admin
def scrubber_status():
    conn = _conn()
    summary = db.get_bitrot_summary(conn)
    summary["progress"] = _SCRUBBER_PROGRESS
    # The latest check of each file: one since repaired is not listed still.
    summary["recent_issues"] = db.current_bitrot_issues(conn, limit=60)
    return jsonify(summary)



# ---------------------------------------------------------------------------
# Stopping, from outside
# ---------------------------------------------------------------------------

#: Addresses a stop request may come from. Loopback and nothing else: this
#: endpoint is for a script running on the same machine, and a server that can
#: be stopped from the network is a server anybody on the network can stop.
LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"}

#: Headers a reverse proxy adds. Their presence means the loopback address is
#: the proxy's, not the person's. One list, kept in server/auth.py, because the
#: first-run setup asks the same question (see ``auth.request_is_local``).
_FORWARDING_HEADERS = auth.FORWARDING_HEADERS


def from_this_machine() -> bool:
    """Whether the request really started on this computer.

    Behind Caddy or nginx every request arrives from 127.0.0.1. With
    ``trusted_proxies`` set, ProxyFix has already put the real client address
    in ``remote_addr``; without it, a forwarded request is somebody else's and
    must not pass for local.
    """
    return auth.request_is_local(int(getattr(_cfg(), "trusted_proxies", 0) or 0))


def _own_addresses() -> set[str]:
    """Every address this computer answers on, loopback included."""
    found = set(LOOPBACK)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            found.add(str(info[4][0]).split("%", 1)[0])
    except OSError:
        pass
    return found


def from_this_computer() -> bool:
    """Whether the browser is on the computer Ninaivu runs on, however it got here.

    Wider than :func:`from_this_machine`, on purpose. Opening the console as
    ``ninaivu-admin.local`` or by the computer's own network address reaches the
    server from that address, not from loopback, so a person sitting at the
    Ninaivu computer was told the file browser "only opens on the computer the
    server is running on". The address is still one only this computer can
    make a connection from. Stopping the server keeps the loopback-only rule.
    """
    if from_this_machine():
        return True
    proxied = any(request.headers.get(h) for h in _FORWARDING_HEADERS)
    if proxied and not int(getattr(_cfg(), "trusted_proxies", 0) or 0):
        return False
    address = (request.remote_addr or "").strip()
    if address.startswith("::ffff:"):
        address = address[len("::ffff:"):]
    return bool(address) and address in _own_addresses()


@admin_bp.post("/api/admin/shutdown")
def shutdown():
    """Stop Ninaivu properly, at the request of something on this machine.

    Not behind :func:`require_admin`, and deliberately so — the caller is
    ``stop.bat``, which has no session and no password to offer. It proves
    itself instead with the token from the run file in the state directory,
    which is written readable only by the account Ninaivu runs as. Anybody able
    to read that file can already stop the process by other means; anybody who
    cannot read it gets a 404 from here.

    404, not 403, because a route that answers "wrong token" is a route that
    confirms it exists and invites guessing at the right one.
    """
    if not from_this_machine():
        return jsonify({"error": "not found"}), 404

    stopper = current_app.config.get("MV_SHUTDOWN")
    expected = current_app.config.get("MV_STOP_TOKEN")
    if not stopper or not expected:
        return jsonify({"error": "not found"}), 404

    data = request.get_json(silent=True)
    data = data if isinstance(data, dict) else {}       # still 404, never 400
    offered = str(data.get("token") or request.headers.get("X-Ninaivu-Token") or "")
    # Constant time, because the comparison is the whole of the check.
    import hmac

    if not hmac.compare_digest(offered, str(expected)):
        return jsonify({"error": "not found"}), 404

    conn = _conn()
    user = current_user()
    auth.audit(conn, getattr(user, "id", None), "shutdown",
               "asked to stop from this machine")
    # The answer goes out before anything is taken down, so the caller is told
    # the request was accepted rather than left holding a dropped connection
    # and having to guess whether it worked.
    stopper()
    return jsonify({"stopping": True})


# ---------------------------------------------------------------------------
# Phone backups (ninaivu/media/phone_backup.py)
# ---------------------------------------------------------------------------

@admin_bp.get("/api/admin/phone-backups")
@require_admin
def phone_backups_overview():
    """Who backs up a phone, and how much of it waits for review."""
    from ..media import phone_backup                         # noqa: PLC0415

    conn = _conn()
    phone_backup.init_schema(conn)
    people = [dict(r) for r in conn.execute(
        "SELECT pb.user_id, COALESCE(u.display_name, u.username, 'Former family member') "
        "AS name, COUNT(DISTINCT pb.device_id) AS phones, "
        "SUM(pb.state IN ('staged','done','duplicate')) AS safe, "
        "SUM(pb.state='staged' AND pu.status='pending') AS waiting, "
        "MAX(pb.finished_at) AS last_backup "
        "FROM phone_backups pb LEFT JOIN users u ON u.id = pb.user_id "
        "LEFT JOIN pending_uploads pu ON pu.id = pb.upload_id "
        "GROUP BY pb.user_id ORDER BY last_backup DESC")]
    return jsonify(trusted=bool(_cfg().phone_backup_trusted), people=people)


@admin_bp.post("/api/admin/phone-backups/settings")
@require_admin
def phone_backups_settings():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get("trusted"), bool):
        return jsonify(error="Send trusted: true or false."), 400
    cfg = _cfg()
    cfg.phone_backup_trusted = data["trusted"]
    cfg.save()
    auth.audit(_conn(), current_user().id, "phone_backup_trust",
               "on" if cfg.phone_backup_trusted else "off")
    return phone_backups_overview()


@admin_bp.post("/api/admin/phone-backups/<int:user_id>/approve")
@require_admin
def phone_backups_approve(user_id):
    """Approve everything one person's phones have backed up, in one go."""
    from .api_phone_backup import file_now                   # noqa: PLC0415

    conn = _conn()
    result = file_now(conn, _cfg(), user_id, current_user().id)
    if result.get("later"):
        return jsonify(error="Wait for the current library operation to finish."), 409
    auth.audit(conn, current_user().id, "phone_backup_approve",
               f"user {user_id}: {result['approved']} filed")
    return jsonify(result)


def scrubber_running() -> bool:
    return _SCRUBBER_RUNNING


# -- repairing what the check found (ninaivu/storage/repair.py) -----------------

def _repairer():
    return current_app.config["MV_SERVICES"].repairer


@admin_bp.get("/api/admin/repair")
@require_admin
def repair_status():
    return jsonify(_repairer().status())


@admin_bp.post("/api/admin/repair")
@require_admin
def repair_start():
    """Put back what the storage check found damaged or missing, from a copy
    whose bytes match what the file was."""
    data = json_object()
    ids = data.get("ids")
    if ids is not None and (not isinstance(ids, list)
                            or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids)):
        return jsonify({"error": "ids must be a list of numbers", "status": 400}), 400
    try:
        status = _repairer().start(ids or None)
    except ValueError as exc:
        return jsonify({"error": str(exc), "status": 409}), 409
    auth.audit(_conn(), current_user().id, "repair", f"{status['waiting']} waiting")
    return jsonify(status)


@admin_bp.post("/api/admin/repair/stop")
@require_admin
def repair_stop():
    _repairer().stop()
    return jsonify(_repairer().status())


@admin_bp.post("/api/admin/repair/settings")
@require_admin
def repair_settings():
    """How often the storage check runs by itself, and whether it repairs."""
    data = json_object()
    cfg = _cfg()
    if "every_days" in data:
        try:
            days = int(data["every_days"])
        except (TypeError, ValueError):
            return jsonify({"error": "every_days must be a number", "status": 400}), 400
        if not 0 <= days <= 365:
            return jsonify({"error": "Between 0 (never) and 365 days.", "status": 400}), 400
        cfg.scrub_every_days = days
    if "automatic" in data:
        if not isinstance(data["automatic"], bool):
            return jsonify({"error": "automatic must be true or false", "status": 400}), 400
        cfg.scrub_repair = data["automatic"]
    cfg.save()
    return jsonify(_repairer().status())


# ---------------------------------------------------------------------------
# Location privacy: the home zone (ninaivu/utils/location.py)
# ---------------------------------------------------------------------------

def _privacy() -> dict[str, Any]:
    from ..utils import location                            # noqa: PLC0415
    cfg = _cfg()
    conn = _conn()
    inside = None
    zone = location.home_zone(cfg)
    if zone is not None:
        lat, lon, km = zone
        pad_lat = km / 111.0
        inside = int(conn.execute(
            "SELECT COUNT(*) FROM assets WHERE trashed=0 AND gps_lat BETWEEN ? AND ? "
            "AND gps_lon IS NOT NULL AND ninaivu_km(gps_lat, gps_lon, ?, ?) <= ?",
            (lat - pad_lat, lat + pad_lat, lat, lon, km)).fetchone()[0])
    return {"home_lat": cfg.home_lat, "home_lon": cfg.home_lon,
            "home_radius_m": int(cfg.home_radius_m or 300),
            "strip_location": cfg.strip_location, "inside": inside,
            "suggestion": location.suggest_home(conn, list(cfg.roots or ([cfg.active_root] if cfg.active_root else [])))}


@admin_bp.get("/api/admin/privacy")
@require_admin
def privacy_settings():
    return jsonify(_privacy())


@admin_bp.post("/api/admin/privacy")
@require_admin
def save_privacy_settings():
    from ..utils import location                            # noqa: PLC0415
    data = json_object()
    cfg = _cfg()
    if "clear" in data and data["clear"] is True:
        cfg.home_lat = cfg.home_lon = None
    if "home_lat" in data or "home_lon" in data:
        try:
            lat, lon = float(data.get("home_lat")), float(data.get("home_lon"))
        except (TypeError, ValueError):
            return jsonify({"error": "Give the latitude and longitude as numbers.", "status": 400}), 400
        # NaN compares false with everything, so it passed the range below.
        if not (-90 <= lat <= 90 and -180 <= lon <= 180) or lat != lat or lon != lon:
            return jsonify({"error": "That is not a place on Earth.", "status": 400}), 400
        cfg.home_lat, cfg.home_lon = round(lat, 6), round(lon, 6)
    if "home_radius_m" in data:
        try:
            radius = int(data["home_radius_m"])
        except (TypeError, ValueError, OverflowError):
            return jsonify({"error": "The radius must be a number of metres.", "status": 400}), 400
        cfg.home_radius_m = max(50, min(radius, 20000))
    if "strip_location" in data:
        if data["strip_location"] not in location.MODES:
            return jsonify({"error": "off, home or all", "status": 400}), 400
        cfg.strip_location = data["strip_location"]
    cfg.save()
    auth.audit(_conn(), current_user().id, "privacy",
               f"home zone {'set' if cfg.home_lat is not None else 'cleared'}, "
               f"strip {cfg.strip_location}")
    return jsonify(_privacy())


# ---------------------------------------------------------------------------
# XMP sidecars beside the photographs (ninaivu/storage/xmp.py)
# ---------------------------------------------------------------------------

def _xmp():
    return current_app.config["MV_SERVICES"].xmp


@admin_bp.get("/api/admin/xmp")
@require_admin
def xmp_status():
    return jsonify(_xmp().status())


@admin_bp.post("/api/admin/xmp")
@require_admin
def xmp_settings():
    """Turn the sidecars on or off, or write them now (``force`` rewrites all)."""
    data = json_object()
    writer = _xmp()
    cfg = _cfg()
    if "enabled" in data:
        if not isinstance(data["enabled"], bool):
            return jsonify({"error": "enabled must be true or false", "status": 400}), 400
        cfg.xmp_sidecars = data["enabled"]
        cfg.save()
        auth.audit(_conn(), current_user().id, "xmp",
                   "sidecars on" if cfg.xmp_sidecars else "sidecars off")
    if data.get("write") or (data.get("enabled") is True):
        if not cfg.xmp_sidecars:
            return jsonify({"error": "Turn the sidecars on first.", "status": 409}), 409
        try:
            writer.start(force=bool(data.get("force")))
        except ValueError as exc:
            return jsonify({"error": str(exc), "status": 409}), 409
    return jsonify(writer.status())
