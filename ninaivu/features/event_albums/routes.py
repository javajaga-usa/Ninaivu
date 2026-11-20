"""Event albums for the family app's administrator (features/event_albums).

``GET /api/event-albums`` is the trips and festivals waiting for an answer,
newest first, each with its title, dates, count and a few covers.
``POST /api/event-albums/<key>/approve`` makes one an ordinary album (an
optional ``title`` renames it first); ``POST .../decline`` turns it down for
good, and ``POST .../again`` offers a turned-down one again.

Administrators only: a suggestion is made from the whole library, hidden
photographs included, so a family member's list would tell them what is in
photographs they may not open. Nothing is shown to the family until an
administrator approves it, and then only as an album, which shows each
person the photographs in it they may see (storage/db_albums.py). Explicit
photographs and the bin are never put in one.

Off when ``event_albums`` is switched off (config.json), when every
route answers 404.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from flask import abort, jsonify

from ...api._body import json_object
from ...api.api import _cfg, _conn, _roots, _viewer, bp
from ...server import auth
from ...server.auth import current_user, require_admin
from ...storage import db
from ...words import said
from . import media, storage

log = logging.getLogger(__name__)

#: Seconds the list of suggestions is kept: finding the festivals asks the
#: index for every year with photographs in it.
KEEP_SECONDS = 600.0
#: Suggestions sent at once.
LIMIT = 24
#: The longest title an administrator may give one.
TITLE_MAX = 120

#: The suggestions last found, by index: (when, what).
_kept: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_lock = threading.Lock()


def _switched_on() -> None:
    if not getattr(_cfg(), "event_albums", False):
        abort(404)


def _occasions(conn) -> list[dict[str, Any]]:  # noqa: ANN001
    roots = _roots()
    if not roots:
        return []
    marks = ",".join("?" * len(roots))
    return [dict(r) for r in conn.execute(
        f"SELECT id, key, place, started_at, ended_at, count FROM occasions "
        f"WHERE root IN ({marks}) ORDER BY started_at", roots)]


def _festivals(conn) -> list[dict[str, Any]]:  # noqa: ANN001
    from ..festivals import media as festivals_media                     # noqa: PLC0415
    from ..festivals import routes as festivals_routes                   # noqa: PLC0415
    try:
        check = festivals_routes._check(conn)                           # noqa: SLF001
    except Exception:                                                   # noqa: BLE001 - dates alone then
        log.debug("no picture search for the festival suggestions", exc_info=True)
        check = None
    found = festivals_media.albums(conn, _roots(), _viewer(), check=check, include_nsfw=False)
    return media.festivals(found)


def _candidates(conn, fresh: bool = False) -> list[dict[str, Any]]:  # noqa: ANN001
    index = str(_cfg().db_path)
    with _lock:
        now = time.time()
        kept = _kept.get(index)
        if not fresh and kept and now - kept[0] < KEEP_SECONDS:
            return kept[1]
        found = [*media.trips(_occasions(conn), now=now), *_festivals(conn)]
        _kept[index] = (now, found)
        return found


def _forget_kept() -> None:
    with _lock:
        _kept.pop(str(_cfg().db_path), None)


def _members(conn, candidate: dict[str, Any]) -> list[int]:  # noqa: ANN001
    """The photographs and videos an album made from *candidate* holds:
    never one in the bin or marked explicit."""
    if candidate["kind"] == "festival":
        ids = candidate["ids"]
    else:
        marks = ",".join("?" * len(candidate["occasions"]))
        ids = [int(r[0]) for r in conn.execute(
            f"SELECT id FROM assets WHERE occasion_id IN ({marks}) ORDER BY captured_at",
            candidate["occasions"])]
    if not ids:
        return []
    kept = []
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        marks = ",".join("?" * len(chunk))
        allowed = {int(r[0]) for r in conn.execute(
            f"SELECT id FROM assets WHERE id IN ({marks}) AND trashed=0 AND nsfw=0", chunk)}
        kept += [i for i in chunk if i in allowed]
    return kept


def _covers(conn, candidate: dict[str, Any]) -> list[int]:  # noqa: ANN001
    if candidate.get("cover_ids"):
        return candidate["cover_ids"]
    if candidate["kind"] == "festival":
        # A festival year carries its photographs' ids, not occasions; one
        # whose answer named no cover photographs took the whole list with a
        # KeyError (a 500 on /api/event-albums).
        ids = [int(i) for i in candidate.get("ids", [])][:200]
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        return [int(r[0]) for r in conn.execute(
            f"SELECT id FROM assets WHERE id IN ({marks}) AND thumb IS NOT NULL "
            f"AND trashed=0 AND nsfw=0 ORDER BY sharpness DESC LIMIT 4", ids)]
    marks = ",".join("?" * len(candidate["occasions"]))
    return [int(r[0]) for r in conn.execute(
        f"SELECT id FROM assets WHERE occasion_id IN ({marks}) AND thumb IS NOT NULL "
        f"AND trashed=0 AND nsfw=0 ORDER BY sharpness DESC LIMIT 4", candidate["occasions"])]


def _shown(conn, candidate: dict[str, Any]) -> dict[str, Any]:  # noqa: ANN001
    keep = ("key", "kind", "title", "from", "to", "count", "places", "days", "festival",
            "year", "checked")
    return {**{k: candidate[k] for k in keep if k in candidate},
            "covers": _covers(conn, candidate)}


def _find(conn, key: str) -> dict[str, Any]:  # noqa: ANN001
    for candidate in _candidates(conn):
        if candidate["key"] == key:
            return candidate
    for candidate in _candidates(conn, fresh=True):
        if candidate["key"] == key:
            return candidate
    abort(404)


def _free_name(conn, title: str) -> str:  # noqa: ANN001
    """*title*, or *title* (2) and so on when an album has that name: an
    album's name is unique, and a new one must not fill somebody's old one."""
    name, n = title, 1
    while conn.execute("SELECT 1 FROM albums WHERE name=?", (name,)).fetchone():
        n += 1
        name = f"{title} ({n})"
    return name


@bp.get("/api/event-albums")
@require_admin
def event_albums_list():
    _switched_on()
    conn = _conn()
    storage.create(conn)
    decided = storage.decided(conn)
    waiting = media.pending(_candidates(conn), decided)
    return jsonify({
        "suggestions": [_shown(conn, c) for c in waiting[:LIMIT]],
        "waiting": len(waiting),
        "approved": sum(1 for d in decided.values() if d["state"] == storage.APPROVED),
        "declined": sum(1 for d in decided.values() if d["state"] == storage.DECLINED),
    })


@bp.post("/api/event-albums/<key>/approve")
@require_admin
def event_albums_approve(key: str):
    _switched_on()
    conn = _conn()
    storage.create(conn)
    if key in storage.decided(conn):
        return jsonify({"error": said("That suggestion has already been answered."),
                        "status": 409}), 409
    candidate = _find(conn, key)
    data = json_object()
    title = data.get("title", candidate["title"])
    if not isinstance(title, str) or not title.strip() or len(title) > TITLE_MAX:
        return jsonify({"error": said("Give the album a name of up to 120 letters."),
                        "status": 400}), 400
    members = _members(conn, candidate)
    if not members:
        return jsonify({"error": said("None of those photographs are in the library any more."),
                        "status": 409}), 409
    user = current_user()
    album_id = db.create_album(conn, _free_name(conn, title.strip()), user.id)
    db.album_add(conn, album_id, members)
    storage.decide(conn, key, kind=candidate["kind"], title=title.strip(),
                   state=storage.APPROVED, album_id=album_id, user_id=user.id)
    auth.audit(conn, user.id, "event_album", f"made album {album_id} from {key}")
    return jsonify({"ok": True, "album_id": album_id, "count": len(members)})


@bp.post("/api/event-albums/<key>/decline")
@require_admin
def event_albums_decline(key: str):
    _switched_on()
    conn = _conn()
    storage.create(conn)
    candidate = _find(conn, key)
    if key in storage.decided(conn):
        return jsonify({"error": said("That suggestion has already been answered."),
                        "status": 409}), 409
    user = current_user()
    storage.decide(conn, key, kind=candidate["kind"], title=candidate["title"],
                   state=storage.DECLINED, album_id=None, user_id=user.id)
    auth.audit(conn, user.id, "event_album", f"turned down {key}")
    return jsonify({"ok": True})


@bp.post("/api/event-albums/<key>/again")
@require_admin
def event_albums_again(key: str):
    _switched_on()
    conn = _conn()
    storage.create(conn)
    if not storage.forget(conn, key):
        abort(404)
    _forget_kept()
    auth.audit(conn, current_user().id, "event_album", f"offered again {key}")
    return jsonify({"ok": True})
