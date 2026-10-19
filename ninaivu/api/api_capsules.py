"""Time capsules (storage/capsules.py, static/js/capsules.js).

Off until ``time_capsules`` is switched on, when every route answers 404 as if
it did not exist. Family members and administrators seal and open them;
guests have none.

The one rule every route keeps: before the day it opens, a capsule's letter
and photographs are not sent to anybody, whoever asks. A sealed capsule
answers 423 (Locked) with when it opens, so the page can show the envelope.
"""

from __future__ import annotations

from typing import Any

from flask import abort, jsonify

from ..server import auth
from ..server.auth import current_user, require_family
from ..storage import capsules
from ..words import Said
from ._body import json_object
from .api import _as_id, _cfg, _conn, _id_list, _visible_ids, bp


def _switched_on() -> None:
    if not _cfg().time_capsules:
        abort(404)


def _names(conn) -> dict[int, str]:
    return {u.id: (u.display_name or u.username) for u in auth.list_users(conn)}


def _outline(capsule: dict[str, Any], names: dict[int, str]) -> dict[str, Any]:
    """What anybody shown a capsule may know of it, sealed or not."""
    user = current_user()
    sealed = capsules.is_sealed(capsule)
    for_user = capsule.get("for_user")
    return {
        "id": int(capsule["id"]),
        "title": capsule.get("title") or "",
        "from": capsule.get("from_name") or names.get(int(capsule.get("created_by") or 0), ""),
        "for": None if for_user is None else names.get(int(for_user), ""),
        "for_everybody": for_user is None,
        "opens_on": capsule["opens_on"],
        "sealed_on": capsule.get("created_at"),
        "sealed": sealed,
        "items": int(capsule.get("items") or 0) if "items" in capsule else None,
        "mine": bool(user.id) and int(capsule.get("created_by") or 0) == user.id,
        "for_me": capsules.is_reader(capsule, user.id, guest=user.is_guest),
        # Ready and not yet opened: the family app says so once, on the day.
        "waiting": (not sealed and not capsule.get("opened_at")
                    and capsules.is_reader(capsule, user.id, guest=user.is_guest)),
    }


def _capsule_or_404(capsule_id: int) -> dict[str, Any]:
    """The capsule, if this person is shown it: theirs, or one they sealed."""
    capsule = capsules.get(_conn(), capsule_id)
    user = current_user()
    if capsule is None:
        abort(404)
    if not (capsules.is_reader(capsule, user.id, guest=user.is_guest)
            or int(capsule.get("created_by") or 0) == user.id):
        abort(404)
    return capsule


@bp.get("/api/capsules")
@require_family
def capsules_list():
    """The capsules for this person and the ones they sealed, and who in the
    household a new one can be sealed for."""
    _switched_on()
    conn = _conn()
    user = current_user()
    names = _names(conn)
    readers = [{"id": u.id, "name": u.display_name or u.username}
               for u in auth.list_users(conn, include_inactive=False)
               if u.role in (auth.ROLE_FAMILY, auth.ROLE_ADMIN)]
    return jsonify({
        "capsules": [_outline(c, names) for c in capsules.listed_for(conn, user.id,
                                                                     guest=user.is_guest)],
        "readers": readers,
        "max_items": capsules.MAX_ITEMS,
        "max_letter": capsules.MAX_LETTER,
    })


@bp.post("/api/capsules")
@require_family
def capsules_seal():
    """Seal one: ``title``, ``letter``, ``opens_on`` (YYYY-MM-DD, after
    today), ``for`` (a profile's id, or null for everybody in the family) and
    ``ids``, the photographs, every one of which the sealer may see."""
    _switched_on()
    data = json_object()
    conn = _conn()
    user = current_user()
    try:
        title = capsules_clean(data.get("title"), capsules.MAX_TITLE)
        letter = capsules_clean(data.get("letter"), capsules.MAX_LETTER)
        opens_on = capsules.parse_date(data.get("opens_on"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not title:
        return jsonify({"error": "Give the time capsule a name."}), 400
    for_user = None
    if data.get("for") not in (None, "", 0):
        for_user = _as_id(data.get("for"))
        reader = auth.get_user(conn, for_user) if for_user else None
        if reader is None or not reader.active or reader.role not in (
                auth.ROLE_FAMILY, auth.ROLE_ADMIN):
            return jsonify({"error": "A time capsule is for somebody in the family."}), 400
    asked = _id_list(data, limit=None)
    if len(asked) > capsules.MAX_ITEMS:
        return jsonify({"error": Said("A time capsule holds at most {max_items} photographs.",
                                      max_items=capsules.MAX_ITEMS)}), 400
    ids = _visible_ids(asked)
    if not ids and not letter:
        return jsonify({"error": "Put a photograph or a letter in it."}), 400
    capsule_id = capsules.create(
        conn, title=title, letter=letter, opens_on=opens_on, for_user=for_user,
        created_by=user.id or None, from_name=(user.display_name or user.username or "")[:80],
        asset_ids=ids)
    capsule = capsules.get(conn, capsule_id)
    capsule["items"] = len(ids)
    return jsonify({"capsule": _outline(capsule, _names(conn))}), 201


def capsules_clean(value: Any, limit: int) -> str:
    from ..storage import stories                               # noqa: PLC0415
    try:
        return stories.clean_text(value, limit)
    except ValueError as exc:
        raise ValueError(Said("That is longer than {limit} characters.", limit=limit)) from exc


@bp.get("/api/capsules/<int:capsule_id>")
@require_family
def capsules_open(capsule_id: int):
    """Open one. Sealed: 423 with when it opens, and nothing of what is in it.
    Open: the letter and the photographs this reader may see."""
    _switched_on()
    capsule = _capsule_or_404(capsule_id)
    conn = _conn()
    names = _names(conn)
    ids = capsules.items_of(conn, capsule_id)
    capsule["items"] = len(ids)
    outline = _outline(capsule, names)
    if outline["sealed"]:
        return jsonify({"error": "This time capsule is still sealed.",
                        "capsule": outline}), 423
    shown = _visible_ids(ids)
    order = {asset_id: n for n, asset_id in enumerate(ids)}
    shown.sort(key=lambda i: order.get(i, 0))
    if outline["for_me"]:
        capsules.mark_opened(conn, capsule_id)
    return jsonify({
        "capsule": outline,
        "letter": capsule.get("letter") or "",
        "ids": shown,
        # Chosen when it was sealed, and since hidden or kept from this reader.
        "not_shown": len(ids) - len(shown),
    })


@bp.delete("/api/capsules/<int:capsule_id>")
@require_family
def capsules_delete(capsule_id: int):
    """Take one away: whoever sealed it, or an administrator. The photographs
    are not touched."""
    _switched_on()
    capsule = capsules.get(_conn(), capsule_id)
    user = current_user()
    if capsule is None:
        abort(404)
    if not (user.is_admin or int(capsule.get("created_by") or 0) == user.id):
        abort(403, description="Only whoever sealed this time capsule, or an administrator, "
                               "can delete it.")
    capsules.delete(_conn(), capsule_id)
    return jsonify({"ok": True})
