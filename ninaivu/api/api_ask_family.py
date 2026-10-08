"""Ask the family: "who is this?" links, and the answers that come back.

Two halves with different doors.

The family half (``/api/ask-family/...``) is for family members and
administrators. They pick unnamed faces from photographs they can already
open, make a link, withdraw it, and review the answers. Accepting an answer
names the face through :meth:`FaceIndexer.confirm` — the same call the
console's review queue makes — so the person's centroid is rebuilt from it
and the matcher can then suggest them across the whole library.

That is a deliberate widening of who may name a face. On the console naming
is management; here it is limited to a face the reviewer asked about
themselves (an administrator may review any), on a photograph they can open,
and the name came from somebody in the family who knew. A family member can
attach the face only to a person they can already see on the People page, or
to a name they type.

The public half (``/ask/<token>`` and ``/api/ask/<token>/...``) answers to
whoever holds the link, with no account at all, so like a share link
everything it shows is resolved from the token and judged again on every
request (see :func:`_shown`): the person who made it must still be able to
open each photograph, a photograph made more private since the link was made
drops out, and the visitor is shown the face crops — the whole photograph
only when the asker said so, and never one only administrators may see. No
name the house already uses is ever sent there.
"""

from __future__ import annotations

from pathlib import Path
import logging
import math
import secrets
import time
from typing import Any

from flask import abort, current_app, jsonify, render_template, request, send_file

from ..server import auth
from ..server.auth import current_user, require_family
from ..storage import ask_family, db
from ..media import media
from ._body import json_object
from .api import (bp, _asset_file, _cfg, _conn, _guard, _int_arg, _roots, _viewer_limits,
                  _viewing_copy)

log = logging.getLogger(__name__)

#: How long a link lasts when nobody says (days), and the longest anybody but
#: an administrator may make one last — the same as a share link's.
ASK_DEFAULT_DAYS = 30
ASK_MAX_DAYS = 365

#: Answers through one link from one address, and from anywhere, before the
#: link waits a while; and answers from one address across every link. Kept
#: in the same counters as the share links' password guesses (accounts_api).
ANSWERS_PER_ADDRESS = (10, 600.0)
ANSWERS_PER_LINK = (60, 1800.0)
ANSWERS_ACROSS_LINKS = (30, 600.0)

#: Faces shown to choose from at a time.
PICKER_PAGE = 60


# ---------------------------------------------------------------------------
# What may be shown
# ---------------------------------------------------------------------------

def _visible_to_me(row: dict[str, Any]) -> bool:
    """Whether the signed-in caller may open the photograph a face is in —
    ``_guard`` without the 404, for filtering a list."""
    limits = _viewer_limits()
    user = current_user()
    if row.get("root") not in _roots():
        return False
    if row.get("trashed") or (row.get("nsfw") and not user.is_admin):
        return False
    return db.visible_to(row, limits["max_visibility"], limits["scope"])


def _shown(face: dict[str, Any], limits: dict[str, Any]) -> bool:
    """May the holder of a link see this face, now?

    *limits* are the link maker's as they stand today (api_share's
    ``_creator_limits``). Trash and photographs flagged as explicit are never
    shown to a visitor, and a photograph whose visibility has been raised
    since it was asked about is withdrawn from the link — the same promise
    ``revoke_asset_shares_on_hide`` keeps for a share link, checked here at
    every view because a question holds several photographs.
    """
    if face.get("trashed") or face.get("nsfw"):
        return False
    if int(face.get("visibility") or 0) > int(face.get("asked_visibility") or 0):
        return False
    if face.get("root") not in limits["roots"]:
        return False
    return db.visible_to(face, limits["max_visibility"], limits["scope"])


def _photo_allowed(question: dict[str, Any], face: dict[str, Any]) -> bool:
    """The whole photograph goes out only when the asker ticked the box, and
    never one that only administrators may see — then or now."""
    return (bool(question.get("show_photo"))
            and int(face.get("asked_visibility") or 0) < auth.VIS_HIDDEN
            and int(face.get("visibility") or 0) < auth.VIS_HIDDEN
            and face.get("kind") in ("picture", "video"))


def _question_or_404(token: str) -> dict[str, Any]:
    question = ask_family.get_question(_conn(), token)
    if not question:
        abort(404, description="This link was not found.")
    if question["revoked"]:
        abort(410, description="This link has been withdrawn.")
    if question["expired"]:
        abort(410, description="This link has expired.")
    return question


def _shown_faces(question: dict[str, Any]) -> list[dict[str, Any]]:
    from .api_share import _creator_limits                     # noqa: PLC0415

    conn = _conn()
    limits = _creator_limits(question, conn)
    if limits is None:
        return []
    return [face for face in ask_family.question_faces(conn, question["id"])
            if _shown(face, limits)]


def _shown_face_or_404(token: str, position: int) -> tuple[dict, dict]:
    question = _question_or_404(token)
    for face in _shown_faces(question):
        if int(face["position"]) == int(position):
            return question, face
    abort(404)


# ---------------------------------------------------------------------------
# The public page
# ---------------------------------------------------------------------------

@bp.get("/ask/<token>")
def ask_page(token: str):
    """The page the link opens: no sign-in, a phone in somebody's hand."""
    _question_or_404(token)
    return render_template("ask.html", token=token,
                           app_name=current_app.config.get("APP_NAME", "Ninaivu"))


@bp.get("/api/ask/<token>")
def ask_view(token: str):
    """What the page shows: numbered faces, and nothing that names anybody."""
    question = _question_or_404(token)
    faces = _shown_faces(question)
    if not faces:
        abort(404, description="Nothing here any more.")
    return jsonify({
        "faces": [{
            "n": int(face["position"]),
            "face": f"/api/ask/{token}/face/{int(face['position'])}",
            "photo": (f"/api/ask/{token}/photo/{int(face['position'])}"
                      if _photo_allowed(question, face) else None),
        } for face in faces],
        "limits": {"name": ask_family.NAME_MAX, "note": ask_family.NOTE_MAX,
                   "who": ask_family.WHO_MAX},
    })


@bp.get("/api/ask/<token>/face/<int:position>")
def ask_face(token: str, position: int):
    _, face = _shown_face_or_404(token, position)
    if not face.get("face_thumb"):
        abort(404)
    path = Path(_cfg().state_dir) / "faces" / str(face["face_thumb"])
    if not path.is_file():
        abort(404)
    response = send_file(path, mimetype="image/jpeg", max_age=3600)
    response.headers["Cache-Control"] = "private, max-age=3600"
    return response


@bp.get("/api/ask/<token>/photo/<int:position>")
def ask_photo(token: str, position: int):
    """The photograph the face is in, as a copy with nothing in it but the
    picture: no place, no camera, no date (``_viewing_copy``). A video gives
    its thumbnail."""
    question, face = _shown_face_or_404(token, position)
    if not _photo_allowed(question, face):
        abort(404)
    if face.get("kind") == "picture":
        return _viewing_copy(face, _asset_file(face), max_age=3600, turned=True)
    cfg = _cfg()
    if not face.get("thumb"):
        abort(404)
    size = max(cfg.thumb_sizes)
    path = cfg.thumbs_dir / media.thumb_file(face["thumb"], size, cfg.thumb_format)
    if not path.is_file():
        abort(404)
    mime = "image/webp" if cfg.thumb_format.upper() == "WEBP" else "image/jpeg"
    response = send_file(path, conditional=True, mimetype=mime, max_age=3600)
    response.headers["Cache-Control"] = "private, max-age=3600"
    return response


def _answer_allowance(token: str) -> bool:
    """Count one sending of answers, or refuse it: per address on this link,
    on this link from anywhere, and per address across every link."""
    from .accounts_api import reserve                         # noqa: PLC0415

    address = request.remote_addr or "?"
    return reserve([
        (f"{address}|ask:{token}", *ANSWERS_PER_ADDRESS),
        (f"*|ask:{token}", *ANSWERS_PER_LINK),
        (f"{address}|ask", *ANSWERS_ACROSS_LINKS),
    ])


@bp.post("/api/ask/<token>/answer")
def ask_answer(token: str):
    """Names (and notes) for some of the faces, from whoever has the link."""
    data = json_object()
    question = _question_or_404(token)
    faces = {int(f["position"]): f for f in _shown_faces(question)}
    if not faces:
        abort(404, description="Nothing here any more.")
    entries = data.get("answers")
    if not isinstance(entries, list) or len(entries) > len(faces):
        abort(400, description="Answers must be a list, one for each face at most.")
    if not _answer_allowance(token):
        return jsonify({"error": "Thank you — that is a lot of answers at once. "
                                 "Wait a little and send the rest.", "status": 429}), 429
    try:
        who = ask_family.clean_text(data.get("who"), ask_family.WHO_MAX, "Your name")
        kept: dict[int, tuple[int, str, str]] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("Each answer must be an object")
            n = entry.get("n")
            if isinstance(n, bool) or not isinstance(n, int) or n not in faces:
                raise ValueError("That face is not on this page")
            name = ask_family.clean_text(entry.get("name"), ask_family.NAME_MAX, "A name")
            note = ask_family.clean_text(entry.get("note"), ask_family.NOTE_MAX, "A note")
            if name:
                kept[n] = (int(faces[n]["face_id"]), name, note)
        if not kept:
            raise ValueError("Write a name under at least one face.")
        saved = ask_family.add_answers(_conn(), question["id"], list(kept.values()), who)
    except ValueError as exc:
        abort(400, description=str(exc))
    return jsonify({"ok": True, "saved": saved})


# ---------------------------------------------------------------------------
# The family's half
# ---------------------------------------------------------------------------

@bp.get("/api/ask-family/faces")
@require_family
def ask_family_faces():
    """Unnamed faces in photographs this viewer can open, one per group the
    matcher formed (its best face) and every face it could not group, the
    largest groups first."""
    from ..media.faceindex import CLUSTER_MIN_QUALITY          # noqa: PLC0415

    conn = _conn()
    limits = _viewer_limits()
    guard, params = db._face_visibility_sql(                   # noqa: SLF001
        "a", limits["max_visibility"], limits["scope"], _roots())
    limit = max(1, min(_int_arg("limit", PICKER_PAGE), 200))
    offset = max(0, _int_arg("offset", 0))
    rows = conn.execute(
        "SELECT f.id, a.visibility, MAX(f.quality) AS quality, COUNT(*) AS size "
        "FROM faces f JOIN assets a ON a.id = f.asset_id "
        f"WHERE {guard} AND f.person_id IS NULL AND f.thumb IS NOT NULL "
        "AND f.quality >= ? "
        "GROUP BY COALESCE(f.cluster_key, 'face:' || f.id) "
        "ORDER BY size DESC, quality DESC, f.id LIMIT ? OFFSET ?",
        (*params, CLUSTER_MIN_QUALITY, limit + 1, offset)).fetchall()
    faces = [{"face_id": int(r["id"]), "size": int(r["size"]),
              "admin_only": int(r["visibility"]) >= auth.VIS_HIDDEN}
             for r in rows[:limit]]
    return jsonify({"faces": faces, "more": len(rows) > limit})


def _expiry(data: dict[str, Any]) -> float | None:
    """When a new link ends, as a share link's would (api_share.create_share)."""
    raw = data.get("expires_in_days")
    if raw is None:
        raw = ASK_DEFAULT_DAYS
    try:
        if isinstance(raw, bool):
            raise TypeError
        days = float(raw)
    except (TypeError, ValueError, OverflowError):
        abort(400, description="Expiry must be a number of days.")
    if not math.isfinite(days) or days < 0:
        abort(400, description="Expiry must be a finite, non-negative number of days.")
    if not current_user().is_admin and (days == 0 or days > ASK_MAX_DAYS):
        days = ASK_MAX_DAYS
    days = min(days, 3650)
    return time.time() + days * 86400 if days > 0 else None


@bp.post("/api/ask-family/questions")
@require_family
def ask_family_create():
    """Make a "who is this?" link for up to twenty unnamed faces."""
    data = json_object()
    raw = data.get("face_ids")
    if not isinstance(raw, list) or not raw:
        abort(400, description="Choose at least one face.")
    if len(raw) > ask_family.MAX_FACES:
        abort(400, description=f"Ask about at most {ask_family.MAX_FACES} faces at a time.")
    face_ids: list[int] = []
    for value in raw:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 2 ** 63:
            abort(400, description="Faces are given by number.")
        if value not in face_ids:
            face_ids.append(value)
    show_photo = data.get("show_photo", False)
    if not isinstance(show_photo, bool):
        abort(400, description="show_photo must be true or false.")
    expires_at = _expiry(data)

    conn = _conn()
    user = current_user()
    chosen: list[tuple[int, int]] = []
    for face_id in face_ids:
        face = db.get_face(conn, face_id)
        if not face:
            abort(404)
        # The face is asked about only if its photograph is one this person
        # can open now — which keeps an administrator-only photograph to an
        # administrator — and the answer is 404 either way, as for the crop.
        asset = _guard(db.get_asset(conn, int(face["asset_id"])))
        if int(asset.get("visibility") or 0) >= auth.VIS_HIDDEN and not user.is_admin:
            abort(404)
        if face.get("person_id") is not None:
            abort(409, description="One of those faces has been named already.")
        chosen.append((face_id, int(asset.get("visibility") or 0)))

    token = secrets.token_urlsafe(16)
    question = ask_family.create_question(
        conn, token=token, created_by=user.id, faces=chosen,
        expires_at=expires_at, show_photo=show_photo)
    return jsonify({"token": token, "url": f"/ask/{token}",
                    "question": _question_public(question)})


def _question_public(question: dict[str, Any]) -> dict[str, Any]:
    keys = ("token", "created_by", "created_at", "expires_at", "show_photo", "revoked",
            "expired", "open", "face_count", "answer_count", "pending_count")
    item = {k: question.get(k) for k in keys if k in question}
    item["url"] = f"/ask/{question['token']}"
    return item


@bp.get("/api/ask-family/questions")
@require_family
def ask_family_questions():
    """This person's links (an administrator's: everybody's), newest first."""
    user = current_user()
    rows = ask_family.list_questions(_conn(), None if user.is_admin else user.id)
    return jsonify({"questions": [_question_public(q) for q in rows]})


@bp.delete("/api/ask-family/questions/<token>")
@require_family
def ask_family_revoke(token: str):
    """Withdraw a link. What it has collected stays to be reviewed."""
    conn = _conn()
    question = ask_family.get_question(conn, token)
    user = current_user()
    if not question or (not user.is_admin and question.get("created_by") != user.id):
        abort(404)
    ask_family.revoke(conn, token)
    return jsonify({"ok": True})


def _reviewable(answer: dict[str, Any] | None) -> dict[str, Any]:
    """An answer this person may review, or 404: their own link's (any, for an
    administrator), about a face in a photograph they can open."""
    user = current_user()
    if not answer or (not user.is_admin and answer.get("created_by") != user.id):
        abort(404)
    if not _visible_to_me(answer):
        abort(404)
    return answer


def _visible_people() -> list[dict[str, Any]]:
    return db.list_people(_conn(), _roots(), **_viewer_limits())


@bp.get("/api/ask-family/answers")
@require_family
def ask_family_answers():
    """Answers waiting for a review, each with the people already named who
    have that name (matched ignoring case) for the reviewer to choose from."""
    user = current_user()
    conn = _conn()
    rows = ask_family.list_answers(conn, created_by=None if user.is_admin else user.id)
    by_name: dict[str, list[dict[str, Any]]] = {}
    for person in _visible_people():
        by_name.setdefault(person["name"].casefold(), []).append(
            {"id": person["id"], "name": person["name"],
             "cover_face_id": person.get("cover_face_id")})
    answers = []
    for row in rows:
        if not _visible_to_me(row):
            continue
        answers.append({
            "id": row["id"], "face_id": row["face_id"], "name": row["name"],
            "note": row["note"], "answered_by": row["answered_by"],
            "created_at": row["created_at"],
            "named": row["face_person_id"] is not None,
            "matches": by_name.get(row["name"].casefold(), []),
        })
    return jsonify({"answers": answers})


@bp.post("/api/ask-family/answers/<int:answer_id>/accept")
@require_family
def ask_family_accept(answer_id: int):
    """Name the face: an existing person (``person_id``) or a name (``name``),
    which joins whoever already has it or makes a new person."""
    from .api_faces import _face_indexer                       # noqa: PLC0415

    data = json_object()
    conn = _conn()
    answer = _reviewable(ask_family.get_answer(conn, answer_id))
    if answer["status"] != "pending":
        abort(409, description="This answer has been dealt with already.")
    face = db.get_face(conn, int(answer["face_id"]))
    if not face:
        abort(404)
    if face.get("person_id") is not None and face.get("source") == "confirmed":
        abort(409, description="This face has been named since.")

    person_id = data.get("person_id")
    if person_id is not None:
        if isinstance(person_id, bool) or not isinstance(person_id, int):
            abort(400, description="person_id must be a number.")
        known = {int(p["id"]) for p in _visible_people()}
        if person_id not in known and not (
                current_user().is_admin and db.get_person_cluster(conn, person_id)):
            abort(404)
        person = db.get_person_cluster(conn, person_id)
    else:
        try:
            name = ask_family.clean_text(data.get("name"), ask_family.NAME_MAX, "A name")
        except ValueError as exc:
            abort(400, description=str(exc))
        if not name:
            abort(400, description="A name is required.")
        person = db.create_or_update_person_cluster(conn, name)
    if not person:
        abort(404)
    # The same path the People screen's "Yes" takes: the face is confirmed and
    # the person's centroid rebuilt, so suggestions for them improve at once.
    result = _face_indexer().confirm(conn, int(face["id"]), int(person["id"]))
    ask_family.settle_answer(conn, answer_id, "accepted", current_user().id,
                             person_id=int(person["id"]))
    return jsonify({"ok": True, "person": {"id": person["id"], "name": person["name"]},
                    "face": result})


@bp.post("/api/ask-family/answers/<int:answer_id>/reject")
@require_family
def ask_family_reject(answer_id: int):
    """Dismiss an answer. Nothing about the face changes."""
    json_object()
    conn = _conn()
    answer = _reviewable(ask_family.get_answer(conn, answer_id))
    if answer["status"] != "pending":
        abort(409, description="This answer has been dealt with already.")
    ask_family.settle_answer(conn, answer_id, "rejected", current_user().id)
    return jsonify({"ok": True})
