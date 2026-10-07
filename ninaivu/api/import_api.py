"""The console's import from Google Photos and iCloud (ninaivu/storage/importer.py).

Console only: it reads whatever folder on this machine it is pointed at and
writes into the library.
"""

from __future__ import annotations

from pathlib import Path

from flask import Blueprint, current_app, jsonify

from ..server import auth
from ..server.auth import current_user, require_admin
from ..storage import db
from ..storage import importer as importer_mod
from ._body import json_object

import_bp = Blueprint("import", __name__)


def _cfg():
    return current_app.config["MV_CONFIG"]


def _importer() -> importer_mod.Importer:
    return current_app.config["MV_SERVICES"].importer


def _folder(data) -> tuple[Path | None, str | None]:
    """The export folder asked for, or why it cannot be used."""
    raw = str((data or {}).get("folder") or "").strip()
    if not raw:
        return None, "Choose the folder the export was downloaded into."
    folder = Path(raw).expanduser()
    if not folder.is_absolute():
        return None, "Give the whole path to the folder."
    if not folder.is_dir():
        return None, "That folder is not there."
    resolved = folder.resolve()
    cfg = _cfg()
    for root in [*(cfg.roots or []), str(cfg.state_dir)]:
        other = Path(root).expanduser().resolve()
        if resolved == other or other in resolved.parents or resolved in other.parents:
            # Importing the library into itself would find every photograph a
            # duplicate of itself; a folder holding the library, the same.
            return None, (f"That folder is part of {other}, which Ninaivu already uses. "
                          "Choose the folder the export was downloaded into.")
    return resolved, None


def _root(data) -> tuple[str | None, str | None]:
    cfg = _cfg()
    libraries = list(cfg.libraries or ([cfg.active_root] if cfg.active_root else []))
    wanted = str((data or {}).get("library") or "").strip() or (
        cfg.active_root if cfg.active_root in libraries else (libraries[0] if libraries else ""))
    if not wanted or wanted not in libraries:
        return None, "Choose which library folder the photographs go into."
    return wanted, None


@import_bp.post("/api/import/look")
@require_admin
def look():
    """What an import from a folder would find. Copies nothing."""
    folder, problem = _folder(json_object())
    if problem:
        return jsonify({"error": problem, "status": 400}), 400
    return jsonify(importer_mod.look(folder))


@import_bp.post("/api/import/start")
@require_admin
def start():
    # json_object: a body that is not an object is a 400, where a list
    # reached .get() below and answered 500.
    data = json_object()
    folder, problem = _folder(data)
    if problem:
        return jsonify({"error": problem, "status": 400}), 400
    root, problem = _root(data)
    if problem:
        return jsonify({"error": problem, "status": 400}), 400
    result = _importer().start(str(folder), root, current_user().id)
    if not result.get("started"):
        return jsonify({"error": result.get("reason"), "status": 409}), 409
    auth.audit(db.connect(_cfg().db_path), current_user().id, "import",
               f"from {folder} into {root}")
    return jsonify({"ok": True, **_importer().status()})


@import_bp.post("/api/import/stop")
@require_admin
def stop():
    _importer().stop()
    return jsonify({"ok": True, **_importer().status()})


@import_bp.get("/api/import/status")
@require_admin
def status():
    cfg = _cfg()
    libraries = list(cfg.libraries or ([cfg.active_root] if cfg.active_root else []))
    return jsonify({**_importer().status(), "libraries": libraries})
