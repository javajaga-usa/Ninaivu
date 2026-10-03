"""Bring a household's Ninaivu Lite into Ninaivu.

Ninaivu Lite writes everything the household *made* — people, who sees what,
favourites, albums and share links — into one file
(``python -m ninaivu_lite --export lite-export.json``; the format is in Lite's
``docs/UPGRADE.md``). Its photographs are never in it: Lite never changes a
photograph, so Ninaivu reads the same folders, and each photograph in the
file is named by its library folder and its path inside it, which is how
Ninaivu's index knows it too. That is why this runs after Ninaivu has
scanned those folders.

Nothing already in Ninaivu is overwritten. Someone with the same username is
left as they are, an album with the same name gets the Lite ones' photographs
added rather than replaced, a folder that already has a rule keeps it, and a
share token already in use is skipped. A photograph that cannot be found (moved,
renamed, or too small for Ninaivu to index) is counted and listed, never
guessed.

Passwords and PINs carry across: Lite hashes them as ``scrypt$n$r$p$salt$hash``,
which is what Ninaivu verifies. A hash in another scheme (Lite's fallback on a
Python without scrypt) cannot be checked here, so that person is asked to
choose a new password, and that PIN is left off.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from ..server import auth
from . import db

FORMAT = "ninaivu-lite-export"
#: The newest version of Lite's format this reads. Fields are only ever added
#: within a version, so a newer minor addition is simply ignored.
FORMAT_VERSION = 1

_ROLES = {auth.ROLE_ADMIN, auth.ROLE_FAMILY, auth.ROLE_GUEST}
_LEVELS = {auth.VIS_PUBLIC, auth.VIS_FAMILY, auth.VIS_HIDDEN}
#: Listed in the report, at most this many, so a big miss is still readable.
MISSING_SHOWN = 50


class LiteImportError(ValueError):
    """The file is not a Ninaivu Lite export this can read."""


def load(path: str | Path) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LiteImportError(f"That file could not be read as JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise LiteImportError("That is not a Ninaivu Lite export file.")
    version = data.get("format_version")
    if not isinstance(version, int) or version > FORMAT_VERSION:
        raise LiteImportError(
            f"This export is format version {version!r}; this Ninaivu reads up "
            f"to version {FORMAT_VERSION}. Update Ninaivu and try again.")
    return data


def _key(path: str) -> str:
    """A comparable form of a folder: resolved where it exists, and folded the
    way the operating system compares names."""
    text = str(path).strip()
    try:
        text = str(Path(text).expanduser().resolve())
    except (OSError, RuntimeError):
        pass
    return os.path.normcase(text.rstrip("/\\"))


def _scrypt_or_none(stored: Any) -> str | None:
    return stored if isinstance(stored, str) and stored.startswith("scrypt$") else None


class _Finder:
    """Lite's ``{folder, path}`` references, as Ninaivu asset ids."""

    def __init__(self, conn: sqlite3.Connection, roots: list[str]) -> None:
        self.conn = conn
        self.roots = {_key(root): root for root in roots}
        self.missing: list[str] = []
        self._seen_missing: set[str] = set()

    def root(self, folder: Any) -> str | None:
        return self.roots.get(_key(folder)) if isinstance(folder, str) and folder else None

    def asset(self, ref: Any) -> int | None:
        if not isinstance(ref, dict):
            return None
        folder, rel = ref.get("folder"), ref.get("path")
        root = self.root(folder)
        if root is not None and isinstance(rel, str) and rel:
            rel = rel.replace("\\", "/").strip("/")
            row = self.conn.execute(
                "SELECT id FROM assets WHERE root=? AND rel_path=?", (root, rel)).fetchone()
            if row is None and os.name == "nt":
                row = self.conn.execute(
                    "SELECT id FROM assets WHERE root=? AND rel_path=? COLLATE NOCASE",
                    (root, rel)).fetchone()
            if row is not None:
                return int(row[0])
        label = f"{folder}/{rel}" if folder else str(rel)
        if label not in self._seen_missing:
            self._seen_missing.add(label)
            self.missing.append(label)
        return None


def import_export(conn: sqlite3.Connection, roots: list[str], data: dict[str, Any], *,
                  by: int | None = None) -> dict[str, Any]:
    """Write *data* (a loaded Lite export) into Ninaivu's index.

    *roots* are Ninaivu's library folders; photographs are found only under
    them. Returns a report of what came across and what did not.
    """
    report: dict[str, Any] = {
        "people_added": [], "people_already_here": [], "new_password_needed": [],
        "pins_left_off": [], "folder_rules": 0, "folder_rules_kept": 0,
        "visibility": 0, "favourites": 0, "albums": 0, "album_photos": 0,
        "shares": 0, "shares_skipped": 0, "folders_not_in_library": [],
        "photos_not_found": 0, "photos_not_found_examples": [],
    }
    finder = _Finder(conn, roots)
    now = time.time()

    for folder in data.get("folders") or []:
        if isinstance(folder, str) and finder.root(folder) is None:
            report["folders_not_in_library"].append(folder)

    # People, then who made whom: the second needs every id.
    ids: dict[str, int] = {}
    made_by: dict[int, str] = {}
    for person in data.get("people") or []:
        if not isinstance(person, dict):
            continue
        username = str(person.get("username") or "").strip()
        if not username:
            continue
        existing = auth.get_user_by_name(conn, username)
        if existing is not None:
            ids[username.lower()] = existing.id
            report["people_already_here"].append(username)
            continue
        role = person.get("role") if person.get("role") in _ROLES else auth.ROLE_FAMILY
        password = _scrypt_or_none(person.get("password"))
        pin = _scrypt_or_none(person.get("pin"))
        must_change = bool(person.get("must_change"))
        if person.get("password") and password is None:
            must_change = True
            report["new_password_needed"].append(username)
        if person.get("pin") and pin is None:
            report["pins_left_off"].append(username)
        library = auth.normalise_library(person.get("library"))
        if library is not None:
            root = finder.root(library)
            library = root if root is not None else library
        language = person.get("language") if person.get("language") in ("en", "ta") else None
        cur = conn.execute(
            "INSERT INTO users(username, display_name, role, password, color, active, "
            "created_at, last_login, must_change, pin, library, home_label, language) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (username, str(person.get("name") or username)[:80], role, password,
             person.get("color") if isinstance(person.get("color"), str) else None,
             int(bool(person.get("active", True))),
             float(person.get("created_at") or now), person.get("last_login"),
             int(must_change), pin, library,
             person.get("home_label") if isinstance(person.get("home_label"), str) else None,
             language))
        ids[username.lower()] = int(cur.lastrowid)
        if person.get("created_by"):
            made_by[int(cur.lastrowid)] = str(person["created_by"]).lower()
        report["people_added"].append(username)
    for user_id, maker in made_by.items():
        if maker in ids:
            conn.execute("UPDATE users SET created_by=? WHERE id=?", (ids[maker], user_id))
    conn.commit()

    def who(name: Any) -> int | None:
        return ids.get(str(name).lower()) if name else None

    # Folder rules: a folder Ninaivu already has a rule for keeps its own.
    for rule in data.get("folder_rules") or []:
        if not isinstance(rule, dict) or rule.get("level") not in _LEVELS:
            continue
        root = finder.root(rule.get("folder"))
        if root is None:
            continue
        folder = str(rule.get("path") or "").replace("\\", "/").strip("/")
        if conn.execute("SELECT 1 FROM folder_rules WHERE root=? AND folder=?",
                        (root, folder)).fetchone():
            report["folder_rules_kept"] += 1
            continue
        db.set_folder_visibility(conn, root, folder, int(rule["level"]), user_id=by,
                                 record_undo=False)
        report["folder_rules"] += 1

    # Per-photo decisions. Those that came from a folder rule were applied
    # with the rule above.
    for item in data.get("visibility") or []:
        if not isinstance(item, dict) or item.get("level") not in _LEVELS \
                or item.get("source") == "rule":
            continue
        asset_id = finder.asset(item)
        if asset_id is not None:
            conn.execute("UPDATE assets SET visibility=?, vis_source='item' WHERE id=?",
                         (int(item["level"]), asset_id))
            report["visibility"] += 1

    for favourite in data.get("favourites") or []:
        user_id = who(favourite.get("user")) if isinstance(favourite, dict) else None
        asset_id = finder.asset(favourite) if user_id is not None else None
        if asset_id is not None:
            conn.execute(
                "INSERT INTO user_assets(user_id, asset_id, favorite) VALUES(?,?,1) "
                "ON CONFLICT(user_id, asset_id) DO UPDATE SET favorite=1",
                (user_id, asset_id))
            report["favourites"] += 1
    conn.commit()

    # Albums, by name: Ninaivu's album names are unique, Lite's need not be,
    # so a second Lite album of the same name adds to the first.
    album_ids: dict[int, int] = {}
    for album in data.get("albums") or []:
        if not isinstance(album, dict):
            continue
        name = str(album.get("name") or "").strip()[:120]
        if not name:
            continue
        album_id = db.create_album(conn, name, created_by=who(album.get("owner")))
        if isinstance(album.get("id"), int):
            album_ids[album["id"]] = album_id
        report["albums"] += 1
        for item in album.get("items") or []:
            asset_id = finder.asset(item)
            if asset_id is None:
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO album_items(album_id, asset_id, added_at) "
                "VALUES(?,?,?)",
                (album_id, asset_id, float(item.get("added_at") or now)))
            report["album_photos"] += cur.rowcount
        cover = finder.asset(album["cover"]) if album.get("cover") else None
        if cover is not None:
            conn.execute("UPDATE albums SET cover_id=? WHERE id=? AND cover_id IS NULL",
                         (cover, album_id))
    conn.commit()

    # Share links keep their tokens, so a link already sent keeps working.
    for share in data.get("shares") or []:
        if not isinstance(share, dict) or not isinstance(share.get("token"), str):
            continue
        token = share["token"]
        if share.get("kind") == "album":
            scope, target = "album", album_ids.get(share.get("album_id"))
        elif share.get("kind") == "asset":
            scope, target = "asset", finder.asset(share)
        else:
            scope, target = None, None
        password = share.get("password")
        if target is None or (password and _scrypt_or_none(password) is None) or \
                conn.execute("SELECT 1 FROM shares WHERE token=?", (token,)).fetchone():
            report["shares_skipped"] += 1
            continue
        conn.execute(
            "INSERT INTO shares(token, scope, target_id, created_at, created_by, "
            "expires_at, password, view_count) VALUES(?,?,?,?,?,?,?,?)",
            (token, scope, target, float(share.get("created_at") or now),
             who(share.get("owner")), share.get("expires_at"), password or None,
             int(share.get("views") or 0)))
        report["shares"] += 1
    conn.commit()

    report["photos_not_found"] = len(finder.missing)
    report["photos_not_found_examples"] = finder.missing[:MISSING_SHOWN]
    auth.audit(conn, by, "import_lite",
               f"{len(report['people_added'])} people, {report['albums']} albums, "
               f"{report['shares']} shares, {report['photos_not_found']} not found")
    return report
