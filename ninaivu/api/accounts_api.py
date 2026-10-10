"""Authentication, profiles and people management endpoints.

Kept separate from :mod:`ninaivu.api` so the permission surface is easy to read
in one place: every route here either belongs to the signed-in user or is
wrapped in ``@require_admin``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from flask import Blueprint, abort, current_app, has_app_context, jsonify, request, send_file
from PIL import Image

from ._body import json_body, refuse_oversized_json
from ..server import auth, hosts
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

log = logging.getLogger(__name__)

_HEX_COLOUR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

AVATAR_SIZE = 256
MAX_AVATAR_BYTES = 6 * 1024 * 1024
ALLOWED_AVATAR_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


def _cfg():
    return current_app.config["MV_CONFIG"]


def _conn():
    return db.connect(_cfg().db_path)


def _json_object() -> dict[str, Any]:
    refuse_oversized_json()
    data = json_body()
    if not isinstance(data, dict):
        abort(400, description="Account settings must be a JSON object")
    return data


def _secure() -> bool:
    """Whether the session cookie goes out marked ``Secure``.

    ``request.is_secure`` alone used to decide, and behind the shipped nginx
    and Caddy configurations with ``trusted_proxies`` at 0 it is never true:
    the proxy speaks plain HTTP to Ninaivu and its forwarded-proto header is
    dropped unread. So the cookie went out without the flag, and the proxy's
    own HTTP-to-HTTPS redirect — the first thing a browser that typed the
    bare name meets — carried the session across the internet in the clear.

    The names that are only ever served over HTTPS get the flag whatever this
    connection says: the household's public name (``remote_hostname``) and
    Tailscale's ``.ts.net``, the same two the HSTS header is sent for
    (``ninaivu/__init__.py``). Never a plain-HTTP address or ``.local`` name at
    home: a browser drops a ``Secure`` cookie set over HTTP, and nobody could
    sign in. Every face mints its cookie through here, so the console and the
    family app make the same decision.
    """
    if request.is_secure:
        return True
    host = hosts._name(request.host)
    public = hosts._name(str(getattr(_cfg(), "remote_hostname", "") or ""))
    return bool(host) and (host == public or host.endswith(".ts.net"))


def _session_token() -> str:
    """This app's own session token.

    Each face keeps its session under its own cookie name, so reading the
    shared one here would act on the *other* app's session: signing out of the
    console would end the gallery session and leave the console signed in.
    The console still reads a session an admin opened before it had a cookie
    of its own under the shared name — but only one the console minted, never
    the gallery session that name now normally holds.
    """
    token = request.cookies.get(auth.cookie_name(_face()), "")
    if not token and _legacy_console_cookie():
        token = request.cookies.get(auth.SESSION_COOKIE, "")
    return token


def _legacy_console_cookie() -> bool:
    """On the console: does the shared cookie hold a *console* session?"""
    if _face() != "admin" or request.cookies.get(auth.cookie_name("admin")):
        return False
    legacy = request.cookies.get(auth.SESSION_COOKIE, "")
    return bool(legacy) and auth.session_face(_conn(), legacy) == "admin"


def _end_own_session(conn, response):
    """End this face's session, and clear this face's cookie — nothing else.

    A token minted by the other face is left alone even if it reached here:
    signing out of the console must not end the gallery's session.
    """
    legacy = _legacy_console_cookie()
    token = _session_token()
    if token and auth.session_face(conn, token) in (None, _face()):
        auth.end_session(conn, token)
    return auth.clear_session_cookie(response, face=_face(), shared_too=legacy)


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


def _from_the_internet() -> bool:
    from ..server import remote                                  # noqa: PLC0415
    return remote.from_the_internet(_cfg(), request)


def _entry_kind(person: auth.User) -> str:
    """What the picker asks for. An administrator's tile always takes the
    password, whatever PIN the account may also carry."""
    if person.is_admin:
        return "password"
    return "pin" if person.has_pin else ("password" if person.has_password else "open")


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
        "kind": _entry_kind(person),
    }


def _setup_is_local() -> bool:
    """On this computer: no setup code needed.

    Only the address decides, whichever port the request came in on. The
    console used to count as "local" by itself, and the console listens on
    the whole home network when it is opened to it — so on a new install
    anybody at home who opened port 3000 before the owner did could make
    themselves the administrator, with no code asked.

    And loopback alone is not "this computer" either: behind Tailscale Funnel,
    Caddy or a Cloudflare tunnel every visitor arrives from 127.0.0.1, so the
    first stranger to find a new library skipped the code. A forwarded request
    is somebody else's unless ``trusted_proxies`` says a proxy has already put
    the real address in its place — the same rule as stopping the server.

    This computer's own network addresses count too: the Control Panel and
    the tray open ``ninaivu.local``, which arrives from the computer's LAN
    address, and the owner sitting at it was asked for a code printed in a
    window that panel-started servers do not have.
    """
    return auth.request_is_from_this_computer(int(getattr(_cfg(), "trusted_proxies", 0) or 0))


@accounts.post("/api/auth/setup")
def setup():
    """Create the first administrator. Refused once one exists.

    From another device on the family port it also takes the one-time setup
    code the server printed when it started, so the first person on the
    network to find a new library cannot make themselves its administrator.
    """
    conn = _conn()
    if not auth.needs_setup(conn):
        return jsonify({"error": auth.ALREADY_SET_UP}), 409

    data = _json_object()
    if not _setup_is_local():
        import hmac                                               # noqa: PLC0415
        # Guesses are limited like a password's: per address, and for the
        # whole server, so many addresses together cannot walk the code.
        if not reserve([(f"{request.remote_addr}|setup", 10, _WINDOW),
                        ("*|setup", 50, _WINDOW)]):
            return jsonify({"error": "Too many attempts. Wait a few minutes and try again."}), 429
        given = str(data.get("setup_code", "")).strip().upper()
        # As bytes: compare_digest refuses a str with anything outside ASCII
        # in it, and a code typed with a stray "é" answered 500, not 403.
        state_dir = getattr(_cfg(), "state_dir", None)
        if not hmac.compare_digest(given.encode("utf-8"),
                                   auth.setup_code(state_dir).encode("utf-8")):
            # The file's name, not its path: the path names the account, and
            # this answer goes to a device nobody has vouched for yet.
            return jsonify({
                "error": "Enter the setup code shown where Ninaivu started "
                         f"(the window, the log, or {auth.SETUP_CODE_FILE} in "
                         "Ninaivu's state folder), or create the administrator "
                         "on the computer Ninaivu runs on.",
                "setup_code_required": True}), 403
    try:
        user = auth.bootstrap_admin(
            conn,
            str(data.get("username", "")),
            str(data.get("password", "")),
            str(data.get("name", "")),
        )
    except PermissionError as exc:
        # Two setup requests at once: the check above let both through, and
        # ``bootstrap_admin`` turned the second away. The same answer as if it
        # had arrived a moment later.
        return jsonify({"error": str(exc)}), 409
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    auth.forget_setup_code(getattr(_cfg(), "state_dir", None))

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

#: The pause after an allowance is used up doubles each time, up to this
#: (``strike_if_spent``): thirty minutes, an hour, two … about a day and a
#: half. A key left alone for this long after its pause ended starts again
#: from the first pause. That forgives nothing a guesser could use — at the
#: cap they get twenty guesses a day and a half anyway, and waiting it out to
#: start over buys the same rate — and it is what lets made-up names be
#: paused like real ones (``login``) without keeping every one for ever.
_LONGEST_PAUSE = _PROFILE_WINDOW * 2 ** 6

#: Failed sign-ins with a name that is nobody's go to the activity log at
#: most this often, from everywhere together. A line per miss is the right
#: record for a real name — it is how an administrator sees their own account
#: being guessed at — but a made-up name costs the caller nothing to invent,
#: and the internet was writing the log at whatever rate it liked. Twenty
#: lines a half hour still says "somebody is guessing"; the Activity page
#: shows the last hundred.
_UNKNOWN_NAME_LOG = ("audit|login_failed:unknown", 20, _PROFILE_WINDOW)


def _sweep(now: float) -> None:
    """Drop every key whose attempts have all aged out, and every pause that
    ended long enough ago to be forgotten."""
    global _last_sweep
    if (now - _last_sweep < _SWEEP_EVERY and len(_ATTEMPTS) < _MAX_KEYS
            and len(_LOCKOUTS) < _MAX_KEYS):
        return
    _last_sweep = now
    longest = max(_WINDOW, _PROFILE_WINDOW)
    for key in [k for k, v in _ATTEMPTS.items()
                if not v or now - v[-1] >= longest]:
        _ATTEMPTS.pop(key, None)
    for key in [k for k, (_strikes, until) in _LOCKOUTS.items()
                if now - until >= _LONGEST_PAUSE]:
        _LOCKOUTS.pop(key, None)
    if len(_LOCKOUTS) >= _MAX_KEYS:
        # Full of pauses that have not faded: made-up names, twenty guesses
        # each. The fewest strikes go first, then the ones ending soonest. By
        # end time alone, a flood of fresh made-up names (each paused for the
        # full half hour) displaced a real profile's earlier pause and its
        # strike count, and its guessing started again from twenty; now each
        # throwaway name must earn as many pauses as the one it displaces.
        for key in sorted(_LOCKOUTS, key=lambda k: _LOCKOUTS[k])[:len(_LOCKOUTS) // 2]:
            _LOCKOUTS.pop(key, None)
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


#: Database files the saved allowances have been read back from (see below).
_LOADED: set[str] = set()


def _saved_limits():
    """The index the account-wide allowances are kept in: inside a request,
    and None outside one (the functions here are also called directly)."""
    if not has_app_context():
        return None
    try:
        return _conn()
    except Exception:                                   # noqa: BLE001 - kept in memory then
        return None


def _load_saved() -> None:
    """Read the allowances and pauses saved before a restart, once per index.

    Only the ones for an account, a profile or a link from anywhere (``*|``)
    are kept: a restart used to give somebody walking a PIN a fresh twenty
    guesses and forget how many times the profile had been paused. The ones
    per address last five minutes and are not worth a write per guess.
    """
    conn = _saved_limits()
    if conn is None:
        return
    path = str(_cfg().db_path)
    if path in _LOADED:
        return
    try:
        rows = conn.execute("SELECT key, attempts, strikes, until FROM auth_limits").fetchall()
    except sqlite3.Error:
        return
    now = time.time()
    longest = max(_WINDOW, _PROFILE_WINDOW)
    with _attempts_lock:
        for key, attempts, strikes, until in rows:
            try:
                tries = [float(t) for t in json.loads(attempts or "[]") if now - float(t) < longest]
            except (TypeError, ValueError):
                tries = []
            if tries and key not in _ATTEMPTS:
                _ATTEMPTS[key] = tries
            if strikes and key not in _LOCKOUTS:
                _LOCKOUTS[key] = (int(strikes), float(until))
        _LOADED.add(path)


def _save(*keys: str | None) -> None:
    """Write the account-wide allowances among *keys* back to the index."""
    keys_ = [k for k in keys if k and k.startswith("*|")]
    if not keys_:
        return
    conn = _saved_limits()
    if conn is None:
        return
    with _attempts_lock:
        now = time.time()
        state = {k: (list(_ATTEMPTS.get(k, [])), _LOCKOUTS.get(k, (0, 0.0))) for k in keys_}
    try:
        for key, (tries, (strikes, until)) in state.items():
            if tries or strikes:
                conn.execute(
                    "INSERT OR REPLACE INTO auth_limits(key, attempts, last_at, strikes, until) "
                    "VALUES(?,?,?,?,?)",
                    (key, json.dumps(tries), max(tries, default=0.0), int(strikes), float(until)))
            else:
                conn.execute("DELETE FROM auth_limits WHERE key=?", (key,))
        # Kept only while it counts: an allowance until its window has gone
        # by, a pause until it has faded (``_LONGEST_PAUSE``). Made-up names
        # are paused like real ones, so without this the table would keep a
        # row for every name anybody on the internet ever tried twenty times.
        conn.execute("DELETE FROM auth_limits WHERE last_at < ? AND until < ?",
                     (now - max(_WINDOW, _PROFILE_WINDOW), now - _LONGEST_PAUSE))
        conn.commit()
    except sqlite3.Error as exc:
        log.debug("sign-in limits not saved: %s", exc)
        try:
            conn.rollback()
        except sqlite3.Error:
            pass


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
    _load_saved()
    now = time.time()
    with _attempts_lock:
        _sweep(now)
        for key, most, window in limits:
            if len([t for t in _ATTEMPTS.get(key, []) if now - t < window]) >= most:
                return False
        for key, _most, window in limits:
            _ATTEMPTS[key] = [t for t in _ATTEMPTS.get(key, []) if now - t < window] + [now]
    _save(*(key for key, _most, _window in limits))
    return True


#: Profiles that used up their allowance, and how many times: each time it
#: happens the pause doubles (30 minutes, an hour, two hours … up to about a
#: day and a half), so a four-digit PIN cannot be worn down over days at the
#: steady pace a fixed window allows. A correct PIN clears it.
_LOCKOUTS: dict[str, tuple[int, float]] = {}


def locked_out(key: str) -> bool:
    _load_saved()
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
        _LOCKOUTS[key] = (strikes, now + min(_PROFILE_WINDOW * 2 ** (strikes - 1), _LONGEST_PAUSE))
        _ATTEMPTS.pop(key, None)
    _save(key)


def clear_lockout(key: str) -> None:
    with _attempts_lock:
        _LOCKOUTS.pop(key, None)
    _save(key)


def release(key: str) -> None:
    """Give back one reserved attempt (the secret was right)."""
    with _attempts_lock:
        tries = _ATTEMPTS.get(key)
        if tries:
            tries.pop()
            if not tries:
                _ATTEMPTS.pop(key, None)
    _save(key)


def reauthenticate_limited(conn, user_id: int, password: str) -> bool | None:
    """``auth.reauthenticate`` within one allowance per person: True if the
    password is right, False if wrong, None if refused unchecked.

    Every step-up prompt — changing the password, deleting photographs,
    emptying the recycle bin — spends the same ``password|<id>`` allowance.
    Deleting and emptying had none, so a session left open on a shared
    machine could be used to try passwords there without limit, and a limit
    on changing the password alone limited nothing. An empty password is the
    first step of the delete dialog asking for one and is not counted.
    """
    if not password:
        return False
    key = f"password|{int(user_id)}"
    if not reserve([(key, _MAX_ATTEMPTS, _WINDOW)]):
        return None
    if not auth.reauthenticate(conn, user_id, password):
        return False
    with _attempts_lock:
        _ATTEMPTS.pop(key, None)
    return True


def verify_pin_limited(conn, user_id: int, pin: str) -> bool | None:
    """``auth.verify_pin`` within the profile's own allowance: True if the PIN
    is right, False if wrong, None if refused unchecked.

    The allowance is the one the picker spends (``enter``), paused for longer
    each time it runs out, so a PIN cannot be walked here once the tile has
    paused, nor the tile's guesses topped up from here. Used where a profile
    that opens with a PIN has to prove who is at the keyboard (the handover
    claim); an empty PIN is not counted.
    """
    if not pin:
        return False
    everywhere = f"*|profile:{int(user_id)}{_zone()}"
    if _paused(everywhere) or not reserve([(everywhere, _PROFILE_MAX_ATTEMPTS, _PROFILE_WINDOW)]):
        return None
    if not auth.verify_pin(conn, user_id, pin):
        strike_if_spent(everywhere, _PROFILE_MAX_ATTEMPTS)
        return False
    release(everywhere)
    clear_lockout(everywhere)
    return True


def _zone() -> str:
    """The account-wide allowances are kept apart for guesses from the
    internet: spending one there (twenty addresses are cheap) must not pause
    the household's own sign-ins at home. Home keeps the plain key; the
    internet's has its own, which limits guessing from there just as well."""
    return "|net" if _from_the_internet() else ""


def _paused(everywhere: str) -> bool:
    """Is this account-wide allowance paused? One from the internet also
    stops when the household's own is spent or paused: the split (``_zone``)
    keeps the internet from locking the house out, not the other way round,
    and must not hand a guesser out there a fresh allowance of their own
    once the house's has run out."""
    if locked_out(everywhere):
        return True
    if not everywhere.endswith("|net"):
        return False
    home = everywhere[:-len("|net")]
    return locked_out(home) or rate_limited(home, _PROFILE_MAX_ATTEMPTS, _PROFILE_WINDOW)


def _log_failed_login(conn, username: str) -> None:
    """A line in the activity log for a miss: every one for a name that is
    somebody's, and for a name that is nobody's only within
    ``_UNKNOWN_NAME_LOG``. The lookup is made for both, so the answer takes
    the same time either way."""
    if auth.get_user_by_name(conn, username) is not None or reserve([_UNKNOWN_NAME_LOG]):
        auth.audit(conn, None, "login_failed", username[:60])


@accounts.post("/api/auth/login")
def login():
    data = _json_object()
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    key = f"{request.remote_addr}|{username.lower()}"
    # And one for the account wherever the guesses come from: a household
    # machine has as many IPv6 addresses as it likes, so a limit per address
    # alone was no limit on guessing one password.
    everywhere = f"*|user:{username.lower()}{_zone()}"

    too_many = jsonify({
        "error": "Too many attempts. Wait a few minutes and try again."
    }), 429
    # The account-wide allowance is anybody's to spend: a phone on the home
    # network guessing twenty times kept the administrator out of their own
    # console. So once it is spent, the computer Ninaivu runs on — really
    # this computer, not a proxy on it — still has its password checked, and
    # a right one still signs in, against its own per-address allowance.
    # Everywhere else is refused unchecked, as before: accepting a right
    # password from any address once the allowance is gone would tell a
    # guesser with many addresses which guess was right, and the allowance
    # would limit nothing. Both are reserved together, so an attempt refused
    # here counts against neither.
    # An account that used up its allowance is paused for longer each time
    # (``strike_if_spent``), as the administrator's tile on the picker always
    # was: a fixed window let anyone on the internet try about a thousand
    # passwords a day for as long as they liked.
    if not _paused(everywhere) and reserve([
            (key, _MAX_ATTEMPTS, _WINDOW),
            (everywhere, _PROFILE_MAX_ATTEMPTS, _PROFILE_WINDOW)]):
        counted = True
    elif _setup_is_local() and reserve([(key, _MAX_ATTEMPTS, _WINDOW)]):
        counted = False
    else:
        return too_many

    conn = _conn()
    user = auth.authenticate(conn, username, password)
    if user is None:
        _log_failed_login(conn, username)
        if not counted:
            return too_many
        # For a made-up name as much as a real one. Only real names used to
        # be paused for longer each time, made-up ones merely window-limited,
        # so from the second round on, when the 429 lifted said which kind of
        # name it was — and the 401 is worded alike precisely so that nothing
        # does. What leaving made-up names out saved the table is done
        # instead by forgetting pauses once they have faded (``_sweep``).
        strike_if_spent(everywhere, _PROFILE_MAX_ATTEMPTS)
        return jsonify({"error": "That username and password don't match."}), 401

    if current_app.config.get("NINAIVU_FACE") == "admin" and not user.is_admin:
        return jsonify({
            "error": "This console is for administrators. "
                     "Use the family app to sign in.",
        }), 403

    if counted:
        release(everywhere)
        clear_lockout(everywhere)
    with _attempts_lock:
        _ATTEMPTS.pop(key, None)
    token, expires = auth.start_session(conn, user.id, request.user_agent.string,
                                        face=_face())
    auth.audit(conn, user.id, "login", user.username)
    response = jsonify({"ok": True, "user": user.public()})
    return auth.set_session_cookie(response, token, expires, _secure(), face=_face())


@home_accounts.get("/api/auth/profiles")
def profiles():
    """Faces for the picker: every active profile. An administrator's tile is
    locked and asks for the password, so being listed opens nothing."""
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
    target = auth.get_user(conn, user_id)
    key = f"{request.remote_addr}|profile:{user_id}"
    everywhere = f"*|profile:{user_id}{_zone()}"
    if target is not None and target.is_admin:
        # An administrator's tile is the username login by another door, so
        # it spends the same allowances. Its own would have doubled the
        # guesses anyone gets at the one password that runs the house.
        key = f"{request.remote_addr}|{target.username.lower()}"
        everywhere = f"*|user:{target.username.lower()}{_zone()}"
    if target is not None and _entry_kind(target) == "open" and _from_the_internet():
        # A profile with nothing to type opens for whoever can reach the
        # page. Through a public tunnel or proxy that is anybody on the
        # internet who learns the address, and they could then download
        # every original the profile sees. At home, and over Tailscale or
        # WireGuard (devices the household let in), it still opens on a tap.
        # Refused before anything is counted: a tap that can never succeed
        # must not spend the allowance the household's own taps need.
        return jsonify({"error": "This profile has no PIN, so it opens only at home. "
                                 "Ask the administrator to give it a PIN to use it "
                                 "from outside."}), 403
    # Only a profile that exists is worth a counter. An id that is nobody's
    # costs one lookup and no scrypt, so counting it let a caller mint
    # limiter keys for free — and fill the table with them.
    if target is not None and (_paused(everywhere) or not reserve([
            (key, _MAX_ATTEMPTS, _WINDOW),
            (everywhere, _PROFILE_MAX_ATTEMPTS, _PROFILE_WINDOW)])):
        return jsonify({
            "error": "Too many attempts. Wait a while and try again."
        }), 429

    user = auth.enter_profile(conn, user_id, str(data.get("secret", "")))
    if user is None:
        if target is not None:
            strike_if_spent(everywhere, _PROFILE_MAX_ATTEMPTS)
        if target is not None and _entry_kind(target) == "password":
            return jsonify({"error": "That password isn't right."}), 401
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
    kind = auth.unlock_with(user)
    limits = [(key, _UNLOCK_MAX_ATTEMPTS, _UNLOCK_WINDOW)]
    # A PIN is the same four digits the picker asks for, so a guess here
    # counts against the same profile-wide allowance as a guess there —
    # otherwise a locked screen was a second, separately limited way to walk
    # the PIN. Reserved before the check, like the picker's: checking, then
    # verifying, then recording let every guess already in flight through.
    everywhere = f"*|profile:{user.id}{_zone()}" if kind == "pin" else None
    if everywhere:
        limits.append((everywhere, _PROFILE_MAX_ATTEMPTS, _PROFILE_WINDOW))
    if (everywhere and _paused(everywhere)) or not reserve(limits):
        return _unlock_gives_up(conn, token, user)
    if not auth.verify_unlock(conn, user, str(data.get("secret", ""))):
        auth.audit(conn, user.id, "unlock_failed", _face())
        if everywhere:
            strike_if_spent(everywhere, _PROFILE_MAX_ATTEMPTS)
        if rate_limited(key, _UNLOCK_MAX_ATTEMPTS, _UNLOCK_WINDOW) \
                or (everywhere and locked_out(everywhere)):
            return _unlock_gives_up(conn, token, user)
        return jsonify({"error": "That PIN isn't right." if kind == "pin"
                        else "That password isn't right.", "status": 403}), 403
    if everywhere:
        release(everywhere)
        clear_lockout(everywhere)
    with _attempts_lock:
        _ATTEMPTS.pop(key, None)
    auth.unlock_session(conn, token)
    return jsonify(_lock_payload(conn))


def _unlock_gives_up(conn, token: str, user):
    """Too many wrong answers at a locked screen: sign that session out."""
    auth.audit(conn, user.id, "unlock_signed_out", _face())
    response = jsonify({"error": "Too many wrong attempts, so you have been "
                                 "signed out. Sign in again.", "status": 401})
    response.status_code = 401
    return _end_own_session(conn, response)


@accounts.post("/api/auth/logout")
def logout():
    response = _end_own_session(_conn(), jsonify({"ok": True}))
    # Thumbnails are cached for a year (they never change), so on a shared
    # tablet the next person could read them out of the browser's cache.
    # Signing out empties it; browsers honour this over HTTPS.
    response.headers["Clear-Site-Data"] = '"cache"'
    return response


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
        #
        # Reserved before the check (see ``reserve``), and the same allowance
        # as every other step-up password prompt (``reauthenticate_limited``).
        answer = reauthenticate_limited(conn, user.id, current)
        if answer is None:
            return jsonify({
                "error": "Too many attempts. Wait a few minutes and try again."
            }), 429
        if not answer:
            return jsonify({"error": "Your current password isn't right."}), 403
    try:
        auth.set_password(conn, user.id, new)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    # Changing a password signs other devices out, but not this one.
    token = _session_token()
    conn.execute("DELETE FROM sessions WHERE user_id=? AND token != ?",
                 (user.id, auth.session_key(token)))
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
    from PIL import ImageOps                         # noqa: PLC0415

    from ..media.safe_image import open_untrusted   # noqa: PLC0415
    try:
        with open_untrusted(raw) as img:
            img.load()
            # A phone's photo is stored sideways with a tag saying so; the
            # re-encode below drops the tag, so the turn is made first.
            upright = ImageOps.exif_transpose(img) or img
            square = _centre_crop(upright.convert("RGB"), AVATAR_SIZE)
    except Exception:
        return jsonify({"error": "That file isn't a readable image."}), 400

    # A new file every time, never written over the old one, which a
    # browser may still be reading (that fails on Windows); milliseconds, so
    # two pictures within a second do not share a name. Written beside its
    # final name and moved into place, so nobody is served half a picture.
    name = f"{user.id}_{time.time_ns() // 1_000_000}.webp"
    path = _avatar_dir() / name
    partial = path.with_name(f".{name}.part")
    try:
        square.save(partial, "WEBP", quality=88, method=4)
        os.replace(partial, path)
    except OSError:
        with suppress(OSError):
            partial.unlink(missing_ok=True)
        return jsonify({"error": "The picture could not be saved."}), 500

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
        "avatar": fresh.public()["avatar"],
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
    the household; an anonymous visitor may see only the profiles the picker
    itself offers (everyone active, administrators' locked tiles included),
    never a disabled profile's. Without that check this route
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
            # Every configured library folder, on disk or not: a drive that is
            # unplugged today is still one of the household's libraries, and
            # checking only the folders present refused it with "add it on
            # the Library tab first" when it was already there.
            cfg = _cfg()
            libraries = cfg.roots or ([cfg.active_root] if cfg.active_root else [])
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
        if pin and user_id != admin.id:
            # A PIN is put on a profile to lock it, usually because somebody
            # who should not have been in it was. Their browser kept its
            # session for up to a month; now it has to give the PIN too.
            auth.end_all_sessions(conn, user_id)
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

        # Half-sent phone backups live on disk, named by a row delete_user
        # removes; nothing would find them again once it has.
        parts = _phone_backup_parts(conn, user_id)
        removed = auth.delete_user(conn, user_id, heir=admin.id)

    # The picture lives on disk, not in the database.
    if removed["avatar"]:
        with suppress(OSError):
            (_avatar_dir() / removed["avatar"]).unlink(missing_ok=True)
    for part in parts:
        with suppress(OSError):
            part.unlink(missing_ok=True)
    # Their photo books' PDFs: the rows went with the profile.
    from ..storage import books as book_store                 # noqa: PLC0415
    for name in removed.get("book_files") or []:
        path = book_store.file_of(_cfg().state_dir, {"file": name})
        if path is not None:
            with suppress(OSError):
                path.unlink(missing_ok=True)

    auth.audit(conn, admin.id, "delete_person",
               f"{removed['username']} ({removed['role']})")
    return jsonify({
        "ok": True,
        "removed": {k: v for k, v in removed.items() if k not in ("avatar", "book_files")},
    })


def _phone_backup_parts(conn, user_id: int) -> list[Path]:
    """The partial files of this person's unfinished phone backups."""
    from ..media import phone_backup                          # noqa: PLC0415
    try:
        rows = conn.execute("SELECT id FROM phone_backups WHERE user_id=? AND state=?",
                            (int(user_id), phone_backup.RECEIVING)).fetchall()
    except Exception:                                         # noqa: BLE001 - no table yet
        return []
    return [phone_backup._part(_cfg(), row["id"]) for row in rows]  # noqa: SLF001


@admin_accounts.post("/api/people/<int:user_id>/signout")
@require_admin
def signout_person(user_id: int):
    conn = _conn()
    count = auth.end_all_sessions(conn, user_id)
    auth.audit(conn, current_user().id, "signout_person", str(user_id))
    return jsonify({"ok": True, "sessions_ended": count})


@admin_accounts.delete("/api/people/<int:user_id>/avatar")
@require_admin
def remove_person_avatar(user_id: int):
    """An administrator takes someone's picture down (an unkind one, say),
    and their initials come back. From Ninaivu Lite."""
    conn = _conn()
    target = auth.get_user(conn, user_id)
    if target is None:
        return jsonify({"error": "No such person."}), 404
    if target.avatar:
        auth.update_profile(conn, user_id, avatar=None)
        with suppress(OSError):
            (_avatar_dir() / target.avatar).unlink(missing_ok=True)
        auth.audit(conn, current_user().id, "remove_avatar", target.username)
    return jsonify({"ok": True, "user": auth.get_user(conn, user_id).public()})


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
