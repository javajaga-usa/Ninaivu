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
from typing import Any

from flask import Blueprint, current_app, jsonify, redirect, request

from ..server import auth
from ..storage import db, resume
from ..cloud import service as cloud_service
from ..server.auth import current_user, require_admin
from ..cloud import drive, keyring, limits
from ._body import json_object

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
    data = request.get_json(silent=True)
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
    data = request.get_json(silent=True)
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
    data = request.get_json(silent=True)
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
    data = request.get_json(silent=True)
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
