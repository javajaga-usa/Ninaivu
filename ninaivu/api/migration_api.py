"""Moving Ninaivu to another machine, from the console.

Three things make an installation: the **state folder** (the index, the
thumbnails, accounts, albums, visibility, the archive's record, the
certificate), the **model folder**, and the **library** itself. Copy those
three and Ninaivu is the same on the new machine.

The thing that goes wrong is the library's path. Every row in ``assets``
records an absolute root, and every thumbnail's *filename* is a hash of
``"<root>|<relative path>"`` — so a drive that was ``E:`` here and comes up as
``D:`` there leaves an index pointing at nothing and 366,000 orphaned
thumbnails. Rerooting fixes both in minutes; a rescan takes most of a day and
loses nothing but time.

That has been a command-line tool, which is no use to somebody who has just
copied their photographs onto a new computer and opened the console. This is
the same job (``ninaivu/storage/reroot.py``) with the background work stood
down first, because it rewrites the index underneath a running server and
renames the files that server is busy sending to the gallery.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from flask import Blueprint, current_app, jsonify, request

from ..server.auth import current_user, require_admin
from ..server import auth
from ..storage import db
from ..storage import reroot as reroot_kit
from ._body import json_object
from ..words import said

log = logging.getLogger(__name__)

migration_bp = Blueprint("migration", __name__)

#: The name this job holds the indexer down by, so the strip can say what has
#: the disk. See ninaivu/media/scanner.py.
CLAIM_REROOT = "moving the library to its new folder"

#: Measuring 366,000 thumbnails means walking them, which is seconds. The
#: answer does not change while somebody reads the page, so it is kept.
SIZE_CACHE_SECONDS = 300.0
_sizes: dict[str, tuple[float, dict[str, Any]]] = {}
_sizes_lock = threading.Lock()


def _cfg():
    return current_app.config["MV_CONFIG"]


def _measure(path: Path) -> dict[str, Any]:
    """Files and bytes under *path*, or why it could not be looked at."""
    if not path.exists():
        return {"present": False, "files": 0, "bytes": 0}
    files = total = 0
    for here, _, names in os.walk(path):
        for name in names:
            try:
                total += os.stat(os.path.join(here, name)).st_size
                files += 1
            except OSError:
                continue
    return {"present": True, "files": files, "bytes": total}


def _sized(path: Path) -> dict[str, Any]:
    key = str(path)
    now = time.time()
    with _sizes_lock:
        cached = _sizes.get(key)
        if cached and now - cached[0] < SIZE_CACHE_SECONDS:
            return cached[1]
    measured = _measure(path)
    with _sizes_lock:
        _sizes[key] = (now, measured)
    return measured


def _pieces(cfg, *, sizes: bool) -> list[dict[str, Any]]:
    """The three things to copy, in the order they matter."""
    from ..media import model_catalog                     # noqa: PLC0415

    look = _sized if sizes else (lambda path: {"present": path.exists()})
    state = Path(cfg.state_dir)
    models = model_catalog.models_root()
    out = [
        {
            "id": "state",
            "label": said("Ninaivu's state folder"),
            "path": str(state),
            "what": said("The index, the thumbnails, accounts, albums, visibility, face groups, the archive's record and the certificate. This is the part a rescan cannot rebuild."),
            "note": said("Copy it with Ninaivu stopped. The index is 1 GB and more, and copying it while it is being written gives you a database that will not open."),
            **look(state),
        },
        {
            "id": "models",
            "label": said("The AI models"),
            "path": str(models),
            "what": said("Search, the editing models, face detection and recognition, and which way up a photograph goes."),
            "note": said("Optional: leave it behind and the console will offer every model for download again."),
            **look(models),
        },
    ]
    for root in cfg.roots:
        path = Path(root)
        out.append({
            "id": f"library:{root}",
            "label": said("Your library"),
            "path": root,
            "what": said("The photographs and videos themselves."),
            "note": said("If it lands at a different path on the new machine — a drive letter that changed — reroot it below rather than letting it rescan."),
            **look(path),
        })
    return out


@migration_bp.get("/api/admin/migration")
@require_admin
def migration():
    """What to copy, and what the index currently calls each library folder.

    Sizes are only measured when asked for (``?sizes=1``): walking 366,000
    thumbnails is seconds, and the page is useful before that finishes.
    """
    cfg = _cfg()
    conn = db.connect(cfg.db_path)
    try:
        recorded = reroot_kit.known_roots(conn)
    except Exception:                                     # noqa: BLE001
        log.exception("could not read the library folders from the index")
        recorded = []
    return jsonify({
        "pieces": _pieces(cfg, sizes=request.args.get("sizes") == "1"),
        # What the index says, which is what a reroot has to be given — not
        # what the settings say. After a move those two disagree, and the
        # index is the one that has 196,000 rows pointing at it.
        "recorded_roots": recorded,
        "configured_roots": list(cfg.roots),
        "missing_roots": [root for root in recorded
                          if not Path(root).is_dir()],
        "state_dir": str(cfg.state_dir),
    })


#: How long a re-root waits, in all, for the background jobs to let go.
STAND_DOWN_SECONDS = 60.0


def _stand_everything_down(services) -> list[str]:
    """Stop the background work that would be reading what we are rewriting.

    Returns what was stopped, for the report. The indexer is held by a named
    claim and comes back on its own; the cloud upload is paused and is the
    caller's to restart, because starting an upload somebody did not ask for
    is not a decision this should make. The same goes for the second copy,
    the off-site copy, the repair, the import and the storage check: each
    keyed its work by the old folder, and one left running wrote under it
    while the index was being moved.

    Everything is asked to stop first and waited for after, so the wait is
    the slowest job's rather than the sum of them all.
    """
    from . import admin_api                                # noqa: PLC0415
    stopped = []
    waiting = []
    straightener = getattr(services, "straightener", None)
    if straightener is not None and straightener.running:
        straightener.stop(join=True, timeout=30)
        stopped.append("the straightening pass")
    cloud = getattr(services, "cloud", None)
    engine = getattr(cloud, "_engine", None) if cloud is not None else None
    if engine is not None and engine.running:
        cloud.pause()
        stopped.append("the cloud upload")
        waiting.append(engine)
    for name, label in (("mirror", "the second copy"),
                        ("offsite", "the off-site copy"),
                        ("repairer", "the repair"),
                        ("importer", "the import")):
        job = getattr(services, name, None)
        if job is not None and job.running:
            job.stop(join=False)
            stopped.append(label)
            waiting.append(job)
    if admin_api.stop_scrubber():
        stopped.append("the storage check")
    deadline = time.monotonic() + STAND_DOWN_SECONDS
    for job in waiting:
        thread = getattr(job, "_thread", None)
        if thread is not None:
            thread.join(max(0.0, deadline - time.monotonic()))
    admin_api.stop_scrubber(timeout=max(0.0, deadline - time.monotonic()))
    return stopped


@migration_bp.post("/api/admin/migration/reroot")
@require_admin
def reroot():
    """Point the index at where the library actually is now.

    A dry run changes nothing and says what would happen, which is the only
    honest way to offer something that rewrites every path in the index.
    """
    body = json_object()
    old = str(body.get("from") or "").strip()
    new = str(body.get("to") or "").strip()
    dry_run = bool(body.get("dry_run", True))
    if not old or not new:
        return jsonify({"error": "Give the folder it is recorded as, and the "
                                 "folder it is at now."}), 400

    cfg = _cfg()
    services = current_app.config.get("MV_SERVICES")
    scanner = current_app.config.get("MV_SCANNER")

    from ..archive import scanner as archive_scanner       # noqa: PLC0415
    if archive_scanner.is_scanning():
        return jsonify({"error": "A consolidation is running. Stop it on the "
                                 "Archive page first — it is copying files "
                                 "between the folders this would rename."}), 409

    if dry_run:
        try:
            report = reroot_kit.reroot(cfg.state_dir, old, new, dry_run=True)
        except reroot_kit.Refused as refusal:
            return jsonify({"error": str(refusal)}), 400
        return jsonify({"ok": True, **report.as_dict(), "stopped": []})

    # Where it is at *now* has to be a folder that could be the library. Not
    # checked, a mistyped path emptied the gallery and renamed every
    # thumbnail, and --lock-roots and the system-folder refusal the library
    # picker applies could be stepped around from here.
    from .api_library import _can_be_library              # noqa: PLC0415
    target = Path(new)
    if not target.is_dir() or not _can_be_library(target.resolve(), cfg):
        return jsonify({"error": f"{new} is not a folder the library can be "
                                 "moved to. Check the drive is plugged in and "
                                 "the path is right."}), 400

    stopped = _stand_everything_down(services) if services is not None else []
    try:
        # The indexer is the one that would actively fight this: it walks the
        # library and writes rows for what it finds, against the same table
        # being rewritten. Held for the length of the job, then released.
        if scanner is not None:
            with scanner.held(CLAIM_REROOT):
                report = reroot_kit.reroot(cfg.state_dir, old, new, dry_run=False)
        else:
            report = reroot_kit.reroot(cfg.state_dir, old, new, dry_run=False)
    except reroot_kit.Refused as refusal:
        return jsonify({"error": str(refusal)}), 400
    except Exception as exc:                              # noqa: BLE001
        log.exception("the reroot failed")
        return jsonify({"error": f"The move could not be finished: {exc}"}), 500

    # The settings on disk were rewritten; the ones this process is running on
    # were not, and everything from here until a restart reads those.
    cfg.roots = [new if reroot_kit.normalise(root) == report.old_root else root
                 for root in cfg.roots]
    if cfg.active_root and reroot_kit.normalise(cfg.active_root) == report.old_root:
        cfg.active_root = new
    with _sizes_lock:
        _sizes.clear()

    auth.audit(db.connect(cfg.db_path), current_user().id, "library_reroot",
               f"{report.old_root} -> {report.new_root} "
               f"({report.items} items, {report.renamed} thumbnails)")
    log.info("library rerooted from the console: %s -> %s",
             report.old_root, report.new_root)
    return jsonify({"ok": True, **report.as_dict(), "stopped": stopped})
