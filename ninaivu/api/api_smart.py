"""Smart albums in the family app: a search kept under a name (storage/smart.py).

Saved from whatever the gallery is showing — the search box read into
people, a place, a kind and dates, plus the filters on screen — and opened
with ``?smart=<id>`` on any listing, where :func:`apply_rules` puts the rules
in place of those filters. Every listing then runs it through the viewer's
own limits, so a smart album can never show anybody more than they could
find by searching.
"""

from __future__ import annotations

from typing import Any

from flask import abort, jsonify

from ..server.auth import current_user, require_family
from ..storage import db, smart
from ._body import json_object
from .api import (_conn, _filters_from_request, _roots, _semantic_ids, _thumb_version, _viewer,
                  _viewer_limits,
                  bp)


def apply_rules(filters: dict[str, Any], album_id: int) -> None:
    """Put a smart album's rules in place of the request's own filters."""
    user = current_user()
    album = smart.get(_conn(), album_id)
    if user.is_guest or not smart.may_open(album, user.id, user.is_admin):
        abort(404)
    rules = album["rules"]
    typed = filters.get("text") or ""
    people = set(filters.get("people") or []) | set(rules.get("people") or [])
    filters.update(
        text=" ".join(t for t in (rules.get("text", ""), typed) if t),
        people=sorted(people) or None,
        place=rules.get("place") or filters.get("place") or "",
        kinds=rules.get("kinds") or filters.get("kinds"),
        favorites=bool(rules.get("favorites") or filters.get("favorites")),
        date_from=max(filter(None, (rules.get("date_from"), filters.get("date_from"))), default=""),
        date_to=min(filter(None, (rules.get("date_to"), filters.get("date_to"))), default=""),
        camera=rules.get("camera") or filters.get("camera") or "",
        tag=rules.get("tag") or filters.get("tag") or "",
        folder=rules.get("folder") or filters.get("folder") or "",
        album=None,
    )


def _rules_from_request() -> dict[str, Any]:
    """The search on screen, as it was understood, as rules."""
    filters = _filters_from_request()
    people = list(filters.get("people") or [])
    if filters.get("person"):
        people.append(int(filters["person"]))
    return {
        "text": filters.get("text") or "",
        "people": sorted(set(people)),
        "place": filters.get("place") or "",
        "kinds": list(filters.get("kinds") or []),
        "favorites": bool(filters.get("favorites")),
        "date_from": filters.get("date_from") or "",
        "date_to": filters.get("date_to") or "",
        "camera": filters.get("camera") or "",
        "tag": filters.get("tag") or "",
        "folder": filters.get("folder") or "",
    }


def _describe(album: dict[str, Any]) -> dict[str, Any]:
    """An album as the sidebar needs it: how many this viewer would see, a cover."""
    conn = _conn()
    roots = _roots()
    rules = album["rules"]
    text = rules.get("text", "")
    # Words go to the picture search where there is one, the way the gallery
    # sends them, so the count beside the name is the count it will show.
    ranked, _ = _semantic_ids(text, roots, conn)
    rows, total = db.query_assets(
        conn, roots, limit=1, offset=0, **_viewer(),
        text="" if ranked is not None else text, ids=ranked,
        people=rules.get("people") or None,
        place=rules.get("place", ""), kinds=rules.get("kinds") or None,
        favorites=bool(rules.get("favorites")), date_from=rules.get("date_from", ""),
        date_to=rules.get("date_to", ""), camera=rules.get("camera", ""),
        tag=rules.get("tag", ""), folder=rules.get("folder", ""))
    user = current_user()
    names = []
    if rules.get("people"):
        # Only names this viewer could see on the People page: an id in a rule
        # is whatever the request said, and reading the name straight from
        # the table told a family member who was in the hidden photographs.
        wanted = set(rules["people"])
        known = {int(p["id"]): p["name"] for p in
                 db.list_people(conn, roots, **_viewer_limits())
                 if int(p["id"]) in wanted}
        names = [known[p] for p in rules["people"] if p in known]
    return {
        "id": album["id"], "name": album["name"], "shared": album["shared"],
        "mine": album["created_by"] == user.id,
        "can_change": smart.may_change(album, user.id, user.is_admin),
        "n": int(total), "cover_id": rows[0]["id"] if rows else None,
        "cover_v": _thumb_version(rows[0]) if rows else "",
        "rules": {**rules, "people_names": names},
    }


@bp.get("/api/smart-albums")
@require_family
def smart_albums():
    user = current_user()
    out = []
    for album in smart.listed(_conn(), user.id, user.is_admin):
        described = _describe(album)
        # Like a hand-made album: somebody else's that shows this viewer
        # nothing is not their business.
        if not described["n"] and album["created_by"] != user.id and not user.is_admin:
            continue
        out.append(described)
    return jsonify({"albums": out})


@bp.post("/api/smart-albums")
@require_family
def smart_album_create():
    """Keep the search on screen (sent as the gallery's own query string)."""
    data = json_object()
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        abort(400, description="Give the smart album a name.")
    shared = data.get("shared", True)
    if not isinstance(shared, bool):
        abort(400, description="shared must be true or false")
    try:
        rules = smart.clean_rules(_rules_from_request())
    except ValueError as exc:
        abort(400, description=str(exc))
    album_id = smart.create(_conn(), name.strip()[:120], rules,
                            created_by=current_user().id, shared=shared)
    return jsonify(_describe(smart.get(_conn(), album_id))), 201


def _changeable(album_id: int) -> dict[str, Any]:
    user = current_user()
    album = smart.get(_conn(), album_id)
    if not smart.may_open(album, user.id, user.is_admin):
        abort(404)
    if not smart.may_change(album, user.id, user.is_admin):
        abort(403)
    return album


@bp.patch("/api/smart-albums/<int:album_id>")
@require_family
def smart_album_update(album_id: int):
    _changeable(album_id)
    data = json_object()
    name = data.get("name")
    if name is not None and (not isinstance(name, str) or not name.strip()):
        abort(400, description="The name cannot be empty.")
    shared = data.get("shared")
    if shared is not None and not isinstance(shared, bool):
        abort(400, description="shared must be true or false")
    smart.rename(_conn(), album_id, name.strip()[:120] if name else None, shared)
    return jsonify(_describe(smart.get(_conn(), album_id)))


@bp.delete("/api/smart-albums/<int:album_id>")
@require_family
def smart_album_delete(album_id: int):
    _changeable(album_id)
    smart.delete(_conn(), album_id)
    return jsonify({"ok": True})

