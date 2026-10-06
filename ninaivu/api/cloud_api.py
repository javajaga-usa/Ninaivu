"""The Cloud tab's endpoints — console only, never the family port.

Every route here is registered on the admin app alone, so on port 5000 they do
not exist. That is not decoration: these routes hand out a Google consent URL,
accept an OAuth code, and can start a process that copies the household's
photographs off the premises. Nobody browsing the gallery should be able to
reach them, and on the family app they are a 404 rather than a 403 — there is
nothing there to find.

Three things this layer is careful about:

**No token ever leaves.** Status is built from :meth:`Credentials.public`,
which is a whitelist rather than a redaction, so a token cannot escape by
somebody adding a field later.

**The callback is checked.** The ``state`` value handed to Google has to come
back with the code, or the exchange is refused — otherwise any page in the
administrator's browser could complete a sign-in to an account of its choosing.

**Nothing here deletes anything.** Not a local file, not a remote one. The most
destructive thing available is disconnecting the account, and even that keeps
the record of what has already been uploaded, because losing it would mean
sending the whole library again.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from flask import Blueprint, current_app, jsonify, redirect, request

from ..server import auth
from ..storage import db, resume
from ..cloud import service as cloud_service
from ..server.auth import current_user, require_admin
from ..cloud import approvals, drive, keyring, limits
from ..cloud.rules import KINDS, Rules, clean_folders, clean_words
from ._body import json_body, json_object

log = logging.getLogger(__name__)

cloud_bp = Blueprint("cloud", __name__)


def _cfg():
    return current_app.config["MV_CONFIG"]


def _service():
    services = current_app.config["MV_SERVICES"]
    return services.cloud


def _loopback_redirect_uri() -> str:
    """The callback on this computer's own name, which Google does accept."""
    cfg = _cfg()
    scheme = current_app.config.get("MV_SCHEME") or "http"
    port = int(cfg.admin_port or 0)
    default = 443 if scheme == "https" else 80
    suffix = "" if port in (0, default) else f":{port}"
    return f"{scheme}://localhost{suffix}/api/cloud/callback"


def _redirect_uri() -> str:
    """Where Google sends the browser back to.

    Composed from the address this request actually arrived on, because a
    console reached as ``ninaivu.local:3000`` and one reached as
    ``192.168.1.20:3000`` need different values, and only the request knows
    which one the administrator is using. Whichever it is has to be listed in
    the Google Cloud project as an authorised redirect URI — the console says
    so, and shows the exact string to paste.
    """
    return request.url_root.rstrip("/") + "/api/cloud/callback"


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

@cloud_bp.get("/api/cloud/status")
@require_admin
def status():
    payload = _service().status()
    payload["redirect_uri"] = _redirect_uri()
    # Why Google would turn this address down, and the one it does take. A
    # console opened as ninaivu-admin.local or by its network address cannot
    # have its callback registered at all, and Google says only
    # "redirect_uri_mismatch" about it.
    problem = drive.redirect_uri_problem(payload["redirect_uri"])
    payload["redirect_uri_problem"] = problem
    payload["redirect_uri_ok"] = problem is None
    payload["redirect_uri_loopback"] = _loopback_redirect_uri()
    return jsonify(payload)


#: Beyond this, Google's limit on how quickly one account may add files, not
#: the line, sets the pace.
MAX_PARALLEL = 6


@cloud_bp.post("/api/cloud/settings")
@require_admin
def settings():
    data = json_body()
    if not isinstance(data, dict):
        return jsonify({"error": "Cloud settings must be a JSON object", "status": 400}), 400
    cfg = _cfg()
    service = _service()
    changed: list[str] = []

    # Validate the entire request before changing live settings or pausing an
    # upload. A rejected time must not silently enable backup of hidden files.
    for name in ("enabled", "autostart", "encrypt"):
        if name in data and not isinstance(data[name], bool):
            return jsonify({"error": f"{name} must be true or false", "status": 400}), 400
    if "rate_kbps" in data:
        try:
            rate = max(0, int(data["rate_kbps"] or 0))
        except (TypeError, ValueError, OverflowError):
            return jsonify({"error": "The speed limit has to be a number of kilobytes per "
                            "second, or 0 for no limit.", "status": 400}), 400
    if "window_start" in data or "window_end" in data:
        start = _clock(data.get("window_start", cfg.cloud_window_start))
        end = _clock(data.get("window_end", cfg.cloud_window_end))
        if start is None or end is None:
            return jsonify({"error": "Use a 24-hour time like 22:00, or leave both empty "
                            "to upload at any hour.", "status": 400}), 400

    if "hidden" in data and data["hidden"] not in ("never", "encrypted", "always"):
        return jsonify({"error": "hidden must be never, encrypted or always", "status": 400}), 400
    if "full_speed" in data and not isinstance(data["full_speed"], bool):
        return jsonify({"error": "full_speed must be true or false", "status": 400}), 400
    if "parallel" in data:
        try:
            parallel = int(data["parallel"])
        except (TypeError, ValueError, OverflowError):
            parallel = 0
        if not 1 <= parallel <= MAX_PARALLEL:
            return jsonify({"error": f"Files at once has to be from 1 to {MAX_PARALLEL}.",
                            "status": 400}), 400

    if data.get("encrypt") and keyring.load(cfg.state_dir) is None:
        return jsonify({"error": "Create an encryption key before turning encryption on.",
                        "status": 400}), 400

    if "encrypt" in data:
        cfg.cloud_encrypt = bool(data["encrypt"])
        changed.append(f"encryption {'on' if cfg.cloud_encrypt else 'off'}")
    if "enabled" in data:
        cfg.cloud_enabled = bool(data["enabled"])
        changed.append(f"cloud {'on' if cfg.cloud_enabled else 'off'}")
        if not cfg.cloud_enabled:
            service.pause()
    if "autostart" in data:
        cfg.cloud_autostart = bool(data["autostart"])
        changed.append(f"autostart {'on' if cfg.cloud_autostart else 'off'}")

    if "hidden" in data:
        cfg.cloud_hidden = data["hidden"]
        changed.append(f"hidden things backed up: {cfg.cloud_hidden}")

    if "parallel" in data:
        cfg.cloud_parallel = parallel
        changed.append(f"{parallel} files at once")
    if "full_speed" in data:
        cfg.cloud_full_speed = bool(data["full_speed"])
        changed.append(f"full speed {'on' if cfg.cloud_full_speed else 'off'}")

    if "rate_kbps" in data:
        cfg.cloud_rate_kbps = rate
        changed.append(f"speed limit {rate} KB/s" if rate
                       else "speed limit removed")

    if "window_start" in data or "window_end" in data:
        cfg.cloud_window_start, cfg.cloud_window_end = start, end
        label = service.window().label()
        changed.append(f"upload window {label}" if label
                       else "upload window: any time")

    if "folder_name" in data and service.set_folder(str(data["folder_name"])):
        changed.append(f"folder “{service.creds.folder_name}”")

    # A cap or a window changed while an upload is running applies to that
    # upload, not the next one — otherwise the fix for "it is eating the
    # broadband" would be to stop and restart the thing eating the broadband.
    service.apply_settings()
    cfg.save()
    if changed:
        auth.audit(db.connect(cfg.db_path), current_user().id, "cloud_settings",
                   ", ".join(changed))
    return jsonify({"ok": True, "changed": changed, **_service().status()})


@cloud_bp.post("/api/cloud/encryption/key")
@require_admin
def create_encryption_key():
    """Make the backup encryption key, once, and hand back its recovery file.

    The passphrase is used to derive the key and then forgotten; it is never
    stored. Turning encryption on is a separate setting, so the recovery file
    can be saved before anything is uploaded with the key.
    """
    cfg = _cfg()
    data = json_body()
    if not isinstance(data, dict) or set(data) - {"passphrase", "confirm"}:
        return jsonify({"error": "Send passphrase and confirm.", "status": 400}), 400
    try:
        record = keyring.create(cfg.state_dir, data.get("passphrase"), data.get("confirm"))
    except ValueError as error:
        status = 409 if "already exists" in str(error) else 400
        return jsonify({"error": str(error), "status": status}), status
    auth.audit(db.connect(cfg.db_path), current_user().id, "cloud_encryption_key",
               f"created key {record['key_id']}")
    return jsonify({"ok": True, "recovery": keyring.recovery_document(record),
                    **_service().status()})


@cloud_bp.post("/api/cloud/encryption/import")
@require_admin
def import_encryption_key():
    """Bring a key back from its recovery file — on a rebuilt machine, the key
    the off-site copy and the Drive backups were made with.

    ``{"recovery": <the recovery file's JSON>, "replace": false}``. The same
    key already here is no change. A different key already here is replaced
    only with ``replace`` true (409 with ``needs_replace`` otherwise), and is
    kept beside it in the state folder, never deleted.
    """
    cfg = _cfg()
    data = json_body()
    if not isinstance(data, dict) or set(data) - {"recovery", "replace"} \
            or not isinstance(data.get("recovery"), dict):
        return jsonify({"error": "Send the recovery file's contents as recovery.",
                        "status": 400}), 400
    replace = data.get("replace", False)
    if not isinstance(replace, bool):
        return jsonify({"error": "replace must be true or false", "status": 400}), 400
    before = keyring.load(cfg.state_dir)
    try:
        record = keyring.import_recovery(cfg.state_dir, data["recovery"], replace=replace)
    except ValueError as error:
        if before is not None and "already has a different" in str(error):
            return jsonify({"error": str(error), "needs_replace": True, "status": 409}), 409
        return jsonify({"error": str(error), "status": 400}), 400
    changed = before is None or before["key_id"] != record["key_id"]
    if changed:
        auth.audit(db.connect(cfg.db_path), current_user().id, "cloud_encryption_key",
                   f"imported key {record['key_id']}"
                   + (f", replacing {before['key_id']}" if before is not None else ""))
    return jsonify({"ok": True, "key_id": record["key_id"], "changed": changed,
                    **_service().status()})


@cloud_bp.get("/api/cloud/encryption/recovery")
@require_admin
def encryption_recovery():
    """The recovery file again. The key is on this machine, so this reveals nothing new
    to an administrator — but every download is recorded."""
    cfg = _cfg()
    record = keyring.load(cfg.state_dir)
    if record is None:
        return jsonify({"error": "There is no encryption key.", "status": 404}), 404
    auth.audit(db.connect(cfg.db_path), current_user().id, "cloud_encryption_key",
               f"recovery file downloaded for {record['key_id']}")
    response = jsonify(keyring.recovery_document(record))
    response.headers["Cache-Control"] = "no-store"
    return response


@cloud_bp.post("/api/cloud/client")
@require_admin
def set_client():
    """Save the household's own Google OAuth client id and secret."""
    data = json_object()
    client_id = str(data.get("client_id", "")).strip()
    client_secret = str(data.get("client_secret", "")).strip()
    if not client_id or not client_secret:
        return jsonify({"error": "Both the client ID and the secret are needed.",
                        "status": 400}), 400
    service = _service()
    service.set_client(client_id, client_secret)
    auth.audit(db.connect(_cfg().db_path), current_user().id, "cloud_client",
               client_id[-12:])
    return jsonify({"ok": True, **service.status()})


# ---------------------------------------------------------------------------
# Signing in
# ---------------------------------------------------------------------------

@cloud_bp.post("/api/cloud/connect")
@require_admin
def connect():
    """Hand back the Google consent URL for the console to open."""
    problem = drive.redirect_uri_problem(_redirect_uri())
    if problem:
        # Refused here rather than at Google, which answers every one of these
        # with the same "redirect_uri_mismatch" and no hint of the cause.
        return jsonify({
            "error": f"{problem} Open the console on the Ninaivu computer at "
                     f"{_loopback_redirect_uri().rsplit('/api/', 1)[0]} and "
                     f"connect from there.",
            "redirect_uri": _redirect_uri(),
            "redirect_uri_loopback": _loopback_redirect_uri(),
            "status": 409}), 409
    try:
        url = _service().begin_connect(_redirect_uri())
    except ValueError as exc:
        return jsonify({"error": str(exc), "status": 400}), 400
    return jsonify({"url": url, "redirect_uri": _redirect_uri()})


@cloud_bp.get("/api/cloud/callback")
@require_admin
def callback():
    """Where Google sends the browser back. Ends on the Cloud tab either way."""
    error = request.args.get("error", "")
    if error:
        return redirect(f"/#cloud?error={error}")
    try:
        _service().finish_connect(
            request.args.get("code", ""), request.args.get("state", ""),
            _redirect_uri())
    except Exception as exc:                                # noqa: BLE001
        log.warning("cloud sign-in failed: %s", exc)
        return redirect("/#cloud?error=" + _slug(str(exc)))
    auth.audit(db.connect(_cfg().db_path), current_user().id, "cloud_connect",
               _service().creds.account)
    return redirect("/#cloud?connected=1")


@cloud_bp.post("/api/cloud/disconnect")
@require_admin
def disconnect():
    data = json_object()
    service = _service()
    account = service.creds.account
    service.disconnect(forget_uploads=bool(data.get("forget_uploads")))
    auth.audit(db.connect(_cfg().db_path), current_user().id, "cloud_disconnect",
               account)
    return jsonify({"ok": True, **service.status()})


# ---------------------------------------------------------------------------
# Running it
# ---------------------------------------------------------------------------

@cloud_bp.post("/api/cloud/start")
@require_admin
def start():
    cfg = _cfg()
    if not cfg.cloud_enabled:
        return jsonify({"error": "Cloud backup is switched off.",
                        "status": 409}), 409
    # Uploading until everything is up, or until somebody pauses — a restart
    # in between is neither. Written before starting: a library with nothing
    # to send finishes at once, and crosses this off as it does.
    conn = db.connect(cfg.db_path)
    resume.want(conn, cloud_service.RESUME_NAME)
    result = _service().start()
    if not result.get("started") and result.get("reason"):
        resume.done(conn, cloud_service.RESUME_NAME)
        return jsonify({"error": result["reason"], "status": 409}), 409
    return jsonify({"ok": True, **result, **_service().status()})


@cloud_bp.post("/api/cloud/pause")
@require_admin
def pause():
    _service().pause()
    resume.done(db.connect(_cfg().db_path), cloud_service.RESUME_NAME)
    return jsonify({"ok": True, **_service().status()})


@cloud_bp.post("/api/cloud/retry")
@require_admin
def retry():
    count = _service().retry_failures()
    return jsonify({"ok": True, "requeued": count, **_service().status()})


@cloud_bp.post("/api/cloud/queue")
@require_admin
def queue():
    """Offer the library to the record without starting the upload."""
    added = _service().queue_library()
    return jsonify({"ok": True, "queued": added, **_service().status()})


# ---------------------------------------------------------------------------
# Getting it back
# ---------------------------------------------------------------------------
#
# The only routes here that write to the library disk. They never overwrite:
# a restore puts a file beside anything already at its path (see
# ninaivu/cloud/restore.py), so the worst a mistaken restore can do is leave
# extra copies to tidy up.

def _restore_scope(data: dict[str, Any]) -> dict[str, Any] | str:
    """The part of the backup asked for, checked. A string is a refusal."""
    raw = data.get("scope") or {}
    if not isinstance(raw, dict):
        return "scope must be an object"
    source = raw.get("source") or "record"
    if source not in ("record", "drive"):
        return "source must be record or drive"
    roots = raw.get("roots") or []
    if not isinstance(roots, list) or not all(isinstance(r, str) for r in roots):
        return "roots must be a list of library folders"
    folder = raw.get("folder") or ""
    if not isinstance(folder, str) or len(folder) > 1000:
        return "folder must be a path inside the library"
    ids = raw.get("asset_ids") or []
    if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
        return "asset_ids must be a list of numbers"
    return {"source": source, "roots": roots, "folder": folder.strip().strip("/\\"),
            "asset_ids": ids[:20000]}


@cloud_bp.post("/api/cloud/restore/preview")
@require_admin
def restore_preview():
    """What a restore of the chosen part would bring back, before it starts."""
    from ..cloud import restore as restore_mod               # noqa: PLC0415

    from ..cloud import index_copy                           # noqa: PLC0415

    data = json_object()
    data = data if isinstance(data, dict) else {}
    scope = _restore_scope(data)
    if isinstance(scope, str):
        return jsonify({"error": scope, "status": 400}), 400
    recovery, passphrase = _restore_secrets(data)
    service = _service()
    try:
        items = service.restore_items(scope, recovery=recovery, passphrase=passphrase)
    except index_copy.NeedsKey as exc:
        # Encrypted index copy: the wizard shows its key step and asks again.
        return jsonify({"error": str(exc), "needs_key": True, "index_copy": True,
                        "status": 409}), 409
    except ValueError as exc:
        return jsonify({"error": str(exc), "status": 409}), 409
    except drive.DriveError as exc:
        return jsonify({"error": f"Google Drive: {exc}", "status": 502}), 502
    summary = restore_mod.summarise(items)
    # Read from the copy of the index in Drive: full paths, and the original
    # checksums, rather than Drive's two folder levels and its MD5.
    summary["from_index_copy"] = service.index_found
    summary["needs_key"] = summary["encrypted"] > 0
    summary["key_on_this_machine"] = keyring.load(_cfg().state_dir) is not None
    summary["can_restore_in_place"] = scope["source"] == "record"
    summary["libraries"] = list(_cfg().roots)
    return jsonify(summary)


@cloud_bp.post("/api/cloud/restore/start")
@require_admin
def restore_start():
    from .api_library import _can_be_library, _root_refusal  # noqa: PLC0415
    from pathlib import Path                                 # noqa: PLC0415

    cfg = _cfg()
    data = json_body()
    if not isinstance(data, dict):
        return jsonify({"error": "Send the restore as a JSON object.", "status": 400}), 400
    scope = _restore_scope(data)
    if isinstance(scope, str):
        return jsonify({"error": scope, "status": 400}), 400

    destination = str(data.get("destination") or "").strip()
    if destination:
        target = Path(destination).expanduser()
        if not target.is_absolute():
            return jsonify({"error": "Give the whole path of the folder to "
                            r"restore into, like D:\Restored.", "status": 400}), 400
        probe = target if target.exists() else target.parent
        if not probe.is_dir() or not _can_be_library(probe, cfg):
            return jsonify({"error": _root_refusal(target, cfg), "status": 403}), 403

    recovery = data.get("recovery")
    if recovery is not None and not isinstance(recovery, dict):
        return jsonify({"error": "The recovery file did not read as one.",
                        "status": 400}), 400
    passphrase = data.get("passphrase")
    if passphrase is not None and not isinstance(passphrase, str):
        return jsonify({"error": "passphrase must be text", "status": 400}), 400

    service = _service()
    try:
        items = service.restore_items(scope, recovery=recovery,
                                      passphrase=passphrase or None)
        if scope["source"] == "drive" and not destination:
            # "Back where they were", read from the copy of the index in
            # Drive, means the absolute folders that copy names — another
            # computer's paths. Written unchecked, that could be anywhere on
            # this one. Only this machine's own library folders are trusted
            # as places to put files back without a folder being chosen.
            outside = service.roots_outside_libraries(items)
            if outside:
                shown = ", ".join(outside[:3]) + (" and more" if len(outside) > 3 else "")
                return jsonify({
                    "error": ("Choose a folder to restore into. The copy of the index "
                              f"puts these files in {shown}, which "
                              f"{'is not a library' if len(outside) == 1 else 'are not libraries'}"
                              " on this computer."),
                    "roots": outside, "status": 400}), 400
        key = service.restore_key(items, recovery=recovery, passphrase=passphrase or None)
        status = service.restore_start(items, destination=destination or None, key=key)
    except (ValueError, KeyError) as exc:
        return jsonify({"error": str(exc).strip("'\""), "status": 409}), 409
    except drive.DriveError as exc:
        return jsonify({"error": f"Google Drive: {exc}", "status": 502}), 502
    where = destination or "original folders"
    what = scope["folder"] or ("chosen photographs" if scope["asset_ids"]
                               else "the whole backup")
    auth.audit(db.connect(cfg.db_path), current_user().id, "cloud_restore",
               f"{len(items)} files ({what}) to {where}")
    return jsonify({"ok": True, "restore": status})


@cloud_bp.get("/api/cloud/restore/status")
@require_admin
def restore_status():
    return jsonify(_service().restore_status())


@cloud_bp.post("/api/cloud/restore/stop")
@require_admin
def restore_stop():
    _service().restore_stop()
    return jsonify({"ok": True, "restore": _service().restore_status()})


def _restore_secrets(data: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    recovery = data.get("recovery")
    passphrase = data.get("passphrase")
    return (recovery if isinstance(recovery, dict) else None,
            passphrase if isinstance(passphrase, str) and passphrase else None)


# ---------------------------------------------------------------------------
# The copy of the index in Drive (ninaivu/cloud/index_copy.py)
# ---------------------------------------------------------------------------

def _index_copy():
    return current_app.config["MV_SERVICES"].index_copy


@cloud_bp.get("/api/cloud/index-copy")
@require_admin
def index_copy_status():
    return jsonify(_index_copy().status())


@cloud_bp.post("/api/cloud/index-copy/run")
@require_admin
def index_copy_run():
    """Send a copy of the index now, rather than at the next daily look."""
    if not _service().creds.connected:
        return jsonify({"error": "Connect the Google account first.", "status": 409}), 409
    started = _index_copy().run_in_background()
    auth.audit(db.connect(_cfg().db_path), current_user().id, "cloud_index_copy",
               "sent now")
    return jsonify({"ok": True, "started": started, **_index_copy().status()})


def _clock(value: Any) -> str | None:
    """A settings field as a normalised ``HH:MM``. ``None`` if it is not a time.

    Empty is a legitimate answer — it means "no window" — so the caller has to
    distinguish it from a rejection with ``is None`` rather than by truthiness.
    Rejecting a typo here rather than letting :class:`~ninaivu.cloud.limits.Window`
    quietly ignore it is the difference between being told the time was wrong
    and finding out weeks later that the nightly backup never ran.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    minutes = limits.parse_clock(raw)
    return limits.format_clock(minutes) if minutes is not None else None


def _slug(text: str) -> str:
    from urllib.parse import quote
    return quote(text[:160], safe="")


# ---------------------------------------------------------------------------
# Test restores (ninaivu/cloud/restore_test.py)
# ---------------------------------------------------------------------------

def _tester():
    return current_app.config["MV_SERVICES"].restore_tests


@cloud_bp.get("/api/cloud/restore-tests")
@require_admin
def restore_tests():
    """When the backup was last proven to restore, and how it went."""
    return jsonify(_tester().status())


@cloud_bp.post("/api/cloud/restore-tests/run")
@require_admin
def restore_tests_run():
    """Test a restore now, rather than waiting for the week to come round."""
    if not _service().creds.connected:
        return jsonify({"error": "Connect the Google account first.", "status": 409}), 409
    started = _tester().run_in_background()
    auth.audit(db.connect(_cfg().db_path), current_user().id, "restore_test", "run now")
    return jsonify({"ok": True, "started": started, **_tester().status()})


@cloud_bp.post("/api/cloud/restore-tests/settings")
@require_admin
def restore_tests_settings():
    data = json_body()
    if not isinstance(data, dict) or set(data) - {"every_days"}:
        return jsonify({"error": "Send every_days.", "status": 400}), 400
    try:
        days = int(data.get("every_days"))
    except (TypeError, ValueError, OverflowError):
        days = -1
    if isinstance(data.get("every_days"), bool) or not 0 <= days <= 90:
        return jsonify({"error": "Choose a number of days from 0 (never) to 90.",
                        "status": 400}), 400
    cfg = _cfg()
    cfg.restore_test_days = days
    cfg.save()
    auth.audit(db.connect(cfg.db_path), current_user().id, "restore_test",
               f"every {days} days" if days else "off")
    return jsonify({"ok": True, **_tester().status()})


# ---------------------------------------------------------------------------
# What is left out of the backup (ninaivu/cloud/rules.py)
# ---------------------------------------------------------------------------

#: The most megabytes a size rule may name: a terabyte, which is no rule at all.
MAX_RULE_MB = 1024 * 1024


def _rules_from(data: Any) -> Rules | tuple[Any, int]:
    """The rules a request describes, or the refusal to send back."""
    if not isinstance(data, dict):
        return jsonify({"error": "Backup rules must be a JSON object", "status": 400}), 400
    current = _service().rules()
    kinds = data.get("kinds", current.kinds)
    if kinds not in KINDS:
        return jsonify({"error": "kinds must be all, no_video or pictures",
                        "status": 400}), 400
    try:
        max_mb = int(data.get("max_mb", current.max_mb) or 0)
    except (TypeError, ValueError, OverflowError):
        max_mb = -1
    if isinstance(data.get("max_mb"), bool) or not 0 <= max_mb <= MAX_RULE_MB:
        return jsonify({"error": "The largest size is a number of megabytes, or 0 "
                        "for no limit.", "status": 400}), 400
    for name in ("folders", "words"):
        if name in data and not isinstance(data[name], (list, str)):
            return jsonify({"error": f"{name} is a list", "status": 400}), 400
    return Rules(
        kinds=kinds, max_mb=max_mb,
        folders=tuple(clean_folders(data["folders"])) if "folders" in data else current.folders,
        words=tuple(clean_words(data["words"])) if "words" in data else current.words)


@cloud_bp.post("/api/cloud/rules/preview")
@require_admin
def rules_preview():
    """What a set of rules would keep back, before it is saved."""
    rules = _rules_from(json_body())
    if not isinstance(rules, Rules):
        return rules
    return jsonify({"rules": rules.public(),
                    **rules.preview(db.connect(_cfg().db_path))})


@cloud_bp.post("/api/cloud/rules")
@require_admin
def rules_save():
    """Set what is left out of the backup. Nothing already in Drive is touched."""
    rules = _rules_from(json_body())
    if not isinstance(rules, Rules):
        return rules
    cfg = _cfg()
    cfg.cloud_kinds = rules.kinds
    cfg.cloud_max_mb = rules.max_mb
    cfg.cloud_skip_folders = list(rules.folders)
    cfg.cloud_skip_words = list(rules.words)
    cfg.save()
    moved = _service().apply_rules()
    said = []
    if rules.kinds != "all":
        said.append("photographs only" if rules.kinds == "pictures" else "no videos")
    if rules.max_mb:
        said.append(f"nothing over {rules.max_mb:,} MB")
    if rules.folders:
        said.append(f"{len(rules.folders)} folders left out")
    if rules.words:
        said.append(f"{len(rules.words)} words left out")
    auth.audit(db.connect(cfg.db_path), current_user().id, "cloud_rules",
               (", ".join(said) or "everything is backed up")
               + f" ({moved['set_aside']:,} set aside, {moved['released']:,} released)")
    return jsonify({"ok": True, **moved, **_service().status()})


# ---------------------------------------------------------------------------
# Large files wait for approval (ninaivu/cloud/approvals.py)
# ---------------------------------------------------------------------------

@cloud_bp.get("/api/cloud/approvals")
@require_admin
def cloud_approvals():
    """Files waiting for an administrator's yes (``?declined=1``: those refused)."""
    from .api import _thumb_version                          # noqa: PLC0415
    conn = db.connect(_cfg().db_path)
    declined = request.args.get("declined") == "1"
    items = []
    for row in approvals.waiting(conn, declined=declined):
        items.append({"id": row["id"], "name": row["filename"] or Path(row["rel_path"]).name,
                      "path": row["rel_path"], "size": int(row["size"] or 0), "kind": row["kind"],
                      "why": row["error"], "asset_id": row["asset_id"],
                      "thumb_v": _thumb_version(dict(row)) if row["thumb"] else ""})
    return jsonify({"limit_mb": int(getattr(_cfg(), "cloud_approval_mb", 1024) or 0),
                    **approvals.totals(conn), "items": items})


@cloud_bp.post("/api/cloud/approvals")
@require_admin
def cloud_approvals_decide():
    """Approve or decline large files by their queue id, or change the size."""
    data = json_body()
    if not isinstance(data, dict):
        return jsonify({"error": "Send an object.", "status": 400}), 400
    cfg = _cfg()
    conn = db.connect(cfg.db_path)
    if "ids" in data:
        ids, decision = data.get("ids"), data.get("decision")
        if (not isinstance(ids, list) or not all(isinstance(i, int) and not isinstance(i, bool)
                                                 for i in ids) or len(ids) > 5000):
            return jsonify({"error": "ids must be a list of numbers", "status": 400}), 400
        if decision not in ("approved", "declined"):
            return jsonify({"error": "decision is approved or declined", "status": 400}), 400
    if "limit_mb" in data:
        limit = data["limit_mb"]
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= MAX_RULE_MB:
            return jsonify({"error": "limit_mb is a number of megabytes, or 0 for off",
                            "status": 400}), 400
        cfg.cloud_approval_mb = limit
        cfg.save()
        approvals.apply(conn, cfg)
        auth.audit(conn, current_user().id, "cloud_approval",
                   f"files over {limit} MB need approval" if limit else "approval off")
    if "ids" in data:
        done = approvals.decide(conn, data["ids"], data["decision"], current_user().id)
        auth.audit(conn, current_user().id, "cloud_approval", f"{data['decision']} {done} file(s)")
    return cloud_approvals()


# ---------------------------------------------------------------------------
# A second copy, on another disk (ninaivu/storage/mirror.py)
# ---------------------------------------------------------------------------
#
# Here with the cloud backup because it is the same question — where else is
# the library? — and console only for the same reason: it copies everything
# the household has, hidden things included, to wherever it is pointed.

def _mirror():
    return current_app.config["MV_SERVICES"].mirror


@cloud_bp.get("/api/mirror/status")
@require_admin
def mirror_status():
    return jsonify(_mirror().status())


@cloud_bp.post("/api/mirror/settings")
@require_admin
def mirror_settings():
    from ..storage.mirror import folder_problem              # noqa: PLC0415

    data = json_body()
    if not isinstance(data, dict) or set(data) - {"enabled", "folder", "every_hours"}:
        return jsonify({"error": "Send enabled, folder or every_hours.", "status": 400}), 400
    cfg = _cfg()
    mirror = _mirror()
    if "enabled" in data and not isinstance(data["enabled"], bool):
        return jsonify({"error": "enabled must be true or false", "status": 400}), 400
    hours = data.get("every_hours", cfg.mirror_every_hours)
    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not 0 <= hours <= 24 * 90:
        return jsonify({"error": "Choose a number of hours from 0 (only when asked) "
                        "to 2160.", "status": 400}), 400
    folder = str(data.get("folder", cfg.mirror_dir) or "").strip()
    enabled = bool(data.get("enabled", cfg.mirror_enabled))
    if "folder" in data and not isinstance(data["folder"], str):
        return jsonify({"error": "folder is a path", "status": 400}), 400
    if folder != (cfg.mirror_dir or "") or (enabled and not cfg.mirror_enabled):
        # Checked when it is chosen and when it is switched on. A disk that is
        # merely unplugged later is not a wrong setting; it is a disk that is away.
        problem = folder_problem(folder, mirror.roots, cfg.state_dir) if (folder or enabled) else None
        if problem:
            return jsonify({"error": problem, "status": 400}), 400
        if mirror.running and folder != (cfg.mirror_dir or ""):
            return jsonify({"error": "Stop the copy that is running before moving it.",
                            "status": 409}), 409
    cfg.mirror_dir, cfg.mirror_enabled, cfg.mirror_every_hours = folder, enabled, float(hours)
    cfg.save()
    if not enabled:
        mirror.stop()
    auth.audit(db.connect(cfg.db_path), current_user().id, "second_copy",
               f"{'on' if enabled else 'off'}; folder {folder or '(none)'}; "
               f"every {hours:g} h")
    return jsonify({"ok": True, **mirror.status()})


@cloud_bp.post("/api/mirror/start")
@require_admin
def mirror_start():
    mirror = _mirror()
    if not mirror.enabled:
        return jsonify({"error": "Switch the second copy on first.", "status": 409}), 409
    result = mirror.start()
    if result.get("reason"):
        return jsonify({"error": result["reason"], "status": 409}), 409
    auth.audit(db.connect(_cfg().db_path), current_user().id, "second_copy", "copy now")
    return jsonify({"ok": True, **result, **mirror.status()})


@cloud_bp.post("/api/mirror/verify")
@require_admin
def mirror_verify():
    """Read the whole copy back now and compare every fingerprint."""
    result = _mirror().verify()
    if result.get("reason"):
        return jsonify({"error": result["reason"], "status": 409}), 409
    auth.audit(db.connect(_cfg().db_path), current_user().id, "second_copy", "check now")
    return jsonify({"ok": True, **result, **_mirror().status()})


@cloud_bp.post("/api/mirror/restore")
@require_admin
def mirror_restore():
    """Put back what is on the copy and missing from the library; ``folder`` narrows it."""
    data = json_body() or {}
    folder = data.get("folder", "") if isinstance(data, dict) else ""
    if not isinstance(folder, str):
        return jsonify({"error": "folder is a path inside the library", "status": 400}), 400
    result = _mirror().restore(folder)
    if result.get("reason"):
        return jsonify({"error": result["reason"], "status": 409}), 409
    auth.audit(db.connect(_cfg().db_path), current_user().id, "second_copy",
               f"restore {folder or 'everything missing'}")
    return jsonify({"ok": True, **result, **_mirror().status()})


@cloud_bp.post("/api/mirror/stop")
@require_admin
def mirror_stop():
    _mirror().stop()
    return jsonify({"ok": True, **_mirror().status()})


# ---------------------------------------------------------------------------
# How many copies there are (ninaivu/storage/copies.py)
# ---------------------------------------------------------------------------

def _library_roots() -> list[str]:
    return _cfg().library_roots


@cloud_bp.get("/api/copies")
@require_admin
def copies_summary():
    """Files in one, two and three places, and why the ones are in one."""
    from ..storage import copies                           # noqa: PLC0415

    return jsonify(copies.summary(db.connect(_cfg().db_path), _library_roots()))


@cloud_bp.get("/api/copies/single")
@require_admin
def copies_single():
    """The files that are only in the library, largest first."""
    from ..storage import copies                           # noqa: PLC0415

    try:
        offset = int(request.args.get("offset", 0))
        limit = int(request.args.get("limit", 100))
    except ValueError:
        return jsonify({"error": "offset and limit are numbers", "status": 400}), 400
    return jsonify(copies.single(db.connect(_cfg().db_path), _library_roots(),
                                 reason=request.args.get("reason", ""),
                                 limit=limit, offset=offset))


# ---------------------------------------------------------------------------
# The off-site copy: encrypted, somewhere else (ninaivu/cloud/offsite.py)
# ---------------------------------------------------------------------------

def _offsite():
    return current_app.config["MV_SERVICES"].offsite


_OFFSITE_TEXT = {"folder": 1000, "endpoint": 300, "region": 60, "bucket": 120, "prefix": 120,
                 "access_key": 200}


@cloud_bp.get("/api/offsite")
@require_admin
def offsite_status():
    return jsonify(_offsite().status())


@cloud_bp.post("/api/offsite")
@require_admin
def offsite_settings():
    """Where the off-site copy goes, and whether it is kept. The secret key is
    written apart, owner-only, and never sent back."""
    data = json_body()
    if not isinstance(data, dict):
        return jsonify({"error": "Send the settings as an object.", "status": 400}), 400
    cfg = _cfg()
    offsite = _offsite()
    if "kind" in data:
        if data["kind"] not in ("folder", "s3"):
            return jsonify({"error": "kind is folder or s3", "status": 400}), 400
        cfg.offsite_kind = data["kind"]
    for name, limit in _OFFSITE_TEXT.items():
        if name in data:
            value = data[name]
            if not isinstance(value, str) or len(value) > limit:
                return jsonify({"error": f"{name} must be text", "status": 400}), 400
            value = value.strip()
            if name == "endpoint" and value and not value.startswith(("https://", "http://")):
                return jsonify({"error": "The address starts with https://", "status": 400}), 400
            setattr(cfg, f"offsite_{name}", value)
    if "every_hours" in data:
        try:
            cfg.offsite_every_hours = max(1, min(int(data["every_hours"]), 24 * 30))
        except (TypeError, ValueError):
            return jsonify({"error": "every_hours must be a number", "status": 400}), 400
    if "secret_key" in data:
        if not isinstance(data["secret_key"], str) or len(data["secret_key"]) > 400:
            return jsonify({"error": "secret_key must be text", "status": 400}), 400
        if data["secret_key"].strip():
            offsite.save_secret(data["secret_key"].strip())
    if "enabled" in data:
        if not isinstance(data["enabled"], bool):
            return jsonify({"error": "enabled must be true or false", "status": 400}), 400
        cfg.offsite_enabled = data["enabled"]
        if not cfg.offsite_enabled:
            offsite.stop()
    cfg.save()
    auth.audit(db.connect(cfg.db_path), current_user().id, "offsite",
               f"{cfg.offsite_kind} {'on' if cfg.offsite_enabled else 'off'}")
    return jsonify(offsite.status())


@cloud_bp.post("/api/offsite/test")
@require_admin
def offsite_test():
    try:
        return jsonify(_offsite().test())
    except (OSError, ValueError) as exc:
        return jsonify({"error": f"That did not work: {exc}", "status": 409}), 409


@cloud_bp.post("/api/offsite/start")
@require_admin
def offsite_start():
    """Bring the copy up to date. ``{"start_over": true}`` forgets what was sent
    to the destination now chosen and sends everything again — for a copy
    removed there on purpose, which is otherwise refused as a disk that is
    not connected."""
    data = json_body() or {}
    start_over = data.get("start_over", False) if isinstance(data, dict) else False
    if not isinstance(start_over, bool):
        return jsonify({"error": "start_over must be true or false", "status": 400}), 400
    try:
        status = _offsite().start(start_over=start_over)
    except ValueError as exc:
        return jsonify({"error": str(exc), "status": 409}), 409
    if start_over:
        cfg = _cfg()
        auth.audit(db.connect(cfg.db_path), current_user().id, "offsite",
                   "started over at the destination")
    return jsonify(status)


@cloud_bp.post("/api/offsite/stop")
@require_admin
def offsite_stop():
    _offsite().stop()
    return jsonify(_offsite().status())


@cloud_bp.post("/api/offsite/restore")
@require_admin
def offsite_restore():
    data = json_body() or {}
    folder = data.get("folder", "") if isinstance(data, dict) else ""
    if not isinstance(folder, str) or not folder.strip():
        return jsonify({"error": "Choose a folder to put the files in.", "status": 400}), 400
    cfg = _cfg()
    chosen = Path(folder).expanduser().resolve()
    for root in cfg.library_roots:
        if chosen.is_relative_to(Path(root).resolve()):
            return jsonify({"error": "Restore into a folder outside the library; add it afterwards.",
                            "status": 400}), 400
    try:
        status = _offsite().restore(folder)
    except ValueError as exc:
        return jsonify({"error": str(exc), "status": 409}), 409
    auth.audit(db.connect(cfg.db_path), current_user().id, "offsite", f"restore into {folder}")
    return jsonify(status)
