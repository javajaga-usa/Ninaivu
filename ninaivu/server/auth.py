"""Accounts, sessions and permissions.

Three roles, ordered by capability:

``admin``
    Everything a family member can do, plus: set the visibility of any item or
    whole folder (public / family / hidden), create and disable profiles, and
    manage the library itself.

``family``
    View and download everything that is not hidden, keep private favourites,
    save photo rotations, and edit their own profile.

``guest``
    View public media only. No downloads, no favourites, no metadata beyond
    what is on screen.

Passwords use scrypt — the standard library's, or cryptography's on a Python
built without it — and no plaintext ever reaches the database. Sessions are
opaque random tokens stored server-side, so revoking a profile logs it out
everywhere at once.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Sequence

from flask import g, jsonify, request

from ..utils import kdf

# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------

ROLE_GUEST = "guest"
ROLE_FAMILY = "family"
ROLE_ADMIN = "admin"

ROLES = (ROLE_GUEST, ROLE_FAMILY, ROLE_ADMIN)

#: Higher wins. Used for "at least this role" checks.
ROLE_RANK = {ROLE_GUEST: 0, ROLE_FAMILY: 1, ROLE_ADMIN: 2}

ROLE_LABELS = {
    ROLE_GUEST: "Guest",
    ROLE_FAMILY: "Family member",
    ROLE_ADMIN: "Admin",
}

# Visibility levels, stored on each asset.
VIS_PUBLIC = 0    # everyone, including guests
VIS_FAMILY = 1    # family members and admins (the default for new media)
VIS_HIDDEN = 2    # admins only

VIS_NAMES = {VIS_PUBLIC: "public", VIS_FAMILY: "family", VIS_HIDDEN: "hidden"}
VIS_VALUES = {name: value for value, name in VIS_NAMES.items()}


def max_visibility_for(role: str) -> int:
    """The highest visibility level a role is allowed to see."""
    if role == ROLE_ADMIN:
        return VIS_HIDDEN
    if role == ROLE_FAMILY:
        return VIS_FAMILY
    return VIS_PUBLIC


SESSION_COOKIE = "ninaivu_session"
#: The console keeps its session under a different name.
#:
#: Cookies are scoped to a host, never to a port, so both faces live in one
#: cookie jar. Sharing a name there does not share the session — the ``face``
#: column still refuses a token minted by the other app — but it does mean the
#: second sign-in *overwrites* the first. Signing in on the family app would
#: silently end the console session, and the admin would be asked to sign in
#: again every single time they clicked through. Two names let both sessions
#: exist at once, which is what makes moving between the two apps seamless
#: without weakening the boundary between them by a single line.
ADMIN_SESSION_COOKIE = "ninaivu_admin_session"


def cookie_name(face: str | None) -> str:
    return ADMIN_SESSION_COOKIE if face == "admin" else SESSION_COOKIE
SESSION_TTL = 60 * 60 * 24 * 30          # 30 days
SESSION_REFRESH_AFTER = 60 * 60 * 24      # slide the window at most daily
#: However often it is used, a session ends this long after sign-in. The
#: window above slides on use, so a lost phone or a browser in somebody
#: else's house otherwise stayed signed in for as long as it was opened
#: once a month.
SESSION_MAX_AGE = 60 * 60 * 24 * 180      # 180 days

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,30}$")
MIN_PASSWORD = 8


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

AUTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id           INTEGER PRIMARY KEY,
    username     TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT NOT NULL,
    role         TEXT NOT NULL DEFAULT 'family',
    password     TEXT,
    avatar       TEXT,
    color        TEXT,
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   REAL NOT NULL,
    created_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    last_login   REAL,
    must_change  INTEGER NOT NULL DEFAULT 0,
    -- Library scope: a folder (relative to the library root) below which this
    -- profile may see media. NULL means the whole library. Admins ignore it.
    scope        TEXT,
    -- Optional 4–8 digit PIN for the profile picker. NULL means "tap to enter",
    -- which is what most household profiles want on a shared screen.
    pin          TEXT,
    -- The library folder this profile may see: an absolute path that is either
    -- one of the configured library folders or a folder inside one. NULL means
    -- every library folder. Admins ignore it.
    library      TEXT,
    -- What this person calls the home, shown only to them. NULL means they are
    -- happy with whatever the admin named it. This is deliberately private: it
    -- appears on their screen and nobody else's, which is what makes it safe to
    -- let a nine-year-old set it to something silly.
    home_label   TEXT,
    language     TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    seen_at    REAL NOT NULL,
    expires_at REAL NOT NULL,
    agent      TEXT,
    -- Which app face created the session. A session opens the app that made
    -- it and not the other one: the two-port split is a security boundary,
    -- so a family-app session must never unlock the console.
    face       TEXT NOT NULL DEFAULT 'home',
    -- The last time somebody was actually there — a tap, a key, a video
    -- playing — as opposed to the page polling. See the screen lock below.
    active_at  REAL NOT NULL DEFAULT 0,
    -- Locked for inactivity: still signed in, so work in progress carries on,
    -- but nothing is shown until the person unlocks it.
    locked     INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_exp  ON sessions(expires_at);

-- The guess allowances that are for an account, a profile or a share link
-- from anywhere, and the pauses after one is used up (api/accounts_api.py).
-- Kept here so that a restart does not hand a guesser a fresh allowance.
CREATE TABLE IF NOT EXISTS auth_limits (
    key      TEXT PRIMARY KEY,
    attempts TEXT NOT NULL DEFAULT '[]',
    last_at  REAL NOT NULL DEFAULT 0,
    strikes  INTEGER NOT NULL DEFAULT 0,
    until    REAL NOT NULL DEFAULT 0
);

-- Per-user favourites and ratings: one family member starring a photo must
-- not put it in everybody else's favourites.
CREATE TABLE IF NOT EXISTS user_assets (
    user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    favorite INTEGER NOT NULL DEFAULT 0,
    rating   INTEGER NOT NULL DEFAULT 0,
    seen_at  REAL,
    PRIMARY KEY (user_id, asset_id)
);

CREATE INDEX IF NOT EXISTS idx_user_assets_fav
    ON user_assets(user_id, favorite) WHERE favorite = 1;
-- Deleting a photograph cascades here; without this each deletion read the
-- whole table.
CREATE INDEX IF NOT EXISTS idx_user_assets_asset ON user_assets(asset_id);

-- Visibility rules that apply to a folder and everything beneath it, so newly
-- scanned files inherit the decision the admin already made.
CREATE TABLE IF NOT EXISTS folder_rules (
    id         INTEGER PRIMARY KEY,
    root       TEXT NOT NULL,
    folder     TEXT NOT NULL,
    visibility INTEGER NOT NULL,
    created_at REAL NOT NULL,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    UNIQUE(root, folder)
);

CREATE INDEX IF NOT EXISTS idx_folder_rules_root ON folder_rules(root);

CREATE TABLE IF NOT EXISTS audit (
    id      INTEGER PRIMARY KEY,
    at      REAL NOT NULL,
    user_id INTEGER,
    action  TEXT NOT NULL,
    detail  TEXT
);
"""


#: Columns added to ``users`` after a release shipped. ``CREATE TABLE IF NOT
#: EXISTS`` cannot add a column to a table that already exists, so every
#: column added to ``AUTH_SCHEMA`` above must also be listed here — that is
#: what lets an existing household database pick it up on the next start.
LATE_USER_COLUMNS: dict[str, str] = {
    "home_label": "TEXT",
    "language": "TEXT",
    "must_change": "INTEGER NOT NULL DEFAULT 0",
    "scope": "TEXT",
    "pin": "TEXT",
    "library": "TEXT",
    "avatar": "TEXT",
    "color": "TEXT",
}

#: Same repair for sessions. Existing sessions predate the face binding and
#: are treated as family-app sessions: the safe direction, since it costs a
#: console sign-in rather than opening the console to a family-app token.
LATE_SESSION_COLUMNS: dict[str, str] = {
    "face": "TEXT NOT NULL DEFAULT 'home'",
    "active_at": "REAL NOT NULL DEFAULT 0",
    "locked": "INTEGER NOT NULL DEFAULT 0",
}


def init_auth_schema(conn: sqlite3.Connection) -> None:
    from ..storage import db

    conn.executescript(AUTH_SCHEMA)
    db.ensure_columns(conn, "users", LATE_USER_COLUMNS)
    db.ensure_columns(conn, "sessions", LATE_SESSION_COLUMNS)
    _hash_stored_tokens(conn)
    conn.commit()


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

_SCRYPT = dict(n=2 ** 14, r=8, p=1, dklen=32)


def hash_password(password: str) -> str:
    """``scrypt$n$r$p$salt$hash`` — self-describing, so parameters can change."""
    salt = secrets.token_bytes(16)
    digest = kdf.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)
    return "scrypt${n}${r}${p}${salt}${hash}".format(
        n=_SCRYPT["n"], r=_SCRYPT["r"], p=_SCRYPT["p"],
        salt=salt.hex(), hash=digest.hex(),
    )


def verify_password(password: str, stored: str | None) -> bool:
    if not stored or not password:
        return False
    try:
        scheme, n, r, p, salt, expected = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = kdf.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt),
            n=int(n), r=int(r), p=int(p), dklen=len(expected) // 2,
        )
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError):
        return False


def password_problem(password: str) -> str | None:
    """Return a human-readable reason the password is unacceptable, or None."""
    if len(password or "") < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters."
    if password.lower() in {"password", "12345678", "qwertyui", "ninaivu123"}:
        return "That password is too common — pick something else."
    return None


def username_problem(username: str) -> str | None:
    if not USERNAME_RE.match(username or ""):
        return ("Usernames are 2–31 characters: lowercase letters, digits, "
                "dots, dashes or underscores, starting with a letter or digit.")
    return None


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

# A person with no picture is their initials on a colour of their own. The
# colour comes from their id, so it is the same on the picker, the lock screen,
# the top bar and the console, and neighbours in the family get different
# ones. Each is dark enough for white initials to read (4.5:1 or better), and
# the profile sheet offers the same eight — static/js/accounts.js keeps a copy.
AVATAR_COLORS = [
    "#1f6fb2", "#6247d6", "#b5306f", "#b4531a",
    "#1d7a47", "#0d7477", "#c02e36", "#7a5c1e",
]

# What profiles used to be given. New profiles were stamped with one of these
# by the clock, so a family made in the same minute all came out the same
# pink, and several failed contrast behind white initials. A stored colour
# from this list is not a choice anybody can be told apart from that stamp,
# so it yields to the colour from the id; any other stored colour is kept.
_OLD_AVATAR_COLORS = frozenset({
    "#0b7fd4", "#6d5efc", "#e05299", "#e8833a", "#2fbf71",
    "#00a3a3", "#d8353d", "#8b5cf6", "#4a8fe7", "#c2410c",
})


def avatar_color(user_id: int, stored: str | None) -> str:
    """The colour behind someone's initials: theirs if they chose one."""
    if stored and stored.lower() not in _OLD_AVATAR_COLORS:
        return stored
    return AVATAR_COLORS[int(user_id) % len(AVATAR_COLORS)]


def _avatar_version(name: str) -> str:
    """The cache-busting part of a picture's address: what follows the id in
    its file name (``<id>_<time>.webp``), or the whole stem."""
    stem = name.rsplit(".", 1)[0]
    return stem.split("_", 1)[-1] if "_" in stem else stem


@dataclass
class User:
    id: int
    username: str
    display_name: str
    role: str
    active: bool
    avatar: str | None = None
    color: str | None = None
    must_change: bool = False
    created_at: float = 0.0
    last_login: float | None = None
    scope: str | None = None
    library: str | None = None
    has_pin: bool = False
    has_password: bool = False
    home_label: str | None = None
    #: The language this person reads Ninaivu in, on every device; empty
    #: means whatever the device asks for.
    language: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    @property
    def is_guest(self) -> bool:
        return self.role == ROLE_GUEST

    @property
    def can_download(self) -> bool:
        return ROLE_RANK[self.role] >= ROLE_RANK[ROLE_FAMILY]

    @property
    def can_favorite(self) -> bool:
        return ROLE_RANK[self.role] >= ROLE_RANK[ROLE_FAMILY]

    @property
    def can_rotate(self) -> bool:
        """Whether this profile may save a shared photograph rotation."""
        return ROLE_RANK[self.role] >= ROLE_RANK[ROLE_FAMILY]

    @property
    def max_visibility(self) -> int:
        return max_visibility_for(self.role)

    @property
    def requires_secret(self) -> bool:
        """Whether the profile picker must ask for something before entry.

        Admins always authenticate. For everyone else it is the admin's
        choice, per profile.
        """
        if self.role == ROLE_ADMIN:
            return True
        return self.has_pin or self.has_password

    @property
    def effective_scope(self) -> str:
        """Legacy sub-folder scope. Admins are never confined."""
        return "" if self.is_admin else (self.scope or "")

    @property
    def assigned_library(self) -> str | None:
        """Absolute library folder assigned to this profile, if any."""
        return None if self.is_admin else (self.library or None)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "username": self.username,
            "name": self.display_name,
            "role": self.role,
            "role_label": ROLE_LABELS.get(self.role, self.role),
            "active": self.active,
            # Versioned by the picture's own file name, which is new with
            # every picture: versioned by the profile's creation time, a
            # replaced picture kept the old address and browsers showed the
            # old one for the day they are allowed to keep it.
            "avatar": f"/api/avatar/{self.id}?v={_avatar_version(self.avatar)}"
                      if self.avatar else None,
            "color": avatar_color(self.id, self.color),
            "initials": initials(self.display_name),
            "anonymous": self.id == 0,
            "must_change": self.must_change,
            "scope": self.effective_scope or None,
            "library": self.assigned_library,
            "locked": self.requires_secret,
            # Raw, not resolved: an empty value means "use the house name", and
            # the profile sheet needs to know the difference so it can show the
            # default in the placeholder rather than pretending it was typed.
            "home_label": self.home_label or "",
            "language": self.language or "",
            "can": {
                "download": self.can_download,
                "favorite": self.can_favorite,
                "rotate": self.can_rotate,
                "set_visibility": self.is_admin,
                "manage_people": self.is_admin,
                # Deleting media is the one management action that lives in
                # the gallery, because choosing what to delete means looking
                # at it. It belongs in this block with the rest so the page
                # asks one source what this viewer may do, rather than one
                # button inferring it from the role on its own.
                "delete_media": self.is_admin,
                "manage_library": self.is_admin,
                "see_hidden": self.is_admin,
            },
        }


#: The implicit account for someone browsing without logging in.
ANONYMOUS = User(
    id=0, username="guest", display_name="Guest", role=ROLE_GUEST,
    active=True, color="#8b94a3",
)


def normalise_scope(scope: str | None) -> str | None:
    """Clean a folder scope: relative, forward slashes, no traversal."""
    if not scope:
        return None
    cleaned = str(scope).replace("\\", "/").strip().strip("/")
    parts = [p for p in cleaned.split("/") if p not in ("", ".", "..")]
    return "/".join(parts) or None


def normalise_library(path: str | None) -> str | None:
    """Clean an absolute library assignment, or None for 'everything'."""
    if not path:
        return None
    text = str(path).strip().rstrip("/\\")
    return text or None


def resolve_library(assignment: str | None, roots: Sequence[str],
                    ) -> tuple[list[str], str | None]:
    """Turn a profile's assignment into ``(roots to query, sub-folder)``.

    The assignment may be one of the configured library folders (then the
    whole of it is visible), or a folder inside one (then only that subtree).
    An assignment that matches nothing yields no roots at all — a profile
    pointed at a folder that has since been removed sees nothing, rather than
    silently falling back to the whole library.
    """
    if not assignment:
        return list(roots), None

    target = _compare(assignment)
    best_root: str | None = None
    for root in roots:
        candidate = _compare(root)
        if target == candidate:
            return [root], None
        if target.startswith(candidate.rstrip("/\\") + "/") or \
                target.startswith(candidate.rstrip("/\\") + "\\"):
            # Deepest matching root wins, in case one root nests in another.
            if best_root is None or len(_compare(best_root)) < len(candidate):
                best_root = root
    if best_root is None:
        return [], None

    # Slice the *original* string, so folder names keep their real case —
    # the index stores "Maya", not "maya".
    original = str(assignment).replace("\\", "/").rstrip("/")
    relative = original[len(_compare(best_root)):].strip("/")
    return [best_root], (relative or None)


def _compare(path: str) -> str:
    """Comparable form: forward slashes, and case-folded for Windows paths."""
    text = str(path).replace("\\", "/").rstrip("/")
    if len(text) > 1 and text[1] == ":":
        text = text.lower()
    return text or "/"


def initials(name: str) -> str:
    parts = [p for p in re.split(r"[\s_-]+", (name or "").strip()) if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def row_to_user(row: sqlite3.Row | None) -> User | None:
    if row is None:
        return None
    return User(
        id=int(row["id"]),
        username=row["username"],
        display_name=row["display_name"],
        role=row["role"],
        active=bool(row["active"]),
        avatar=row["avatar"],
        color=row["color"],
        must_change=bool(row["must_change"]),
        created_at=row["created_at"] or 0.0,
        last_login=row["last_login"],
        scope=(row["scope"] or None) if "scope" in row.keys() else None,
        library=(row["library"] or None) if "library" in row.keys() else None,
        has_pin=bool(row["pin"]) if "pin" in row.keys() else False,
        has_password=bool(row["password"]) if "password" in row.keys() else False,
        home_label=((row["home_label"] or None)
                    if "home_label" in row.keys() else None),
        language=(row["language"] or None) if "language" in row.keys() else None,
    )


def get_user(conn: sqlite3.Connection, user_id: int) -> User | None:
    return row_to_user(
        conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    )


def get_user_by_name(conn: sqlite3.Connection, username: str) -> User | None:
    return row_to_user(
        conn.execute(
            "SELECT * FROM users WHERE username=? COLLATE NOCASE",
            (username.strip(),),
        ).fetchone()
    )


def list_users(conn: sqlite3.Connection, include_inactive: bool = True) -> list[User]:
    sql = "SELECT * FROM users"
    if not include_inactive:
        sql += " WHERE active=1"
    sql += " ORDER BY CASE role WHEN 'admin' THEN 0 WHEN 'family' THEN 1 ELSE 2 END, " \
           "display_name COLLATE NOCASE"
    return [row_to_user(row) for row in conn.execute(sql)]


def count_active_admins(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) n FROM users WHERE role='admin' AND active=1"
    ).fetchone()["n"]


def create_user(conn: sqlite3.Connection, username: str, password: str, *,
                display_name: str = "", role: str = ROLE_FAMILY,
                created_by: int | None = None,
                must_change: bool = False,
                scope: str | None = None,
                pin: str | None = None,
                library: str | None = None) -> User:
    username = (username or "").strip().lower()
    if problem := username_problem(username):
        raise ValueError(problem)
    if role not in ROLES:
        raise ValueError("Unknown role")
    # Admins must have a real password. For family and guest profiles the
    # admin chooses: a password, a PIN, or neither (tap to enter).
    if role == ROLE_ADMIN or password:
        if problem := password_problem(password):
            raise ValueError(problem)
    if pin and (problem := pin_problem(pin)):
        raise ValueError(problem)
    if get_user_by_name(conn, username):
        raise ValueError(f"“{username}” is already taken.")

    now = time.time()
    cur = conn.execute(
        "INSERT INTO users(username, display_name, role, password, color, "
        "active, created_at, created_by, must_change, scope) "
        "VALUES(?,?,?,?,?,1,?,?,?,?)",
        (username, (display_name or username).strip()[:60], role,
         hash_password(password) if password else None,
         None,  # no colour of their own yet: public() derives one from the id
         now, created_by, int(must_change), normalise_scope(scope)),
    )
    if pin:
        conn.execute("UPDATE users SET pin=? WHERE id=?",
                     (hash_password(pin), int(cur.lastrowid)))
    if library:
        conn.execute("UPDATE users SET library=? WHERE id=?",
                     (normalise_library(library), int(cur.lastrowid)))
    conn.commit()
    return get_user(conn, int(cur.lastrowid))


def set_password(conn: sqlite3.Connection, user_id: int, password: str) -> None:
    if problem := password_problem(password):
        raise ValueError(problem)
    conn.execute(
        "UPDATE users SET password=?, must_change=0 WHERE id=?",
        (hash_password(password), user_id),
    )
    conn.commit()


def set_active(conn: sqlite3.Connection, user_id: int, active: bool) -> None:
    conn.execute("UPDATE users SET active=? WHERE id=?", (int(active), user_id))
    if not active:
        # Disabling a profile ends every session it has open.
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    conn.commit()


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


def delete_user(conn: sqlite3.Connection, user_id: int,
                heir: int | None = None) -> dict[str, Any]:
    """Remove a profile and everything personal to it. Irreversible.

    Deleting is deliberately *not* the same as disabling. Disabling keeps the
    profile and its favourites so it can be switched back on; deleting takes
    the row away, and with it that person's favourites, ratings and open
    sessions. Media files are never touched — the library is shared, and what
    is deleted here is only who was allowed to look at it.

    The albums the person made pass to *heir* (an administrator; by default
    the longest-standing one left) — see below for why.

    Returns what was removed, so the console can say so plainly. Callers are
    responsible for the guards (self, last admin) and the avatar file, which
    lives outside the database.
    """
    row = conn.execute(
        "SELECT username, display_name, role, avatar FROM users WHERE id=?",
        (user_id,),
    ).fetchone()
    if row is None:
        raise ValueError("No such profile.")

    counts = {
        table: conn.execute(
            f"SELECT COUNT(*) n FROM {table} WHERE user_id=?", (user_id,)
        ).fetchone()["n"]
        for table in ("sessions", "user_assets")
    }
    favourites = conn.execute(
        "SELECT COUNT(*) n FROM user_assets WHERE user_id=? AND favorite=1",
        (user_id,),
    ).fetchone()["n"]

    # Explicit, in case this database was created before the foreign keys
    # were declared or is opened with them off.
    conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM user_assets WHERE user_id=?", (user_id,))
    # Folder rules are the library's, not the person's: keep the decision and
    # forget who made it.
    conn.execute("UPDATE folder_rules SET created_by=NULL WHERE created_by=?", (user_id,))
    conn.execute("UPDATE users SET created_by=NULL WHERE created_by=?", (user_id,))

    # Profile ids are reused — ``users.id`` is a plain INTEGER PRIMARY KEY, so
    # the next profile made after the newest is deleted gets its number — and
    # the tables below point at a profile with no foreign key to clear them.
    # Left alone, whoever was made next inherited the deleted person's albums
    # as their own, their share links came back to life, and their phones'
    # backup records told the newcomer's phone its files were already safe.
    #
    # Albums stay: they are the household's photographs, arranged. They pass
    # to an administrator (``heir``, else the longest-standing one), which is
    # what they were to everyone else already — NULL would mean "made before
    # owners were recorded", and make them any family member's to edit.
    if heir is None:
        found = conn.execute(
            "SELECT id FROM users WHERE role=? AND active=1 AND id != ? "
            "ORDER BY id LIMIT 1", (ROLE_ADMIN, user_id)).fetchone()
        heir = int(found["id"]) if found is not None else None
    if _has_table(conn, "albums"):
        conn.execute("UPDATE albums SET created_by=? WHERE created_by=?", (heir, user_id))
    # A link stops working when the person who made it is gone (see
    # api_share._creator_limits); the column's own ON DELETE SET NULL does
    # this only when foreign keys are on.
    if _has_table(conn, "shares"):
        conn.execute("UPDATE shares SET created_by=NULL WHERE created_by=?", (user_id,))
    # "Who is this?" links the same way (api_ask_family.py), and the record of
    # who reviewed an answer or drew a line in the family tree.
    for table, column in (("ask_questions", "created_by"), ("ask_answers", "reviewed_by"),
                          ("person_relations", "created_by")):
        if _has_table(conn, table):
            conn.execute(f"UPDATE {table} SET {column}=NULL WHERE {column}=?", (user_id,))
    # What arrived is already in the review queue or the library; these rows
    # only remember which of this person's files a phone has sent.
    if _has_table(conn, "phone_backups"):
        conn.execute("DELETE FROM phone_backups WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    conn.commit()

    return {
        "username": row["username"],
        "name": row["display_name"],
        "role": row["role"],
        "avatar": row["avatar"],
        "sessions": counts["sessions"],
        "favorites": favourites,
        "personal_rows": counts["user_assets"],
    }


def update_profile(conn: sqlite3.Connection, user_id: int, **fields: Any) -> None:
    if "scope" in fields:
        fields["scope"] = normalise_scope(fields["scope"])
    if "library" in fields:
        fields["library"] = normalise_library(fields["library"])
    allowed = {k: v for k, v in fields.items()
               if k in {"display_name", "avatar", "color", "role", "scope",
                        "library", "home_label", "language"}}
    if not allowed:
        return
    assignments = ", ".join(f"{k}=?" for k in allowed)
    conn.execute(f"UPDATE users SET {assignments} WHERE id=?",
                 (*allowed.values(), user_id))
    conn.commit()


PIN_RE = re.compile(r"^\d{4,8}$")


def pin_problem(pin: str) -> str | None:
    if not PIN_RE.match(pin or ""):
        return "A PIN is 4 to 8 digits."
    if pin in {"0000", "1234", "1111", "123456", "000000"}:
        return "That PIN is too easy to guess."
    return None


def set_pin(conn: sqlite3.Connection, user_id: int, pin: str | None) -> None:
    """Set or clear a profile's PIN. Clearing makes it tap-to-enter."""
    if pin:
        if problem := pin_problem(pin):
            raise ValueError(problem)
        stored = hash_password(pin)
    else:
        stored = None
    conn.execute("UPDATE users SET pin=? WHERE id=?", (stored, user_id))
    conn.commit()


def verify_pin(conn: sqlite3.Connection, user_id: int, pin: str) -> bool:
    row = conn.execute("SELECT pin FROM users WHERE id=?", (user_id,)).fetchone()
    return bool(row) and verify_password(pin, row["pin"])


def enter_profile(conn: sqlite3.Connection, user_id: int,
                  secret: str = "") -> User | None:
    """Sign in from the profile picker.

    An administrator's tile takes the password and nothing less, as the
    username login does: never a PIN, never a tap. For everyone else a
    profile with neither PIN nor password is tap-to-enter by design.
    """
    user = get_user(conn, user_id)
    if user is None or not user.active:
        return None

    if user.role == ROLE_ADMIN:
        row = conn.execute(
            "SELECT password FROM users WHERE id=?", (user_id,)).fetchone()
        if not verify_password(secret, row["password"]):
            return None
    elif user.has_pin:
        if not verify_pin(conn, user_id, secret):
            return None
    elif user.has_password:
        row = conn.execute(
            "SELECT password FROM users WHERE id=?", (user_id,)).fetchone()
        if not verify_password(secret, row["password"]):
            return None

    conn.execute("UPDATE users SET last_login=? WHERE id=?", (time.time(), user_id))
    conn.commit()
    return user


def pickable_profiles(conn: sqlite3.Connection) -> list[User]:
    """Profiles shown on the picker: everyone active, administrators too.

    An administrator's is a locked tile that asks for the password, as in
    Ninaivu Lite; the name and picture are all it shows.
    """
    return list_users(conn, include_inactive=False)


def reauthenticate(conn: sqlite3.Connection, user_id: int, password: str) -> bool:
    """Check the signed-in person's own password, for a step-up confirmation.

    Being signed in proves who opened the console; it does not prove who is at
    the keyboard now. A change that rewrites who can see the whole library is
    worth asking again for — the console is often left open on a machine the
    household shares.
    """
    if not password:
        return False
    row = conn.execute("SELECT password FROM users WHERE id=? AND active=1",
                       (int(user_id),)).fetchone()
    if row is None:
        return False
    return verify_password(password, row["password"])


_DECOY_HASH: str | None = None


def _decoy_hash() -> str:
    """A stored-format hash nobody's password matches, made once per process."""
    global _DECOY_HASH
    if _DECOY_HASH is None:
        _DECOY_HASH = hash_password(secrets.token_urlsafe(24))
    return _DECOY_HASH


def authenticate(conn: sqlite3.Connection, username: str, password: str) -> User | None:
    user = get_user_by_name(conn, username)
    row = conn.execute(
        "SELECT password FROM users WHERE id=?", (user.id,)
    ).fetchone() if user is not None else None
    stored = row["password"] if row is not None else None
    if not stored:
        # Exactly one scrypt whatever the answer. Hashing the decoy here on
        # every miss cost two, and a profile with no password cost none, so
        # the time taken said which names exist and which have a password.
        verify_password(password or "-", _decoy_hash())
        return None
    if not verify_password(password, stored):
        return None
    if not user.active:
        return None
    conn.execute("UPDATE users SET last_login=? WHERE id=?", (time.time(), user.id))
    conn.commit()
    return user


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def session_key(token: str) -> str:
    """What the sessions table keeps for *token*: its SHA-256, never the token.

    The database is a file other accounts on a shared computer may be able to
    read, or a copy that left the house; a token read out of it would have
    been a signed-in session for a month. A hash read out of it is not.
    """
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def _hash_stored_tokens(conn: sqlite3.Connection) -> None:
    """Sessions saved before tokens were hashed, hashed in place: still valid,
    nobody is signed out. A hash is 64 hex digits; a token never is."""
    raw = [r[0] for r in conn.execute(
        "SELECT token FROM sessions WHERE length(token) != 64")]
    for token in raw:
        conn.execute("UPDATE OR REPLACE sessions SET token=? WHERE token=?",
                     (session_key(token), token))


def start_session(conn: sqlite3.Connection, user_id: int,
                  agent: str = "", face: str = "home") -> tuple[str, float]:
    token = secrets.token_urlsafe(32)
    now = time.time()
    expires = now + SESSION_TTL
    conn.execute(
        "INSERT INTO sessions(token, user_id, created_at, seen_at, expires_at, agent, "
        "face, active_at, locked) VALUES(?,?,?,?,?,?,?,?,0)",
        (session_key(token), user_id, now, now, expires, (agent or "")[:200], face, now),
    )
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
    conn.commit()
    return token, expires


def session_user(conn: sqlite3.Connection, token: str,
                 face: str | None = None) -> User | None:
    if not token:
        return None
    row = conn.execute(
        "SELECT s.user_id, s.created_at, s.seen_at, s.expires_at, s.face "
        "FROM sessions s WHERE s.token=?",
        (session_key(token),),
    ).fetchone()
    if row is None:
        return None
    if face is not None and (row["face"] or "home") != face:
        # A real session, but minted by the other app face. Cookies are not
        # port-scoped, so the browser will offer it here — refuse it without
        # deleting it, because it is still valid where it belongs.
        return None
    now = time.time()
    ends = (row["created_at"] or now) + SESSION_MAX_AGE
    if row["expires_at"] < now or ends < now:
        conn.execute("DELETE FROM sessions WHERE token=?", (session_key(token),))
        conn.commit()
        return None

    user = get_user(conn, int(row["user_id"]))
    if user is None or not user.active:
        conn.execute("DELETE FROM sessions WHERE token=?", (session_key(token),))
        conn.commit()
        return None

    if now - (row["seen_at"] or 0) > SESSION_REFRESH_AFTER:
        conn.execute(
            "UPDATE sessions SET seen_at=?, expires_at=? WHERE token=?",
            (now, min(now + SESSION_TTL, ends), session_key(token)),
        )
        conn.commit()
    return user


# ---------------------------------------------------------------------------
# The screen lock
# ---------------------------------------------------------------------------
#
# After a spell with nobody there, a session is *locked*, not ended. The two
# are different on purpose: ending it would stop whatever the page was doing —
# a phone backup half-way through, a restore being watched — and the household
# asked for the screen to be covered, not for the work to stop. So the session
# stays valid, and while it is locked the server answers only what the lock
# screen itself and the work already under way need; everything else gets
# 423 Locked until the person proves it is them again. That is what makes the
# lock more than a curtain: taking the overlay off in the browser's developer
# tools shows nothing, because nothing behind it will load.
#
# "Somebody there" is decided by the page, which sees taps, keys and a video
# playing, and says so at most once a minute. Polling for status says nothing
# about a person and does not count — otherwise an open tab never locked.

#: What a locked session may still ask for. The lock screen's own needs (who
#: this is, their picture, unlocking, signing out); a phone backup the page is
#: in the middle of; and the status line, which names jobs and nothing else.
OPEN_WHILE_LOCKED = ("/api/auth/", "/api/phone-backup/", "/api/avatar/",
                     "/api/status")


def open_while_locked(path: str, method: str = "GET") -> bool:
    if not path.startswith("/api/"):
        return True                     # the page itself, scripts, styles
    if path.startswith(OPEN_WHILE_LOCKED):
        return True
    return path == "/api/me" and method == "GET"


def lock_state(conn: sqlite3.Connection, token: str, lock_after: float,
               now: float | None = None) -> dict[str, Any] | None:
    """Whether this session is locked, and how long until it would be.

    Locking is sticky: once idle for *lock_after* seconds the session is
    marked locked, and only :func:`unlock_session` clears it. A session from
    before this existed has no activity time yet and is given one now,
    rather than being locked the moment the server is upgraded.
    """
    if not token:
        return None
    now = time.time() if now is None else now
    row = conn.execute("SELECT active_at, locked FROM sessions WHERE token=?",
                       (session_key(token),)).fetchone()
    if row is None:
        return None
    active = float(row["active_at"] or 0)
    locked = bool(row["locked"])
    if not active:
        active = now
        conn.execute("UPDATE sessions SET active_at=? WHERE token=?", (now, session_key(token)))
        conn.commit()
    if not locked and lock_after > 0 and now - active >= lock_after:
        locked = True
        conn.execute("UPDATE sessions SET locked=1 WHERE token=?", (session_key(token),))
        conn.commit()
    return {
        "locked": locked,
        "lock_after": lock_after,
        "locks_in": (0.0 if locked else round(max(0.0, active + lock_after - now), 1))
        if lock_after > 0 else None,
    }


def mark_active(conn: sqlite3.Connection, token: str,
                now: float | None = None) -> bool:
    """Somebody is there. False if the session is locked — a locked session
    is opened by unlocking it, never by being busy."""
    now = time.time() if now is None else now
    cur = conn.execute(
        "UPDATE sessions SET active_at=? WHERE token=? AND locked=0", (now, session_key(token)))
    conn.commit()
    return cur.rowcount > 0


def lock_session(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("UPDATE sessions SET locked=1 WHERE token=?", (session_key(token),))
    conn.commit()


def unlock_session(conn: sqlite3.Connection, token: str,
                   now: float | None = None) -> None:
    now = time.time() if now is None else now
    conn.execute("UPDATE sessions SET locked=0, active_at=? WHERE token=?",
                 (now, session_key(token)))
    conn.commit()


def unlock_with(user: User) -> str:
    """What unlocks this person's screen: ``pin``, ``password`` or ``none``.

    The same thing that let them in. An administrator always has a password;
    a family profile with a PIN uses it, one with only a password uses that,
    and a tap-to-enter profile unlocks with a tap — the lock still covers the
    screen, and asking for a secret it never had would lock them out.
    """
    if user.is_admin:
        return "password"
    if user.has_pin:
        return "pin"
    if user.has_password:
        return "password"
    return "none"


def verify_unlock(conn: sqlite3.Connection, user: User, secret: str) -> bool:
    kind = unlock_with(user)
    if kind == "none":
        return True
    if kind == "pin":
        return verify_pin(conn, user.id, secret)
    row = conn.execute("SELECT password FROM users WHERE id=? AND active=1",
                       (user.id,)).fetchone()
    return bool(row) and verify_password(secret, row["password"])


def session_face(conn: sqlite3.Connection, token: str) -> str | None:
    """Which app face minted *token*, or None if it is no session at all."""
    if not token:
        return None
    row = conn.execute("SELECT face FROM sessions WHERE token=?", (session_key(token),)).fetchone()
    return (row["face"] or "home") if row is not None else None


def end_session(conn: sqlite3.Connection, token: str) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token=?", (session_key(token),))
        conn.commit()


def end_all_sessions(conn: sqlite3.Connection, user_id: int) -> int:
    cur = conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    conn.commit()
    return cur.rowcount


def audit(conn: sqlite3.Connection, user_id: int | None, action: str,
          detail: str = "") -> None:
    conn.execute(
        "INSERT INTO audit(at, user_id, action, detail) VALUES(?,?,?,?)",
        (time.time(), user_id, action, detail[:500]),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

def needs_setup(conn: sqlite3.Connection) -> bool:
    """True when no admin exists yet, so the app should show first-run setup."""
    return count_active_admins(conn) == 0


def bootstrap_admin(conn: sqlite3.Connection, username: str, password: str,
                    display_name: str = "") -> User:
    """Create the first admin. Only allowed while no admin exists."""
    if not needs_setup(conn):
        raise PermissionError("An administrator already exists.")
    user = create_user(conn, username, password, display_name=display_name,
                       role=ROLE_ADMIN)
    audit(conn, user.id, "bootstrap_admin", user.username)
    return user


# ---------------------------------------------------------------------------
# Flask integration
# ---------------------------------------------------------------------------

def current_user() -> User:
    """The signed-in user, or the anonymous guest."""
    return getattr(g, "user", None) or ANONYMOUS


def load_user(conn: sqlite3.Connection, face: str | None = None) -> User:
    token = request.cookies.get(cookie_name(face), "")
    user = session_user(conn, token, face)
    if user is None and face == "admin":
        # An admin signed in before the console had a cookie of its own still
        # holds a perfectly good session under the shared name. session_user
        # re-checks the face, so this cannot promote a family-app token.
        legacy = request.cookies.get(SESSION_COOKIE, "")
        if legacy:
            user = session_user(conn, legacy, face)
            token = legacy
    if user is None:
        g.session_token = ""
        return ANONYMOUS
    #: The token that signed this request in, for the screen lock.
    g.session_token = token
    return user


def _deny(message: str, status: int = 403):
    return jsonify({"error": message, "status": status}), status


def require(role: str) -> Callable:
    """Endpoint decorator: caller must hold *role* or higher."""
    needed = ROLE_RANK[role]

    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any):
            user = current_user()
            if ROLE_RANK.get(user.role, 0) < needed:
                if user.id == 0:
                    return _deny("Sign in to do that.", 401)
                return _deny(
                    f"{ROLE_LABELS[user.role]}s cannot do that.", 403
                )
            return fn(*args, **kwargs)
        return wrapper
    return decorator


require_family = require(ROLE_FAMILY)
require_admin = require(ROLE_ADMIN)


def may_send_photos_out(user, cfg) -> bool:
    """Whether *user* may hand a photograph to an extension that sends it
    out of the house (Gemini). The administrator always; family members only
    when the administrator allowed it (Config.outside_ai_for_family)."""
    if user is None or getattr(user, "id", 0) == 0:
        return False
    if user.is_admin:
        return True
    return bool(getattr(cfg, "outside_ai_for_family", False)) and user.role == ROLE_FAMILY


def require_outside_ai(view):
    """``require_family``, and then :func:`may_send_photos_out`."""
    from functools import wraps                                   # noqa: PLC0415
    from flask import current_app, jsonify                        # noqa: PLC0415

    @wraps(view)
    @require_family
    def wrapper(*args, **kwargs):
        if not may_send_photos_out(current_user(), current_app.config["MV_CONFIG"]):
            return jsonify({"error": "Sending photographs to an outside service is kept to "
                                     "the administrator. An administrator can allow family "
                                     "members on the Home & extensions page."}), 403
        return view(*args, **kwargs)
    return wrapper


def set_session_cookie(response, token: str, expires: float, secure: bool = False,
                       face: str | None = None):
    response.set_cookie(
        cookie_name(face), token,
        max_age=int(expires - time.time()),
        httponly=True,
        samesite="Lax",
        secure=secure,
        path="/",
    )
    return response


def clear_session_cookie(response, face: str | None = None,
                         shared_too: bool = False):
    """Sign out of *this* app only.

    Leaving the console no longer signs you out of the gallery on the same
    machine, and vice versa — they are separate sessions and now they are
    separate cookies too.

    ``shared_too`` also retires a console session held under the old shared
    name, for the console only and only when the caller has checked that the
    shared cookie *is* a console session. Deleting it unconditionally signed
    the same browser out of the gallery whenever it left the console.
    """
    response.delete_cookie(cookie_name(face), path="/")
    if face == "admin" and shared_too:
        response.delete_cookie(SESSION_COOKIE, path="/")
    return response


# ---------------------------------------------------------------------------
# The first administrator
# ---------------------------------------------------------------------------

_SETUP_CODE: str | None = None

#: Where the code is kept in the state folder until the administrator exists.
#: A server started by the Control Panel, the tray, the sign-in task or a
#: restart from the console has no window to print it in, and a restart used
#: to make a new code, so the one the owner had written down stopped working.
SETUP_CODE_FILE = "setup-code.txt"


def setup_code_path(state_dir: str | Path) -> Path:
    return Path(state_dir) / SETUP_CODE_FILE


def _saved_setup_code(state_dir: str | Path) -> str | None:
    try:
        text = setup_code_path(state_dir).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    for line in text.splitlines():
        code = line.strip().upper()
        if re.fullmatch(r"[0-9A-F]{10}", code):
            return code
    return None


def _save_setup_code(state_dir: str | Path, code: str) -> None:
    """Written owner-only, like the run file: reading it proves the same thing
    as reading the window the code was printed in, that the reader is on this
    computer."""
    target = setup_code_path(state_dir)
    partial = target.with_name(target.name + ".part")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            partial.unlink()
        except OSError:
            pass
        fd = os.open(partial, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(f"{code}\n\n"
                         "The Ninaivu setup code. Enter it where Ninaivu asks for it\n"
                         "to create the first administrator from another device.\n"
                         "This file is removed once the administrator exists.\n")
        os.replace(partial, target)
    except OSError:
        pass                     # still printed and logged; the file is a help


def setup_code(state_dir: str | Path | None = None) -> str:
    """A one-time code for making the first administrator from another device.

    Printed where the server starts, logged, and kept in the state folder
    (:data:`SETUP_CODE_FILE`) until the administrator exists, so it is the
    same code across restarts and can be found when the server runs with no
    window. Without it, whoever reached the family port first — anyone on the
    network — could make themselves the administrator of a new library.
    """
    global _SETUP_CODE
    if _SETUP_CODE is None and state_dir is not None:
        _SETUP_CODE = _saved_setup_code(state_dir)
    if _SETUP_CODE is None:
        # Ten characters, and the setup route limits guesses: six was about
        # sixteen million values with no limit on trying them.
        _SETUP_CODE = secrets.token_hex(5).upper()
    if state_dir is not None and _saved_setup_code(state_dir) != _SETUP_CODE:
        _save_setup_code(state_dir, _SETUP_CODE)
    return _SETUP_CODE


def forget_setup_code(state_dir: str | Path | None = None) -> None:
    """Once there is an administrator the code has done its job: removed, so
    a stale one is never found and typed somewhere it means nothing."""
    global _SETUP_CODE
    _SETUP_CODE = None
    if state_dir is not None:
        try:
            setup_code_path(state_dir).unlink()
        except OSError:
            pass


def is_loopback(address: str | None) -> bool:
    import ipaddress                                          # noqa: PLC0415
    try:
        return ipaddress.ip_address((address or "").split("%", 1)[0]).is_loopback
    except ValueError:
        return False


#: Headers a reverse proxy or tunnel adds on the way in. Caddy, nginx and
#: Tailscale Serve set X-Forwarded-For; Cloudflare's tunnel CF-Connecting-IP;
#: Tailscale Serve and Funnel their own identity headers. Any of them on a
#: request from loopback means the loopback address is the proxy's.
FORWARDING_HEADERS = (
    "X-Forwarded-For", "X-Forwarded-Host", "X-Real-IP", "Forwarded",
    "CF-Connecting-IP", "True-Client-IP", "X-Client-IP",
    "Tailscale-User-Login", "Tailscale-User-Name", "Tailscale-Funnel-Request",
)

_LOOPBACK_NAMES = {"127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"}


def request_is_local(trusted_proxies: int = 0) -> bool:
    """Whether the current request really started on this computer.

    Behind Caddy, a Cloudflare tunnel or Tailscale Funnel every request
    arrives from 127.0.0.1, so the address alone made the whole internet
    "local". With ``trusted_proxies`` set, ProxyFix has already put the real
    client address in ``remote_addr``; without it, a request carrying any
    forwarding header is somebody else's and must not pass for local.
    """
    address = (request.remote_addr or "").strip()
    if address not in _LOOPBACK_NAMES and not is_loopback(address):
        return False
    if int(trusted_proxies or 0) > 0:
        if _proxied_from_elsewhere():
            return False
        # ProxyFix reads X-Forwarded-For and nothing else. A tunnel that
        # names its visitor some other way (CF-Connecting-IP, Tailscale's
        # headers, Forwarded) left 127.0.0.1 in place, and that address is
        # the tunnel's, not a person's at this computer.
        return not any(request.headers.get(h) for h in FORWARDING_HEADERS
                       if not h.startswith("X-Forwarded-"))
    return not any(request.headers.get(h) for h in FORWARDING_HEADERS)


def _proxied_from_elsewhere() -> bool:
    """Whether ProxyFix took the address from a proxy on another computer.

    ProxyFix reads the headers only from a proxy (server/hosts.py), and a
    proxy on another machine that says 127.0.0.1 means itself, not this one.
    """
    peer = (request.environ.get("werkzeug.proxy_fix.orig") or {}).get("REMOTE_ADDR")
    return peer is not None and peer not in _LOOPBACK_NAMES and not is_loopback(peer)


def request_is_from_this_computer(trusted_proxies: int = 0) -> bool:
    """Whether the browser is on the computer Ninaivu runs on, however it
    reached the server.

    Wider than :func:`request_is_local`: opening ``ninaivu.local`` (what the
    Control Panel and the tray open) or the computer's own network address
    arrives from that address, not from loopback. Only this computer can make
    a connection from one of its own addresses. The proxy rule is the same:
    a forwarded request is somebody else's unless ``trusted_proxies`` says
    the real address is already in place.
    """
    if request_is_local(trusted_proxies):
        return True
    if not int(trusted_proxies or 0) and any(request.headers.get(h) for h in FORWARDING_HEADERS):
        return False
    if int(trusted_proxies or 0) and _proxied_from_elsewhere():
        return False
    address = (request.remote_addr or "").strip().split("%", 1)[0]
    if address.lower().startswith("::ffff:"):
        address = address[len("::ffff:"):]
    if not address:
        return False
    from .workload import own_addresses                     # noqa: PLC0415
    return address in own_addresses()
