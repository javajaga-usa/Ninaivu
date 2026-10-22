"""Photo books in the family app: pick, build, download (media/books.py).

From an album, an occasion, a smart album, a person or a year — and, without
anybody choosing a source, the ready-made books Ninaivu offers on its own
(``/api/books/ideas``): the best of each year, of each trip, of each person
the family has named. Every photograph is
found the way the gallery finds it — ``db.query_assets`` with this viewer's
own limits — so a book can hold nothing its maker could not have scrolled to:
a family member's book never has the admin-only photographs in it, nor
anything outside the folders they were given, and a name is printed under a
photograph only for somebody they could find on the People page.

Picking happens in the request, where those limits are known (the family
date policy is read from the request itself). The background build is handed
a fixed list of files and never asks the index what else there is.

Guests make no books: a book is a copy to take away, and a guest may only
look.
"""

from __future__ import annotations
from ..words import Said

import re
from pathlib import Path
from typing import Any

from flask import abort, jsonify, send_file

from ..media import books as maker, taste as taste_mod
from ..server.auth import current_user, require_family
from ..storage import books as store, db
from ._body import json_object
from .api import _cfg, _conn, _engine, _roots, _safe_under, _semantic_ids, _thumb_version, \
    _viewer, _viewer_limits, _visible_ids, bp

#: Rows a pick looks at. A person can be in twenty thousand photographs; a
#: book is thirty-six. Past this, an even sample across the whole run is read,
#: which keeps the spread across time the pick is after.
MAX_CANDIDATES = 4000
_CHUNK = 500
_SOURCES = ("album", "festival", "occasion", "person", "smart", "year")
#: Years a book can be of: the first photographs to the far future of a
#: camera clock set wrong.
_YEARS = (1826, 2200)
#: A ready-made book is only offered where there is something to choose
#: from: at least twice as many photographs as the smallest book.
IDEA_MIN_PHOTOS = 2 * maker.MIN_COUNT
#: How many of each kind are offered, newest (or most photographed) first.
IDEA_LIMIT = 6


def _source() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """The request's ``source`` as query filters, a description, and the body."""
    data = json_object()
    source = data.get("source")
    if not isinstance(source, dict) or source.get("kind") not in _SOURCES:
        abort(400, description="Choose an album, an occasion, a smart album, a person or a year.")
    kind, sid = source["kind"], source.get("id")
    if isinstance(sid, bool) or not isinstance(sid, int) or not 0 < sid < 2 ** 63:
        abort(400, description="The source needs an id.")
    conn = _conn()
    meta: dict[str, Any] = {"kind": kind, "id": sid, "name": ""}
    filters: dict[str, Any] = {}
    if kind == "year":
        if not _YEARS[0] <= sid <= _YEARS[1]:
            abort(400, description="The source needs an id.")
        filters["date_from"], filters["date_to"] = f"{sid:04d}-01-01", f"{sid:04d}-12-31"
        if not _query(filters, columns=("id",), limit=1):
            abort(404)
        meta["name"] = str(sid)
    elif kind == "album":
        from ..server import date_policy                       # noqa: PLC0415
        album = db.get_album(conn, sid)
        if not album or not date_policy.allows(album):
            abort(404)
        filters["album"] = sid
        # The same rule as opening the album (api.album_get): one with nothing
        # this viewer can see is somebody else's, and its name is theirs too.
        user = current_user()
        if not _query(filters, columns=("id",), limit=1) and (
                date_policy.restricted()
                or (album.get("created_by") != user.id and not user.is_admin)):
            abort(404)
        meta["name"] = album["name"]
    elif kind == "occasion":
        row = conn.execute("SELECT id, place FROM occasions WHERE id=?", (sid,)).fetchone()
        if row is None:
            abort(404)
        filters["occasion"] = sid
        # Its place is only told to somebody who can see one of its photographs.
        if not _query(filters, columns=("id",), limit=1):
            abort(404)
        meta["name"] = row["place"] or ""
    elif kind == "festival":
        # Every year of a festival album (api/api_festivals.py), as this
        # viewer may see it; nothing while the albums are switched off.
        from ..media import festivals                          # noqa: PLC0415
        from .api_festivals import _albums                     # noqa: PLC0415
        key = festivals.key_of(sid)
        if key is None or not getattr(_cfg(), "festival_albums", False):
            abort(404)
        ids = festivals.ids_of(_albums(), key)
        if not ids:
            abort(404)
        filters["ids"] = ids
        meta["name"] = festivals.FESTIVALS[key]["name"]
    elif kind == "person":
        # Only somebody this viewer could find on the People page: a name
        # read straight from the table would say who is in the photographs
        # they may not see.
        known = {int(p["id"]): p["name"] for p in
                 db.list_people(conn, _roots(), **_viewer_limits())}
        if sid not in known:
            abort(404)
        filters["person"] = sid
        meta["name"] = known[sid] or ""
    else:
        from ..storage import smart                            # noqa: PLC0415
        from .api_smart import apply_rules                     # noqa: PLC0415
        rules: dict[str, Any] = {"text": "", "people": None, "place": "", "kinds": None,
                                 "favorites": False, "date_from": "", "date_to": "",
                                 "camera": "", "tag": "", "folder": "", "album": None}
        apply_rules(rules, sid)                                # 404 for one they may not open
        rules.pop("album", None)
        if rules["kinds"] and "picture" not in rules["kinds"]:
            abort(400, description="That smart album has no photographs in it.")
        rules.pop("kinds", None)
        ranked, _ = _semantic_ids(rules["text"], _roots(), conn)
        if ranked is not None:
            rules["text"] = ""
        filters = {k: v for k, v in rules.items() if v not in (None, "", False, [])}
        if ranked is not None:
            filters["ids"] = ranked
        meta["name"] = (smart.get(conn, sid) or {}).get("name", "")
    return filters, meta, data


def _query(filters: dict[str, Any], ids: list[int] | None = None, *,
           columns: tuple[str, ...] | None = None, limit: int = 100000) -> list[dict[str, Any]]:
    """Pictures of the source, as this viewer may see them, oldest first."""
    wanted = dict(filters)
    if ids is not None:
        allowed = wanted.pop("ids", None)
        if allowed is not None:
            allowed = set(allowed)
            ids = [i for i in ids if i in allowed]
        wanted["ids"] = ids
        limit = len(ids) or 1
    rows, _ = db.query_assets(_conn(), _roots(), kinds=["picture"], sort="date_asc",
                              limit=limit, offset=0, columns=columns, with_total=False,
                              **_viewer(), **wanted)
    return rows


def _rows(filters: dict[str, Any], ids: list[int]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for start in range(0, len(ids), _CHUNK):
        out += _query(filters, ids[start:start + _CHUNK])
    out.sort(key=lambda r: (maker.when(r), r["id"]))
    return out


def _in_chunks(sql: str, ids: list[int], params: list[Any] | None = None) -> list[Any]:
    conn, found = _conn(), []
    for start in range(0, len(ids), _CHUNK):
        piece = ids[start:start + _CHUNK]
        found += conn.execute(sql.format(marks=",".join("?" * len(piece))),
                              (*piece, *(params or []))).fetchall()
    return found


def _faces(ids: list[int]) -> dict[int, int]:
    return {int(r[0]): int(r[1]) for r in _in_chunks(
        "SELECT asset_id, COUNT(*) FROM faces WHERE asset_id IN ({marks}) GROUP BY asset_id", ids)}


def _screens(ids: list[int]) -> set[int]:
    clause, params = db.screen_clause(_conn(), "a")
    # The clause's own parameters come first in it, so they are bound after
    # the ids by putting the ids in a subquery of their own.
    rows = []
    conn = _conn()
    for start in range(0, len(ids), _CHUNK):
        piece = ids[start:start + _CHUNK]
        rows += conn.execute(
            f"SELECT a.id FROM assets a WHERE {clause} AND a.id IN ({','.join('?' * len(piece))})",
            (*params, *piece)).fetchall()
    return {int(r[0]) for r in rows}


def _names(ids: list[int]) -> dict[int, list[str]]:
    """Who is in each photograph, among the people this viewer may see."""
    visible = {int(p["id"]): p["name"] for p in
               db.list_people(_conn(), _roots(), **_viewer_limits()) if p.get("name")}
    names: dict[int, list[str]] = {}
    for asset_id, person_id in _in_chunks(
            "SELECT DISTINCT asset_id, person_id FROM faces "
            "WHERE asset_id IN ({marks}) AND person_id IS NOT NULL", ids):
        name = visible.get(int(person_id))
        if name and name not in names.setdefault(int(asset_id), []):
            names[int(asset_id)].append(name)
    return {k: sorted(v, key=str.casefold) for k, v in names.items()}


def _signals(ids: list[int]) -> dict[int, dict[str, Any]]:
    """What the rest of the library says about each photograph (books.signal_bonus).

    Only numbers come back, and only for *ids*, which are already what this
    viewer may see: how clear its best face is and how many of the people the
    family named are in it (among the people this viewer can find), whether
    anybody recorded a story about it or put it in an album, and how well
    made it looks to the search model compared with the others.
    """
    if not ids:
        return {}
    signals: dict[int, dict[str, Any]] = {}
    visible = {int(p["id"]) for p in db.list_people(_conn(), _roots(), **_viewer_limits())
               if p.get("name")}
    for asset_id, best in _in_chunks(
            "SELECT asset_id, MAX(quality) FROM faces WHERE asset_id IN ({marks}) "
            "GROUP BY asset_id", ids):
        signals.setdefault(int(asset_id), {})["face_quality"] = float(best or 0.0)
    for asset_id, person_id in _in_chunks(
            "SELECT DISTINCT asset_id, person_id FROM faces "
            "WHERE asset_id IN ({marks}) AND person_id IS NOT NULL", ids):
        if int(person_id) in visible:
            entry = signals.setdefault(int(asset_id), {})
            entry["named"] = entry.get("named", 0) + 1
    try:
        told = _in_chunks("SELECT asset_id, COUNT(*) FROM stories WHERE asset_id IN ({marks}) "
                          "GROUP BY asset_id", ids)
    except Exception:                                      # noqa: BLE001 - no stories table yet
        told = []
    for asset_id, n in told:
        signals.setdefault(int(asset_id), {})["stories"] = int(n)
    for asset_id, n in _in_chunks(
            "SELECT asset_id, COUNT(*) FROM album_items WHERE asset_id IN ({marks}) "
            "GROUP BY asset_id", ids):
        signals.setdefault(int(asset_id), {})["albums"] = int(n)
    try:
        judged = taste_mod.relative(taste_mod.taste_for(_engine()), ids,
                                    db.embedding_store(_conn()))
    except Exception:                                      # noqa: BLE001 - never stops a book
        judged = {}
    for asset_id, z in judged.items():
        signals.setdefault(int(asset_id), {})["taste"] = z
    return signals


def _human_caption(row: dict[str, Any]) -> str:
    """The caption a person wrote, or a camera's description — not the
    tagger's three words ("beach, sea, sky"), which the AI pass stores as a
    caption, nor the note an edited copy is saved with."""
    caption = (row.get("caption") or "").strip()
    tags = row.get("tags") or []
    if isinstance(tags, list) and tags and caption == ", ".join(tags[:3]):
        return ""
    if caption.startswith("Edited copy of "):
        return ""
    return caption[:200]


def _candidates(filters: dict[str, Any]) -> list[dict[str, Any]]:
    ids = [int(r["id"]) for r in _query(filters, columns=("id",))]
    if len(ids) > MAX_CANDIDATES:
        step = len(ids) / MAX_CANDIDATES
        ids = [ids[int(i * step)] for i in range(MAX_CANDIDATES)]
    return _rows(filters, ids)


def _count(value: Any) -> int:
    if value is None:
        return maker.DEFAULT_COUNT
    if isinstance(value, bool) or not isinstance(value, int) \
            or not maker.MIN_COUNT <= value <= maker.MAX_COUNT:
        abort(400, description=Said("A book has {min_count} to {max_count} photographs.", min_count=maker.MIN_COUNT, max_count=maker.MAX_COUNT))
    return value


@bp.get("/api/books/options")
@require_family
def book_options():
    return jsonify({"templates": list(maker.TEMPLATES), "sizes": list(maker.SIZES),
                    "count": {"default": maker.DEFAULT_COUNT, "min": maker.MIN_COUNT,
                              "max": maker.MAX_COUNT},
                    "fonts": maker.font_report(), "busy": maker.building()})


@bp.post("/api/books/pick")
@require_family
def book_pick():
    """The photographs Ninaivu would put in a book of this source."""
    filters, meta, data = _source()
    count = _count(data.get("count"))
    rows = _candidates(filters)
    ids = [int(r["id"]) for r in rows]
    picked = maker.pick(rows, count, screens=_screens(ids), faces=_faces(ids),
                        signals=_signals(ids))
    places = {r.get("city") for r in picked}
    times = [maker.when(r) for r in picked]
    return jsonify({
        "source": {**meta, "place": next(iter(places)) if len(places) == 1 else "",
                   "started_at": min(times) if times else None,
                   "ended_at": max(times) if times else None},
        "candidates": len(rows),
        "items": [{"id": r["id"], "thumb_v": _thumb_version(r), "date": r.get("date_key"),
                   "width": r.get("width"), "height": r.get("height"), "hero": r["hero"]}
                  for r in picked],
        "fonts": maker.font_report(),
    })


def _text(data: dict[str, Any], key: str, limit: int) -> str:
    value = data.get(key) or ""
    if not isinstance(value, str):
        abort(400, description=Said("{key} must be text", key=key))
    return " ".join(value.split())[:limit]


def _describe(book: dict[str, Any]) -> dict[str, Any]:
    live = maker.progress(book["id"])
    state = book["state"]
    if state == "building" and live is None:
        # Nothing here is making it: Ninaivu was restarted part of the way.
        store.set_state(_conn(), book["id"], "error",
                        error="Ninaivu was restarted while this book was being made.")
        state, book["error"] = "error", "interrupted"
    return {
        "id": book["id"], "title": book["title"], "subtitle": book["subtitle"],
        "template": book["template"], "size": book["size"], "bleed": book["bleed"],
        "captions": book["captions"], "photos": len(book["item_ids"]),
        "pages": book["pages"] or (live or {}).get("total") or 0,
        "bytes": book["bytes"], "state": state, "error": book["error"],
        "created_at": book["created_at"], "finished_at": book["finished_at"],
        "progress": {"done": (live or {}).get("done", 0), "total": (live or {}).get("total", 0)},
        "download": f"/api/books/{book['id']}/download" if state == "done" else None,
    }


@bp.post("/api/books")
@require_family
def book_build():
    """Make a book of the photographs chosen, in the background."""
    filters, meta, data = _source()
    raw = data.get("ids")
    if not isinstance(raw, list) or not raw or len(raw) > maker.MAX_COUNT or not all(
            isinstance(i, int) and not isinstance(i, bool) and 0 < i < 2 ** 63 for i in raw):
        abort(400, description=Said("Choose 1 to {max_count} photographs.", max_count=maker.MAX_COUNT))
    template = data.get("template") or "plain"
    size = data.get("size") or "a4-portrait"
    if template not in maker.TEMPLATES or size not in maker.SIZES:
        abort(400, description="Unknown template or size.")
    for flag in ("bleed", "captions"):
        if data.get(flag) not in (None, True, False):
            abort(400, description=Said("{flag} must be true or false", flag=flag))
    months = data.get("months")
    if months is not None and not (isinstance(months, list) and len(months) == 12 and all(
            isinstance(m, str) and 0 < len(m) <= 24 for m in months)):
        abort(400, description="months must be twelve names")

    # Only what this viewer may see of this source — whatever ids were sent.
    rows = _rows(filters, list(dict.fromkeys(raw)))
    if not rows:
        abort(400, description="None of those photographs can go in a book.")
    ids = [int(r["id"]) for r in rows]
    names = _names(ids) if data.get("captions") else {}
    items = []
    for row in maker.mark_heroes(rows, _faces(ids), _signals(ids)):
        root = Path(row["root"]).resolve()
        path = (root / row["rel_path"]).resolve()
        if not _safe_under(path, root) or not path.is_file():
            continue
        items.append({"id": int(row["id"]), "path": str(path), "rotation": row.get("rotation") or 0,
                      "width": row.get("width"), "height": row.get("height"),
                      "score": row["score"], "hero": row["hero"], "when": maker.when(row),
                      "caption": _human_caption(row), "names": names.get(int(row["id"]), [])})
    if not items:
        abort(409, description="The files of those photographs cannot be reached.")
    title = _text(data, "title", 120) or meta["name"] or "Photo book"
    spec = {"title": title, "subtitle": _text(data, "subtitle", 160),
            "closing": _text(data, "closing", 120), "template": template, "size": size,
            "bleed": bool(data.get("bleed")), "captions": bool(data.get("captions")),
            "months": months, "items": items, "cover_id": data.get("cover_id")}
    if maker.building():
        abort(409, description="A book is already being made. Try again when it is finished.")
    conn = _conn()
    book_id = store.create(conn, owner_id=current_user().id, title=title,
                           subtitle=spec["subtitle"], source_kind=meta["kind"],
                           source_id=meta["id"], template=template, size=size,
                           bleed=spec["bleed"], captions=spec["captions"],
                           item_ids=[i["id"] for i in items])
    cfg = _cfg()
    try:
        maker.start(book_id, spec, db_path=cfg.db_path, state_dir=cfg.state_dir)
    except maker.Busy as exc:
        store.delete(conn, cfg.state_dir, store.get(conn, book_id))
        abort(409, description=str(exc))
    return jsonify(_describe(store.get(conn, book_id))), 202


@bp.get("/api/books/ideas")
@require_family
def book_ideas():
    """Books Ninaivu would make without being asked which photographs.

    The best of each year, of each trip, and of each person the family has
    named, where there are enough photographs to choose from — each counted
    as this viewer can see them, so an idea never mentions a year, a place or
    a person they could not have found themselves. The photographs are only
    chosen when one is opened (``/api/books/pick``), so this stays cheap.
    """
    conn, limits = _conn(), _viewer_limits()
    years = [{"kind": "year", "id": y["year"], "name": str(y["year"]), "photos": y["count"]}
             for y in db.picture_years(conn, _roots(), **limits)
             if y["count"] >= IDEA_MIN_PHOTOS and _YEARS[0] <= y["year"] <= _YEARS[1]]
    viewer = _viewer()
    trips = [{"kind": "occasion", "id": o["id"], "name": o.get("place") or o.get("title") or "",
              "place": o.get("place") or "", "started_at": o["started_at"],
              "ended_at": o["ended_at"], "photos": o["count"], "cover_id": o.get("cover_id")}
             for o in db.list_occasions(conn, _roots(), viewer_id=viewer["viewer_id"],
                                        max_visibility=viewer["max_visibility"],
                                        scope=viewer["scope"])
             if o["count"] >= IDEA_MIN_PHOTOS]
    people = [{"kind": "person", "id": p["id"], "name": p["name"], "photos": p["photo_count"],
               "cover_face_id": p.get("cover_face_id")}
              for p in db.list_people(conn, _roots(), **limits)
              if p.get("name") and p["photo_count"] >= IDEA_MIN_PHOTOS]
    return jsonify({"ideas": [*years[:IDEA_LIMIT], *trips[:IDEA_LIMIT], *people[:IDEA_LIMIT]],
                    "count": maker.DEFAULT_COUNT, "busy": maker.building()})


def _mine(book_id: int) -> dict[str, Any]:
    """The book, if it is this person's (or they are an administrator); 404 otherwise."""
    user = current_user()
    book = store.get(_conn(), book_id)
    if not store.may_open(book, user.id, user.is_admin):
        abort(404)
    return book


@bp.get("/api/books")
@require_family
def books_list():
    return jsonify({"books": [_describe(b) for b in store.listed(_conn(), current_user().id)],
                    "busy": maker.building()})


@bp.get("/api/books/<int:book_id>")
@require_family
def book_status(book_id: int):
    return jsonify(_describe(_mine(book_id)))


@bp.post("/api/books/<int:book_id>/cancel")
@require_family
def book_cancel(book_id: int):
    _mine(book_id)
    return jsonify({"ok": maker.cancel(book_id)})


@bp.get("/api/books/<int:book_id>/download")
@require_family
def book_download(book_id: int):
    book = _mine(book_id)
    path = store.file_of(_cfg().state_dir, book)
    if book["state"] != "done" or path is None or not path.is_file():
        abort(404)
    # The PDF is a copy of the photographs as they were. One since hidden,
    # deleted or erased is not handed out again inside it.
    wanted = [int(i) for i in book["item_ids"]]
    if len(_visible_ids(wanted)) != len(set(wanted)):
        return jsonify({"error": "Some photographs in this book can no longer be seen. "
                                 "Make the book again."}), 410
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", book["title"]).strip() or "Photo book"
    return send_file(path, mimetype="application/pdf", as_attachment=True,
                     download_name=f"{name[:80]}.pdf", max_age=0)


@bp.delete("/api/books/<int:book_id>")
@require_family
def book_delete(book_id: int):
    book = _mine(book_id)
    maker.cancel(book_id)
    store.delete(_conn(), _cfg().state_dir, book)
    return jsonify({"ok": True})
