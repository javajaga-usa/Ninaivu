"""Faces: who is in a photograph, and what the household calls them.

Split out of api.py, which had grown past three thousand lines. The routes,
their paths and their blueprints are exactly as they were — this is where they
live, not what they do."""

from __future__ import annotations

from pathlib import Path
import json

from flask import abort, current_app, jsonify, send_file
from ..storage import db
from ..server.auth import current_user, require_admin, require_family
from ._body import json_object

# The blueprints and the shared helpers stay in api.py: these routes
# are registered on the same two blueprints they always were, so every
# path and every endpoint name is unchanged.
from .api import (admin_only, bp, _cfg, _conn, _guard, _int_arg, _root, _roots,
                  _scanner, _viewer_limits)


# ---------------------------------------------------------------------------
# Faces
# ---------------------------------------------------------------------------
#
# The split here is the same one the whole application is built on. Reading
# who is in the photographs you can already see is a family-app capability.
# Detecting faces, naming people, confirming matches and merging identities
# are management, and they live on ``admin_only`` — which means that on port
# 5000 those paths do not resolve at all.

def _face_indexer():
    """The shared indexer, created on first use.

    Held on the app rather than made per request because loading the two ONNX
    models takes a moment and they are perfectly reusable. The scan's own is
    used when there is one: making a second loaded both models again — a
    pause on first opening the Faces page, and a second copy in memory
    beside the one the scan was already holding.
    """
    scanner = current_app.config.get("MV_SCANNER")
    shared = getattr(scanner, "_face_indexer", None)
    indexer = shared() if shared else None
    if indexer is not None:
        return indexer
    indexer = current_app.config.get("MV_FACES")
    if indexer is None:
        from ..media.faceindex import FaceIndexer  # noqa: PLC0415
        indexer = FaceIndexer(_cfg())
        current_app.config["MV_FACES"] = indexer
    return indexer


@bp.get("/api/faces/people")
@require_family
def face_people():
    """Named people, counted as this viewer can see them.

    Somebody who appears only in photographs this viewer may not open is not
    in the list at all — not present with a count of zero, which would itself
    disclose that they are somewhere in the library.
    """
    conn = _conn()
    people = db.list_people(conn, _roots(), **_viewer_limits())
    return jsonify({"people": people})


@bp.get("/api/faces/asset/<int:asset_id>")
@require_family
def face_boxes(asset_id: int):
    """The faces in one photograph, for the viewer's overlay."""
    conn = _conn()
    _guard(db.get_asset(conn, asset_id, current_user().id))
    rows = db.faces_for_asset(conn, asset_id)
    for row in rows:
        try:
            row["bbox"] = json.loads(row["bbox"])
        except (TypeError, ValueError):
            row["bbox"] = []
    return jsonify({"faces": rows})


@bp.get("/api/faces/thumb/<int:face_id>")
@require_family
def face_thumb(face_id: int):
    """One 112x112 face crop.

    Guarded through the face's *asset*, not through the face. A crop is a
    piece of the photograph, so anybody who may not open the photograph may
    not have the crop either, and the answer is 404 rather than 403 so the
    existence of the face is not confirmed.
    """
    conn = _conn()
    face = db.get_face(conn, face_id)
    if not face or not face.get("thumb"):
        abort(404)
    _guard(db.get_asset(conn, int(face["asset_id"]), current_user().id))
    path = Path(_cfg().state_dir) / "faces" / str(face["thumb"])
    if not path.exists():
        abort(404)
    response = send_file(path, mimetype="image/jpeg", max_age=604800)
    response.headers["Cache-Control"] = "private, max-age=604800"
    return response


# --- console only ----------------------------------------------------------

@admin_only.get("/api/faces/status")
@require_admin
def faces_status():
    conn = _conn()
    return jsonify(_face_indexer().status(conn, _root()))


@admin_only.post("/api/faces/models")
@require_admin
def faces_download_models():
    """Fetch the two ONNX models. Ninaivu does not ship them."""
    from ..media import faces as faces_mod  # noqa: PLC0415
    result = faces_mod.fetch_models(_cfg().state_dir)
    current_app.config.pop("MV_FACES", None)   # reload with the models present
    scanner = current_app.config.get("MV_SCANNER")
    if scanner is not None:
        scanner._faces = None
    return jsonify(result)


@admin_only.post("/api/faces/scan")
@require_admin
def faces_scan():
    """Detect faces across the library, then regroup.

    A whole library is handed to the scan, whose last pass is exactly this and
    which runs it in the background: with progress on screen, taking turns
    with the other work, and stopping when asked. Done here it held one
    request open for as long as the library took — most of a day on a large
    one — with the button saying "Looking for faces…" and nothing to show or
    stop it. ``limit`` still asks for a small batch, answered here.
    """
    conn = _conn()
    indexer = _face_indexer()
    if not indexer.engine.available:
        abort(409, description=indexer.engine.unavailable_reason
              or "Face recognition is not available.")
    limit = _int_arg("limit", 0)
    if limit:
        result = indexer.detect_pass(conn, _root(), limit=limit)
        if result.get("ok"):
            result["grouping"] = indexer.regroup(conn, _roots())
        return jsonify(result)

    cfg = _cfg()
    if not cfg.faces_enabled:
        abort(409, description="Finding people is switched off. Turn on "
              "\"Find the people in photographs\" in Settings and the scan "
              "will look for faces.")
    pending = sum(db.face_stats(conn, root)["photos_pending"] for root in _roots())
    scanner = _scanner()
    running = bool(scanner.progress.snapshot().get("running"))
    if pending and not running:
        scanner.start()
    return jsonify({"ok": True, "background": True, "pending": pending,
                    "started": bool(pending and not running),
                    "already_running": running})


@admin_only.post("/api/faces/regroup")
@require_admin
def faces_regroup():
    conn = _conn()
    return jsonify(_face_indexer().regroup(conn, _roots()))


@admin_only.get("/api/faces/clusters")
@require_admin
def faces_clusters():
    """Groups the matcher formed that nobody has named yet."""
    conn = _conn()
    clusters = db.list_unnamed_clusters(
        conn, _roots(), max_visibility=current_user().max_visibility,
        min_size=_int_arg("min_size", 3), limit=_int_arg("limit", 60))
    return jsonify({"clusters": clusters})


@admin_only.get("/api/faces/cluster/<cluster_key>")
@require_admin
def faces_cluster_detail(cluster_key: str):
    conn = _conn()
    ids = db.cluster_face_ids(conn, cluster_key, limit=_int_arg("limit", 120))
    rows = []
    for face_id in ids:
        face = db.get_face(conn, face_id)
        if face:
            rows.append({"face_id": face_id, "asset_id": face["asset_id"],
                         "thumb": face["thumb"],
                         "quality": round(float(face["quality"] or 0), 3)})
    return jsonify({"cluster_key": cluster_key, "faces": rows})


@admin_only.post("/api/faces/clusters/name")
@require_admin
def faces_name_cluster():
    data = json_object()
    key = str(data.get("cluster_key", "")).strip()
    name = str(data.get("name", "")).strip()
    if not key or not name:
        abort(400, description="A cluster and a name are both required.")
    conn = _conn()
    try:
        result = _face_indexer().name_cluster(conn, key, name)
    except LookupError:
        abort(409, description="This group has changed since the page loaded. "
                               "The list has been refreshed; please name it again.")
    return jsonify(result)


@admin_only.get("/api/faces/suggestions/<int:person_id>")
@require_admin
def faces_suggestions(person_id: int):
    """The review queue: faces that might be this person, best guess first."""
    conn = _conn()
    return jsonify({
        "person_id": person_id,
        "suggestions": _face_indexer().suggestions(
            conn, person_id, _roots(), limit=_int_arg("limit", 60)),
    })


@admin_only.post("/api/faces/confirm")
@require_admin
def faces_confirm():
    data = json_object()
    face_id, person_id = data.get("face_id"), data.get("person_id")
    if not face_id or not person_id:
        abort(400, description="A face and a person are both required.")
    conn = _conn()
    if not db.get_face(conn, int(face_id)):
        abort(404)
    return jsonify(_face_indexer().confirm(conn, int(face_id), int(person_id)))


@admin_only.post("/api/faces/reject")
@require_admin
def faces_reject():
    data = json_object()
    face_id, person_id = data.get("face_id"), data.get("person_id")
    if not face_id or not person_id:
        abort(400, description="A face and a person are both required.")
    conn = _conn()
    if not db.get_face(conn, int(face_id)):
        abort(404)
    return jsonify(_face_indexer().reject(conn, int(face_id), int(person_id)))


@admin_only.post("/api/faces/person")
@require_admin
def faces_create_person():
    data = json_object()
    name = str(data.get("name", "")).strip()
    if not name:
        abort(400, description="A name is required.")
    conn = _conn()
    return jsonify({"person": db.create_or_update_person_cluster(
        conn, name, data.get("avatar_asset_id"))})


@admin_only.post("/api/faces/person/<int:person_id>/rename")
@require_admin
def faces_rename_person(person_id: int):
    """Fix a name — or, if it is already somebody else's, fold this person
    into them. See :func:`db.rename_person_cluster` for why those are the
    same action.
    """
    data = json_object()
    name = str(data.get("name", "")).strip()
    if not name:
        abort(400, description="A name is required.")
    conn = _conn()
    if not db.get_person_cluster(conn, person_id):
        abort(404)
    result = db.rename_person_cluster(conn, person_id, name)
    if result["merged"]:
        _face_indexer().refresh_person(conn, result["person_id"])
    return jsonify(result)


@admin_only.post("/api/faces/merge")
@require_admin
def faces_merge_people():
    """Fold one person into another — two clusters that were one person."""
    data = json_object()
    source, target = data.get("source_id"), data.get("target_id")
    if not source or not target:
        abort(400, description="Two people are required.")
    conn = _conn()
    moved = db.merge_people(conn, int(source), int(target))
    _face_indexer().refresh_person(conn, int(target))
    return jsonify({"moved": moved, "target_id": int(target)})


@admin_only.delete("/api/faces/person/<int:person_id>")
@require_admin
def faces_delete_person(person_id: int):
    """Forget a person. Their faces become unassigned; no photograph moves."""
    conn = _conn()
    db.delete_person_cluster(conn, person_id)
    return jsonify({"deleted": person_id})
