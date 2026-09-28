"""Straightening sideways photographs, and reading a phone over USB.

Split out of api.py. The routes, their paths and their blueprints are exactly
as they were."""

from __future__ import annotations


from flask import current_app, jsonify, request
from ..server import auth
from ..media import media, straighten
from ..storage import resume
from ..server.auth import current_user, require_admin
from ._body import json_object

# The blueprints and the shared helpers stay in api.py: these routes
# are registered on the same two blueprints they always were, so every
# path and every endpoint name is unchanged.
from .api import admin_only, _cfg, _conn, _id_list, _int_arg, _recipe, _roots


# ---------------------------------------------------------------------------
# Straightening
# ---------------------------------------------------------------------------
#
# Console only, and for the same reason the library controls are: this rewrites
# thumbnails across the whole library. The family app has no business offering
# it, and on port 5000 these paths do not exist at all.


def _straightener():
    return current_app.config["MV_STRAIGHTEN"]


@admin_only.get("/api/straighten/status")
@require_admin
def straighten_status():
    from ..media import orientnet                            # noqa: PLC0415

    from ..media import faces as faces_mod                    # noqa: PLC0415

    cfg = _cfg()
    summary = _straightener().summary()
    summary["model"] = orientnet.model_status()
    # The tab needs two things, not one: the model that says which way up a
    # photograph goes, and — while straightening is set to only touch
    # photographs with people in them — the face detector that says whether
    # anybody is in it. Reporting only the first is how somebody waits out a
    # 77 MB download and is then told about a second one.
    summary["requires_face"] = bool(
        getattr(cfg, "straighten_requires_face", True))
    summary["faces"] = faces_mod.model_status(cfg.state_dir)
    summary["ready"] = bool(
        summary["model"]["ready"]
        and (not summary["requires_face"] or summary["faces"]["ready"]))
    return jsonify(summary)


@admin_only.post("/api/straighten/model")
@require_admin
def straighten_model():
    """Fetch everything the tab needs, in one press.

    Both models, because needing two and being told about them one at a time
    is the same wait split into two disappointments.
    """
    from ..media import faces as faces_mod, orientnet         # noqa: PLC0415

    cfg = _cfg()
    result = orientnet.fetch_model()
    orientnet.reset()
    if result.get("error"):
        return jsonify({"error": result["error"]}), 502

    if getattr(cfg, "straighten_requires_face", True):
        faces = faces_mod.fetch_models(cfg.state_dir)
        result["faces"] = faces
        if faces.get("errors"):
            return jsonify({
                "error": "The orientation model downloaded, but the face "
                         "models did not: " + "; ".join(faces["errors"]),
            }), 502
    return jsonify(result)


@admin_only.post("/api/straighten/survey")
@require_admin
def straighten_survey():
    data = json_object()
    started = _straightener().survey(
        _roots(),
        limit=_int_arg("limit") or None,
        rescan=bool(data.get("rescan")),
    )
    if not started:
        return jsonify({"error": "A straightening pass is already running."}), 409
    return jsonify({"ok": True, "started": True})


@admin_only.post("/api/straighten/stop")
@require_admin
def straighten_stop():
    _straightener().stop()
    # Stopped by somebody, so not carried on after a restart. A shutdown stops
    # it too, without this, and that one is.
    resume.done(_conn(), straighten.RESUME_NAME)
    return jsonify({"ok": True})


@admin_only.get("/api/straighten/proposals")
@require_admin
def straighten_proposals():
    """The turns waiting to be looked at, strongest first."""
    conn = _conn()
    limit = min(max(_int_arg("limit", 60), 1), 200)
    offset = max(_int_arg("offset", 0), 0)
    rows = conn.execute(
        "SELECT p.asset_id, p.rotation, p.confidence, a.filename, a.folder, "
        "       a.thumb, a.width, a.height, a.indexed_at "
        "FROM orientation_proposals p JOIN assets a ON a.id=p.asset_id "
        "WHERE p.status='pending' "
        "ORDER BY p.confidence DESC, p.asset_id "
        "LIMIT ? OFFSET ?", (limit, offset)).fetchall()
    total = conn.execute("SELECT COUNT(*) n FROM orientation_proposals "
                         "WHERE status='pending'").fetchone()["n"]
    return jsonify({
        "total": total,
        "offset": offset,
        "items": [{
            "id": r["asset_id"],
            "rotation": r["rotation"],
            "confidence": round(r["confidence"], 3),
            "filename": r["filename"],
            "folder": r["folder"],
            "thumb": f"/api/thumb/{r['asset_id']}?s=320",
            "thumb_v": media.thumb_version(r, _recipe()),
        } for r in rows],
    })


@admin_only.post("/api/straighten/apply")
@require_admin
def straighten_apply():
    data = json_object()
    ids = _id_list(data, limit=None)
    try:
        floor = float(data.get("min_confidence") or 0.0)
    except (TypeError, ValueError):
        return jsonify({"error": "A confidence is a number from 0 to 1."}), 400
    if not ids and floor <= 0:
        return jsonify({"error": "Choose photographs, or a confidence to "
                                 "apply above."}), 400
    started = _straightener().apply(ids or None, floor)
    if not started:
        return jsonify({"error": "A straightening pass is already running."}), 409
    auth.audit(_conn(), current_user().id, "straighten_apply",
               f"{len(ids) or 'all'} above {floor}")
    return jsonify({"ok": True, "started": True})


@admin_only.post("/api/straighten/dismiss")
@require_admin
def straighten_dismiss():
    data = json_object()
    ids = _id_list(data, limit=None)
    return jsonify({"ok": True, "dismissed": _straightener().dismiss(ids)})


@admin_only.post("/api/straighten/undo")
@require_admin
def straighten_undo():
    """Put the last applied batch back, thumbnails and all."""
    data = json_object()
    batch = data.get("batch")
    result = _straightener().undo(int(batch) if batch else None)
    auth.audit(_conn(), current_user().id, "straighten_undo",
               f"batch {result.get('batch')}: {result.get('restored')} restored")
    return jsonify({"ok": True, **result})


# ---------------------------------------------------------------------------
# Phones and cameras
# ---------------------------------------------------------------------------
#
# Console only. Reading a device enumerates everything on somebody's phone,
# which is management, not something the family app offers.


@admin_only.get("/api/devices")
@require_admin
def devices_list():
    """Every phone or camera Windows can see, or why it cannot see any.

    Deliberately its own endpoint and the first thing the Archive tab calls:
    a device that cannot be listed will never be copied from either, and
    finding that out in a second beats finding it out forty minutes into a
    transfer.
    """
    from ..utils import devices                                   # noqa: PLC0415

    reason = devices.unavailable_reason()
    if reason:
        return jsonify({"available": False, "reason": reason, "devices": []})
    try:
        found = devices.list_devices()
    except Exception as exc:                                # noqa: BLE001
        return jsonify({"available": True, "error": str(exc), "devices": []}), 200
    return jsonify({
        "available": True,
        "devices": found,
        "hint": ("Nothing is listed. Unlock the phone and tap Trust — Windows "
                 "hides the storage until you do." if not found else ""),
    })


@admin_only.get("/api/devices/browse")
@require_admin
def devices_browse():
    """List one folder on a device, so the picker can walk into it."""
    from ..utils import devices                                   # noqa: PLC0415

    reason = devices.unavailable_reason()
    if reason:
        return jsonify({"error": reason}), 501
    raw = request.args.get("path", "").strip()
    try:
        entries = devices.list_folder(raw)
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except Exception as exc:                                # noqa: BLE001
        return jsonify({"error": f"That device did not answer: {exc}"}), 502
    return jsonify({
        "path": raw or "This PC",
        "entries": entries,
        "folders": [e for e in entries if e.get("is_folder")],
        "files": sum(1 for e in entries if not e.get("is_folder")),
    })
