"""The duplicates review, side by side (storage/duplicate_review.py).

Every family member who may see the Duplicates view may look; only an
administrator decides, because a decision hides photographs and hiding is
theirs (``/api/visibility``). Nothing here deletes a file or writes one.
"""
from __future__ import annotations

from typing import Any

from flask import jsonify, request

from ..media import media
from ..server import auth, visibility
from ..server.auth import VIS_NAMES, current_user, require_admin, require_family
from ..storage import db, duplicate_review
from ._body import json_object
from .api import _conn, _id_list, _int_arg, _recipe, bp

#: How many groups one page of the review carries.
PAGE = 20


def _where() -> tuple[str, list[Any], visibility.Viewer]:
    """The rows of ``assets a`` this caller may see: their limits, and for
    anybody but an administrator no explicit photograph either."""
    viewer = visibility.current()
    clause, params = viewer.sql("a")
    if viewer.nsfw_shield:
        clause += " AND a.nsfw=0"
    return clause, params, viewer


def _all_groups() -> list[dict[str, Any]]:
    conn = _conn()
    where, params, viewer = _where()
    # A fingerprint arriving does not move the library's number; how many
    # there are is cheap to count and is in the key instead.
    prints = conn.execute("SELECT COUNT(*) FROM content_fingerprints").fetchone()[0]
    return db.cached_aggregate(
        conn, ("duplicate-review", viewer.cache_key(), where, tuple(params), prints),
        lambda: duplicate_review.groups(conn, where, params, viewer_id=viewer.viewer_id),
        also=(duplicate_review.GENERATION_KEY,) if db.get_meta(
            conn, duplicate_review.GENERATION_KEY, "") else (),
        # Favourites and stars (which pick the best) change without the
        # library's number moving: worked out again at most a minute later.
        max_age=60.0)


def _item(row: dict[str, Any], recipe: str, admin: bool) -> dict[str, Any]:
    """A member as the screen draws it: enough to tell two copies apart."""
    out = {
        "id": row["id"], "name": row["filename"], "folder": row["folder"],
        "ext": row["ext"], "kind": row["kind"], "date": row["date_key"],
        "taken": row.get("captured_at"), "camera": row.get("camera") or "",
        "size": row["size"], "size_h": media.human_size(row["size"] or 0),
        "width": row["width"], "height": row["height"],
        "sharpness": (round(float(row["sharpness"]), 1)
                      if row.get("sharpness") is not None else None),
        "has_thumb": bool(row["thumb"]), "thumb_v": media.thumb_version(row, recipe),
        "favorite": bool(row["favorite"]), "rating": int(row["rating"] or 0),
        "same_bytes": bool(row.get("same_bytes")),
        "fingerprint": (row.get("digest") or "")[:12],
    }
    if admin:
        out["visibility"] = VIS_NAMES.get(int(row.get("visibility", 1)), "family")
        out["explicit"] = bool(row.get("nsfw"))
    return out


@bp.get("/api/duplicates/review")
@require_family
def duplicates_review():
    """One page of the groups still to review: ``?kind=exact|near``,
    ``?offset=``. Each group's members, the best one first."""
    kind = (request.args.get("kind") or "").strip().lower()
    if kind not in ("", "all", "exact", "near"):
        return jsonify({"error": "kind is exact, near or all", "status": 400}), 400
    offset = max(0, _int_arg("offset", 0))
    limit = max(1, min(_int_arg("limit", PAGE), 100))
    found = _all_groups()
    counts = {"exact": sum(1 for g in found if g["kind"] == "exact"),
              "near": sum(1 for g in found if g["kind"] == "near")}
    if kind in ("exact", "near"):
        found = [g for g in found if g["kind"] == kind]
    page = found[offset:offset + limit]
    conn = _conn()
    # How many albums each member is in, for the page only: what a merge
    # would carry is what tells two identical-looking copies apart.
    ids = [i["id"] for g in page for i in g["items"]]
    albums: dict[int, int] = {}
    for start in range(0, len(ids), 500):
        piece = ids[start:start + 500]
        for row in conn.execute(
                "SELECT asset_id, COUNT(*) FROM album_items WHERE asset_id IN "
                f"({','.join('?' * len(piece))}) GROUP BY asset_id", piece):
            albums[int(row[0])] = int(row[1])
    recipe = _recipe()
    admin = current_user().is_admin
    groups = []
    for g in page:
        items = [_item(i, recipe, admin) for i in g["items"]]
        for item in items:
            item["albums"] = albums.get(item["id"], 0)
        groups.append({"group": g["group"], "kind": g["kind"], "best": g["best"],
                       "count": g["count"], "wasted": g["wasted"],
                       "wasted_h": media.human_size(g["wasted"]), "items": items})
    return jsonify({
        "groups": groups, "total": len(found), "offset": offset, "counts": counts,
        "total_wasted": sum(g["wasted"] for g in found),
        "total_wasted_h": media.human_size(sum(g["wasted"] for g in found)),
        "can_decide": admin,
    })


@bp.post("/api/duplicates/review/decide")
@require_admin
def duplicates_decide():
    """``{keep, others: [ids], action: hide|merge|keep_all}``. The others must
    be duplicates of the kept one (by fingerprint or look) and visible to the
    caller. Hides; never deletes."""
    data = json_object()
    action = str(data.get("action") or "").strip().lower()
    if action not in duplicate_review.ACTIONS:
        return jsonify({"error": "action is hide, merge or keep_all", "status": 400}), 400
    try:
        keep = int(data.get("keep"))
    except (TypeError, ValueError):
        return jsonify({"error": "keep is the id of the photograph to keep",
                        "status": 400}), 400
    others = [i for i in _id_list({"ids": data.get("others")}, limit=500) if i != keep]
    if not others:
        return jsonify({"error": "others lists the copies to decide about",
                        "status": 400}), 400
    conn = _conn()
    viewer = visibility.current()
    for aid in (keep, *others):
        if not viewer.may_see(db.get_asset(conn, aid)):
            return jsonify({"error": "not found", "status": 404}), 404
    if not duplicate_review.connected(conn, [keep, *others]):
        return jsonify({"error": "These photographs are not copies of each other.",
                        "status": 409}), 409
    done = duplicate_review.decide(conn, keep, others, action, user_id=current_user().id)
    auth.audit(conn, current_user().id, "duplicates_" + action,
               f"kept {keep}; {len(done['others'])} others")
    return jsonify(done)


@bp.get("/api/duplicates/review/set-aside")
@require_admin
def duplicates_set_aside():
    """The decisions that set photographs aside, newest first, to look at or
    put back."""
    offset = max(0, _int_arg("offset", 0))
    limit = max(1, min(_int_arg("limit", 30), 100))
    conn = _conn()
    entries, total = duplicate_review.set_aside(conn, limit=limit, offset=offset)
    recipe = _recipe()
    wanted = {i for e in entries for i in (e["keeper"], *e["others"]) if i}
    rows: dict[int, dict[str, Any]] = {}
    for aid in wanted:
        row = db.get_asset(conn, aid)
        if row:
            rows[aid] = {"id": aid, "name": row["filename"], "folder": row["folder"],
                         "size_h": media.human_size(row["size"] or 0),
                         "thumb_v": media.thumb_version(row, recipe),
                         "has_thumb": bool(row["thumb"])}
    for entry in entries:
        entry["keeper_item"] = rows.get(entry["keeper"])
        entry["items"] = [rows[i] for i in entry["others"] if i in rows]
    return jsonify({"entries": entries, "total": total, "offset": offset})


@bp.post("/api/duplicates/review/put-back")
@require_admin
def duplicates_put_back():
    """``{batch}``: undo one decision. What was changed since is left alone."""
    data = json_object()
    try:
        batch = int(data.get("batch"))
    except (TypeError, ValueError):
        return jsonify({"error": "batch is a whole number", "status": 400}), 400
    conn = _conn()
    done = duplicate_review.put_back(conn, batch)
    if not done["found"]:
        return jsonify({"error": "not found", "status": 404}), 404
    auth.audit(conn, current_user().id, "duplicates_put_back",
               f"batch {batch}; {done['shown']} shown again")
    return jsonify(done)
