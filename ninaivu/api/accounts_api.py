"""Authentication, profiles and people management endpoints.

Kept separate from :mod:`ninaivu.api` so the permission surface is easy to read
in one place: every route here either belongs to the signed-in user or is
wrapped in ``@require_admin``.
"""

from __future__ import annotations

import re
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from flask import Blueprint, abort, current_app, jsonify, request, send_file
from PIL import Image

from ..server import auth
from ..storage import db
from ..server.config import clean_home_name, home_name_for, house_name
from ..server.auth import (
    ROLE_ADMIN, ROLE_FAMILY, ROLES, ROLE_LABELS,
    current_user, require_admin,
)

#: Endpoints every face needs (sign in/out, own profile, avatars).
accounts = Blueprint("accounts", __name__)
#: The profile picker — only mounted on the home app.
home_accounts = Blueprint("home_accounts", __name__)
#: People management — only mounted on the admin console.
admin_accounts = Blueprint("admin_accounts", __name__)

_HEX_COLOUR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

AVATAR_SIZE = 256
MAX_AVATAR_BYTES = 6 * 1024 * 1024
ALLOWED_AVATAR_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


def _cfg():
    return current_app.config["MV_CONFIG"]


def _conn():
    return db.connect(_cfg().db_path)


def _json_object() -> dict[str, Any]:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400, description="Account settings must be a JSON object")
    return data


def _secure() -> bool:
    return request.is_secure


def _session_token() -> str:
    """This app's own session token.

    Each face keeps its session under its own cookie name, so reading the
    shared one here would act on the *other* app's session: signing out of the
    console would end the gallery session and leave the console signed in.
    """
    token = request.cookies.get(auth.cookie_name(_face()), "")
    if not token and _face() == "admin":
        token = request.cookies.get(auth.SESSION_COOKIE, "")
    return token


def _face() -> str:
    """The app face this request arrived on; new sessions are bound to it."""
    return current_app.config.get("NINAIVU_FACE", "home")


def _avatar_dir() -> Path:
    path = _cfg().state_dir / "avatars"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# First run
# ---------------------------------------------------------------------------

@accounts.get("/api/auth/state")
def auth_state():
    """What the entry screen needs before anyone has signed in."""
    conn = _conn()
    cfg = _cfg()
    user = current_user()
    face = current_app.config.get("NINAIVU_FACE", "home")
    scheme = current_app.config.get("MV_SCHEME") or ("https" if cfg.port == 443 else "http")
    hostnames = current_app.config.get("MV_HOSTNAMES", {})

    needs_setup = auth.needs_setup(conn)
    payload = {
        "setup_required": needs_setup,
        "setup_code_required": needs_setup and not _setup_is_local(),
        "open_browsing": cfg.open_browsing,
        "user": user.public(),
        "signed_in": user.id != 0,
        "app_name": current_app.config.get("APP_NAME", "Ninaivu"),
        "house_name": house_name(cfg),
        "home_name": home_name_for(user, cfg),
        "face": face,
        "home_port": cfg.port,
        "admin_port": cfg.admin_port,
        "scheme": scheme,
        "hostnames": hostnames,
    }
    if face == "home":
        payload["profiles"] = [_picker_entry(p) for p in auth.pickable_profiles(conn)]
    # So a page opened on a locked session shows the lock screen straight away.
    payload["lock"] = _lock_payload(conn)
    return jsonify(payload)


def _picker_entry(person: auth.User) -> dict[str, Any]:
    """The little that the picker needs — never more than a name and a face."""
    data = person.public()
    return {
        "id": data["id"],
        "name": data["name"],
        "role": data["role"],
        "role_label": data["role_label"],
        "avatar": data["avatar"],
        "color": data["color"],
        "initials": data["initials"],
        "locked": data["locked"],
        "kind": "pin" if person.has_pin else ("password" if person.has_password else "open"),
    }


def _setup_is_local() -> bool:
    """On this computer: no setup code needed.

    Only the address decides, whichever port the request came in on. The
    console used to count as "local" by itself, and the console listens on
    the whole home network when it is opened to it — so on a new install
    anybody at home who opened port 3000 before the owner did could make
    themselves the administrator, with no code asked.
    """
    return auth.is_loopback(request.remote_addr)


@accounts.post("/api/auth/setup")
def setup():
    """Create the first administrator. Refused once one exists.

    From another device on the family port it also takes the one-time setup
    code the server printed when it started, so the first person on the
    network to find a new library cannot make themselves its administrator.
    """
    conn = _conn()
    if not auth.needs_setup(conn):
        return jsonify({"error": "This library already has an administrator."}), 409

    data = _json_object()
    if not _setup_is_local():
        import hmac                                               # noqa: PLC0415
        # Guesses are limited like a password's: per address, and for the
        # whole server, so many addresses together cannot walk the code.
        if not reserve([(f"{request.remote_addr}|setup", 10, _WINDOW),
                        ("*|setup", 50, _WINDOW)]):
            return jsonify({"error": "Too many attempts. Wait a few minutes and try again."}), 429
        given = str(data.get("setup_code", "")).strip().upper()
        if not hmac.compare_digest(given, auth.setup_code()):
            return jsonify({
                "error": "Enter the setup code shown where Ninaivu started "
                         "(the window or the log), or create the administrator "
                         "on the computer Ninaivu runs on.",
                "setup_code_required": True}), 403
    try:
        user = auth.bootstrap_admin(
            conn,
            str(data.get("username", "")),
            str(data.get("password", "")),
            str(data.get("name", "")),
        )
    except (ValueError, PermissionError) as exc:
        return jsonify({"error": str(exc)}), 400

    token, expires = auth.start_session(conn, user.id, request.user_agent.string,
                                        face=_face())
    response = jsonify({"ok": True, "user": user.public()})
    return auth.set_session_cookie(response, token, expires, _secure(), face=_face())


# ---------------------------------------------------------------------------
# Sign in / out
# ---------------------------------------------------------------------------

_ATTEMPTS: dict[str, list[float]] = {}
_MAX_ATTEMPTS = 8
_WINDOW = 300.0

#: Sweep the whole table once a minute, and never let it grow past this.
#:
#: Entries used to be pruned only when their own key was touched again, so a
#: caller working through a list of usernames left one dead entry behind per
#: name and the table grew for the life of the process. It is small, but it is
#: attacker-controlled, and attacker-controlled growth with no ceiling is a
#: slow leak somebody else gets to choose the rate of.
_SWEEP_EVERY = 60.0
_MAX_KEYS = 4096
_last_sweep = 0.0
_attempts_lock = threading.Lock()

#: Held while an administrator is demoted, disabled or deleted, from the count
#: of administrators left to the write. See ``update_person``.
_ADMIN_COUNT_LOCK = threading.Lock()


#: Failed PINs for one profile, from anywhere, before it pauses. The per-caller
#: limit alone let a 4-digit PIN be walked in a few days from one address, and
#: in hours from several. Pausing the profile for everybody is the price: a
#: tap-to-enter tile that says "wait a few minutes" beats one that gives in.
_PROFILE_MAX_ATTEMPTS = 20
_PROFILE_WINDOW = 1800.0


def _sweep(now: float) -> None:
    """Drop every key whose attempts have all aged out."""
    global _last_sweep
    if now - _last_sweep < _SWEEP_EVERY and len(_ATTEMPTS) < _MAX_KEYS:
        return
    _last_sweep = now
    longest = max(_WINDOW, _PROFILE_WINDOW)
    for key in [k for k, v in _ATTEMPTS.items()
                if not v or now - v[-1] >= longest]:
        _ATTEMPTS.pop(key, None)
    if len(_ATTEMPTS) >= _MAX_KEYS:
        # Still full of live entries: this is a broad attack rather than a
        # leak. Keep the keys with the most attempts on them, because those are
        # the ones a limit is currently doing work for. "Most recent" was the
        # rule before, and it was the wrong way round: a paused profile's
        # counter was necessarily touched *before* the flood that filled the
        # table, so a few thousand throwaway keys — one attempt each — pushed
        # it out and the PIN guessing could start again. Now each throwaway
        # key has to carry as many attempts as the counter it is to displace,
        # and on the login form every one of those is a scrypt.
        for key in sorted(_ATTEMPTS,
                          key=lambda k: (len(_ATTEMPTS[k]), _ATTEMPTS[k][-1]))[:len(_ATTEMPTS) // 2]:
            _ATTEMPTS.pop(key, None)


def rate_limited(key: str, max_attempts: int = _MAX_ATTEMPTS,
                 window: float = _WINDOW) -> bool:
    now = time.time()
    with _attempts_lock:
        _sweep(now)
        tries = [t for t in _ATTEMPTS.get(key, []) if now - t < window]
        if tries:
            _ATTEMPTS[key] = tries
        else:
            _ATTEMPTS.pop(key, None)
        return len(tries) >= max_attempts


def record_attempt(key: str) -> None:
    with _attempts_lock:
        _ATTEMPTS.setdefault(key, []).append(time.time())


def reserve(limits: list[tuple[str, int, float]]) -> bool:
    """Count an attempt against every ``(key, max, window)`` at once, before
    the secret is checked — or refuse, counting nothing, if any is used up.

    Checking, then verifying, then recording let every guess already in
    flight through: sixty-four at once were all checked against a limit of
    twenty. Reserving under the lock closes that.
    """
    now = time.time()
    with _attempts_lock:
        _sweep(now)
        for key, most, window in limits:
            if len([t for t in _ATTEMPTS.get(key, []) if now - t < window]) >= most:
                return False
        for key, _most, window in limits:
            _ATTEMPTS[key] = [t for t in _ATTEMPTS.get(key, []) if now - t < window] + [now]
        return True


#: Profiles that used up their allowance, and how many times: each time it
#: happens the pause doubles (30 minutes, an hour, two hours … up to about a
#: day and a half), so a four-digit PIN cannot be worn down over days at the
#: steady pace a fixed window allows. A correct PIN clears it.
_LOCKOUTS: dict[str, tuple[int, float]] = {}


def locked_out(key: str) -> bool:
    with _attempts_lock:
        _strikes, until = _LOCKOUTS.get(key, (0, 0.0))
        return time.time() < until


def strike_if_spent(key: str, most: int) -> None:
    """After a wrong secret: when *key* has just used up its allowance, pause
    it for longer than last time."""
    with _attempts_lock:
        now = time.time()
        if len([t for t in _ATTEMPTS.get(key, []) if now - t < _PROFILE_WINDOW]) < most:
            return
        strikes = _LOCKOUTS.get(key, (0, 0.0))[0] + 1
        _LOCKOUTS[key] = (strikes, now + _PROFILE_WINDOW * (2 ** min(strikes - 1, 6)))
        _ATTEMPTS.pop(key, None)


def clear_lockout(key: str) -> None:
    with _attempts_lock:
        _LOCKOUTS.pop(key, None)


def release(key: str) -> None:
    """Give back one reserved attempt (the secret was right)."""
    with _attempts_lock:
        tries = _ATTEMPTS.get(key)
        if tries:
            tries.pop()
            if not tries:
                _ATTEMPTS.pop(key, None)


@accounts.post("/api/auth/login")
def login():
    data = _json_object()
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    key = f"{request.remote_addr}|{username.lower()}"
    # And one for the account wherever the guesses come from: a household
    # machine has as many IPv6 addresses as it likes, so a limit per address
    # alone was no limit on guessing one password.
    everywhere = f"*|user:{username.lower()}"

    if not reserve([(key, _MAX_ATTEMPTS, _WINDOW),
                    (everywhere, _PROFILE_MAX_ATTEMPTS, _PROFILE_WINDOW)]):
        return jsonify({
            "error": "Too many attempts. Wait a few minutes and try again."
        }), 429

    conn = _conn()
    user = auth.authenticate(conn, username, password)
    if user is None:
        auth.audit(conn, None, "login_failed", username[:60])
        return jsonify({"error": "That username and password don't match."}), 401

    if current_app.config.get("NINAIVU_FACE") == "admin" and not user.is_admin:
        return jsonify({
            "error": "This console is for administrators. "
                     "Use the family app to sign in.",
        }), 403

    release(everywhere)
    with _attempts_lock:
        _ATTEMPTS.pop(key, None)
    token, expires = auth.start_session(conn, user.id, request.user_agent.string,
                                        face=_face())
    auth.audit(conn, user.id, "login", user.username)
    response = jsonify({"ok": True, "user": user.public()})
    return auth.set_session_cookie(response, token, expires, _secure(), face=_face())


@home_accounts.get("/api/auth/profiles")
def profiles():
    """Faces for the picker. Admins are deliberately absent — they sign in
    on the console, not from a tap-to-enter list."""
    conn = _conn()
    return jsonify({
        "profiles": [_picker_entry(p) for p in auth.pickable_profiles(conn)],
        "open_browsing": _cfg().open_browsing,
    })


@home_accounts.post("/api/auth/enter")
def enter():
    """Sign in by picking a profile, with a PIN only when one is set."""
    data = _json_object()
    raw_id = data.get("id", 0)
    try:
        user_id = int(raw_id)
    except (TypeError, ValueError, OverflowError):
        return jsonify({"error": "Pick a profile."}), 400
    if isinstance(raw_id, (bool, float)) or not 0 < user_id <= 2**63 - 1:
        return jsonify({"error": "Pick a profile."}), 400

    conn = _conn()
    key = f"{request.remote_addr}|profile:{user_id}"
    everywhere = f"*|profile:{user_id}"
    target = auth.get_user(conn, user_id)
    # Only a profile that exists is worth a counter. An id that is nobody's
    # costs one lookup and no scrypt, so counting it let a caller mint
    # limiter keys for free — and fill the table with them.
    if target is not None and (locked_out(everywhere) or not reserve([
            (key, _MAX_ATTEMPTS, _WINDOW),
            (everywhere, _PROFILE_MAX_ATTEMPTS, _PROFILE_WINDOW)])):
        return jsonify({
            "error": "Too many attempts. Wait a while and try again."
        }), 429

    user = auth.enter_profile(conn, user_id, str(data.get("secret", "")))
    if user is None:
        if target is not None:
            strike_if_spent(everywhere, _PROFILE_MAX_ATTEMPTS)
        if target is not None and target.role == auth.ROLE_ADMIN:
            return jsonify({
                "error": "Administrators sign in on the admin console.",
            }), 403
        return jsonify({"error": "That PIN isn't right."}), 401

    release(everywhere)
    clear_lockout(everywhere)
    with _attempts_lock:
        _ATTEMPTS.pop(key, None)
    token, expires = auth.start_session(conn, user.id, request.user_agent.string,
                                        face=_face())
    auth.audit(conn, user.id, "enter_profile", user.username)
    response = jsonify({"ok": True, "user": user.public()})
    return auth.set_session_cookie(response, token, expires, _secure(), face=_face())


# ---------------------------------------------------------------------------
# The screen lock (see ninaivu/server/auth.py)
# ---------------------------------------------------------------------------

#: Wrong unlock attempts before the session is ended outright. Somebody who
#: cannot unlock a screen has to sign in again, which the picker and the login
#: form rate-limit on their own terms.
_UNLOCK_MAX_ATTEMPTS = 8
_UNLOCK_WINDOW = 900.0


def _lock_after() -> float:
    return max(0, int(getattr(_cfg(), "lock_after_minutes", 0) or 0)) * 60.0


def _lock_payload(conn) -> dict[str, Any]:
    user = current_user()
    if user.id == 0:
        return {"signed_in": False, "locked": False, "lock_after": 0}
    state = auth.lock_state(conn, _session_token(), _lock_after()) or {
        "locked": False, "lock_after": _lock_after(), "locks_in": None}
    return {"signed_in": True, **state, "unlock_with": auth.unlock_with(user),
            "user": user.public()}


@accounts.get("/api/auth/session")
def session_state():
    """Locked or not, and how long until it would be — without counting as
    anybody being there."""
    return jsonify(_lock_payload(_conn()))


@accounts.post("/api/auth/active")
def session_active():
    """The page saw somebody: a tap, a key, a video playing."""
    if current_user().id == 0:
        return jsonify({"error": "Sign in to do that.", "status": 401}), 401
    conn = _conn()
    if not auth.mark_active(conn, _session_token()):
        return jsonify({**_lock_payload(conn), "status": 423}), 423
    return jsonify(_lock_payload(conn))


@accounts.post("/api/auth/lock")
def session_lock():
    """Lock now, from the page's own clock or somebody asking."""
    if current_user().id == 0:
        return jsonify({"error": "Sign in to do that.", "status": 401}), 401
    conn = _conn()
    auth.lock_session(conn, _session_token())
    return jsonify(_lock_payload(conn))


@accounts.post("/api/auth/unlock")
def session_unlock():
    """Prove it is still the same person: their PIN or password."""
    user = current_user()
    if user.id == 0:
        return jsonify({"error": "Sign in to do that.", "status": 401}), 401
    data = _json_object()
    conn = _conn()
    token = _session_token()
    key = f"unlock|{user.id}|{request.remote_addr}"
    if rate_limited(key, _UNLOCK_MAX_ATTEMPTS, _UNLOCK_WINDOW):
        return _unlock_gives_up(conn, token, user)
    if not auth.verify_unlock(conn, user, str(data.get("secret", ""))):
        record_attempt(key)
        auth.audit(conn, user.id, "unlock_failed", _face())
        if rate_limited(key, _UNLOCK_MAX_ATTEMPTS, _UNLOCK_WINDOW):
            return _unlock_gives_up(conn, token, user)
        kind = auth.unlock_with(user)
        return jsonify({"error": "That PIN isn't right." if kind == "pin"
                        else "That password isn't right.", "status": 403}), 403
    with _attempts_lock:
        _ATTEMPTS.pop(key, None)
    auth.unlock_session(conn, token)
    return jsonify(_lock_payload(conn))


def _unlock_gives_up(conn, token: str, user):
    """Too many wrong answers at a locked screen: sign that session out."""
    auth.end_session(conn, token)
    auth.audit(conn, user.id, "unlock_signed_out", _face())
    response = jsonify({"error": "Too many wrong attempts, so you have been "
                                 "signed out. Sign in again.", "status": 401})
    response.status_code = 401
    return auth.clear_session_cookie(response, face=_face())


@accounts.post("/api/auth/logout")
def logout():
    conn = _conn()
    auth.end_session(conn, _session_token())
    return auth.clear_session_cookie(jsonify({"ok": True}), face=_face())


# ---------------------------------------------------------------------------
# Own profile
# ---------------------------------------------------------------------------

@accounts.get("/api/me")
def me():
    return jsonify(current_user().public())


@accounts.post("/api/me")
def update_me():
    """Anyone signed in may change their own display name and colour."""
    user = current_user()
    if user.id == 0:
        return jsonify({"error": "Sign in first."}), 401

    data = _json_object()
    conn = _conn()
    fields: dict[str, Any] = {}

    if "name" in data:
        name = str(data["name"]).strip()[:60]
        if not name:
            return jsonify({"error": "A display name can't be empty."}), 400
        fields["display_name"] = name
    if "color" in data:
        color = str(data["color"]).strip()
        # Hex digits, not merely the right length: "#zzzzzz" was saved, and a
        # colour every page then fails to draw is a broken profile tile.
        if color and not _HEX_COLOUR.match(color):
            return jsonify({"error": "Colour must be a hex value like #4aa8ff."}), 400
        fields["color"] = color or None
    if "home_label" in data:
        # What this person calls the home. It is shown to them and to nobody
        # else, so there is nothing to moderate — but guests are excluded: a
        # guest tile is usually shared and short-lived, and letting one rename
        # the house for the next visitor is a small nuisance with no upside.
        if current_user().is_guest:
            return jsonify({
                "error": "Guests can't rename the home."}), 403
        fields["home_label"] = clean_home_name(data["home_label"]) or None

    if "language" in data:
        # The language follows the person, not the browser: chosen once, it
        # is the same on the phone, the tablet and the television. Empty
        # means "whatever this device asks for", as before.
        language = str(data["language"] or "").strip().lower()[:8]
        if language and not re.fullmatch(r"[a-z]{2,3}(-[a-z0-9]{2,8})?", language):
            return jsonify({"error": "language is a code like en or ta."}), 400
        fields["language"] = language or None

    if fields:
        auth.update_profile(conn, user.id, **fields)
    return jsonify(auth.get_user(conn, user.id).public())


@accounts.post("/api/me/password")
def change_password():
    user = current_user()
    if user.id == 0:
        return jsonify({"error": "Sign in first."}), 401

    data = _json_object()
    conn = _conn()
    current = str(data.get("current", ""))
    new = str(data.get("password", ""))

    # A profile created by an admin starts with a temporary password and is
    # allowed to set its own without repeating it.
    if not user.must_change:
        # Limited like every other place a password is checked. This one had
        # no limit, so a session left open on a shared machine could be used
        # to try passwords until one came back right — and learn it.
        key = f"password|{user.id}"
        if rate_limited(key):
            return jsonify({
                "error": "Too many attempts. Wait a few minutes and try again."
            }), 429
        if not auth.reauthenticate(conn, user.id, current):
            record_attempt(key)
            return jsonify({"error": "Your current password isn't right."}), 403
        with _attempts_lock:
            _ATTEMPTS.pop(key, None)
    try:
        auth.set_password(conn, user.id, new)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    # Changing a password signs other devices out, but not this one.
    token = _session_token()
    conn.execute("DELETE FROM sessions WHERE user_id=? AND token != ?",
                 (user.id, token))
    conn.commit()
    auth.audit(conn, user.id, "password_changed", user.username)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Avatars — every role may set their own
# ---------------------------------------------------------------------------

@accounts.post("/api/me/avatar")
def upload_avatar():
    user = current_user()
    if user.id == 0:
        return jsonify({"error": "Sign in to set a picture."}), 401

    upload = request.files.get("avatar")
    if upload is None or not upload.filename:
        return jsonify({"error": "No image was sent."}), 400
    if upload.mimetype not in ALLOWED_AVATAR_TYPES:
        return jsonify({
            "error": "Use a JPEG, PNG, WebP or GIF image."
        }), 400

    raw = upload.read(MAX_AVATAR_BYTES + 1)
    if len(raw) > MAX_AVATAR_BYTES:
        return jsonify({"error": "That image is larger than 6 MB."}), 413

    # Re-encode rather than storing what was uploaded: it normalises the
    # format, strips metadata, and guarantees the bytes we serve are an image.
    from ..media.safe_image import open_untrusted   # noqa: PLC0415
    try:
        with open_untrusted(raw) as img:
            img.load()
            square = _centre_crop(img.convert("RGB"), AVATAR_SIZE)
    except Exception:
        return jsonify({"error": "That file isn't a readable image."}), 400

    name = f"{user.id}_{int(time.time())}.webp"
    path = _avatar_dir() / name
    square.save(path, "WEBP", quality=88, method=4)

    conn = _conn()
    previous = auth.get_user(conn, user.id).avatar
    auth.update_profile(conn, user.id, avatar=name)
    if previous and previous != name:
        try:
            (_avatar_dir() / previous).unlink(missing_ok=True)
        except OSError:
            pass

    fresh = auth.get_user(conn, user.id)
    return jsonify({
        "ok": True,
        "avatar": f"/api/avatar/{user.id}?v={int(time.time())}",
        "user": fresh.public(),
    })


@accounts.delete("/api/me/avatar")
def delete_avatar():
    user = current_user()
    if user.id == 0:
        return jsonify({"error": "Sign in first."}), 401
    conn = _conn()
    existing = auth.get_user(conn, user.id).avatar
    auth.update_profile(conn, user.id, avatar=None)
    if existing:
        try:
            (_avatar_dir() / existing).unlink(missing_ok=True)
        except OSError:
            pass
    return jsonify({"ok": True, "user": auth.get_user(conn, user.id).public()})


def _centre_crop(img: Image.Image, size: int) -> Image.Image:
    width, height = img.size
    edge = min(width, height)
    left = (width - edge) // 2
    top = (height - edge) // 2
    return img.crop((left, top, left + edge, top + edge)).resize(
        (size, size), Image.Resampling.LANCZOS
    )


@accounts.get("/api/avatar/<int:user_id>")
def avatar(user_id: int):
    """Profile pictures are visible to anyone who can see the profile.

    "Can see the profile" is the load-bearing half. Anyone signed in may see
    the household; an anonymous visitor on the home page may see only the
    profiles the picker itself offers, which deliberately excludes admins so
    the console does not advertise who runs it. Without that check this route
    answers 200 for a real profile and 404 for a made-up id — enough to
    enumerate the household one number at a time.
    """
    conn = _conn()
    user = auth.get_user(conn, user_id)
    if user is not None and current_user().id == 0:
        if user.id not in {p.id for p in auth.pickable_profiles(conn)}:
            user = None
    if user is None or not user.avatar:
        return jsonify({"error": "No picture"}), 404
    path = (_avatar_dir() / user.avatar).resolve()
    if not path.is_file() or _avatar_dir().resolve() not in path.parents:
        return jsonify({"error": "No picture"}), 404
    # Avatars are always normalised to WebP on upload.  State that explicitly
    # rather than relying on the operating system's MIME registry: Windows
    # may otherwise serve them as application/octet-stream, and Ninaivu's
    # ``nosniff`` header correctly stops a browser from guessing around it.
    response = send_file(
        path, conditional=True, mimetype="image/webp", max_age=86400)
    response.headers["Cache-Control"] = "private, max-age=86400"
    return response


# ---------------------------------------------------------------------------
# People — admin only
# ---------------------------------------------------------------------------

def _person_payload(conn, user: auth.User) -> dict[str, Any]:
    root = _cfg().active_root or ""
    payload = user.public()
    payload["username"] = user.username
    payload["last_login"] = user.last_login
    payload["created_at"] = user.created_at
    payload["scope"] = user.scope or None
    payload["library"] = user.library or None
    payload["has_pin"] = user.has_pin
    payload["has_password"] = user.has_password
    payload["entry"] = ("password" if user.has_password
                        else "pin" if user.has_pin else "open")
    payload["sessions"] = conn.execute(
        "SELECT COUNT(*) n FROM sessions WHERE user_id=? AND expires_at > ?",
        (user.id, time.time()),
    ).fetchone()["n"]
    # What deleting this profile would cost — shown in the confirmation.
    payload["favorites"] = conn.execute(
        "SELECT COUNT(*) n FROM user_assets WHERE user_id=? AND favorite=1",
        (user.id,),
    ).fetchone()["n"]
    # How much they can actually see, resolved the same way a request would.
    libraries = _cfg().libraries or ([root] if root else [])
    roots, sub = auth.resolve_library(user.assigned_library, libraries)
    if user.is_admin:
        roots, sub = libraries, None
    if roots:
        clause, params = db.roots_clause("a", roots)
        # Their visibility ceiling belongs in this count. Without it the
        # console said a family member could see nine things when the query
        # their browser actually runs returns eight — and an admin checking a
        # profile before handing out a login would be checking it against a
        # number no request ever produces.
        sql = (f"SELECT COUNT(*) n FROM assets a WHERE {clause} "
               f"AND a.trashed=0 "
               f"AND {db.visibility_clause('a', user.max_visibility)}")
        args = [*params, int(user.max_visibility)]
        if sub:
            sub_sql, sub_params = db.folder_clause("a.folder", sub)
            sql += f" AND {sub_sql}"
            args += sub_params
        # One full count per profile, and the page lists every profile:
        # remembered until the library changes, like the gallery's own counts.
        payload["visible_count"] = db.cached_aggregate(
            conn, ("visible_count", sql, tuple(args)),
            lambda: conn.execute(sql, args).fetchone()["n"])
    else:
        payload["visible_count"] = 0
    return payload


@admin_accounts.get("/api/people")
@require_admin
def people():
    conn = _conn()
    return jsonify({
        "people": [_person_payload(conn, u) for u in auth.list_users(conn)],
        "roles": [{"value": r, "label": ROLE_LABELS[r]} for r in ROLES],
    })


@admin_accounts.post("/api/people")
@require_admin
def create_person():
    data = _json_object()
    conn = _conn()
    admin = current_user()

    role = str(data.get("role", ROLE_FAMILY))
    if role not in ROLES:
        return jsonify({"error": "Unknown role."}), 400

    try:
        user = auth.create_user(
            conn,
            str(data.get("username", "")),
            str(data.get("password", "")),
            display_name=str(data.get("name", "")),
            role=role,
            created_by=admin.id,
            must_change=bool(data.get("must_change", True)) and bool(data.get("password")),
            scope=data.get("scope") or None,
            pin=str(data.get("pin") or "") or None,
            library=str(data.get("library") or "") or None,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    auth.audit(conn, admin.id, "create_person", f"{user.username} ({role})")
    return jsonify({"ok": True, "person": _person_payload(conn, user)})


@admin_accounts.post("/api/people/<int:user_id>")
@require_admin
def update_person(user_id: int):
    data = _json_object()
    conn = _conn()
    admin = current_user()
    target = auth.get_user(conn, user_id)
    if target is None:
        return jsonify({"error": "No such profile."}), 404

    # Every check runs before anything is written. Applying the role and then
    # refusing the PIN in the same request answered 400 with the role already
    # changed — the console said it failed, and half of it had not.
    fields: dict[str, Any] = {}
    last_admin = (target.role == ROLE_ADMIN and target.active
                  and auth.count_active_admins(conn) <= 1)

    if "role" in data:
        role = str(data["role"])
        if role not in ROLES:
            return jsonify({"error": "Unknown role."}), 400
        # Never let the last administrator demote themselves out of existence.
        if role != ROLE_ADMIN and last_admin:
            return jsonify({
                "error": "This is the only administrator — promote someone else first."
            }), 409
        # An administrator signs in on the console, and the console only takes
        # a password. Promoting a tap-to-enter or PIN-only profile made an
        # administrator nobody could ever sign in as.
        if role == ROLE_ADMIN and target.role != ROLE_ADMIN \
                and not target.has_password and not data.get("password"):
            return jsonify({
                "error": f"{target.display_name} has no password, and an "
                         f"administrator signs in with one. Give them a "
                         f"password when making them an administrator.",
            }), 400
        if role != target.role:
            fields["role"] = role

    if "name" in data:
        name = str(data["name"]).strip()[:60]
        if name:
            fields["display_name"] = name

    if "scope" in data:
        # Admins are never scoped; setting one on an admin is meaningless.
        fields["scope"] = auth.normalise_scope(data["scope"])

    if "library" in data:
        assignment = auth.normalise_library(data["library"])
        if assignment:
            libraries = _cfg().libraries
            roots, _ = auth.resolve_library(assignment, libraries)
            if not roots:
                return jsonify({
                    "error": f"“{assignment}” is not inside any library folder. "
                             f"Add it on the Library tab first."
                }), 400
            # An explicit folder replaces the old relative scope entirely.
            fields["scope"] = None
        fields["library"] = assignment

    active: bool | None = None
    if "active" in data:
        active = bool(data["active"])
        if not active and target.role == ROLE_ADMIN and last_admin:
            return jsonify({
                "error": "You can't disable the only administrator."
            }), 409
        if not active and target.id == admin.id:
            return jsonify({"error": "You can't disable your own profile."}), 409

    pin: str | None = None
    if "pin" in data:
        pin = str(data["pin"] or "") or None
        if pin and (problem := auth.pin_problem(pin)):
            return jsonify({"error": problem}), 400

    password = str(data.get("password") or "")
    if password and (problem := auth.password_problem(password)):
        return jsonify({"error": problem}), 400

    # Nothing below can be refused — except by the re-check under the lock.
    # ``last_admin`` above was read at the top of the request; two requests
    # taking the last two administrators away at once both read "two left"
    # and both went through, and a household with no administrator reopens
    # ``/api/auth/setup`` to whoever is on the network. So the change that
    # can lower the count is made with the count looked at again, and no
    # other such change happening in between.
    loses_an_admin = (target.role == ROLE_ADMIN and target.active
                      and (fields.get("role", ROLE_ADMIN) != ROLE_ADMIN or active is False))
    with _ADMIN_COUNT_LOCK:
        if loses_an_admin and auth.count_active_admins(conn) <= 1:
            return jsonify({
                "error": "This is the only administrator — promote someone else first."
            }), 409
        if fields:
            auth.update_profile(conn, user_id, **fields)
        if active is not None:
            auth.set_active(conn, user_id, active)
    if "role" in fields:
        auth.audit(conn, admin.id, "change_role",
                   f"{target.username}: {target.role} → {fields['role']}")
        if fields["role"] == ROLE_ADMIN:
            # Whatever let them in so far — a tap, a PIN — is not what an
            # administrator signs in with, so it does not carry over.
            auth.end_all_sessions(conn, user_id)
    if "library" in fields:
        auth.audit(conn, admin.id, "set_library",
                   f"{target.username}: {fields['library'] or 'every library folder'}")

    if active is not None:
        auth.audit(conn, admin.id,
                   "enable_person" if active else "disable_person", target.username)

    if "pin" in data:
        auth.set_pin(conn, user_id, pin)
        auth.audit(conn, admin.id,
                   "set_pin" if pin else "clear_pin", target.username)

    if password:
        auth.set_password(conn, user_id, password)
        conn.execute("UPDATE users SET must_change=1 WHERE id=?", (user_id,))
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        conn.commit()
        auth.audit(conn, admin.id, "reset_password", target.username)

    fresh = auth.get_user(conn, user_id)
    return jsonify({"ok": True, "person": _person_payload(conn, fresh)})


@admin_accounts.delete("/api/people/<int:user_id>")
@require_admin
def delete_person(user_id: int):
    """Remove a profile for good.

    Guarded so an admin cannot lock themselves — or the household — out:
    you may not delete yourself, and you may not delete the last working
    administrator. Everything else is fair game, and the library's media is
    never touched.
    """
    conn = _conn()
    admin = current_user()
    target = auth.get_user(conn, user_id)
    if target is None:
        return jsonify({"error": "No such profile."}), 404

    if target.id == admin.id:
        return jsonify({
            "error": "You can't delete the profile you're signed in with. "
                     "Ask another administrator to remove it."
        }), 409

    with _ADMIN_COUNT_LOCK:         # the count and the change, with nothing between
        if target.role == auth.ROLE_ADMIN and target.active \
                and auth.count_active_admins(conn) <= 1:
            return jsonify({
                "error": "This is the only administrator — make someone else an "
                         "admin first."
            }), 409

        removed = auth.delete_user(conn, user_id)

    # The picture lives on disk, not in the database.
    if removed["avatar"]:
        with suppress(OSError):
            (_avatar_dir() / removed["avatar"]).unlink(missing_ok=True)

    auth.audit(conn, admin.id, "delete_person",
               f"{removed['username']} ({removed['role']})")
    return jsonify({
        "ok": True,
        "removed": {k: v for k, v in removed.items() if k != "avatar"},
    })


@admin_accounts.post("/api/people/<int:user_id>/signout")
@require_admin
def signout_person(user_id: int):
    conn = _conn()
    count = auth.end_all_sessions(conn, user_id)
    auth.audit(conn, current_user().id, "signout_person", str(user_id))
    return jsonify({"ok": True, "sessions_ended": count})


@admin_accounts.get("/api/people/folders")
@require_admin
def scope_folders():
    """Folder tree of the active library, for assigning a profile's scope."""
    conn = _conn()
    root = _cfg().active_root
    if not root:
        return jsonify({"folders": []})
    rows = conn.execute(
        "SELECT folder, COUNT(*) n FROM assets WHERE root=? AND trashed=0 "
        "GROUP BY folder ORDER BY folder",
        (root,),
    ).fetchall()

    # Roll leaf counts up into every ancestor so parents show a useful total.
    totals: dict[str, int] = {}
    for row in rows:
        parts = [p for p in (row["folder"] or "").split("/") if p]
        for depth in range(1, len(parts) + 1):
            prefix = "/".join(parts[:depth])
            totals[prefix] = totals.get(prefix, 0) + row["n"]
    return jsonify({
        "folders": [
            {"path": path, "count": count, "depth": path.count("/")}
            for path, count in sorted(totals.items())
        ],
    })


@admin_accounts.get("/api/audit")
@require_admin
def audit_log():
    conn = _conn()
    rows = conn.execute(
        "SELECT a.at, a.action, a.detail, u.display_name, u.username "
        "FROM audit a LEFT JOIN users u ON u.id = a.user_id "
        "ORDER BY a.id DESC LIMIT 100"
    ).fetchall()
    return jsonify({"entries": [dict(r) for r in rows]})
