"""The phone inbox: WebDAV for sync apps, signed in with a phone key.

A sync app on a phone (PhotoSync and its kind) can send new photographs home
in the background, which the family app's backup page cannot. What those apps
speak is WebDAV, so this is the small part of WebDAV they use, at ``/dav/``,
with the person's username and a phone key (``media/phone_keys.py``) as the
password.

**Upload only.** PUT sends a file; MKCOL makes a folder (folders are the
phone's own idea and cost nothing here); PROPFIND and HEAD answer whether a file
this phone sent is safely here; MOVE renames what it sent. GET of a file, and
DELETE, are refused: a key can add photographs and nothing else, so it never
shows anybody the library.

**The same backup as the family app's.** Every file is checked, recognised if
the library already has it, and filed into the review queue or straight into
the library by exactly the code the backup page uses (``media/phone_backup``),
by the date it was taken rather than the folder the phone chose. When the
person's backups need no review, the files are filed a minute after the phone
stops sending, one stand-down of the indexer for the lot.

**Home and the house's own devices only.** The inbox does not answer the
internet (``server/remote.from_the_internet``): over Tailscale or WireGuard,
or on the home network.
"""

from __future__ import annotations

import base64
import binascii
import logging
import threading
from datetime import datetime, timezone
from email.utils import format_datetime
from typing import Any
from urllib.parse import quote, unquote, urlsplit
from xml.sax.saxutils import escape

from flask import Response, abort, current_app, g, jsonify, request

from ..media import phone_backup, phone_keys
from ..server.auth import ANONYMOUS, audit, current_user, require_family
from ._body import json_object
from .api import _cfg, _conn, _viewer, bp
from .api_phone_backup import _destination, _supported, file_now

log = logging.getLogger(__name__)

PREFIX = "/dav"
REALM = "Ninaivu phone backup"
METHODS = ["OPTIONS", "PROPFIND", "PROPPATCH", "MKCOL", "PUT", "GET", "HEAD",
           "DELETE", "MOVE", "COPY"]
#: Read from the phone this much at a time.
_PIECE = 1024 * 1024
#: Free space asked about again after this much more has arrived.
_RECHECK = 256 * 1024 * 1024
#: Seconds after the last file before a trusted person's backups are filed.
FILE_AFTER = 60.0


def _ready(conn) -> None:
    phone_backup.init_schema(conn)
    phone_keys.init_schema(conn)


# ---------------------------------------------------------------------------
# Signing in, before anything else is asked (called from the app's _identify)
# ---------------------------------------------------------------------------

def identify(conn, cfg):
    """Who this /dav request is, from its key; or the answer refusing it.

    Cookies are not looked at: the inbox is for apps, and a browser session
    must not be able to send files through it on a page's behalf.
    """
    from ..server import remote                               # noqa: PLC0415

    g.user = ANONYMOUS
    g.phone_key = None
    g.session_token = ""
    g.locked = False
    if remote.from_the_internet(cfg, request):
        return _plain(403, "The phone inbox does not answer the internet. Use it "
                           "at home or over Tailscale.")
    if request.method == "OPTIONS":
        return None
    secret = _basic_password(request.headers.get("Authorization", ""))
    if secret is None:
        return _ask_for_key()
    _ready(conn)
    found = phone_keys.lookup(conn, secret)
    if found is None:
        return _ask_for_key()
    row, user = found
    g.user = user
    g.phone_key = {"id": int(row["id"]), "name": row["name"]}
    phone_keys.used(conn, int(row["id"]))
    return None


def _basic_password(header: str) -> str | None:
    """The password of an HTTP Basic sign-in. The username is not needed — the
    key says whose it is — so any username is taken, the person's own or not."""
    kind, _, value = header.partition(" ")
    if kind.lower() != "basic" or not value:
        return None
    try:
        decoded = base64.b64decode(value.strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    _, sep, password = decoded.partition(":")
    return password if sep else None


def _ask_for_key() -> Response:
    response = _plain(401, "Sign in with your username and a phone key.")
    response.headers["WWW-Authenticate"] = f'Basic realm="{REALM}", charset="UTF-8"'
    return response


def _plain(status: int, message: str) -> Response:
    return Response(message + "\n", status=status, mimetype="text/plain")


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _path(sub: str) -> str:
    """The phone's path, tidied: no empty, '.' or '..' parts, no backslashes."""
    parts = [p for p in unquote(sub or "").replace("\\", "/").split("/")
             if p and p not in (".", "..")]
    return "/".join(parts)[:1000]


def _is_file(path: str) -> bool:
    """A path that names a photograph or video. Everything else is a folder:
    the phone's folders exist as soon as it asks for them."""
    return bool(path) and _supported(path.rsplit("/", 1)[-1]) is not None


def _href(path: str, folder: bool) -> str:
    href = PREFIX + "/" + quote(path)
    if folder and not href.endswith("/"):
        href += "/"
    return href


def _key_id() -> int:
    key = g.get("phone_key")
    if not key:
        abort(401)
    return int(key["id"])


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------

@bp.route(PREFIX, methods=METHODS, strict_slashes=False)
@bp.route(PREFIX + "/<path:sub>", methods=METHODS)
def webdav(sub: str = ""):
    if request.method == "OPTIONS":
        response = Response(status=200)
        response.headers["DAV"] = "1"
        response.headers["MS-Author-Via"] = "DAV"
        response.headers["Allow"] = ", ".join(METHODS)
        return response
    _key_id()
    path = _path(sub)
    handler = {
        "PROPFIND": _propfind, "PROPPATCH": _proppatch, "MKCOL": _mkcol,
        "PUT": _put, "HEAD": _head, "MOVE": _move,
    }.get(request.method)
    if handler is None:
        # GET, DELETE, COPY: a phone key adds photographs and does nothing else.
        return _plain(403, "A phone key can only send photographs to Ninaivu.")
    return handler(path)


def _mkcol(path: str):
    if _is_file(path):
        return _plain(405, "That name is a file's, not a folder's.")
    return Response(status=201)


def _head(path: str):
    if not _is_file(path):
        return Response(status=200)
    conn = _conn()
    _ready(conn)
    row = phone_keys.path_row(conn, _key_id(), path)
    if row is None:
        return Response(status=404)
    response = Response(status=200, mimetype="application/octet-stream")
    response.headers["Content-Length"] = str(int(row["size"]))
    response.headers["Last-Modified"] = _http_date(row["modified"] or row["finished_at"])
    return response


def _move(path: str):
    destination = _path(urlsplit(request.headers.get("Destination", "")).path
                        .removeprefix(PREFIX))
    if not destination:
        return _plain(400, "Say where to with a Destination header.")
    if not _is_file(path):
        # A folder of the phone's: nothing is kept for folders, so nothing moves.
        return Response(status=201)
    conn = _conn()
    _ready(conn)
    if not phone_keys.move_path(conn, _key_id(), path, destination):
        return Response(status=404)
    return Response(status=201)


def _proppatch(path: str):
    """Sync apps set a file's dates this way after sending it. Ninaivu dates a
    photograph by what is inside it, so every property is accepted and kept by
    nobody — which is what the app needs to hear to carry on."""
    body = (f'<?xml version="1.0" encoding="utf-8"?>\n<D:multistatus xmlns:D="DAV:">'
            f"<D:response><D:href>{escape(_href(path, not _is_file(path)))}</D:href>"
            "<D:propstat><D:prop/><D:status>HTTP/1.1 200 OK</D:status></D:propstat>"
            "</D:response></D:multistatus>")
    return Response(body, status=207, mimetype="application/xml")


def _propfind(path: str):
    depth = request.headers.get("Depth", "1").strip()
    conn = _conn()
    _ready(conn)
    key_id = _key_id()
    entries: list[str] = []
    if _is_file(path):
        row = phone_keys.path_row(conn, key_id, path)
        if row is None:
            return Response(status=404)
        entries.append(_file_entry(path, row))
    else:
        entries.append(_folder_entry(path))
        if depth != "0":
            seen_folders: set[str] = set()
            base = (path + "/") if path else ""
            for child, row in phone_keys.paths_under(conn, key_id, path):
                rest = child[len(base):]
                if "/" in rest:
                    folder = base + rest.split("/", 1)[0]
                    if folder not in seen_folders:
                        seen_folders.add(folder)
                        entries.append(_folder_entry(folder))
                else:
                    entries.append(_file_entry(child, row))
    body = ('<?xml version="1.0" encoding="utf-8"?>\n<D:multistatus xmlns:D="DAV:">'
            + "".join(entries) + "</D:multistatus>")
    return Response(body, status=207, mimetype="application/xml")


def _folder_entry(path: str) -> str:
    name = path.rsplit("/", 1)[-1] if path else "Ninaivu"
    return (f"<D:response><D:href>{escape(_href(path, True))}</D:href><D:propstat><D:prop>"
            f"<D:displayname>{escape(name)}</D:displayname>"
            "<D:resourcetype><D:collection/></D:resourcetype>"
            "</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>")


def _file_entry(path: str, row) -> str:
    when = row["modified"] or row["finished_at"]
    return (f"<D:response><D:href>{escape(_href(path, False))}</D:href><D:propstat><D:prop>"
            f"<D:displayname>{escape(path.rsplit('/', 1)[-1])}</D:displayname>"
            "<D:resourcetype/>"
            f"<D:getcontentlength>{int(row['size'])}</D:getcontentlength>"
            f"<D:getlastmodified>{_http_date(when)}</D:getlastmodified>"
            f"<D:getetag>\"{escape(row['sha256'][:32] or str(row['id']))}\"</D:getetag>"
            "</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>")


def _http_date(stamp: Any) -> str:
    try:
        when = datetime.fromtimestamp(float(stamp or 0), tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        when = datetime.fromtimestamp(0, tz=timezone.utc)
    return format_datetime(when, usegmt=True)


def _modified() -> float:
    """The file's own time, when the app says it (Nextcloud's header, which many
    sync apps send). Ninaivu dates by what is inside the file; this is only
    what it falls back on, and part of how a file is recognised next time."""
    for header in ("X-OC-Mtime", "X-File-Mtime"):
        value = request.headers.get(header, "").strip()
        if value:
            try:
                number = float(value)
            except ValueError:
                continue
            if 0 < number < 4e9:
                return number
    return 0.0


def _put(path: str):
    name = _supported(path.rsplit("/", 1)[-1]) if path else None
    if name is None:
        return _plain(415, "Ninaivu takes photographs, video and audio. That file "
                           "type is not one of them.")
    cfg = _cfg()
    limit = max(1, int(getattr(cfg, "phone_upload_max_gb", 8) or 8)) * 1024 ** 3
    length = request.content_length
    stream = request.environ.get("wsgi.input")
    if stream is None or (length is None and not request.environ.get("wsgi.input_terminated")):
        return _plain(411, "Say how large the file is with Content-Length.")
    if length is not None and length > limit:
        return _plain(413, f"Ninaivu takes files up to {limit // 1024 ** 3} GB from a phone.")
    if length is not None and length <= 0:
        return _plain(400, "That file is empty.")
    root, scope = _destination()
    if length:
        # Before a byte is read: a phone over its person's allowance of files
        # waiting for review is told to try later (507, which sync apps retry),
        # not that the file is unwelcome.
        over = _over_quota(cfg, length)
        if over is not None:
            return over
    target = phone_backup.incoming(cfg)
    if not phone_backup.has_room(target.parent, length or 0):
        return _plain(507, "Ninaivu's computer is nearly out of space, so this file "
                           "cannot be taken right now.")
    received = 0
    try:
        with open(target, "xb") as out:
            next_check = _RECHECK
            while length is None or received < length:
                piece = stream.read(_PIECE if length is None else min(_PIECE, length - received))
                if not piece:
                    break
                received += len(piece)
                if received > limit:
                    raise _Refused(413, "That file is larger than Ninaivu takes from a phone.")
                out.write(piece)
                if received >= next_check:
                    next_check += _RECHECK
                    if not phone_backup.has_room(target.parent, 0):
                        raise _Refused(507, "Ninaivu's computer is nearly out of space, "
                                            "so this file cannot be taken right now.")
        if length is not None and received != length:
            raise _Refused(400, "The file did not arrive whole. Send it again.")
        if received == 0:
            raise _Refused(400, "That file is empty.")
        if length is None:
            # Sent in chunks, so its size is known only now. Past the
            # allowance it would fail its review staging and be answered 415,
            # which a sync app takes as "never send this file again".
            over = _over_quota(cfg, received)
            if over is not None:
                raise _Refused(over.status_code, over.get_data(as_text=True).strip())
    except _Refused as refusal:
        target.unlink(missing_ok=True)
        return _plain(refusal.status, refusal.message)
    except OSError as exc:
        target.unlink(missing_ok=True)
        log.warning("a phone's upload of %s could not be written: %s", name, exc)
        return _plain(500, "The file could not be written. Send it again.")

    conn = _conn()
    _ready(conn)
    key = g.phone_key
    user = current_user()
    known = phone_keys.path_row(conn, key["id"], path)
    try:
        answer = phone_backup.receive_whole(
            conn, cfg, user.id, phone_keys.device_id(key["id"]), key["name"],
            name=name, path=target, modified=_modified(), root=root, scope=scope,
            max_visibility=_viewer()["max_visibility"])
    except phone_backup.BackupError as exc:
        return _plain(exc.status, str(exc))
    if answer["state"] not in phone_backup.SAFE:
        message = answer.get("error") or "The file could not be taken."
        status = 415 if answer["state"] == phone_backup.FAILED else 500
        if status == 415:
            # Two files sent side by side can both pass the check before the
            # first byte; the one that tipped the allowance is retried later.
            over = _over_quota(cfg, received)
            if over is not None:
                return over
        return _plain(status, message)
    phone_keys.remember_path(conn, key["id"], path, int(answer["id"]))
    phone_keys.used(conn, key["id"], files=1)
    if answer["state"] == phone_backup.STAGED and (user.is_admin or cfg.phone_backup_trusted):
        _file_soon(current_app._get_current_object(), cfg, user.id)  # noqa: SLF001
    return Response(status=204 if known is not None else 201)


def _over_quota(cfg, size: int) -> Response | None:
    """The 507 for a file that would take its person past what they may have
    waiting for review, or None."""
    from ..media import upload_review                        # noqa: PLC0415
    try:
        upload_review._refuse_over_quota(                    # noqa: SLF001
            _conn(), cfg, current_user().id, size)
    except ValueError as exc:
        return _plain(507, str(exc))
    return None


class _Refused(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------------------
# Filing, once the phone has stopped sending
# ---------------------------------------------------------------------------

_timers: dict[int, threading.Timer] = {}
_timers_lock = threading.Lock()


def _file_soon(app, cfg, user_id: int) -> None:
    """File this person's staged backups once the phone has been quiet for
    FILE_AFTER seconds. A sync app sends hundreds of files one after another
    and never says it has finished, as the backup page does; filing each as it
    came would stand the indexer down hundreds of times."""

    def fire() -> None:
        from ..storage import db                             # noqa: PLC0415
        with _timers_lock:
            if _timers.get(user_id) is timer:
                _timers.pop(user_id, None)
        scanner = app.config.get("MV_SCANNER")
        if scanner is not None and getattr(scanner, "closed", False):
            return
        try:
            conn = db.connect(cfg.db_path)
            try:
                with app.app_context():
                    file_now(conn, cfg, user_id, user_id)
            finally:
                conn.close()
        except Exception:                                    # noqa: BLE001
            log.exception("could not file the phone backups a sync app sent")

    timer = threading.Timer(FILE_AFTER, fire)
    timer.daemon = True
    timer.name = "ninaivu-phone-inbox-file"
    with _timers_lock:
        previous = _timers.pop(user_id, None)
        if previous is not None:
            previous.cancel()
        _timers[user_id] = timer
    timer.start()


def cancel_pending() -> None:
    """Forget every filing still waiting (on shutdown, and between tests)."""
    with _timers_lock:
        for timer in _timers.values():
            timer.cancel()
        _timers.clear()


# ---------------------------------------------------------------------------
# A person's own keys, from the family app
# ---------------------------------------------------------------------------

@bp.get("/api/phone-keys")
@require_family
def phone_keys_list():
    conn = _conn()
    _ready(conn)
    return jsonify(keys=phone_keys.listed(conn, current_user().id), **_how_to_connect())


@bp.post("/api/phone-keys")
@require_family
def phone_keys_make():
    """A key for one phone, shown this once."""
    data = json_object()
    conn = _conn()
    _ready(conn)
    user = current_user()
    try:
        row, key = phone_keys.make(conn, user.id, str(data.get("name") or ""))
    except phone_keys.PhoneKeyError as exc:
        return jsonify(error=str(exc), status=409), 409
    audit(conn, user.id, "phone_key_made", f"{row['name']} (…{row['hint']})")
    return jsonify(key=key, username=user.username, item=row, **_how_to_connect()), 201


@bp.delete("/api/phone-keys/<int:key_id>")
@require_family
def phone_keys_revoke(key_id: int):
    conn = _conn()
    _ready(conn)
    user = current_user()
    if not phone_keys.revoke(conn, key_id, user.id):
        return jsonify(error="No such phone key.", status=404), 404
    audit(conn, user.id, "phone_key_revoked", str(key_id))
    return jsonify(revoked=True, keys=phone_keys.listed(conn, user.id))


def _how_to_connect() -> dict[str, Any]:
    """What to type into the sync app: the inbox's address as this page was
    reached, which is the address the phone reaches Ninaivu by."""
    base = request.host_url.rstrip("/")
    return {"webdav_url": base + PREFIX + "/",
            "max_file_gb": int(getattr(_cfg(), "phone_upload_max_gb", 8) or 8),
            "needs_review": not (current_user().is_admin or _cfg().phone_backup_trusted)}

