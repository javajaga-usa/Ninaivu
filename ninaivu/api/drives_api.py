"""A pendrive, an external hard drive or a phone was plugged in: the console
can ask whether to bring its photos in or to copy the library out to it.
From Ninaivu Lite 1.5.

Every route is for an administrator, on the console only. Bringing photos in
is the Import page with the drive (or the phone, which the archive reads as a
source directly) as its source; copying out is :class:`utils.drives.Exporter`.
A phone on a Mac is not readable as files: the console says so and can open
Image Capture on the Ninaivu computer. On a Mac the same question is also
asked in a window on the computer itself (:mod:`utils.drive_dialog`).
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

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


#: Drives set aside with "Don't ask about this drive again", in the state folder.
QUIET_FILE = "drives-not-asked.json"


def watcher() -> drives.Watcher:
    remember = Path(_cfg().state_dir) / QUIET_FILE
    return _shared("ninaivu.drives", lambda: drives.Watcher(remember=remember))


def exporter() -> drives.Exporter:
    return _shared("ninaivu.drive_export", drives.Exporter)


def holds_library(drive: drives.Drive, cfg) -> bool:
    """The library, Ninaivu's own data or the import's destination lives on
    it: nothing to ask."""
    if drive.kind == "phone":
        return False
    homes = [*(str(r) for r in cfg.roots), str(cfg.state_dir)]
    try:
        from ..archive import database as adb                     # noqa: PLC0415

        homes.append(str(adb.load_settings().get("destination_dir") or ""))
    except Exception:  # noqa: BLE001 — no archive yet: the library is enough
        pass
    return any(drives.is_within(path, drive.path) for path in homes if path)


def _holds_library(drive: drives.Drive) -> bool:
    return holds_library(drive, _cfg())


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
                    "quiet": watcher().is_quiet(d),
                    "pending": d.id in pending and not home})
    return jsonify({"drives": out, "export": exporter().progress(),
                    "image_capture": sys.platform == "darwin"})


@drives_bp.post("/api/admin/drives/answer")
@require_admin
def answer():
    """Import chosen (the Import page takes it from here), Not now, or
    ``never``: not asked about again, even after a restart."""
    body = json_object()
    drive = _drive()
    if drive is None:
        return _refuse(404, "That drive is no longer plugged in.")
    if body.get("never") is True:
        watcher().never_ask(drive)
    else:
        watcher().answer(drive.id)
    return jsonify({"ok": True})


@drives_bp.post("/api/admin/drives/ask-again")
@require_admin
def ask_again():
    """Undo "Don't ask about this drive again"."""
    drive = _drive()
    if drive is None:
        return _refuse(404, "That drive is no longer plugged in.")
    watcher().ask_again(drive)
    return jsonify({"ok": True})


@drives_bp.post("/api/admin/drives/image-capture")
@require_admin
def image_capture():
    """A phone on a Mac: open Image Capture on the Ninaivu computer, which
    can copy its photos into a folder the Import page then reads."""
    if sys.platform != "darwin":
        return _refuse(409, "Image Capture is only on a Mac.")
    try:
        subprocess.run(["open", "-a", "Image Capture"], check=True, timeout=15,
                       capture_output=True)
    except (OSError, subprocess.SubprocessError):
        return _refuse(500, "Image Capture could not be opened.")
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
    exporter().start(drive, folders, skip=export_skips(cfg, folders))
    return jsonify({"ok": True, "destination": drives.export_root(drive.path),
                    "message": "Copying the library to the drive."})


def export_skips(cfg, folders: list[str]) -> list[str]:
    """What a copy to a drive leaves out: the state folder, and in each
    library the recycle bin and whatever else a scan steps over.

    What was deleted (and the originals kept from before a rotation, which
    live in the bin too) must not turn up again on a pendrive handed to a
    relative.
    """
    from ..storage.recycle import BIN_NAME                         # noqa: PLC0415
    names = set(cfg.ignore_dirs or ()) | {BIN_NAME}
    return [str(cfg.state_dir), *(os.path.join(folder, name)
                                  for folder in folders for name in sorted(names))]


@drives_bp.get("/api/admin/drives/export")
@require_admin
def export_status():
    return jsonify(exporter().progress())


@drives_bp.post("/api/admin/drives/export/stop")
@require_admin
def export_stop():
    exporter().stop()
    return jsonify({"ok": True, "message": "Stopping after the current file."})
