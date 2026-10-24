"""Where the bytes are, and what a light library would give back.

Mugil with two computers (the "Mugil primary and secondary" plan) keeps every
original on the Secondary, untouched, and lets the Mac work from light copies
that look the same and take less room. Before anything is converted the
household should see what that would actually save on *their* files, not on
somebody's benchmark. This is that answer, and it only ever reads: the index
for the library, a walk of the state folder for Ninaivu's own files.

**The library** is grouped by what a light copy of each kind of file would
be (:data:`CLASSES`): a camera JPEG as AVIF, a RAW as an AVIF rendition, an
older video as HEVC. iPhone HEIC photos and iPhone videos are already
efficient, and turning them would risk HDR and Live Photo details, so they
are counted as kept. Each class carries a low and a high share of its size
that the light copy would keep. These are *estimates*, from what each format
typically does to that kind of file: no file is opened, and the codec of a
video is guessed from its container and camera rather than read. Stage 2 of
the plan measures the real figure on a sample of the household's own files.

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

#: ``(key, share kept low, share kept high)`` for each kind of file, in the
#: order a report lists them. A share of 1.0 is a file kept as it is.
CLASSES: tuple[tuple[str, float, float], ...] = (
    ("jpeg", 0.40, 0.60),            # camera or Android JPEG -> AVIF, checked to look the same
    ("raw", 0.10, 0.15),             # a RAW -> an AVIF rendition; the RAW stays on the Secondary
    ("png", 0.60, 0.75),             # PNG and screenshots -> lossless WebP, pixel for pixel
    ("heic", 1.0, 1.0),              # iPhone HEIC: already efficient; HDR and Live Photos kept
    ("other_picture", 1.0, 1.0),     # GIF, TIFF, BMP, WebP, AVIF: kept as they are
    ("video", 0.50, 0.60),           # H.264 in MP4, MOV, M4V, WebM -> HEVC, same size of picture
    ("legacy_video", 0.20, 0.50),    # AVI, MKV, WMV, MTS, 3GP, MPEG -> HEVC in MP4
    ("phone_video", 1.0, 1.0),       # an iPhone's own video: most likely HEVC already
    ("audio", 1.0, 1.0),
    ("other", 1.0, 1.0),
)
_SHARES = {key: (low, high) for key, low, high in CLASSES}

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
    """The library's files by class, by year and by folder, with the light
    library's estimated size. Files in the bin are left out: they are on
    their way out, and Ninaivu does not make light copies of them."""
    where, params = _roots_clause(roots)
    base = f"WHERE trashed=0 AND {where}"

    classes = {key: {"files": 0, "bytes": 0} for key, _, _ in CLASSES}
    for row in conn.execute(
            f"SELECT ext, kind, camera, COUNT(*) AS files, COALESCE(SUM(size), 0) AS bytes "
            f"FROM assets {base} GROUP BY ext, kind, camera", params):
        entry = classes[classify(row["ext"], row["kind"], row["camera"])]
        entry["files"] += int(row["files"])
        entry["bytes"] += int(row["bytes"])

    listed = []
    total = light_low = light_high = 0
    for key, low, high in CLASSES:
        entry = classes[key]
        size = entry["bytes"]
        total += size
        light_low += round(size * low)
        light_high += round(size * high)
        listed.append({"class": key, "files": entry["files"], "bytes": size,
                       "light_bytes_low": round(size * low),
                       "light_bytes_high": round(size * high),
                       "kept_as_is": low >= 1.0})

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
        "light_bytes_low": light_low,
        "light_bytes_high": light_high,
        # The saving is the larger when the light copies keep the smaller share.
        "saving_bytes_low": total - light_high,
        "saving_bytes_high": total - light_low,
        "classes": listed,
        "years": years,
        "folders": folders,
        "estimate": True,
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


def light_shares() -> dict[str, tuple[float, float]]:
    """The share of its size each class's light copy is estimated to keep."""
    return dict(_SHARES)
