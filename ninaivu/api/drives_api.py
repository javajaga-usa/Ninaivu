"""A pendrive, an external hard drive or a phone was plugged in: the console
can ask whether to bring its photos in or to copy the library out to it.
From Ninaivu Lite 1.5.

Every route is for an administrator, on the console only. Bringing photos in
is the Import page with the drive (or the phone, which the archive reads as a
source directly) as its source; copying out is :class:`utils.drives.Exporter`.
A phone on a Mac is not readable as files: the console says so and can open
Image Capture on the Ninaivu computer. The question is a notice at the top of
the console, never a window of the operating system's own: it used to be one on
a Mac (``osascript``), which opened over whatever the person was doing, outside
the browser, and could not take them to the page the answer was for.
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
from ..words import refusal, said
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


def _archive_settings() -> dict:
    try:
        from ..archive import database as adb                     # noqa: PLC0415

        return adb.load_settings() or {}
    except Exception:  # noqa: BLE001 — no archive yet
        return {}


def what_it_holds(drive: drives.Drive, cfg) -> str | None:
    """What of Ninaivu's own lives on *drive*: "library", "data" (Ninaivu's
    own folder), "import" (the import's destination), "copy" (the second
    copy, the backups or the off-site folder), or None."""
    if drive.kind == "phone":
        return None
    homes = [*(("library", str(r)) for r in cfg.roots), ("data", str(cfg.state_dir))]
    # The Secondary's originals disk (mugil/hold.py) is a copy of the library
    # too: offered for import it would bring every photograph in again.
    homes += [("copy", str(getattr(cfg, name, "") or ""))
              for name in ("mirror_dir", "backup_dir", "mugil_originals_dir")]
    if getattr(cfg, "offsite_kind", "folder") == "folder":
        homes.append(("copy", str(getattr(cfg, "offsite_folder", "") or "")))
    homes.append(("import", str(_archive_settings().get("destination_dir") or "")))
    for role, path in homes:
        if path and drives.is_within(path, drive.path):
            return role
    return None


def holds_library(drive: drives.Drive, cfg) -> bool:
    """The library, Ninaivu's own data, the import's destination, the second
    copy or the backups live on it: nothing to ask. (Importing from the
    second-copy disk would bring the whole library in again as duplicates.)"""
    return what_it_holds(drive, cfg) is not None


def import_sources_on(drive: drives.Drive) -> list[str]:
    """The Import page's saved source folders that are on *drive*."""
    if drive.shell or not drive.readable:
        return []
    found = []
    for entry in _archive_settings().get("source_dirs") or []:
        path = str((entry.get("path") if isinstance(entry, dict) else entry) or "")
        if path and drives.is_within(path, drive.path):
            found.append(path)
    return found


def in_use(drive: drives.Drive) -> str | None:
    """Why *drive* must not be pulled out now: "import" while the import
    reads from it or writes to it, "export" while the library is copied onto
    it; None when nothing of Ninaivu's is using it."""
    try:
        from ..archive import scanner as archive_scanner          # noqa: PLC0415

        if any(drives.is_within(path, drive.path) for path in archive_scanner.paths_in_use()):
            return "import"
    except Exception:  # noqa: BLE001 — no archive engine: nothing importing
        pass
    state = exporter().progress()
    if state.get("running") and drives.is_within(state.get("destination") or "", drive.path):
        return "export"
    return None


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
    from ..storage import library_id                              # noqa: PLC0415

    cfg = _cfg()
    pending = {d.id for d in watcher().pending()}
    out = []
    for d in watcher().drives():
        holds = what_it_holds(d, cfg)
        out.append({**d.to_json(), "holds_library": holds is not None, "holds": holds,
                    "sources": import_sources_on(d), "in_use": in_use(d),
                    "quiet": watcher().is_quiet(d),
                    "pending": d.id in pending and holds is None})
    # A library folder that is not connected, found by its id on another
    # disk: the console asks which folder is the library (library_drives.py).
    moved = [{"old": old, "found": list(entry.get("found") or []),
              "confirmed": entry.get("confirmed")}
             for old, entry in library_id.pending_relocations(cfg.state_dir).items()]
    return jsonify({"drives": out, "export": exporter().progress(),
                    "image_capture": sys.platform == "darwin",
                    "moved_libraries": moved})


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


#: What the console says when a drive cannot be ejected because of what is on it.
_HOLDS_REFUSAL = {
    "library": said("This drive holds the photo library. Stop Ninaivu before you unplug it."),
    "data": said("This drive holds Ninaivu's own data. Stop Ninaivu before you unplug it."),
    "import": said("Imports are copied onto this drive. Stop Ninaivu, or choose another destination on the Import page, before you unplug it."),
    "copy": said("This drive holds the second copy or the backups of the library. Stop Ninaivu before you unplug it."),
}


@drives_bp.post("/api/admin/drives/eject")
@require_admin
def eject():
    """Let a drive go so it can be pulled out safely. Not the library's own
    drive, and not while an import or a copy to it is running."""
    drive = _drive()
    if drive is None:
        return _refuse(404, "That drive is no longer plugged in.")
    holds = what_it_holds(drive, _cfg())
    if holds:
        return _refuse(409, _HOLDS_REFUSAL[holds])
    busy = in_use(drive)
    if busy == "import":
        return _refuse(409, said("The import is using this drive. Wait until it finishes, or press Stop on the Import page, then eject it."))
    if busy == "export":
        return _refuse(409, said("The library is being copied onto this drive. Wait until the copy finishes, or stop it, then eject it."))
    try:
        drives.eject(drive)
    except drives.EjectRefused as exc:
        return jsonify(refusal(
            said("The drive could not be ejected: {why}. Close anything that has a file open on it, then try again."),
            why=exc)), 409
    watcher().forget(drive.id)
    return jsonify({"ok": True, "message": said("You can unplug the drive now.")})


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
