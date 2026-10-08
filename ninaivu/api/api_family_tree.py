"""The family tree in the family app (storage/family_tree.py).

Everything here is cut to the people this viewer may see on the People page —
``db.list_people`` with their own limits — before anything is sent or
changed. Somebody who appears only in photographs this viewer cannot open is
not in their tree at all, and a relation reaches them only when they can see
both its ends: "Raman's daughter" would otherwise say there is a daughter.
"""

from __future__ import annotations

from typing import Any

from flask import abort, jsonify

from ..server.auth import current_user, require_family
from ..storage import db, family_tree
from ._body import json_object
from .api import bp, _conn, _roots, _viewer_limits

#: What the person page offers, and the stored relation each one is.
RELATIONS = ("parent", "child", "spouse")


def _people() -> dict[int, dict[str, Any]]:
    return {int(p["id"]): p for p in db.list_people(_conn(), _roots(), **_viewer_limits())}


def _seen(relations: list[dict[str, Any]], people: dict[int, Any]) -> list[dict[str, Any]]:
    return [r for r in relations
            if int(r["person_a"]) in people and int(r["person_b"]) in people]


@bp.get("/api/family-tree")
@require_family
def family_tree_view():
    """Everybody this viewer may see, each with a generation row (None for
    somebody not in the tree yet), and the relations between them."""
    people = _people()
    links = _seen(family_tree.relations(_conn()), people)
    rows = family_tree.generations(people, links)
    return jsonify({
        "people": [{"id": pid, "name": p["name"], "cover_face_id": p.get("cover_face_id"),
                    "generation": rows.get(pid)} for pid, p in people.items()],
        "relations": links,
    })


def _person_id(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 2 ** 63:
        abort(400, description=f"{what} must be a number.")
    return value


@bp.post("/api/family-tree/relations")
@require_family
def family_tree_add():
    """``{person_id, other_id, relation}``: *other* is *person*'s parent,
    child or spouse."""
    data = json_object()
    person = _person_id(data.get("person_id"), "person_id")
    other = _person_id(data.get("other_id"), "other_id")
    relation = data.get("relation")
    if relation not in RELATIONS:
        abort(400, description="A relation is a parent, a child or a spouse.")
    people = _people()
    if person not in people or other not in people:
        abort(404)
    if relation == "parent":
        a, b, kind = other, person, "parent"
    elif relation == "child":
        a, b, kind = person, other, "parent"
    else:
        a, b, kind = person, other, "spouse"
    try:
        row = family_tree.add(_conn(), a, b, kind, current_user().id or None)
    except ValueError as exc:
        abort(409, description=str(exc))
    return jsonify({"ok": True, "relation": row})


@bp.delete("/api/family-tree/relations/<int:relation_id>")
@require_family
def family_tree_remove(relation_id: int):
    conn = _conn()
    row = family_tree.get(conn, relation_id)
    people = _people()
    if not row or int(row["person_a"]) not in people or int(row["person_b"]) not in people:
        abort(404)
    family_tree.remove(conn, relation_id)
    return jsonify({"ok": True})
