"""Endpoints that exist only on the admin console (port 3000).

Nothing here is mounted on the family app, so these paths are not merely
forbidden there — they are absent. A 404 on the home port is the correct
answer, and it is the one the router gives.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
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
from ._body import json_body, json_object
from ..words import said


def _components():
    from ..media import components                            # noqa: PLC0415
    return components

log = logging.getLogger(__name__)

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


@admin_bp.get("/api/admin/library/relocation")
@require_admin
def library_relocation_get():
    """Library folders that went missing and were found elsewhere by their
    id, waiting for an administrator to say which folder is the library (a
    backup copy carries the same id). See storage/library_id.py."""
    from ..storage import library_id                         # noqa: PLC0415
    return jsonify({"pending": library_id.pending_relocations(_cfg().state_dir)})


@admin_bp.post("/api/admin/library/relocation")
@require_admin
def library_relocation_confirm():
    """``{"old": missing folder, "new": one of the folders found}``. The move
    is made at the next start, when the index can be moved safely."""
    from ..storage import library_id                         # noqa: PLC0415
    data = json_object()
    old, new = data.get("old"), data.get("new")
    if not isinstance(old, str) or not isinstance(new, str):
        return jsonify({"error": "Give the missing folder and the folder to use."}), 400
    if not library_id.confirm_relocation(_cfg().state_dir, old, new):
        return jsonify({"error": "That folder was not one of those found for the library."}), 400
    auth.audit(_conn(), current_user().id, "library_relocation", f"{old} -> {new}")
    return jsonify({"ok": True, "restart_needed": True})


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


#: How long the count of unnamed groups of faces is reused. It reads every
#: face in the library (650 ms at 300,000 of them) for a number on the
#: Overview that the page asks for every ten seconds, and that only moves when
#: somebody names faces — which here makes it count again at once.
UNNAMED_FRESH_FOR = 60.0
_unnamed_seen: dict[tuple, tuple[float, int, int]] = {}
_unnamed_lock = threading.Lock()


def _unnamed_groups(conn, roots, max_visibility: int) -> int:
    key = (str(_cfg().db_path), tuple(roots), int(max_visibility))
    now, edits = time.monotonic(), db.face_edits()
    with _unnamed_lock:
        held = _unnamed_seen.get(key)
    if held is not None and held[1] == edits and now - held[0] < UNNAMED_FRESH_FOR:
        return held[2]
    groups = db.count_unnamed_clusters(conn, roots, max_visibility=max_visibility,
                                       min_size=3, limit=200)
    with _unnamed_lock:
        _unnamed_seen[key] = (now, edits, groups)
    return groups


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
        groups = _unnamed_groups(conn, roots, user.max_visibility) if roots else 0
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
    # The handover plan, gently: never written, not looked at for six months,
    # or the backups changed since (api/api_handover.py).
    from .api_handover import current_nudges                  # noqa: PLC0415
    try:
        nudges = current_nudges(conn)
    except sqlite3.Error:
        nudges = []
    items.append({
        "key": "handover", "count": 1 if nudges else 0, "page": "handover",
        "title": said("The handover plan wants a look"),
        "detail": (said("Nobody has written down yet who looks after the library if you cannot.")
                   if "none" in nudges else
                   said("The backups have changed since the plan was last reviewed.")
                   if "backups" in nudges else
                   said("It has not been reviewed for six months.")),
        "nudges": nudges})
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
    from .api import INLINE_TYPES, download_name
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
                         as_attachment=mime not in INLINE_TYPES, conditional=True,
                         download_name=None if mime in INLINE_TYPES else download_name(path.name))
    response.headers["Cache-Control"] = "private, no-store"
    return response


@admin_bp.post("/api/admin/uploads/<int:upload_id>/approve")
@require_admin
def approve_upload(upload_id):
    from ..media import date_edit, upload_review
    data = json_body()
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
        # An approval adds one file and its row and moves nothing already in
        # the library: an analysis under way carries on (Scanner.defer). A
        # scan still reading is stopped and waited for, as before.
        stopped = scanner.defer("approving a family upload", analysis_may_continue=True)
        if stopped:
            scanner.stop(join=True, watching=True)
        try:
            if stopped and scanner.running:
                return jsonify(error="The scanner is still stopping. Try again shortly."), 409
            item = upload_review.approve(_conn(), _cfg(), upload_id, current_user().id,
                                         data.get("creation_date"))
            landed.append(str(Path(item["root"], item["rel_path"]).parent)
                          if item.get("rel_path") else item.get("root"))
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

    A scan the change interrupted carries on. Otherwise only the folders the
    change wrote into are read — *roots* may name library folders or folders
    inside them: every library folder used to be scanned, and on a USB drive
    that was four minutes to index the one photograph just approved; then the
    whole of the one it was filed in, 200,000 files on the Mac mini. Every
    folder is watched afterwards either way (Scanner.start watches even when
    another job still holds the indexer and the scan is only queued).
    """
    if scanner.resume(reason) or not _cfg().watch:
        return
    wanted = sorted({str(root) for root in roots if root})
    libraries = [Path(root) for root in _cfg().libraries]
    inside = [folder for folder in wanted
              if any(Path(folder) != library and Path(folder).is_relative_to(library)
                     for library in libraries)]
    whole = [folder for folder in wanted if folder not in inside]
    if inside:
        scanner.start(folders=inside)
    if whole:
        scanner.start(whole)
    if not wanted:
        scanner.watch()


def _moving_files(scanner) -> bool:
    """Whether something that moves files around the library is under way.

    A consolidation or another approval is: approving now would move files
    under it. A straightening pass or a storage check also holds the indexer
    down, but only reads — an approval does not have to wait hours for one.
    """
    from ..media.scanner import CLAIM_IMPORT, READ_ONLY_CLAIMS   # noqa: PLC0415
    # An import only adds new files, as an approval does, so the two do not
    # get in each other's way: a phone backup finished during an import of a
    # Takeout used to wait for all of it, hours, for nothing.
    beside = READ_ONLY_CLAIMS | {CLAIM_IMPORT}
    held = scanner.deferred or ""
    return bool(held) and not all(claim in beside for claim in held.split("; "))


@admin_bp.post("/api/admin/date-policy")
@require_admin
def date_policy_save():
    import json
    from ..server import date_policy
    try:
        policy = date_policy.validate(json_body())
    except (ValueError, TypeError):
        return jsonify(error="Use a YYYY-MM-DD cutoff and before, after or all for every role."), 400
    db.set_meta(_conn(), date_policy.KEY, json.dumps(policy))
    auth.audit(_conn(), current_user().id, "date_policy", json.dumps(policy))
    return jsonify(policy)


@admin_bp.patch("/api/admin/assets/<int:asset_id>/creation-date")
@require_admin
def change_creation_date(asset_id):
    from ..media import date_edit
    data = json_body()
    try:
        if not isinstance(data, dict) or set(data) != {"creation_date"}:
            raise ValueError("Provide a creation_date in YYYY-MM-DD format.")
        when = date_edit.parse_date(data["creation_date"])
    except (ValueError, TypeError) as exc:
        return jsonify(error=str(exc)), 400
    # Changing the date moves the file to that day's folder, which a read-only
    # library — an NTFS drive on a Mac — cannot do. Say so before trying.
    from ..storage import new_files
    asset = before = db.get_asset(_conn(), asset_id)
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
            # The folder it left and the one it went to, and nothing else:
            # the whole library folder used to be read for one photograph.
            for moved in (before, asset):
                if moved and moved.get("rel_path"):
                    landed.append(str(Path(moved["root"], moved["rel_path"]).parent))
                elif moved:
                    landed.append(moved.get("root"))
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
        "file": str(logs.folder(_cfg().state_dir) / logs.LOG_NAME),
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
            "ffmpeg": _components().ffmpeg_available(),
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
        # Unchanged until the library is, and the Overview asks on every visit.
        row = db.cached_aggregate(conn, ("library_summary", root), lambda root=root: dict(
            conn.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(size),0) bytes FROM assets "
                "WHERE root=? AND trashed=0", (root,),
            ).fetchone()))
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

    # The subfolders, their counts and covers: a pass over everything beneath
    # this folder, nearly half a second at the root of a 200,000-file library.
    # Remembered until the library or a thumbnail changes. The screenshot test
    # also reads the search model's scores, which no counter follows, so it is
    # worked out again after five minutes whatever happens.
    recipe = _recipe()
    children, here, kinds = db.cached_aggregate(
        conn, ("admin_folder", root, folder, show, screen_sql, tuple(screen_params), recipe),
        lambda: _folder_children(conn, root, folder, prefix, screen_sql, screen_params,
                                 keep_sql, keep_params, row_keep, row_keep_params, recipe),
        also=(db.LAYOUT_GENERATION_KEY,), max_age=300.0)

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


def _folder_children(conn, root: str, folder: str, prefix: str, screen_sql: str,
                     screen_params: list, keep_sql: str, keep_params: list, row_keep: str,
                     row_keep_params: list, recipe: str) -> tuple[dict, dict, dict]:
    """The subfolders of *folder* with their counts and covers, the counts of
    what is directly in it, and of each kind beneath it (folder_detail)."""
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
        node["cover_v"] = media.thumb_version(cover, recipe) if cover else ""
    return children, here, kinds


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
    db.ensure_copies_counter(conn)
    roots = cfg.roots or ([cfg.active_root] if cfg.active_root else [])
    empty = {"files": 0, "bytes": 0, "backed_up_bytes": 0, "by_kind": [], "by_year": [],
             "by_camera": [], "by_folder": [], "by_type": []}
    if not roots:
        return jsonify(empty)
    # Five reads of the whole library, half a second at 200,000 files:
    # remembered until the library or the record of uploads changes.
    return jsonify(db.cached_aggregate(
        conn, ("storage_report", tuple(roots)), lambda: _storage_report(conn, roots),
        also=(db.COPIES_GENERATION_KEY,)))


def _storage_report(conn, roots: list[str]) -> dict[str, Any]:
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
    return {
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
    }


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


def _damaged_video(row: dict[str, Any]) -> bool:
    """Whether a listed video is damaged at its source (see
    media/video_compress.py). Only a video the scan could not read a length
    or a picture size from is looked at, so the list stays quick."""
    if row.get("kind") != "video" or (row.get("duration") and row.get("width")):
        return False
    from ..media import video_compress as vc                 # noqa: PLC0415
    return vc.damaged(Path(row["root"]) / row["rel_path"])


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
            "damaged": _damaged_video(row),
            "id": row["id"],
            "filename": row["filename"],
            "folder": row["folder"],
            "kind": row["kind"],
            "ext": row.get("ext"),
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
# Large files — compress a video, or replace it with a smaller one
# ---------------------------------------------------------------------------
#
# The two answers the worklist gained beside Keep and Delete, for videos only.
# The encode, the checks and the queue are media/video_compress.py; what is
# here is who may ask, which file, and what the index says afterwards.

def _compress_row(conn, asset_id: Any) -> dict[str, Any] | None:
    """The video an admin asked about, if it is one they can reach."""
    from .api import _as_id                                  # noqa: PLC0415
    asset_id = _as_id(asset_id)
    if asset_id is None:
        return None
    cfg = _cfg()
    libraries = cfg.libraries or ([cfg.active_root] if cfg.active_root else [])
    rows, _ = db.query_assets(conn, libraries, ids=[asset_id], max_visibility=2,
                              include_nsfw=True, limit=1, with_total=False)
    if not rows:
        return None
    row = db.get_asset(conn, asset_id)
    return row if row and row.get("kind") == "video" and not row.get("trashed") else None


def _carried(row: dict[str, Any]) -> dict[str, Any]:
    from .api import _CARRIED_FIELDS                         # noqa: PLC0415
    return {key: row.get(key) for key in _CARRIED_FIELDS}


def _copy_place(cfg, row: dict[str, Any]) -> tuple[str, Path]:
    """The library folder and the folder on disk a Compress copy goes into:
    beside the original, or under the new-files folder when the original's
    library folder cannot be written."""
    from ..storage import new_files                          # noqa: PLC0415
    root = new_files.destination(cfg, row["root"])
    source = Path(row["root"]) / row["rel_path"]
    return root, (source.parent if root == row["root"]
                  else Path(root) / (row.get("folder") or ""))


_COPY_CAPTION = "Compressed copy of "


def _copy_source(conn, copy: dict[str, Any], path: Path) -> str | None:
    """The file name of the video a compressed copy was made from, or None
    when nothing shows Ninaivu made it.

    The name alone does not tell: ``clip.mov`` and ``clip.mp4`` both have
    their copy called ``clip-compressed…``, and a person may save a file of
    their own under such a name. So, in order: the tag Compress writes into
    every copy; the audit line Compress wrote when it made this one; the
    caption it gave it (which the AI's caption can replace, so it comes last).
    """
    from ..media import video_compress as vc                 # noqa: PLC0415
    name = vc.copy_source(path)
    if name:
        return name
    rel = copy.get("rel_path") or ""
    if rel:
        found = conn.execute(
            "SELECT detail FROM audit WHERE action='compress_video' AND instr(detail, ?) > 0 "
            "ORDER BY rowid DESC LIMIT 1", (f" → {rel} (",)).fetchone()
        if found and found["detail"]:
            source_rel = found["detail"].split(" → ", 1)[0]
            if source_rel:
                return Path(source_rel).name
    caption = copy.get("caption") or ""
    if caption.startswith(_COPY_CAPTION) and len(caption) > len(_COPY_CAPTION):
        return caption[len(_COPY_CAPTION):]
    return None


def _earlier_copy(conn, cfg, row: dict[str, Any], before) -> dict[str, Any] | None:
    """The smaller copy Compress already made of this video, if it is still
    in the library: so Replace can put it in place instead of compressing
    the whole video a second time. The newest one wins, and only one made
    after the original last changed, from this very file (not another video
    with the same name and a different ending)."""
    from ..media import video_compress as vc                 # noqa: PLC0415
    try:
        root, folder = _copy_place(cfg, row)
    except OSError:
        return None
    stem = Path(row["filename"]).stem
    names = [f"{stem}-compressed.{vc.OUTPUT_EXT}"]
    names += [f"{stem}-compressed-{n}.{vc.OUTPUT_EXT}" for n in range(2, 21)]
    best = None
    for name in names:
        path = folder / name
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime < before.st_mtime or stat.st_size >= before.st_size:
            continue
        rel = path.relative_to(Path(root)).as_posix()
        found = conn.execute(
            "SELECT * FROM assets WHERE root=? AND rel_path=? AND kind='video' "
            "AND COALESCE(trashed, 0)=0", (root, rel)).fetchone()
        if found is None or _copy_source(conn, dict(found), path) != row["filename"]:
            continue
        if best is None or stat.st_mtime > best[1].st_mtime:
            best = (dict(found), stat, path)
    if best is None:
        return None
    found, _stat, path = best
    found["path"] = path
    return found


def _forget_copy(conn, cfg, copy: dict[str, Any], keeper_id: int) -> None:
    """The copy Replace used is now the video itself: its file goes, and so
    does its row, with anything it was in (albums, favourites) moved to the
    video it became."""
    with db._write_lock:
        for table in ("album_items", "user_assets"):
            conn.execute(f"UPDATE OR IGNORE {table} SET asset_id=? WHERE asset_id=?",
                         (keeper_id, copy["id"]))
        conn.execute("DELETE FROM assets WHERE id=?", (copy["id"],))
        conn.commit()
    try:
        copy["path"].unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("Could not remove the used compressed copy %s: %s", copy["path"], exc)
    if copy.get("thumb"):
        media.remove_thumbnails(cfg.thumbs_dir, copy["thumb"], cfg.thumb_sizes, cfg.thumb_format)


def _stage_copy(copy_path: Path, temporary: Path) -> None:
    """The earlier copy, as the temporary Replace swaps in: linked when it is
    on the same disk, copied when it is not. The copy itself stays until the
    swap has worked."""
    try:
        os.link(copy_path, temporary)
    except OSError:
        shutil.copyfile(copy_path, temporary)


def _compress_work(cfg, row: dict[str, Any], mode: str, user_id: int, cloud=None):
    """The job for one video: encode beside it, check, then copy or replace.

    Replace first looks for a copy Compress already made, and uses it when
    it passes the same checks a fresh one would.

    *cloud* is the app's cloud backup (CloudService), told when the job has
    changed the library. The job writes the index row itself, with the new
    file's size and date, so the next scan finds nothing changed and says
    nothing; and a scan saying so is the only thing that woke a backup which
    had caught up. A video made smaller, or its smaller copy, stayed off the
    backup until somebody pressed Start or restarted Ninaivu.
    """
    from ..media import video_compress as vc                 # noqa: PLC0415

    source = Path(row["root"]) / row["rel_path"]
    stem = Path(row["filename"]).stem

    def work(job, report, cancelled):
        conn = db.connect(cfg.db_path)
        try:
            before = source.stat()
        except OSError as exc:
            raise vc.CompressError(said("The video is no longer where it was.")) from exc
        root = row["root"]
        if mode == "replace":
            folder = source.parent
        else:
            try:
                root, folder = _copy_place(cfg, row)
            except OSError as exc:
                raise vc.CompressError(str(exc)) from exc
            folder.mkdir(parents=True, exist_ok=True)
        # What a job that never finished (the app closed mid-encode, a power
        # cut) left here: hidden, and often many gigabytes.
        for place in {folder, source.parent}:
            vc.sweep_temporaries(place)
        duration = float(row.get("duration") or 0)
        if duration <= 0:
            # Without a length every copy would pass the length check, the
            # copy of another video included: so it is read from the file.
            duration = float(media.probe_video(source).get("duration") or 0)
        temporary = vc.temporary_for(folder, stem, job["id"])
        vc.remove_quietly(temporary)
        # Replace keeps the original first. Linked, that takes no room;
        # where links cannot be made it is a full copy, and the disk must
        # have room for it as well as for the encode.
        safety = (before.st_size + vc.SPACE_MARGIN
                  if mode == "replace" and not vc.links_work(folder) else 0)
        try:
            earlier = _earlier_copy(conn, cfg, row, before) if mode == "replace" else None
            info = None
            if earlier is not None:
                try:
                    _stage_copy(earlier["path"], temporary)
                    info = vc.verify(duration, before.st_size, temporary)
                    report(1.0)
                except (OSError, vc.CompressError) as exc:
                    log.info("Not using the earlier copy %s: %s", earlier["path"], exc)
                    vc.remove_quietly(temporary)
                    earlier, info = None, None
            if info is None:
                if not vc.same_disk_space(folder, before.st_size // 2 + safety):
                    raise vc.CompressError(said("There is not enough free space on that disk to compress this video."))
                vc.encode(source, temporary, duration, report, cancelled)
                report(1.0)
                info = vc.verify(duration, before.st_size, temporary)
            elif safety and not vc.same_disk_space(folder, safety):
                raise vc.CompressError(
                    "There is not enough free space on that disk for a copy of the original, "
                    "so it was left as it is.")
            if cancelled():
                raise vc.CompressError(said("Stopped."))
            if mode == "copy":
                result = _publish_copy(conn, cfg, row, temporary, folder, root, info, user_id)
            else:
                result = _replace_original(conn, cfg, row, before, temporary, info, user_id,
                                           cancelled)
                if earlier is not None:
                    _forget_copy(conn, cfg, earlier, row["id"])
                    result["reused"] = earlier["filename"]
        finally:
            vc.remove_quietly(temporary)
        _tell_the_backup(cloud)
        return result

    return work


def _tell_the_backup(cloud) -> None:
    """The library changed under the backup's feet: let it look again.

    Never raises. The video is already compressed and in place; a backup
    that could not be woken is not a reason to report the job as failed.
    """
    if cloud is None:
        return
    try:
        cloud.library_changed()
    except Exception:                                        # noqa: BLE001
        log.debug("could not tell the backup about a compressed video", exc_info=True)


def _publish_copy(conn, cfg, row, temporary: Path, folder: Path, root: str,
                  info: dict[str, Any], user_id: int) -> dict[str, Any]:
    """Add the checked copy to the library beside the original."""
    from ..media import video_compress as vc                 # noqa: PLC0415

    target = vc.free_name(folder, Path(row["filename"]).stem, "-compressed")
    rel = target.relative_to(Path(root)).as_posix()
    base = media.thumb_base(root, rel)
    folder_rel = Path(rel).parent.as_posix()
    # The index row goes in before the file appears, carrying the original's
    # visibility: a watching scan must never meet a hidden video's copy first
    # and file it under the folder's default.
    record = {**_carried(row), "visibility": row.get("visibility"),
              "nsfw": row.get("nsfw"), "nsfw_score": row.get("nsfw_score"),
              "root": root, "rel_path": rel, "filename": target.name,
              "folder": "" if folder_rel == "." else folder_rel,
              "ext": vc.OUTPUT_EXT, "kind": "video", "width": info.get("width"),
              "height": info.get("height"), "duration": info.get("duration") or row.get("duration"),
              "thumb": base, "vis_source": "item", "rotation": 0, "rot_source": "manual",
              "caption": f"Compressed copy of {row['filename']}"}
    new_id = db.upsert_asset(conn, record)
    try:
        vc.publish(temporary, target)
    except OSError as exc:
        conn.execute("DELETE FROM assets WHERE id=?", (new_id,))
        conn.commit()
        raise vc.CompressError(f"Could not save the compressed copy: {exc}") from exc
    frame = info["frame"]
    try:
        media.write_thumbnails(frame, cfg.thumbs_dir, base, cfg.thumb_sizes,
                               cfg.thumb_format, cfg.thumb_quality)
        fields = {"blurhash": media.blurhash_encode(frame), "color": media.dominant_color(frame)}
    except Exception:                                         # noqa: BLE001
        fields = {}                                           # the next scan makes them
    stat = target.stat()
    db.update_asset(conn, new_id, size=stat.st_size, mtime=stat.st_mtime,
                    indexed_at=time.time(), **fields)
    auth.audit(conn, user_id, "compress_video",
               f"{row['rel_path']} → {rel} ({media.human_size(row['size'])} → "
               f"{media.human_size(stat.st_size)})")
    return {"mode": "copy", "id": new_id, "name": target.name, "folder": record["folder"],
            "old_size": row["size"], "new_size": stat.st_size}


#: Said when the original cannot be taken out of its place because another
#: program has it open (Windows refuses to replace or remove such a file).
_OPEN_ELSEWHERE = ("The video is open in another program (perhaps it is being played), "
                   "so it was left as it is. Close it and try again.")


def _replace_original(conn, cfg, row, before, temporary: Path,
                      info: dict[str, Any], user_id: int,
                      cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Swap the checked copy in for the original, which goes to the bin first."""
    from ..media import video_compress as vc                 # noqa: PLC0415
    from ..storage import recycle                            # noqa: PLC0415
    from .api import _reshoot                                 # noqa: PLC0415

    root, rel = row["root"], row["rel_path"]
    source = Path(root) / rel

    def unchanged() -> None:
        try:
            now = source.stat()
        except OSError as exc:
            raise vc.CompressError(said("The video is no longer where it was.")) from exc
        if (now.st_size, now.st_mtime) != (before.st_size, before.st_mtime):
            raise vc.CompressError(said("The video changed while it was being compressed, so it was left as it is."))

    unchanged()
    # The row as the index has it now, for the bin's entry: the item itself
    # lives on with the smaller file.
    as_was = conn.execute("SELECT * FROM assets WHERE id=?", (row["id"],)).fetchone()
    as_was = dict(as_was) if as_was is not None else dict(row)

    # The safety copy first, and nothing is replaced without it.
    try:
        made = recycle.keep_original(root, rel, link=True)
        kept = made or recycle.kept_copy_for(root, rel)
    except OSError as exc:
        raise vc.CompressError(
            f"A copy of the original could not be kept, so it was left as it is: {exc}") from exc
    if kept is None or kept.stat().st_size != before.st_size:
        if made is not None:
            recycle.discard_kept(made)
        raise vc.CompressError(said("A copy of the original could not be kept, so it was left as it is."))

    def give_up(message: str, cause: BaseException | None = None):
        if made is not None:
            recycle.discard_kept(made)
        raise vc.CompressError(message) from cause

    # Making the safety copy can take minutes on another disk. Meanwhile
    # the video may have been deleted, changed or moved, or Stop pressed:
    # then nothing is swapped, or the smaller file would come back as a new
    # item at a deleted video's place.
    try:
        if cancelled is not None and cancelled():
            raise vc.CompressError(said("Stopped."))
        unchanged()
        current = db.get_asset(conn, row["id"])
        if current is None or current.get("trashed") or current.get("rel_path") != rel \
                or current.get("root") != root:
            raise vc.CompressError(
                "The video was deleted or moved while it was being compressed, "
                "so nothing was replaced.")
    except vc.CompressError as exc:
        give_up(str(exc), exc)

    same_name = source.suffix.lower().lstrip(".") == vc.OUTPUT_EXT
    if same_name:
        try:
            os.replace(temporary, source)
        except PermissionError as exc:
            give_up(_OPEN_ELSEWHERE, exc)
        except OSError as exc:
            give_up(f"The smaller file could not be put in place, so the original was left "
                    f"as it is: {exc}", exc)
        target, new_rel = source, rel
        try:
            recycle.note_rewritten(kept, root, rel)
        except OSError:
            pass
    else:
        # wedding.avi becomes wedding.mp4 in the same folder. The original's
        # bytes are safe in the bin; its name is only removed once the new
        # file is in place and the index points at it.
        target = vc.free_name(source.parent, source.stem)
        new_rel = target.relative_to(Path(root)).as_posix()
        try:
            vc.publish(temporary, target)
        except OSError as exc:
            give_up(f"The smaller file could not be saved, so the original was left "
                    f"as it is: {exc}", exc)
        try:
            db.update_asset(conn, row["id"], rel_path=new_rel, filename=target.name,
                            ext=vc.OUTPUT_EXT, thumb=media.thumb_base(root, new_rel))
        except Exception:
            vc.remove_quietly(target)
            if made is not None:
                recycle.discard_kept(made)
            raise
        try:
            source.unlink()
        except OSError as exc:
            # Left here, the original would be indexed again at the next scan
            # as a second video beside the smaller one, with the bin holding
            # a third copy. So the swap is undone: the item points at the
            # original again, and the new file and the safety copy go.
            db.update_asset(conn, row["id"], rel_path=rel, filename=row["filename"],
                            ext=row.get("ext"), thumb=row.get("thumb"))
            vc.remove_quietly(target)
            give_up(_OPEN_ELSEWHERE if isinstance(exc, PermissionError) else
                    f"The original could not be removed, so it was left as it is: {exc}", exc)

    fresh = db.get_asset(conn, row["id"]) or row
    fields = _reshoot(dict(fresh), cfg)
    fields["duration"] = info.get("duration") or row.get("duration")
    fields["rotation"] = 0
    fields["rot_source"] = "manual"
    db.update_asset(conn, row["id"], **fields)
    if not same_name and row.get("thumb") and row["thumb"] != fresh.get("thumb"):
        media.remove_thumbnails(cfg.thumbs_dir, row["thumb"], cfg.thumb_sizes, cfg.thumb_format)
    # The original, in Recently deleted like anything else deleted: listed,
    # restorable, and erased by the bin's retention, so Replace does give
    # the room back in the end. Never a reason to fail what has worked.
    try:
        recycle.bin_kept_original(conn, kept, as_was, user_id)
    except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
        log.warning("Could not list the replaced original %s in the bin: %s", kept, exc)
    size = target.stat().st_size
    auth.audit(conn, user_id, "replace_video",
               f"{rel} → {new_rel} ({media.human_size(before.st_size)} → "
               f"{media.human_size(size)}); original kept in the bin")
    return {"mode": "replace", "id": row["id"], "name": target.name,
            "old_size": before.st_size, "new_size": size,
            "kept": str(kept.relative_to(Path(root))) if kept else ""}


#: "party-compressed.mp4", "party-compressed-2.mp4", and the copies of copies
#: older builds let Compress make ("party-compressed-compressed.mp4").
_COPY_NAME = re.compile(r"^(?P<stem>.+?)(?:-compressed(?:-\d+)?)+\.mp4$", re.IGNORECASE)


def _replaced_in(conn, libraries: list[str], root: str, replacements: list[str]) -> bool:
    """Whether Replace has put a smaller file in the original's place in
    *root*, given where the audit says it put one (*replacements*: paths in
    some library, as the audit does not name which).

    The file in that place says so itself when this build made it: it
    carries the tag every compressed file does. A file without it counts
    only when no other library has a file at that path, so a Replace in one
    library never marks a same-named video in another as replaced."""
    from ..media import video_compress as vc                 # noqa: PLC0415
    marks = ",".join("?" * len(libraries))
    for new_rel in replacements:
        holders = [r["root"] for r in conn.execute(
            f"SELECT root FROM assets WHERE root IN ({marks}) AND rel_path=? "
            f"AND COALESCE(trashed, 0)=0", [*libraries, new_rel])]
        if root not in holders:
            continue
        if vc.copy_source(Path(root) / new_rel) or set(holders) == {root}:
            return True
    return False


def _extra_copies(conn) -> tuple[list[dict[str, Any]], int]:
    """The compressed copies that are not needed: everything but the newest
    usable copy of each video, and every copy of a video Replace has already
    made smaller. Never the only copy of a video whose original is gone.

    Only copies Ninaivu made itself (see :func:`_copy_source`), grouped by
    the library they are in, the folder and the very file they were made
    from: a person's own ``holiday-compressed.mp4``, or a copy of
    ``clip.mov`` beside ``clip.mp4``, is never taken for an extra copy.
    Returns (to remove, how many copies are kept)."""
    cfg = _cfg()
    libraries = cfg.libraries or ([cfg.active_root] if cfg.active_root else [])
    if not libraries:
        return [], 0
    marks = ",".join("?" * len(libraries))
    copies = [dict(r) for r in conn.execute(
        f"SELECT id, root, rel_path, folder, filename, size, mtime, caption FROM assets "
        f"WHERE root IN ({marks}) AND kind='video' AND COALESCE(trashed, 0)=0 "
        f"AND lower(filename) LIKE '%-compressed%.mp4'", libraries)]
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for copy in copies:
        if not _COPY_NAME.match(copy["filename"]):
            continue
        source = _copy_source(conn, copy, Path(copy["root"]) / copy["rel_path"])
        if not source:
            continue                  # not one Ninaivu made: never tidied
        copy.pop("caption", None)
        key = (copy["root"], copy.get("folder") or "", source)
        groups.setdefault(key, []).append(copy)
    replaced = [r["detail"] or "" for r in conn.execute(
        "SELECT detail FROM audit WHERE action='replace_video'")]
    remove: list[dict[str, Any]] = []
    kept = 0
    for (root, folder, source), group in groups.items():
        source_rel = f"{folder}/{source}" if folder else source
        # The original itself, preferably in the copy's own library (a copy
        # can sit in the new-files folder when the original's could not be
        # written).
        originals = [dict(r) for r in conn.execute(
            f"SELECT id, root, rel_path, size, mtime, filename FROM assets "
            f"WHERE root IN ({marks}) AND kind='video' AND COALESCE(trashed, 0)=0 "
            f"AND COALESCE(folder, '')=? AND filename=?",
            [*libraries, folder, source])]
        originals.sort(key=lambda o: o["root"] != root)
        # Where Replace has put the smaller file in its place: under the
        # same name, or as an .mp4 when the original was another format.
        replacements = [detail[len(source_rel) + 3:].rsplit(" (", 1)[0]
                        for detail in replaced if detail.startswith(f"{source_rel} → ")]
        group.sort(key=lambda c: (c.get("mtime") or 0, c["id"]), reverse=True)
        if _replaced_in(conn, libraries, originals[0]["root"] if originals else root,
                        replacements):
            why = said("The original has already been replaced by a smaller file.")
            remove += [{**c, "why": why} for c in group]
            continue
        keep = group[0]
        if originals:
            original = originals[0]
            usable = [c for c in group if (c.get("mtime") or 0) >= (original.get("mtime") or 0)
                      and (c.get("size") or 0) < (original.get("size") or 0)]
            keep = usable[0] if usable else group[0]
        kept += 1
        why = said("Another compressed copy of the same video is kept.")
        remove += [{**c, "why": why} for c in group if c["id"] != keep["id"]]
    return remove, kept


@admin_bp.get("/api/admin/large-files/extra-copies")
@require_admin
def extra_copies():
    """What tidying the compressed copies would move to the bin."""
    remove, kept = _extra_copies(_conn())
    return jsonify({
        "items": [{"id": c["id"], "filename": c["filename"], "folder": c.get("folder") or "",
                   "size": c.get("size") or 0, "why": c["why"]} for c in remove],
        "size": sum(c.get("size") or 0 for c in remove),
        "kept": kept,
    })


@admin_bp.post("/api/admin/large-files/extra-copies")
@require_admin
def tidy_extra_copies():
    """Move the extra compressed copies to the bin, as Delete does: password
    first, recoverable from Recently deleted afterwards."""
    from ..storage import recycle                            # noqa: PLC0415
    from .accounts_api import reauthenticate_limited         # noqa: PLC0415

    data = json_object()
    conn = _conn()
    user = current_user()
    answer = reauthenticate_limited(conn, user.id, str(data.get("password", "")))
    if answer is None:
        return jsonify({"error": "Too many attempts. Wait a few minutes "
                                 "and try again."}), 429
    if not answer:
        return jsonify({
            "needs_password": True,
            "error": ("Enter your password to delete." if not data.get("password")
                      else "That password is not right."),
        }), 401
    remove, _kept = _extra_copies(conn)
    cfg = _cfg()
    libraries = cfg.libraries or ([cfg.active_root] if cfg.active_root else [])
    result = recycle.recycle(conn, [c["id"] for c in remove], user_id=user.id, roots=libraries)
    auth.audit(conn, user.id, "tidy_compressed_copies",
               f"{result['deleted']} extra compressed copies moved to the bin")
    return jsonify({"deleted": result["deleted"], "failed": result["failed"],
                    "size": sum(c.get("size") or 0 for c in remove)})


@admin_bp.post("/api/admin/large-files/compress")
@require_admin
def compress_large_file():
    """Start compressing one video: a copy beside it, or in its place.

    Replace takes the original out of its place, so it asks for the password
    as deleting does; the original is kept in the bin either way.
    """
    from ..media import video_compress as vc                 # noqa: PLC0415
    from ..storage import new_files                          # noqa: PLC0415

    data = json_object()
    mode = str(data.get("mode") or "copy")
    if mode not in vc.MODES:
        return jsonify({"error": said("Choose to compress a copy or replace the original.")}), 400
    reason = vc.unavailable_reason()
    if reason:
        return jsonify({"error": reason, "unavailable": True}), 409
    conn = _conn()
    row = _compress_row(conn, data.get("id"))
    if row is None:
        return jsonify({"error": said("Only a video in the library can be compressed.")}), 404
    if row.get("live_clip"):
        return jsonify({"error": said("This is the moving part of a live photo; it is left as it is.")}), 409
    if _COPY_NAME.match(row["filename"]):
        return jsonify({"error": said("This is already a compressed copy; compressing it again would only lose quality.")}), 409
    if _damaged_video(row):
        return jsonify({"error": vc.DAMAGED, "damaged": True}), 409
    if mode == "copy":
        try:
            before = (Path(row["root"]) / row["rel_path"]).stat()
        except OSError:
            before = None
        if before is not None and _earlier_copy(conn, _cfg(), row, before) is not None:
            return jsonify({"error": said("This video already has a smaller copy. Use Replace to put it in the original's place.")}), 409
    user = current_user()
    if mode == "replace":
        why = new_files.read_only_reason(row["root"])
        if why:
            return jsonify({"error": why}), 409
        from .accounts_api import reauthenticate_limited     # noqa: PLC0415
        answer = reauthenticate_limited(conn, user.id, str(data.get("password", "")))
        if answer is None:
            return jsonify({"error": "Too many attempts. Wait a few minutes "
                                     "and try again."}), 429
        if not answer:
            auth.audit(conn, user.id, "replace_video_refused",
                       f"{row['rel_path']} — password not given or wrong")
            return jsonify({
                "needs_password": True,
                "error": ("Enter your password to replace the original."
                          if not data.get("password") else "That password is not right."),
            }), 401
    try:
        # The cloud service is looked up here, in the request: the job runs on
        # a thread of its own, outside any request, where the app is not.
        services = current_app.config.get("MV_SERVICES")
        job = vc.start(row["id"], mode, row["filename"],
                       _compress_work(_cfg(), row, mode, user.id,
                                      cloud=getattr(services, "cloud", None)),
                       plan={"asset_id": row["id"], "mode": mode, "user_id": user.id,
                             **_file_stamp(row)})
    except vc.CompressError:
        # start() refuses only a second job for the same video (the mode was
        # checked above), so its fixed sentence is said here, not the exception.
        return jsonify({"error": said("This video is already being compressed.")}), 409
    return jsonify({"job": job}), 202


def _file_stamp(row: dict[str, Any]) -> dict[str, Any]:
    """The video's size and time as it was when its compression was asked
    for, so a restart carries on only with the same file."""
    try:
        stat = (Path(row["root"]) / row["rel_path"]).stat()
    except OSError:
        return {}
    return {"size": stat.st_size, "mtime": stat.st_mtime}


def resume_compressions(cfg, cloud=None) -> list[dict[str, Any]]:
    """At start-up, queue again the compressions a restart interrupted.

    Each video is asked the same questions the Compress button asks, except
    the password: a Replace was given it when it was asked for, and is carried
    on only within a day of that (video_compress.RESUME_WITHIN). A video that
    has since been deleted, moved, damaged or given a copy is left alone.
    """
    from ..media import video_compress as vc                 # noqa: PLC0415
    from ..storage import new_files                          # noqa: PLC0415

    path = Path(cfg.state_dir) / "compress-queue.json"
    conn = db.connect(cfg.db_path)
    libraries = set(cfg.libraries or ([cfg.active_root] if cfg.active_root else []))

    def make(plan: dict[str, Any]):
        if vc.unavailable_reason():
            return None
        row = db.get_asset(conn, int(plan["asset_id"]))
        mode = plan["mode"]
        if (not row or row.get("kind") != "video" or row.get("trashed")
                or row.get("root") not in libraries or row.get("live_clip")
                or _COPY_NAME.match(row["filename"]) or _damaged_video(row)):
            return None
        try:
            before = (Path(row["root"]) / row["rel_path"]).stat()
        except OSError:
            return None
        if "size" in plan and (int(plan["size"]) != before.st_size
                               or abs(float(plan.get("mtime") or 0) - before.st_mtime) > 1):
            # Another file now (edited, or a different video under the same
            # name): a Replace was agreed to for the one that was there.
            return None
        asker = auth.get_user(conn, int(plan.get("user_id") or 0))
        if mode == "replace" and (asker is None or not asker.active or not asker.is_admin):
            # Whoever gave their password for it is no longer an administrator.
            return None
        if mode == "copy" and _earlier_copy(conn, cfg, row, before) is not None:
            return None
        if mode == "replace" and new_files.read_only_reason(row["root"]):
            return None
        return row["filename"], _compress_work(cfg, row, mode, int(plan.get("user_id") or 0),
                                               cloud=cloud)

    try:
        return vc.resume(path, make)
    finally:
        conn.close()


@admin_bp.get("/api/admin/large-files/compress")
@require_admin
def compress_jobs():
    """Every compression this server still remembers, and whether it can."""
    from ..media import video_compress as vc                 # noqa: PLC0415
    return jsonify({"jobs": vc.active(), "unavailable": vc.unavailable_reason(),
                    "preset": vc.PRESET_LABEL})


@admin_bp.get("/api/admin/large-files/compress/<job_id>")
@require_admin
def compress_job(job_id: str):
    from ..media import video_compress as vc                 # noqa: PLC0415
    job = vc.status(job_id)
    if job is None:
        return jsonify({"error": "Not found."}), 404
    return jsonify({"job": job})


@admin_bp.post("/api/admin/large-files/compress/<job_id>/cancel")
@require_admin
def compress_cancel(job_id: str):
    from ..media import video_compress as vc                 # noqa: PLC0415
    job = vc.cancel(job_id)
    if job is None:
        return jsonify({"error": "Not found."}), 404
    return jsonify({"job": job})


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

    # The remote-access fields are checked here too, before any switch is
    # set. They used to be checked after the switches had already been
    # changed on the running server: a mistyped host name answered 400 while
    # open_browsing, say, was already on, unsaved, and the next save of any
    # setting wrote it to disk.
    remote_access = remote_networks = remote_hostname = None
    if "remote_access" in data:
        from ..server import remote                          # noqa: PLC0415
        remote_access = str(data["remote_access"] or "auto").strip().lower()
        if remote_access not in remote.PROVIDERS:
            return jsonify({"error": "remote_access must be one of "
                                     + ", ".join(remote.PROVIDERS)}), 400
    if "remote_networks" in data:
        import ipaddress                                     # noqa: PLC0415
        nets = data["remote_networks"]
        if isinstance(nets, str):
            nets = [n for n in (p.strip() for p in nets.split(",")) if n]
        if not isinstance(nets, list) or not all(isinstance(n, str) for n in nets):
            return jsonify({"error": "remote_networks is a list of address ranges"}), 400
        remote_networks = []
        for n in nets:
            try:
                remote_networks.append(str(ipaddress.ip_network(n.strip(), strict=False)))
            except ValueError:
                return jsonify({"error": "remote_networks: not an address range: "
                                         + n.strip()[:64]}), 400
    if "remote_hostname" in data:
        remote_hostname = str(data["remote_hostname"] or "").strip().lower()
        if remote_hostname and (len(remote_hostname) > 253 or any(
                c.isspace() or c in "/\\@:" for c in remote_hostname)):
            return jsonify({"error": "remote_hostname is a host name"}), 400

    passes_before = {key: bool(getattr(cfg, key, False)) for key in ("ai_enabled", *SCAN_PASSES)}
    for key in ("open_browsing", "nsfw_filter", "hide_screens", "watch", "ai_enabled",
                "ai_gpu", "straighten_auto", "straighten_auto_apply",
                "straighten_requires_face",
                "outside_ai_for_family",
                *SCAN_PASSES):
        if key in data:
            setattr(cfg, key, bool(data[key]))
            changed.append(key)
    if any(getattr(cfg, key, False) and not was for key, was in passes_before.items()):
        # A pass switched on did nothing until the next scan, which on a
        # library nobody adds to could be days. Its work starts now, with
        # nothing read again: only the analysis is carried on (Scanner._run).
        scanner = current_app.config.get("MV_SCANNER")
        if scanner is not None and not scanner.running:
            scanner.start(scopes={Path(root): frozenset() for root in cfg.libraries})
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
    if remote_access is not None:
        cfg.remote_access = remote_access
        changed.append("remote_access")
    if remote_networks is not None:
        cfg.remote_networks = remote_networks
        changed.append("remote_networks")
    if remote_hostname is not None:
        cfg.remote_hostname = remote_hostname
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
#: Set to end the running check between two files (stop_scrubber).
_SCRUBBER_STOP = threading.Event()
#: The thread the running check is on, so a stop can wait for it.
_SCRUBBER_THREAD: threading.Thread | None = None

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
    never sent back: only whether one is saved. Nor is the webhook address,
    which is a password too (whoever has a Slack, Discord or ntfy address can
    post to it, or read from it): only its scheme and host, as a hint, which
    sent back unchanged keeps the saved one.
    """
    from ..server import settings_groups  # noqa: PLC0415

    cfg = _cfg()
    webhook = getattr(cfg, "notify_webhook", "") or ""
    return {**notifier().snapshot(), "form": {
        "webhook": settings_groups.masked_url(webhook),
        "webhook_saved": bool(webhook),
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
    saved_webhook = getattr(cfg, "notify_webhook", "") or ""
    if ("webhook" in data and saved_webhook
            and str(data["webhook"]).strip() == settings_groups.masked_url(saved_webhook)):
        # The hint the form was filled with, sent back unchanged: keep the
        # saved address. A field emptied on purpose still removes it, as it
        # always has, and so does webhook_clear.
        data = {k: v for k, v in data.items() if k != "webhook"}
    if data.get("webhook_clear") is True:
        data = {**data, "webhook": ""}
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
#: The counts a check carries across a restart or a Stop, so the alert at the
#: end is about the whole pass rather than only the part after the restart.
SCRUBBER_FOUND = ("verified", "corrupt", "missing", "baseline", "changed",
                  "unreadable", "unavailable")

#: What happens after a check reaches the end, per library index: the repair
#: hand-off (Repairer.after_check), registered by the app when it builds its
#: Repairer (:func:`after_every_check`). Keyed by the index's path so two apps
#: in one process (the tests make many) never hand each other's checks over.
_AFTER_CHECK: dict[str, Callable[[], None]] = {}


def after_every_check(db_path: Path | str, on_done: Callable[[], None] | None) -> None:
    """Make *on_done* what every check of *db_path* does when it finishes.

    The repair hand-off used to be passed in by the one caller that knew
    about it, the nightly schedule. A check carried on after a restart was
    started by start-up, which did not, so a check interrupted by a reboot
    ran to the end and nothing repaired what it found until the next
    scheduled check, a week later. Registered once here, every way a check
    can start (the schedule, start-up, the Start button) ends the same way.
    """
    key = str(db_path)
    if on_done is None:
        _AFTER_CHECK.pop(key, None)
    else:
        _AFTER_CHECK[key] = on_done


def start_scrubber_job(db_path: Path | str, after_id: int = 0,
                       scanner=None, notify=None,
                       on_done: Callable[[], None] | None = None) -> bool:
    """Start the storage check in the background. False if one is running.

    *after_id* carries a check on from the last file it had reached, which is
    how start-up picks up one a restart interrupted. *on_done* is called when
    a check reaches the end (ninaivu/storage/repair.py repairs what it found);
    left out, it is whatever :func:`after_every_check` registered for this
    index, so a check carried on after a restart hands over to repair too.

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
    global _SCRUBBER_THREAD
    with _SCRUBBER_LOCK:
        if _SCRUBBER_RUNNING:
            return False
        _SCRUBBER_RUNNING = True
        _SCRUBBER_STOP.clear()
    if on_done is None:
        on_done = _AFTER_CHECK.get(str(db_path))

    def run() -> None:
        if scanner is None:
            finished = _run_scrubber(db_path, int(after_id or 0), notify=notify)
        else:
            from ..media.scanner import CLAIM_STORAGE_CHECK    # noqa: PLC0415
            # Whether the check holds the indexer now. It lets go while it
            # only waits for its turn (overnight mode, somebody watching a
            # video): it used to hold it through the wait, so a check carried
            # on after an afternoon restart stood the indexer down until
            # night, and photographs copied in meanwhile were not in the
            # library all day. Taken back the moment the check may read.
            # Each let-go is matched by one take-back, and the last word is
            # always a let-go.
            holding = [True]

            def aside(waiting: bool) -> None:
                if waiting and holding[0]:
                    holding[0] = False
                    scanner.resume(CLAIM_STORAGE_CHECK)
                elif not waiting and not holding[0]:
                    holding[0] = True
                    scanner.defer(CLAIM_STORAGE_CHECK)

            scanner.defer(CLAIM_STORAGE_CHECK)
            try:
                finished = _run_scrubber(
                    db_path, int(after_id or 0),
                    workload=getattr(scanner, "workload", None), notify=notify,
                    between=_letting_the_indexer_through(scanner, CLAIM_STORAGE_CHECK),
                    aside=aside)
            finally:
                if holding[0]:
                    scanner.resume(CLAIM_STORAGE_CHECK)
        if finished and on_done is not None:
            try:
                on_done()
            except Exception:                                # noqa: BLE001
                import logging
                logging.getLogger(__name__).exception("after the storage check")

    thread = threading.Thread(target=run, name="ninaivu-scrubber", daemon=True)
    _SCRUBBER_THREAD = thread
    thread.start()
    return True


#: How long the storage check waits for a scan it let through to begin.
STEP_ASIDE_START_SECONDS = 5.0


def _letting_the_indexer_through(scanner, claim: str) -> Callable[[], None]:
    """What the storage check does between files while it holds the indexer.

    A check reads the whole library and takes hours on a Pi, and it used to
    hold the indexer for all of them: photographs copied in at breakfast were
    not in the library until the check ended in the evening. When a scan has
    been asked for since the check last looked, the check lets go, waits
    while that scan reads and indexes the new files, and takes the indexer
    back as the scan moves on to analysis, which carries on after the check.
    """
    from ..media.scanner import HAND_OVER_AFTER          # noqa: PLC0415
    seen: list[int | None] = [None]

    def between() -> None:
        asked = getattr(scanner, "queued_requests", None)
        if asked is None or not getattr(scanner, "waiting", False) or asked == seen[0]:
            return
        seen[0] = asked
        if not scanner.resume(claim):
            # Somebody else holds the indexer too: nothing was started.
            scanner.defer(claim)
            return
        _SCRUBBER_PROGRESS["held"] = "letting new files be indexed"
        try:
            began = time.monotonic()
            while not _SCRUBBER_STOP.is_set():
                status = scanner.progress.snapshot().get("status")
                if scanner.running and status not in ("paused", *HAND_OVER_AFTER):
                    break
                if (not scanner.running
                        and time.monotonic() - began > STEP_ASIDE_START_SECONDS):
                    break
                time.sleep(0.2)
        finally:
            scanner.defer(claim)
            seen[0] = scanner.queued_requests
            _SCRUBBER_PROGRESS["held"] = ""

    return between


def stop_scrubber(timeout: float | None = None) -> bool:
    """Ask the running storage check to end after the file it is reading.

    True if one was running. With *timeout*, waits up to that long for it to
    let go. A stopped check is not picked up again after a restart; Start
    carries on from the file it had reached.
    """
    with _SCRUBBER_LOCK:
        running = _SCRUBBER_RUNNING
        if running:
            _SCRUBBER_STOP.set()
        thread = _SCRUBBER_THREAD
    if running and timeout is not None and thread is not None \
            and thread is not threading.current_thread():
        thread.join(timeout)
    return running


def _run_scrubber(db_path: Path | str, after_id: int = 0, workload=None, notify=None,
                  between: Callable[[], None] | None = None,
                  aside: Callable[[bool], None] | None = None) -> bool:
    """Read every file and compare it with its fingerprint. True when it
    reached the end, rather than stopping early.

    *workload* (ninaivu/server/workload.py) is asked before each file, so a
    check that reads the whole library makes way for somebody watching a
    video from it. *between* is called at each checkpoint (see
    _letting_the_indexer_through). *aside(True)* is called when the check
    starts waiting for its turn and *aside(False)* when it may go on (see
    start_scrubber_job). :func:`stop_scrubber` ends it between two files, and
    while it waits for its turn.
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
        found = _found_so_far(conn, after_id)
        _SCRUBBER_PROGRESS.update(
            total=total, processed=int(already), running=True, held="",
            stopped_at=0, **found)
        # A library whose drive is away is not a library whose files are gone.
        # Under a ``nofail`` mount the folder is still there, empty, and every
        # file in it was marked missing, which automatic repair then "fixed" by
        # copying the library onto the system disk. Its files are counted as
        # unavailable and their last result is left as it was.
        from ..storage import roots as roots_kit          # noqa: PLC0415
        present: dict[str, bool] = {}
        resume.want(conn, SCRUBBER_RESUME, {"after_id": after_id, "found": found})

        def say(reason):
            _SCRUBBER_PROGRESS["held"] = reason or ""
            if aside is not None:
                aside(bool(reason))

        # Written about once a second rather than once per file: on a library
        # of small photographs the commit, not the hashing, was most of each
        # file's cost. Gathered here and written in one go (db.record_bitrot_checks),
        # so no transaction stays open while the next file is read.
        pending: list[tuple] = []
        last_write = time.monotonic()
        reached = after_id
        for index, r in enumerate(rows, start=1):
            stopped = _SCRUBBER_STOP.is_set()
            if not stopped and workload is not None:
                # With the stop, so Stop is heard while the check waits: in
                # overnight mode a check started (or carried on after a
                # restart) in the afternoon waited here until night, and
                # Stop did nothing for all those hours. A wait the stop
                # ended (False) ends the check here, before the file.
                stopped = workload.wait_turn(
                    "check", stop=_SCRUBBER_STOP, on_hold=say) is False
            if stopped:
                # Asked to stop: what was read is kept, and a restart does
                # not bring the check back. Start carries on from here.
                db.record_bitrot_checks(conn, pending)
                resume.done(conn, SCRUBBER_RESUME)
                _SCRUBBER_PROGRESS["stopped_at"] = reached
                return False
            asset_id = int(r["id"])
            root = r["root"]
            rel_path = r["rel_path"]
            full_path = Path(root) / rel_path
            if root not in present:
                present[root] = roots_kit.root_present(root)
            elif present[root] and not full_path.is_file():
                # Asked again when a file is missing: the drive may have
                # dropped off part-way through the check.
                present[root] = roots_kit.root_present(root)
            if not present[root]:
                _SCRUBBER_PROGRESS["unavailable"] += 1
                _SCRUBBER_PROGRESS["processed"] += 1
                continue
            previous = db.last_bitrot_fingerprint(conn, asset_id)

            if not full_path.is_file():
                pending.append((asset_id, root, rel_path,
                                previous.get("actual_hash") if previous else None,
                                None, "missing", None, None))
                _SCRUBBER_PROGRESS["missing"] += 1
            else:
                try:
                    stat_result = full_path.stat()
                    actual_hash = _hash_file(full_path)
                except OSError:
                    pending.append((asset_id, root, rel_path,
                                    previous.get("actual_hash") if previous else None,
                                    None, "unreadable", None, None))
                    _SCRUBBER_PROGRESS["unreadable"] += 1
                else:
                    status, expected = _classify(previous, actual_hash, stat_result)
                    pending.append((asset_id, root, rel_path, expected, actual_hash,
                                    status, stat_result.st_mtime, stat_result.st_size))
                    _SCRUBBER_PROGRESS[status] = _SCRUBBER_PROGRESS.get(status, 0) + 1
            _SCRUBBER_PROGRESS["processed"] += 1
            reached = asset_id
            checkpoint = index % SCRUBBER_CHECKPOINT == 0
            if checkpoint or time.monotonic() - last_write > 1.0:
                db.record_bitrot_checks(conn, pending)
                pending = []
                last_write = time.monotonic()
            if checkpoint:
                resume.want(conn, SCRUBBER_RESUME, {
                    "after_id": asset_id,
                    "found": {k: int(_SCRUBBER_PROGRESS.get(k) or 0)
                              for k in SCRUBBER_FOUND}})
                if between is not None:
                    between()
        db.record_bitrot_checks(conn, pending)
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


def _found_so_far(conn, after_id: int) -> dict[str, int]:
    """What the pass being carried on from *after_id* had already found.

    The counts used to start again from nought whenever a check carried on,
    after a restart or a Stop and Start. The alert at the end is built from
    them, so a check that found three damaged files before a reboot and none
    after it ended by saying nothing at all: "0 files changed on disk" is not
    sent. They are written into the check's resume record at every
    checkpoint, beside the file it had reached, and read back here when that
    is where the check carries on from. A Stop crosses the record off, so a
    check stopped from the Storage page carries on with the counts it was
    left showing instead, when it carries on from where it stopped.
    """
    from ..storage import resume                          # noqa: PLC0415
    found = {k: 0 for k in SCRUBBER_FOUND}
    if not after_id:
        return found
    saved: Any = None
    try:
        record = resume.wanted(conn).get(SCRUBBER_RESUME) or {}
    except sqlite3.Error:
        record = {}
    if int(record.get("after_id") or 0) == int(after_id):
        saved = record.get("found")
    elif int(_SCRUBBER_PROGRESS.get("stopped_at") or 0) == int(after_id):
        saved = {k: _SCRUBBER_PROGRESS.get(k) for k in SCRUBBER_FOUND}
    if isinstance(saved, dict):
        for k in SCRUBBER_FOUND:
            try:
                found[k] = max(0, int(saved.get(k) or 0))
            except (TypeError, ValueError):
                pass
    return found


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
    # A check stopped from here carries on from the file it had reached.
    after = int(_SCRUBBER_PROGRESS.get("stopped_at") or 0)
    if not start_scrubber_job(_cfg().db_path, after, scanner=_scanner()):
        return jsonify({"ok": False, "message": "Scrubber is already running", "progress": _SCRUBBER_PROGRESS})
    return jsonify({"ok": True, "message": "Scrubber started", "progress": _SCRUBBER_PROGRESS})


@admin_bp.post("/api/admin/scrubber/stop")
@require_admin
def stop_scrubber_route():
    if not stop_scrubber():
        return jsonify({"ok": False, "message": "No storage check is running",
                        "progress": _SCRUBBER_PROGRESS})
    return jsonify({"ok": True, "message": "The storage check stops after the file it is reading",
                    "progress": _SCRUBBER_PROGRESS})


#: How long the totals are reused while a check runs. The page asks every two
#: seconds, and working them out reads the whole record of every pass; what
#: moves second by second is the progress, which is not part of this.
SCRUBBER_FRESH_FOR = 10.0
_scrubber_seen: dict[str, Any] = {"at": 0.0, "key": None, "summary": None, "issues": None}
_scrubber_seen_lock = threading.Lock()


@admin_bp.get("/api/admin/scrubber/status")
@require_admin
def scrubber_status():
    conn = _conn()
    # A problem found is shown at once; the same totals otherwise wait their
    # turn. Idle, nothing is written, so there is nothing to wait for.
    key = (str(_cfg().db_path), *(int(_SCRUBBER_PROGRESS.get(state, 0) or 0)
                                  for state in ("corrupt", "missing", "unreadable")))
    now = time.monotonic()
    with _scrubber_seen_lock:
        seen = dict(_scrubber_seen)
    if (_SCRUBBER_PROGRESS.get("running") and seen["summary"] is not None
            and seen["key"] == key and now - seen["at"] < SCRUBBER_FRESH_FOR):
        summary, issues = dict(seen["summary"]), list(seen["issues"])
    else:
        summary = db.get_bitrot_summary(conn)
        # The latest check of each file: one since repaired is not listed still.
        issues = db.current_bitrot_issues(conn, limit=60)
        with _scrubber_seen_lock:
            _scrubber_seen.update(at=now, key=key, summary=dict(summary), issues=list(issues))
    summary["progress"] = _SCRUBBER_PROGRESS
    summary["recent_issues"] = issues
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

    data = json_body()
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


@admin_bp.get("/api/admin/phone-keys")
@require_admin
def phone_keys_overview():
    """Every phone key in use, with whose it is (media/phone_keys.py)."""
    from ..media import phone_keys                           # noqa: PLC0415

    conn = _conn()
    phone_keys.init_schema(conn)
    return jsonify(keys=phone_keys.listed(conn),
                   max_file_gb=int(getattr(_cfg(), "phone_upload_max_gb", 8) or 8))


@admin_bp.delete("/api/admin/phone-keys/<int:key_id>")
@require_admin
def phone_keys_revoke_any(key_id):
    """Revoke anybody's phone key: a lost phone, or a key nobody remembers."""
    from ..media import phone_keys                           # noqa: PLC0415

    conn = _conn()
    phone_keys.init_schema(conn)
    if not phone_keys.revoke(conn, key_id):
        return jsonify(error="No such phone key."), 404
    auth.audit(conn, current_user().id, "phone_key_revoked", str(key_id))
    return phone_keys_overview()


@admin_bp.post("/api/admin/phone-backups/settings")
@require_admin
def phone_backups_settings():
    data = json_body()
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
    # Both checked before either is set, so a refused request changes nothing
    # on the running server either. 1e400 arrives as infinity, which int()
    # refuses with OverflowError.
    if "every_days" in data:
        try:
            days = int(data["every_days"])
        except (TypeError, ValueError, OverflowError):
            return jsonify({"error": "every_days must be a number", "status": 400}), 400
        if not 0 <= days <= 365:
            return jsonify({"error": "Between 0 (never) and 365 days.", "status": 400}), 400
    if "automatic" in data and not isinstance(data["automatic"], bool):
        return jsonify({"error": "automatic must be true or false", "status": 400}), 400
    if "every_days" in data:
        cfg.scrub_every_days = days
    if "automatic" in data:
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
    # Everything is checked before anything is set. A bad mode sent with
    # "clear" used to answer 400 with the home zone already gone from the
    # running server, so downloads stopped leaving the home location out.
    if "home_lat" in data or "home_lon" in data:
        try:
            lat, lon = float(data.get("home_lat")), float(data.get("home_lon"))
        except (TypeError, ValueError):
            return jsonify({"error": "Give the latitude and longitude as numbers.", "status": 400}), 400
        # NaN compares false with everything, so it passed the range below.
        if not (-90 <= lat <= 90 and -180 <= lon <= 180) or lat != lat or lon != lon:
            return jsonify({"error": "That is not a place on Earth.", "status": 400}), 400
    if "home_radius_m" in data:
        try:
            radius = int(data["home_radius_m"])
        except (TypeError, ValueError, OverflowError):
            return jsonify({"error": "The radius must be a number of metres.", "status": 400}), 400
    if "strip_location" in data and data["strip_location"] not in location.MODES:
        return jsonify({"error": "off, home or all", "status": 400}), 400

    if "clear" in data and data["clear"] is True:
        cfg.home_lat = cfg.home_lon = None
    if "home_lat" in data or "home_lon" in data:
        cfg.home_lat, cfg.home_lon = round(lat, 6), round(lon, 6)
    if "home_radius_m" in data:
        cfg.home_radius_m = max(50, min(radius, 20000))
    if "strip_location" in data:
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


# ---------------------------------------------------------------------------
# The family archive on a USB drive (ninaivu/storage/keepsake.py)
# ---------------------------------------------------------------------------

def _keepsake():
    return current_app.config["MV_SERVICES"].keepsake


@admin_bp.get("/api/admin/keepsake")
@require_admin
def keepsake_status():
    return jsonify(_keepsake().status())


@admin_bp.post("/api/admin/keepsake")
@require_admin
def keepsake_start():
    """Make (or bring up to date) the archive in ``folder``; ``stop`` stops it;
    ``letter`` alone saves the letter. ``everything`` puts hidden photographs
    in too; ``small`` copies the photographs at 2048 pixels for a small drive."""
    from ..storage.keepsake import LETTER_MAX               # noqa: PLC0415
    data = json_object()
    keeper = _keepsake()
    letter = data.get("letter")
    if letter is not None and (not isinstance(letter, str) or len(letter) > LETTER_MAX):
        return jsonify({"error": "The letter is text, up to 20,000 characters.", "status": 400}), 400
    for flag in ("everything", "small", "stop"):
        if flag in data and not isinstance(data[flag], bool):
            return jsonify({"error": f"{flag} must be true or false", "status": 400}), 400
    if data.get("stop"):
        keeper.stop()
        return jsonify(keeper.status())
    folder = data.get("folder")
    if folder is None:
        if letter is not None:
            keeper.save_letter(letter)
        return jsonify(keeper.status())
    if not isinstance(folder, str):
        return jsonify({"error": "folder is a path", "status": 400}), 400
    try:
        status = keeper.start(folder, everything=data.get("everything") is True,
                              small=data.get("small") is True, letter=letter)
    except ValueError as exc:
        return jsonify({"error": str(exc), "status": 409}), 409
    auth.audit(_conn(), current_user().id, "keepsake",
               f"into {folder}" + (" with hidden photographs" if data.get("everything") is True else ""))
    return jsonify(status)


# The TV album's console routes, on this same blueprint (console only).
from . import tv_album_api  # noqa: E402,F401
