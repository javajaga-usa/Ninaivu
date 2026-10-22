"""Find this print in the family app (media/find_print.py, static/js/find-print.js).

Off until ``find_print`` is switched on (Settings → Advanced), when the route
answers 404 as if it did not exist.

``POST /api/prints/find`` takes one photograph (``photo``) of a framed print
or a page of an album and answers with the library photographs it may be a
copy of, best first, each ``sure`` or only likely. The photograph is held as
a batch of Scan old prints (api_scan_prints.py) and the answer carries that
batch, so when nothing is found the same screen takes the print in with
``POST /api/prints/save``: an administrator's straight into the library, a
family member's into the review queue, as any scan of theirs.

Who may see what: every photograph found is narrowed through
``db.query_assets`` with the viewer's own limits, the check every gallery
listing goes through. A family member is never told that a hidden or
explicit photograph, a folder they are kept out of or somebody else's "Only
to me" item matched, not even by a count; an administrator sees every match.
Hidden photographs are never read by the picture search (hidden-not-read-by-
AI), so for them only the fingerprint can find them. The phone's picture is
compared here and goes nowhere else. Not for guests, who may not scan prints.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from flask import abort, jsonify, request

from ..media import find_print, print_scan
from ..server.auth import current_user, require_family
from ..storage import db
from ..words import Said
from .api import _cfg, _conn, _engine, _roots, _thumb_version, _viewer, bp
from .api_scan_prints import _capabilities, _where, hold

log = logging.getLogger(__name__)

#: Ids narrowed to what the viewer may see in one query.
_CHUNK = 500
#: Candidates looked at, at most, before narrowing: far more than any print
#: has real copies of; the rest are the weakest.
MAX_CANDIDATES = 2000
#: Two lookups at a time at most: each reads every fingerprint in the library and runs
#: the picture search over a dozen views of the print.
_looking = threading.BoundedSemaphore(2)


def _switched_on() -> None:
    if not getattr(_cfg(), "find_print", False):
        abort(404)


def _fingerprints() -> list[tuple[int, str]]:
    rows = _conn().execute(
        "SELECT id, phash FROM assets WHERE phash IS NOT NULL AND kind='picture' "
        "AND trashed=0").fetchall()
    return [(int(r[0]), str(r[1])) for r in rows]


def _visible(ids: list[int]) -> dict[int, dict[str, Any]]:
    """The rows of those *ids* this viewer may open, by id."""
    user = current_user()
    out: dict[int, dict[str, Any]] = {}
    for start in range(0, len(ids), _CHUNK):
        part = ids[start:start + _CHUNK]
        rows, _ = db.query_assets(_conn(), _roots(), ids=part, kinds=["picture"],
                                  include_nsfw=user.is_admin, limit=len(part), offset=0,
                                  with_total=False, include_companions=False, **_viewer())
        out.update({int(r["id"]): r for r in rows})
    return out


def _shown(match: find_print.Match, row: dict[str, Any], admin: bool) -> dict[str, Any]:
    out = match.as_dict()
    out.update({"thumb_v": _thumb_version(row), "date": row.get("date_key") or "",
                "width": row.get("width"), "height": row.get("height")})
    if admin:
        # Only an administrator is ever shown these, so only they are told.
        out["hidden"] = int(row.get("visibility") or 0) >= 2
        out["explicit"] = bool(row.get("nsfw"))
    return out


@bp.post("/api/prints/find")
@require_family
def prints_find():
    """Which library photograph this photograph of a print is a copy of."""
    _switched_on()
    _where()
    if request.content_length and request.content_length > print_scan.MAX_PHOTO_BYTES + 1024 * 1024:
        abort(413, description=Said("That photograph is too large."))
    files = [f for f in request.files.getlist("photo") if f and f.filename]
    if not files:
        abort(400, description=Said("Take or choose a photograph of the print."))
    if len(files) > 1:
        abort(400, description=Said("One photograph at a time."))

    with _looking:
        token, answer, seen = hold(files)
        engine = _engine()
        store = db.embedding_store(_conn())
        found = find_print.find([item.image for item in seen], _fingerprints(), store, engine)
        found = found[:MAX_CANDIDATES]
        rows = _visible([m.id for m in found])

    admin = current_user().is_admin
    matches = [_shown(m, rows[m.id], admin) for m in found if m.id in rows]
    matches = matches[:find_print.MAX_SHOWN]
    verdict = "sure" if any(m["sure"] for m in matches) else ("maybe" if matches else "none")
    guess = None
    if verdict != "sure":
        guess = print_scan.guess_decade([item.image for item in seen][:4], engine)
    log.info("find print: %d prints looked up, %s (%d shown)",
             min(len(seen), find_print.MAX_PRINTS), verdict, len(matches))
    return jsonify({
        "verdict": verdict,
        "matches": matches,
        "looked": min(len(seen), find_print.MAX_PRINTS),
        "search": find_print.look_ready(engine) and bool(store[2]),
        "session": token, "photos": answer, "guess": guess,
        "capabilities": _capabilities(),
    })
