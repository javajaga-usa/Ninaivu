"""Photo books in the family app: pick, build, download (media/books.py).

From an album, an occasion, a smart album or a person. Every photograph is
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

import re
from pathlib import Path
from typing import Any

from flask import abort, jsonify, send_file

from ..media import books as maker
from ..server.auth import current_user, require_family
from ..storage import books as store, db
from ._body import json_object
from .api import _cfg, _conn, _roots, _safe_under, _semantic_ids, _thumb_version, _viewer, \
    _viewer_limits, bp

#: Rows a pick looks at. A person can be in twenty thousand photographs; a
#: book is thirty-six. Past this, an even sample across the whole run is read,
#: which keeps the spread across time the pick is after.
MAX_CANDIDATES = 4000
_CHUNK = 500
_SOURCES = ("album", "occasion", "person", "smart")


def _source() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """The request's ``source`` as query filters, a description, and the body."""
    data = json_object()
    source = data.get("source")
    if not isinstance(source, dict) or source.get("kind") not in _SOURCES:
        abort(400, description="Choose an album, an occasion, a smart album or a person.")
    kind, sid = source["kind"], source.get("id")
    if isinstance(sid, bool) or not isinstance(sid, int) or not 0 < sid < 2 ** 63:
        abort(400, description="The source needs an id.")
    conn = _conn()
    meta: dict[str, Any] = {"kind": kind, "id": sid, "name": ""}
    filters: dict[str, Any] = {}
    if kind == "album":
        from ..server import date_policy                       # noqa: PLC0415
        album = db.get_album(conn, sid)
        if not album or not date_policy.allows(album):
            abort(404)
        filters["album"] = sid
        meta["name"] = album["name"]
    elif kind == "occasion":
        row = conn.execute("SELECT id, place FROM occasions WHERE id=?", (sid,)).fetchone()
        if row is None:
            abort(404)
        filters["occasion"] = sid
        meta["name"] = row["place"] or ""
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
        abort(400, description=f"A book has {maker.MIN_COUNT} to {maker.MAX_COUNT} photographs.")
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
    picked = maker.pick(rows, count, screens=_screens(ids), faces=_faces(ids))
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
        abort(400, description=f"{key} must be text")
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
        abort(400, description=f"Choose 1 to {maker.MAX_COUNT} photographs.")
    template = data.get("template") or "plain"
    size = data.get("size") or "a4-portrait"
    if template not in maker.TEMPLATES or size not in maker.SIZES:
        abort(400, description="Unknown template or size.")
    for flag in ("bleed", "captions"):
        if data.get(flag) not in (None, True, False):
            abort(400, description=f"{flag} must be true or false")
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
    for row in maker.mark_heroes(rows, _faces(ids)):
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
