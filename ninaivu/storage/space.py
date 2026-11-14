"""Where the bytes are.

It only ever reads: the index for the library, a walk of the state folder
for Ninaivu's own files.

**The library** is grouped by kind of file (:data:`CLASSES`), by year and by
folder. No file is opened: a file's kind is told from its extension, and an
iPhone's video from its camera. (Until 4.2 each kind also carried an estimate
of what a smaller "light copy" of it would take. Ninaivu makes no smaller
copies any more, and the estimate went with them.)

**Duplicates** are the exact copies the content fingerprints know of
(storage/fingerprints.py, when ``content_fingerprints`` is on), and the
groups of near-identical photographs the perceptual hash already keeps.

**The state folder** is Ninaivu's own: the index, the thumbnails, the
playable copies of videos, the kept backup bundles, the models. Walking
366,000 thumbnails takes seconds, so that half is kept for a few minutes.

Everything the household has is counted, hidden and explicit files included:
this is an administrator's page, and administrators see everything.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from ..server.config import RAW_EXTS

#: How long the state folder's sizes are kept before they are walked again.
STATE_MAX_AGE = 600.0

#: The kinds of file, in the order a report lists them.
CLASSES: tuple[str, ...] = (
    "jpeg",              # a camera's or an Android phone's JPEG
    "raw",
    "png",               # PNG, screenshots among them
    "heic",              # an iPhone's HEIC
    "other_picture",     # GIF, TIFF, BMP, WebP, AVIF
    "video",             # MP4, MOV, M4V, WebM
    "legacy_video",      # AVI, MKV, WMV, MTS, 3GP, MPEG
    "phone_video",       # an iPhone's own video
    "audio",
    "other",
)

JPEG_EXTS = {"jpg", "jpeg", "jfif"}
HEIC_EXTS = {"heic", "heif"}
LEGACY_VIDEO_EXTS = {"avi", "mkv", "wmv", "flv", "mpg", "mpeg", "3gp", "ts", "mts", "m2ts"}
_RAW = {ext.lstrip(".") for ext in RAW_EXTS}


def classify(ext: str, kind: str, camera: str | None) -> str:
    """The class (:data:`CLASSES`) of a file, from what the index has on it."""
    ext = (ext or "").lower().lstrip(".")
    if kind == "picture":
        if ext in JPEG_EXTS:
            return "jpeg"
        if ext in _RAW:
            return "raw"
        if ext == "png":
            return "png"
        if ext in HEIC_EXTS:
            return "heic"
        return "other_picture"
    if kind == "video":
        if ext in LEGACY_VIDEO_EXTS:
            return "legacy_video"
        if _from_an_iphone(camera) and ext in {"mov", "mp4"}:
            return "phone_video"
        return "video"
    if kind == "audio":
        return "audio"
    return "other"


def _from_an_iphone(camera: str | None) -> bool:
    name = (camera or "").casefold()
    return "iphone" in name or name.startswith("apple")


def _roots_clause(roots: Iterable[str], column: str = "root") -> tuple[str, list[str]]:
    roots = [str(r) for r in roots]
    if not roots:
        return "0", []
    return "%s IN (%s)" % (column, ",".join("?" * len(roots))), roots


# -- the library -------------------------------------------------------------

def library_report(conn: sqlite3.Connection, roots: Iterable[str], *,
                   top_folders: int = 20) -> dict[str, Any]:
    """The library's files by class, by year and by folder. Files in the
    bin are left out: they are on their way out."""
    where, params = _roots_clause(roots)
    base = f"WHERE trashed=0 AND {where}"

    classes = {key: {"files": 0, "bytes": 0} for key in CLASSES}
    for row in conn.execute(
            f"SELECT ext, kind, camera, COUNT(*) AS files, COALESCE(SUM(size), 0) AS bytes "
            f"FROM assets {base} GROUP BY ext, kind, camera", params):
        entry = classes[classify(row["ext"], row["kind"], row["camera"])]
        entry["files"] += int(row["files"])
        entry["bytes"] += int(row["bytes"])

    listed = [{"class": key, "files": classes[key]["files"], "bytes": classes[key]["bytes"]}
              for key in CLASSES]
    total = sum(c["bytes"] for c in listed)

    years = [{"year": row["year"] or "", "files": int(row["files"]), "bytes": int(row["bytes"])}
             for row in conn.execute(
                 f"SELECT substr(date_key, 1, 4) AS year, COUNT(*) AS files, "
                 f"COALESCE(SUM(size), 0) AS bytes FROM assets {base} "
                 f"GROUP BY year ORDER BY year", params)]

    folders = [{"root": row["root"], "folder": row["folder"], "files": int(row["files"]),
                "bytes": int(row["bytes"])}
               for row in conn.execute(
                   f"SELECT root, folder, COUNT(*) AS files, COALESCE(SUM(size), 0) AS bytes "
                   f"FROM assets {base} GROUP BY root, folder "
                   f"ORDER BY bytes DESC, root, folder LIMIT ?", (*params, int(top_folders)))]

    return {
        "files": sum(c["files"] for c in listed),
        "bytes": total,
        "classes": listed,
        "years": years,
        "folders": folders,
    }


def duplicates_report(conn: sqlite3.Connection, roots: Iterable[str]) -> dict[str, Any]:
    """Exact copies by fingerprint, and near-identical groups by perceptual
    hash. Nothing is offered for removal here; this only says how much."""
    roots = list(roots)
    where, params = _roots_clause(roots)
    joined, _ = _roots_clause(roots, "a.root")
    exact: dict[str, Any] = {"available": False, "groups": 0, "spare_files": 0, "spare_bytes": 0}
    try:
        from .fingerprints import ALGO                     # noqa: PLC0415
        row = conn.execute(
            "SELECT COUNT(*) AS groups, COALESCE(SUM(copies - 1), 0) AS spare_files, "
            "COALESCE(SUM((copies - 1) * size), 0) AS spare_bytes FROM ("
            " SELECT f.size AS size, COUNT(*) AS copies FROM content_fingerprints f"
            " JOIN assets a ON a.id=f.asset_id"
            " WHERE f.algo=? AND a.trashed=0 AND f.size=a.size AND f.mtime=a.mtime"
            f" AND {joined}"
            " GROUP BY f.digest, f.size HAVING COUNT(*) > 1)",
            (ALGO, *params)).fetchone()
        fingerprinted = conn.execute(
            "SELECT COUNT(*) FROM content_fingerprints WHERE algo=?", (ALGO,)).fetchone()[0]
        exact = {"available": bool(fingerprinted), "groups": int(row["groups"]),
                 "spare_files": int(row["spare_files"]), "spare_bytes": int(row["spare_bytes"])}
    except sqlite3.OperationalError:
        pass                                   # an index from before fingerprints

    near = conn.execute(
        "SELECT COUNT(*) AS groups, COALESCE(SUM(files), 0) AS files FROM ("
        f" SELECT COUNT(*) AS files FROM assets WHERE trashed=0 AND {where}"
        " AND dup_group IS NOT NULL AND dup_group != '' GROUP BY dup_group"
        " HAVING COUNT(*) > 1)", params).fetchone()
    return {"exact": exact,
            "similar": {"groups": int(near["groups"]), "files": int(near["files"])}}


# -- the state folder --------------------------------------------------------

def folder_bytes(path: Path) -> tuple[int, int]:
    """``(files, bytes)`` under *path*, never following a link, never
    stopping at a folder it may not read."""
    files = size = 0
    pending = [str(path)]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            files += 1
                            size += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return files, size


def _measure(path: Path) -> dict[str, Any]:
    try:
        if path.is_dir() and not path.is_symlink():
            files, size = folder_bytes(path)
        else:
            files, size = 1, path.lstat().st_size
    except OSError:
        files, size = 0, 0
    return {"name": path.name, "files": files, "bytes": size}


def state_report(state_dir: Path, *, elsewhere: Iterable[tuple[str, str]] = ()) -> dict[str, Any]:
    """Ninaivu's own files: each entry at the top of the state folder, largest
    first, and any folder of Ninaivu's kept outside it (*elsewhere*: ``(what,
    path)`` pairs such as the backup or the AI models folder)."""
    state_dir = Path(state_dir)
    entries: list[dict[str, Any]] = []
    try:
        children = sorted(state_dir.iterdir())
    except OSError:
        children = []
    for child in children:
        entries.append(_measure(child))
    entries.sort(key=lambda e: (-e["bytes"], e["name"]))

    outside = []
    for what, place in elsewhere:
        if not place:
            continue
        path = Path(place).expanduser()
        try:
            inside = path.resolve().is_relative_to(state_dir.resolve())
        except OSError:
            inside = False
        if inside or not path.exists():
            continue
        outside.append({"what": what, "path": str(path), **_measure(path)})

    return {"path": str(state_dir), "bytes": sum(e["bytes"] for e in entries),
            "files": sum(e["files"] for e in entries), "entries": entries,
            "elsewhere": outside, "measured_at": time.time()}


_state_lock = threading.Lock()
_state_cache: dict[tuple, dict[str, Any]] = {}


def cached_state_report(state_dir: Path, *, elsewhere: Iterable[tuple[str, str]] = (),
                        fresh: bool = False) -> dict[str, Any]:
    """:func:`state_report`, kept for :data:`STATE_MAX_AGE` seconds. One walk
    at a time: a second caller waits for the first and takes its answer."""
    key = (str(state_dir), tuple(elsewhere))
    with _state_lock:
        held = _state_cache.get(key)
        if held and not fresh and time.time() - held["measured_at"] < STATE_MAX_AGE:
            return held
        report = state_report(state_dir, elsewhere=key[1])
        _state_cache[key] = report
        return report


# -- the forecast ------------------------------------------------------------
#
# How fast the library grows, and when each of its disks fills at that rate.
# The rate is the household's own new photographs and videos: files added in
# the last year that were taken within NEW_WITHIN_DAYS of being added. An
# import of an old archive (a whole drive of 2009 at once) is a one-off, not
# a rate, and counting it would say the disk fills next month. Too little
# history (under MIN_HISTORY_DAYS of it) and the forecast says so instead of
# guessing.

YEAR_DAYS = 365.0
MONTH_DAYS = 30.44
NEW_WITHIN_DAYS = 60.0
MIN_HISTORY_DAYS = 60.0


def growth(conn: sqlite3.Connection, roots: Iterable[str], *, now: float | None = None
           ) -> dict[str, Any]:
    """Bytes a month of new photographs and videos, from the last year."""
    now = now or time.time()
    since = now - YEAR_DAYS * 86400
    where, params = _roots_clause(roots)
    # One read of the library for all three figures: asked for one at a time,
    # each walked every row (three walks, a quarter of a second at 210,000
    # items, on every open of the Space screen, and again for each disk).
    recent = "trashed=0 AND added_at >= ?"
    taken = "COALESCE(captured_at, mtime)"
    window = (since, NEW_WITHIN_DAYS * 86400)
    sums = conn.execute(
        "SELECT MIN(CASE WHEN added_at > 0 THEN added_at END) AS first, "
        f"SUM(CASE WHEN {recent} AND {taken} >= added_at - ? THEN 1 ELSE 0 END) AS new_files, "
        f"SUM(CASE WHEN {recent} AND {taken} >= added_at - ? THEN size ELSE 0 END) AS new_bytes, "
        f"SUM(CASE WHEN {recent} AND {taken} < added_at - ? THEN 1 ELSE 0 END) AS old_files, "
        f"SUM(CASE WHEN {recent} AND {taken} < added_at - ? THEN size ELSE 0 END) AS old_bytes "
        f"FROM assets WHERE {where}",
        (*window, *window, *window, *window, *params)).fetchone()
    first = sums["first"]
    row = {"files": sums["new_files"] or 0, "bytes": sums["new_bytes"] or 0}
    imported = {"files": sums["old_files"] or 0, "bytes": sums["old_bytes"] or 0}
    days = min(YEAR_DAYS, (now - float(first)) / 86400) if first else 0.0
    out = {"history_days": round(days, 1), "new_files": int(row["files"]),
           "new_bytes": int(row["bytes"]), "imported_files": int(imported["files"]),
           "imported_bytes": int(imported["bytes"]), "bytes_per_month": None}
    if days >= MIN_HISTORY_DAYS:
        out["bytes_per_month"] = round(int(row["bytes"]) / days * MONTH_DAYS)
    return out


def forecast(conn: sqlite3.Connection, roots: Iterable[str], *, now: float | None = None,
             steps: Iterable[tuple[str, int]] = (), disk_usage=None) -> dict[str, Any]:
    """When each disk holding a library fills at the library's rate, and how
    many months each step (``(name, bytes it would free)``) would add."""
    import shutil                                          # noqa: PLC0415
    now = now or time.time()
    roots = list(roots)
    usage = disk_usage or shutil.disk_usage
    rate = growth(conn, roots, now=now)
    per_month = rate["bytes_per_month"]
    disks: dict[Any, dict[str, Any]] = {}
    for root in roots:
        try:
            device = os.stat(root).st_dev
            space = usage(root)
        except OSError:
            continue
        disk = disks.setdefault(device, {"roots": [], "free_bytes": int(space.free),
                                         "total_bytes": int(space.total)})
        disk["roots"].append(root)
    listed = []
    for disk in disks.values():
        share = per_month
        if per_month and len(disks) > 1:
            # Several disks: each grows by its own libraries' share of the rate.
            share = growth(conn, disk["roots"], now=now)["bytes_per_month"] or 0
        months = (disk["free_bytes"] / share) if share else None
        listed.append({**disk, "bytes_per_month": share,
                       "months_left": round(months, 1) if months is not None else None,
                       "full_at": now + months * MONTH_DAYS * 86400 if months is not None else None})
    listed.sort(key=lambda d: (d["months_left"] is None, d["months_left"] or 0))
    return {"growth": rate, "disks": listed,
            "steps": [{"step": name, "bytes": int(freed),
                       "months": round(freed / per_month, 1) if per_month else None}
                      for name, freed in steps if freed > 0]}


def steps_from(library: dict[str, Any], duplicates: dict[str, Any],
               extra: Iterable[tuple[str, int]] = ()) -> list[tuple[str, int]]:
    """The step a forecast offers, from the space report: setting the exact
    copies aside. (*library* is taken for the callers that pass it.)"""
    out = [("exact_duplicates", int(duplicates.get("exact", {}).get("spare_bytes") or 0))]
    out.extend(extra)
    return out
