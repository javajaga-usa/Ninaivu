"""Sent for screening: family members flag items, an administrator decides.

The family side (``/api/screening``, on the shared blueprint) takes one item
from the viewer or many from a selection. It only ever takes items the caller
can see now, narrowed by the gallery's own query, and it changes nothing about
who can see them: the item stays in the family's gallery until an
administrator acts on it.

The console side (``/api/admin/screening``, console only) lists the queue and
applies one decision to one item or many at once:

* ``ignore`` — nothing is wrong with it; the request goes.
* ``hide`` — documents and screenshots: only administrators see it now.
* ``explicit`` — adult content only, marked by hand so no later pass undoes it.
* ``delete`` — into the recycle bin, where Recently deleted can put it back.

Each decision is written to the audit log. The storage side is in
ninaivu/storage/screening.py.
"""

from __future__ import annotations

from typing import Any

from flask import abort, jsonify

from ..server import auth
from ..server.auth import VIS_HIDDEN, VIS_NAMES, current_user, require_admin, require_family
from ..server.config import BROWSER_NATIVE
from ..storage import db, screening
from ..utils import proxies
from ._body import json_object
from .api import (_conn, _id_list, _move_to_bin, _roots, _thumb_version, _viewer,
                  admin_only, bp)

#: The most items one request may send, or one decision may settle.
MAX_ITEMS = 2000


def _reachable(conn, ids: list[int], *, admin: bool) -> list[int]:
    """The ids among these the caller can see, by the gallery's own query."""
    allowed: list[int] = []
    for start in range(0, len(ids), 500):
        piece = ids[start:start + 500]
        rows, _ = db.query_assets(
            conn, _roots(), ids=piece, limit=len(piece), offset=0, columns=("id",),
            include_nsfw=admin, include_trashed=admin, with_total=False, **_viewer())
        allowed.extend(int(r["id"]) for r in rows)
    return allowed


@bp.post("/api/screening")
@require_family
def send_for_screening():
    """``{"ids": [...], "note": "...", "sent": true|false}`` — ask an
    administrator to look at these, or take the request back."""
    data = json_object()
    ids = _id_list(data, MAX_ITEMS)
    wanted = data.get("sent", True)
    if not isinstance(wanted, bool):
        abort(400, description="Sent must be true or false.")
    note = data.get("note") or ""
    if not isinstance(note, str):
        abort(400, description="The note must be text.")
    user = current_user()
    conn = _conn()
    if not ids:
        return jsonify({"sent": 0, "already": 0, "withdrawn": 0})
    if not wanted:
        return jsonify({"sent": 0, "already": 0,
                        "withdrawn": screening.withdraw(conn, ids, user.id)})
    allowed = _reachable(conn, ids, admin=user.is_admin)
    sent, already = screening.send(conn, allowed, user.id, note)
    if sent:
        auth.audit(conn, user.id, "screening_sent",
                   f"{sent} item{'s' if sent != 1 else ''} sent for screening")
    return jsonify({"sent": sent, "already": already,
                    "skipped": len(ids) - len(allowed)})


@bp.get("/api/screening/<int:asset_id>")
@require_family
def screening_state(asset_id: int):
    """Whether the caller has sent this item and is still waiting on it."""
    user = current_user()
    return jsonify({"sent": asset_id in screening.sent_by(_conn(), [asset_id], user.id)})


def _queue_item(row: dict[str, Any], entry: dict[str, Any],
                names: dict[int, str]) -> dict[str, Any]:
    level = 1 if row.get("visibility") is None else int(row["visibility"])
    ext = str(row.get("ext") or "").lower().lstrip(".")
    return {
        "id": row["id"],
        "filename": row["filename"],
        "folder": row.get("folder") or "",
        "kind": row.get("kind") or "",
        "size": row.get("size") or 0,
        "date_key": row.get("date_key") or "",
        "thumb": bool(row.get("thumb")),
        "thumb_v": _thumb_version(row),
        "rotation": int(row.get("rotation") or 0),
        "visibility_name": VIS_NAMES.get(level, "family"),
        "explicit": bool(row.get("nsfw")),
        "playable": f".{ext}" in BROWSER_NATIVE,
        "needs_proxy": proxies.needs_proxy(ext, row.get("kind") or ""),
        "first_at": entry["first_at"],
        "requests": [
            {"name": names.get(r["user_id"] or 0, ""), "note": r["note"],
             "at": r["created_at"]}
            for r in entry["requests"]],
    }


@admin_only.get("/api/admin/screening")
@require_admin
def screening_queue():
    """What the family has sent, oldest first, with who sent it and why."""
    conn = _conn()
    entries = screening.queue(conn, MAX_ITEMS)
    ids = [e["asset_id"] for e in entries]
    rows: dict[int, dict[str, Any]] = {}
    for start in range(0, len(ids), 500):
        piece = ids[start:start + 500]
        found, _ = db.query_assets(
            conn, _roots(), ids=piece, limit=len(piece), offset=0,
            include_nsfw=True, with_total=False, **_viewer())
        rows.update({int(r["id"]): dict(r) for r in found})
    user_ids = {r["user_id"] for e in entries for r in e["requests"] if r["user_id"]}
    names = {}
    for user_id in user_ids:
        person = auth.get_user(conn, int(user_id))
        names[int(user_id)] = person.display_name if person else ""
    items = [_queue_item(rows[e["asset_id"]], e, names)
             for e in entries if e["asset_id"] in rows]
    return jsonify({"items": items, "total": len(items)})


def _mark_explicit(conn, ids: list[int]) -> None:
    """Flag as explicit by hand, as the console's Mark explicit does
    (api.bulk_update): a live photo's clip and a RAW go with their still."""
    targets = db.with_companions(conn, ids)
    with db._write_lock:
        conn.execute(
            f"UPDATE assets SET nsfw=1, nsfw_source='manual' "
            f"WHERE id IN ({','.join('?' * len(targets))})", targets)
        conn.commit()


@admin_only.post("/api/admin/screening/decide")
@require_admin
def screening_decide():
    """``{"ids": [...], "action": "ignore"|"hide"|"explicit"|"delete"}``."""
    data = json_object()
    action = str(data.get("action") or "").lower()
    if action not in screening.ACTIONS:
        abort(400, description="Choose ignore, hide, explicit or delete.")
    ids = _id_list(data, MAX_ITEMS)
    if not ids:
        return jsonify({"settled": 0, "action": action})
    conn = _conn()
    user = current_user()
    allowed = _reachable(conn, ids, admin=True)
    # An item no longer in the library has nothing left to decide about.
    gone = sorted(set(ids) - set(allowed))
    if gone:
        screening.settle(conn, gone)
    if not allowed:
        return jsonify({"settled": 0, "action": action})

    payload: dict[str, Any] = {"action": action}
    if action == "delete":
        response = _move_to_bin({"ids": allowed}, ask_password=False)
        if isinstance(response, tuple):
            return response
        result = response.get_json()
        payload.update(deleted=result.get("deleted", 0), failed=result.get("failed", []),
                       bin=result.get("bin"))
        # What moved took its requests with it (the foreign key); what could
        # not be moved stays in the queue, to be tried again or decided
        # another way.
        still = screening.sent_any(conn, allowed)
        settled_ids = [i for i in allowed if i not in still]
    elif action == "hide":
        db.set_visibility(conn, allowed, VIS_HIDDEN, source="item", user_id=user.id)
        settled_ids = allowed
    elif action == "explicit":
        _mark_explicit(conn, allowed)
        settled_ids = allowed
    else:
        settled_ids = allowed
    screening.settle(conn, settled_ids)
    words = {"ignore": "ignored", "hide": "hidden", "explicit": "marked explicit",
             "delete": "moved to the recycle bin"}[action]
    auth.audit(conn, user.id, "screening_" + action,
               f"{len(settled_ids)} screened item{'s' if len(settled_ids) != 1 else ''} {words}")
    payload.update(settled=len(settled_ids), waiting=screening.open_count(conn))
    return jsonify(payload)

