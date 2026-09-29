"""What a Google Takeout export says beside each photograph, and its albums.

Google Photos exports a folder per album and one per year ("Photos from
2019"), each holding the photographs and, beside each, a small JSON sidecar:
when it was taken, where (often the only place the location survives, since
the export strips EXIF from a lot of what it holds), the description typed
under it, and whether it was a favourite. An album folder also holds a
``metadata.json`` with the album's title, and a photograph in three albums
appears in three folders — the import's own dedup keeps one copy.

Dates from the sidecar are read by ``archive/dates.py`` already. This adds
the rest: the location and description into the index (``media/scanner.py``
asks ``extras``), and the albums back, once the import has run
(``albums_in`` finds them, ``recreate`` makes them in the library).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from . import dates as capture_dates
from .safety import long_path

#: The folders Takeout makes that are not albums.
_NOT_ALBUMS = ("photos from ", "untitled", "trash", "archive", "failed videos", "bin")


def read_sidecar(path) -> dict[str, Any] | None:
    """The sidecar beside *path*, as a plain dict, or None when there is none."""
    for sidecar in capture_dates._takeout_sidecars(path):     # noqa: SLF001 — same package
        try:
            with open(long_path(sidecar), "r", encoding="utf-8", errors="replace") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(meta, dict):
            return meta
    return None


def extras(path) -> dict[str, Any]:
    """What the sidecar adds beyond the date: ``lat``/``lon`` (when the export
    holds a real location), ``description`` and ``favorited``. Empty when
    there is no sidecar, so callers can ``update`` a record with it blindly."""
    meta = read_sidecar(path)
    if not meta:
        return {}
    out: dict[str, Any] = {}
    for key in ("geoData", "geoDataExif"):
        geo = meta.get(key)
        if not isinstance(geo, dict):
            continue
        try:
            lat, lon = float(geo.get("latitude") or 0), float(geo.get("longitude") or 0)
        except (TypeError, ValueError):
            continue
        # Google writes 0.0/0.0 for "no location"; nobody photographs the
        # Gulf of Guinea that often.
        if (lat, lon) != (0.0, 0.0) and -90 <= lat <= 90 and -180 <= lon <= 180:
            out["lat"], out["lon"] = round(lat, 6), round(lon, 6)
            break
    description = meta.get("description")
    if isinstance(description, str) and description.strip():
        out["description"] = description.strip()[:1000]
    if isinstance(meta.get("favorited"), bool):
        out["favorited"] = meta["favorited"]
    return out


def is_album_folder(folder) -> bool:
    """A Takeout album: a folder with a ``metadata.json`` that names it."""
    return _album_title(folder) is not None


def _album_title(folder) -> str | None:
    meta = Path(folder) / "metadata.json"
    try:
        data = json.loads(meta.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    title = title.strip()
    if title.lower().startswith(_NOT_ALBUMS):
        return None
    return title[:120]


def albums_in(sources) -> list[dict[str, Any]]:
    """Every album folder under the *sources*: ``{"title", "folder", "files"}``.

    Files are the photographs and videos in the folder itself (an album is
    flat), by absolute path; sidecars and ``metadata.json`` are not files.
    """
    from .scanner import is_sidecar                      # noqa: PLC0415 — avoids a cycle at import

    albums: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in sources or []:
        root = Path(source)
        if not root.is_dir():
            continue
        candidates = [root]
        for dirpath, dirnames, _ in os.walk(long_path(str(root))):
            dirnames.sort()
            candidates.extend(Path(dirpath) / d for d in dirnames)
        for folder in candidates:
            key = os.path.normcase(str(folder))
            if key in seen:
                continue
            seen.add(key)
            title = _album_title(folder)
            if title is None:
                continue
            try:
                names = sorted(os.listdir(long_path(str(folder))))
            except OSError:
                continue
            files = [str(folder / n) for n in names
                     if n != "metadata.json" and not is_sidecar(n)
                     and (folder / n).is_file()]
            if files:
                albums.append({"title": title, "folder": str(folder), "files": files})
    return albums


def _destinations(source_paths) -> dict[str, str]:
    """Where the import put each source file (a duplicate's row names the
    copy that was kept), for the ones it has finished with."""
    from . import database as adb                       # noqa: PLC0415

    out: dict[str, str] = {}
    paths = list(source_paths)
    for start in range(0, len(paths), 500):
        piece = paths[start:start + 500]
        marks = ",".join("?" * len(piece))
        rows = adb.get_db().execute(
            f"SELECT source_path, destination_path FROM files "
            f"WHERE source_path IN ({marks}) AND status IN ('verified', 'duplicate') "
            f"AND destination_path IS NOT NULL", piece).fetchall()
        for row in rows:
            out[row["source_path"]] = row["destination_path"]
    return out


def recreate(conn, roots, sources, *, created_by=None) -> dict[str, Any]:
    """Make the Takeout albums under *sources* in the library.

    For each album folder, every member that the import archived (or found to
    be a duplicate of something archived) and that the library has indexed
    goes into an album of that title; an album of that name already there is
    added to, not duplicated. Members the library does not know yet — a scan
    still running — are counted and left for a second pass.
    """
    from ..storage import db                             # noqa: PLC0415

    roots = [str(Path(r)) for r in roots]
    made: list[dict[str, Any]] = []
    unmatched = 0
    for album in albums_in(sources):
        where = _destinations(album["files"])
        ids: list[int] = []
        for dest in where.values():
            asset = _asset_at(conn, roots, dest)
            if asset is None:
                unmatched += 1
            else:
                ids.append(asset)
        unmatched += len(album["files"]) - len(where)
        if not ids:
            continue
        album_id = db.create_album(conn, album["title"], created_by=created_by)
        db.album_add(conn, album_id, ids)
        made.append({"id": album_id, "title": album["title"], "added": len(ids)})
    return {"albums": made, "unmatched": unmatched}


def _asset_at(conn, roots, destination) -> int | None:
    dest = Path(destination)
    for root in roots:
        try:
            rel = dest.relative_to(root).as_posix()
        except ValueError:
            continue
        row = conn.execute("SELECT id FROM assets WHERE root=? AND rel_path=? AND trashed=0",
                           (root, rel)).fetchone()
        if row:
            return int(row["id"])
    return None
