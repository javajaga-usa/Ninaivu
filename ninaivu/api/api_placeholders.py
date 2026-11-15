"""Blurred previews for the gallery's tiles, a few hundred at a time.

``GET /api/placeholders?ids=1,2,3`` answers, for each id in the order asked,
one byte of length and that many bytes of preview (media/placeholders.py),
or a zero for an item with none, or one the caller may not see — decided
exactly as ``/api/thumb/<id>`` decides it, row by row, by the visibility
gate. A page asks for the stretch of the grid a scroll is heading into, in
blocks of a few hundred, so a tile always has something to show before its
thumbnail arrives.
"""

from __future__ import annotations

import threading
from typing import Any

from flask import current_app, request

from ..media import media, placeholders
from ..server import visibility
from ..storage import db
from .api import _cfg, _conn, _ids_from_csv, _recipe, bp

#: The most ids one request may name: a block of the grid (static/js/placeholder.js).
MOST = 512

_STORE_KEY = "NINAIVU_PLACEHOLDERS"
#: One store and one helper per thumbnails folder, shared by the family app
#: and the console (each its own Flask app), so the library is gone through once.
_HELD: dict[str, tuple[placeholders.Store, placeholders.Filler]] = {}
_HELD_LOCK = threading.Lock()


def _thumb_source(cfg):
    size = min(cfg.thumb_sizes)

    def source(row: dict[str, Any]):
        return cfg.thumbs_dir / media.thumb_file(row["thumb"], size, cfg.thumb_format)
    return source


def _store_and_filler():
    """The previews kept beside this server's thumbnails, and the helper that
    makes the rest of the library's, started the first time a page asks."""
    held = current_app.config.get(_STORE_KEY)
    if held is not None:
        return held
    cfg = _cfg()
    with _HELD_LOCK:
        held = _HELD.get(str(cfg.thumbs_dir))
        if held is None:
            held = _HELD[str(cfg.thumbs_dir)] = _make(cfg)
    current_app.config[_STORE_KEY] = held
    return held


def _make(cfg):
    recipe = _recipe()
    store = placeholders.Store(cfg.thumbs_dir)
    columns = ", ".join(db.BRIEF_COLUMNS)
    db_path = cfg.db_path

    def rows(after: int, limit: int) -> list[dict[str, Any]]:
        conn = db.connect(db_path)
        return [dict(r) for r in conn.execute(
            f"SELECT {columns} FROM assets WHERE id > ? AND thumb IS NOT NULL AND thumb != '' "
            "ORDER BY id LIMIT ?", (after, limit))]

    filler = placeholders.Filler(
        store, rows=rows, source=_thumb_source(cfg),
        version=lambda row: media.thumb_version(row, recipe))
    return store, filler


@bp.get("/api/placeholders")
def placeholders_for():
    ids = _ids_from_csv(request.args.get("ids"), MOST)
    cfg = _cfg()
    store, filler = _store_and_filler()
    viewer = visibility.current()
    found: dict[int, bytes] = {}
    if ids:
        conn = _conn()
        marks = ",".join("?" * len(ids))
        rows = [dict(r) for r in conn.execute(
            f"SELECT {', '.join(db.BRIEF_COLUMNS)} FROM assets WHERE id IN ({marks})", ids)]
        # The same test as a thumbnail's own (api.thumb): admins see everything,
        # a family member nothing hidden from them, nothing explicit, nothing
        # outside their folders or dates.
        visible = [r for r in rows if r.get("thumb") and viewer.may_see(r)]
        recipe = _recipe()
        found = placeholders.previews(
            store, visible, source=_thumb_source(cfg),
            version=lambda row: media.thumb_version(row, recipe))
        filler.start()
    response = current_app.response_class(placeholders.pack(ids, found),
                                          mimetype="application/octet-stream")
    # Private, as everything one viewer may see is. Kept ten minutes: a scroll
    # back over the same stretch asks again for the same block, and access
    # taken away reaches it as soon as it reaches a thumbnail (static/sw.js).
    response.headers["Cache-Control"] = "private, max-age=600"
    return response
