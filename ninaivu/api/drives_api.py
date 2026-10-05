"""A pendrive, an external hard drive or a phone was plugged in: the console
can ask whether to bring its photos in or to copy the library out to it.
From Ninaivu Lite 1.5.

Every route is for an administrator, on the console only. Bringing photos in
is the Import page with the drive (or the phone, which the archive reads as a
source directly) as its source; copying out is :class:`utils.drives.Exporter`.
"""

from __future__ import annotations

import threading

from flask import Blueprint, current_app, jsonify

from ..server.auth import require_admin
from ..utils import drives
from ._body import json_object

drives_bp = Blueprint("drives", __name__)

_made = threading.Lock()


def _cfg():
    return current_app.config["MV_CONFIG"]


def _shared(name: str, factory):
    """One watcher and one exporter per app: the console polls from several
    request threads, and two exports to one drive must not run at once."""
    with _made:
        if name not in current_app.extensions:
            current_app.extensions[name] = factory()
        return current_app.extensions[name]


def watcher() -> drives.Watcher:
    return _shared("ninaivu.drives", drives.Watcher)


def exporter() -> drives.Exporter:
    return _shared("ninaivu.drive_export", drives.Exporter)


def _holds_library(drive: drives.Drive) -> bool:
    """The library (or Ninaivu's own data) lives on it: nothing to ask."""
    if drive.kind == "phone":
        return False
    cfg = _cfg()
    homes = [*(str(r) for r in cfg.roots), str(cfg.state_dir)]
    return any(drives.is_within(path, drive.path) for path in homes if path)


def _refuse(status: int, message: str):
    return jsonify({"error": message}), status


def _drive():
    drive_id = str(json_object().get("id") or "")
    return watcher().find(drive_id) if drive_id else None


@drives_bp.get("/api/admin/drives")
@require_admin
def listed():
    pending = {d.id for d in watcher().pending()}
    out = []
    for d in watcher().drives():
        home = _holds_library(d)
        out.append({**d.to_json(), "holds_library": home,
                    "pending": d.id in pending and not home})
    return jsonify({"drives": out, "export": exporter().progress()})


@drives_bp.post("/api/admin/drives/answer")
@require_admin
def answer():
    """Import chosen (the Import page takes it from here) or Not now."""
    drive = _drive()
    if drive is None:
        return _refuse(404, "That drive is no longer plugged in.")
    watcher().answer(drive.id)
    return jsonify({"ok": True})


@drives_bp.post("/api/admin/drives/export")
@require_admin
def export():
    drive = _drive()
    if drive is None:
        return _refuse(404, "That drive is no longer plugged in.")
    watcher().answer(drive.id)
    cfg = _cfg()
    if drive.kind == "phone":
        return _refuse(409, "Copying the library onto a phone isn't offered. Use a "
                            "pendrive or an external drive.")
    folders = list(cfg.libraries)
    if not folders:
        return _refuse(409, "There is no library to copy yet. Choose a photo folder first.")
    if exporter().running:
        return _refuse(409, "A copy to a drive is already running.")
    if _holds_library(drive):
        return _refuse(409, "This drive holds the library itself, so it cannot be "
                            "copied onto it.")
    exporter().start(drive, folders, skip=[str(cfg.state_dir)])
    return jsonify({"ok": True, "destination": drives.export_root(drive.path),
                    "message": "Copying the library to the drive."})


@drives_bp.get("/api/admin/drives/export")
@require_admin
def export_status():
    return jsonify(exporter().progress())


@drives_bp.post("/api/admin/drives/export/stop")
@require_admin
def export_stop():
    exporter().stop()
    return jsonify({"ok": True, "message": "Stopping after the current file."})
