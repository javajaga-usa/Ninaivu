"""Where a photograph was taken, kept at home.

A phone writes the place into every photograph, to a few metres — and for
most of a family's photographs that place is the house. Ninaivu already
keeps it from anybody outside: a guest and a shared link get a copy with no
metadata at all (``api._viewing_copy``, ``media/stripped_video.py``). What it
did not guard is the other way a location leaves: a family member opening or
downloading the original and sending it on.

The **home zone** is a circle the household draws once — a point and a
radius. With ``strip_location`` at ``home`` (the default once a zone is set),
anything taken inside it leaves Ninaivu without its location when anybody
but an administrator opens or downloads it; at ``all`` everything does.
Nothing about the photograph in the library changes.

How the location is taken out, by kind:

* **JPEG** — losslessly. The GPS block inside the EXIF is emptied in place,
  byte for byte, so every other offset in the file still points where it
  did; the picture data is not touched and the camera, date and orientation
  stay. An XMP packet (which can carry the place again) is left out.
* **other pictures** (HEIC, raw, WebP…) — re-encoded at full size as a
  JPEG with no metadata; PNG is re-saved as PNG, which loses nothing.
* **videos** — the copy guests are given (``media/stripped_video.py``):
  the streams copied into a new container with no metadata, no re-encoding.

Picture copies are made once and kept in the state folder's
``private-copies``, oldest dropped past a size limit — a cache, never the
library. Video copies live with the guests' ones in ``stripped-videos``.
"""

from __future__ import annotations

import hashlib
import io
import logging
import math
import os
import stat as stat_module
import struct
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

__all__ = ["in_home_zone", "should_strip", "strip_jpeg", "LocationFreeCopies", "MODES",
           "suggest_home"]

MODES = ("off", "home", "all")
#: Kept at most, for all the copies together.
CACHE_BYTES = 2 * 1024 ** 3
_NO_LOCATION_EXTS = {"gif", "bmp"}
_JPEG_EXTS = {"jpg", "jpeg", "jpe"}


def _km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0088 * 2 * math.asin(min(1.0, math.sqrt(a)))


def home_zone(cfg) -> tuple[float, float, float] | None:
    lat, lon = getattr(cfg, "home_lat", None), getattr(cfg, "home_lon", None)
    if lat is None or lon is None:
        return None
    radius = max(50, int(getattr(cfg, "home_radius_m", 300) or 300))
    return float(lat), float(lon), radius / 1000.0


def in_home_zone(cfg, lat: Any, lon: Any) -> bool:
    zone = home_zone(cfg)
    if zone is None or lat is None or lon is None:
        return False
    return _km(float(lat), float(lon), zone[0], zone[1]) <= zone[2]


def should_strip(cfg, row: dict[str, Any], *, is_admin: bool) -> bool:
    """Whether this file should leave without where it was taken."""
    if is_admin:
        return False
    mode = str(getattr(cfg, "strip_location", "home") or "home")
    kind = row.get("kind") or ""
    ext = str(row.get("ext") or "").lower().lstrip(".")
    if kind not in ("picture", "video") or ext in _NO_LOCATION_EXTS:
        return False
    if mode == "all":
        return True
    if mode == "home":
        if kind == "video" and row.get("gps_lat") is None and home_zone(cfg) is not None:
            # Videos used to be indexed without their place, so one with none
            # on record may well have been filmed at home: it leaves as a copy
            # without its metadata, which loses nothing to watch.
            return True
        return in_home_zone(cfg, row.get("gps_lat"), row.get("gps_lon"))
    return False


# -- JPEG, in place ----------------------------------------------------------

_TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}
_GPS_TAG = 0x8825


def _blank_gps(tiff: bytearray) -> bool:
    """Empty the GPS directory of a TIFF/EXIF block in place. True if there was one."""
    if len(tiff) < 8 or tiff[:2] not in (b"II", b"MM"):
        return False
    order = "<" if tiff[:2] == b"II" else ">"

    def u16(at: int) -> int:
        return struct.unpack_from(order + "H", tiff, at)[0]

    def u32(at: int) -> int:
        return struct.unpack_from(order + "I", tiff, at)[0]

    ifd0 = u32(4)
    if ifd0 + 2 > len(tiff):
        return False
    gps = None
    for i in range(u16(ifd0)):
        entry = ifd0 + 2 + i * 12
        if entry + 12 > len(tiff):
            break
        if u16(entry) == _GPS_TAG:
            gps = u32(entry + 8)
            break
    if gps is None or gps + 2 > len(tiff):
        return False
    count = u16(gps)
    end = gps + 2 + count * 12
    if end > len(tiff):
        return False
    for i in range(count):
        entry = gps + 2 + i * 12
        kind, many = u16(entry + 2), u32(entry + 4)
        size = _TYPE_SIZES.get(kind, 1) * many
        if size > 4:
            where = u32(entry + 8)
            if where + size <= len(tiff):
                tiff[where:where + size] = bytes(size)
    tiff[gps + 2:end] = bytes(end - gps - 2)
    struct.pack_into(order + "H", tiff, gps, 0)           # an empty directory, still valid
    return True


def _scan_end(data: bytes, at: int) -> int:
    """Where the entropy-coded data that starts at *at* ends: the next marker
    that is neither a stuffed byte nor a restart."""
    while True:
        at = data.find(b"\xff", at)
        if at < 0 or at + 1 >= len(data):
            return len(data)
        following = data[at + 1]
        if following == 0x00 or 0xD0 <= following <= 0xD7 or following == 0xFF:
            at += 1 if following == 0xFF else 2
            continue
        return at


def strip_jpeg(data: bytes) -> bytes:
    """A JPEG with its GPS emptied and any XMP or IPTC left out; pixels untouched.

    The copy ends where the first picture does. Whatever a camera puts after
    it — the second frame of an MPO, a Motion Photo's video, a Samsung
    trailer — carries the place again, and the MPF index that points at it
    and the IPTC block (a city, a country) go with it.
    """
    if data[:2] != b"\xff\xd8":
        raise ValueError("not a JPEG")
    out = bytearray(b"\xff\xd8")
    at = 2
    while at + 2 <= len(data):
        if data[at] != 0xFF:
            raise ValueError("a damaged JPEG")
        marker = data[at + 1]
        if marker == 0xFF:                                  # fill byte before a marker
            at += 1
            continue
        if marker == 0xD9:                                  # the end of the first picture
            out += b"\xff\xd9"
            return bytes(out)
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:
            out += data[at:at + 2]
            at += 2
            continue
        if at + 4 > len(data):
            break
        length = struct.unpack(">H", data[at + 2:at + 4])[0]
        segment = data[at:at + 2 + length]
        body = segment[4:]
        if marker == 0xDA:                                  # a scan: header, then the picture data
            end = _scan_end(data, at + 2 + length)
            out += data[at:end]
            at = end
            continue
        if marker == 0xE1 and body.startswith(b"Exif\x00\x00"):
            tiff = bytearray(body[6:])
            _blank_gps(tiff)
            segment = segment[:4] + b"Exif\x00\x00" + bytes(tiff)
        elif (marker == 0xE1 and (body.startswith(b"http://ns.adobe.com/xap/1.0/\x00")
                                  or body.startswith(b"http://ns.adobe.com/xmp/extension/\x00"))
              or marker == 0xED                             # IPTC: a city, a country
              or marker == 0xE2 and body.startswith(b"MPF\x00")):   # the index of what follows
            at += 2 + length
            continue
        out += segment
        at += 2 + length
    # A file cut short: what there was of the first picture, and nothing after.
    out += b"\xff\xd9"
    return bytes(out)


# -- the copies ---------------------------------------------------------------

class LocationFreeCopies:
    """Copies without a location, made on first asking and kept a while."""

    def __init__(self, state_dir: Path | str, limit: int = CACHE_BYTES):
        self.state_dir = Path(state_dir)
        self.folder = self.state_dir / "private-copies"
        self.limit = limit
        # One lock per copy rather than one for all of them: with a single
        # lock, one family member's large photograph being copied held up
        # every other photograph anybody opened. Two people asking for the
        # same one still make it once.
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._trim_lock = threading.Lock()

    def _lock_for(self, key: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(key, threading.Lock())

    def _key(self, row: dict[str, Any], path: Path) -> str:
        stat = path.stat()
        raw = f"{row['id']}:{stat.st_size}:{stat.st_mtime_ns}"
        return hashlib.sha1(raw.encode()).hexdigest()[:16]  # noqa: S324

    def copy(self, row: dict[str, Any], path: Path) -> tuple[Path, str]:
        """The copy's path and the name to download it as."""
        ext = str(row.get("ext") or path.suffix).lower().lstrip(".")
        kind = row.get("kind") or ""
        stem = Path(row.get("filename") or path.name).stem
        if kind == "video":
            # The same copy a guest is given, made and kept the same way.
            from ..media import stripped_video                  # noqa: PLC0415
            ready = stripped_video.stripped_copy(self.state_dir, int(row["id"]), path)
            if ready is None:
                raise OSError("ffmpeg is not installed, or could not copy this video "
                              "without its metadata")
            return ready, f"{stem}{ready.suffix}"
        if ext in _JPEG_EXTS:
            suffix = path.suffix
        elif ext == "png":
            suffix = ".png"
        else:
            suffix = ".jpg"
        key = self._key(row, path)
        target = self.folder / f"{key}{suffix}"
        name = f"{stem}{suffix}"
        with self._lock_for(key):
            if target.is_file() and target.stat().st_size > 0:
                try:
                    # Its time is how the trim tells recently used copies from
                    # old ones. The name, not the time, is what the copy is
                    # sent with as its ETag, so this does not make a phone
                    # download it again.
                    os.utime(target, None)
                    return target, name
                except FileNotFoundError:
                    pass                    # trimmed just now; made again below
            self.folder.mkdir(parents=True, exist_ok=True)
            temp = target.with_name(f".{target.name}.part")
            try:
                if ext in _JPEG_EXTS:
                    temp.write_bytes(strip_jpeg(path.read_bytes()))
                else:
                    self._picture(path, temp, png=ext == "png")
                os.replace(temp, target)
            finally:
                temp.unlink(missing_ok=True)
        # Outside the copy's lock: listing the folder is not this copy's
        # business, and one trim at a time is enough.
        if self._trim_lock.acquire(blocking=False):
            try:
                self._trim()
            finally:
                self._trim_lock.release()
        return target, name

    @staticmethod
    def _picture(path: Path, out: Path, png: bool) -> None:
        from ..media import media                           # noqa: PLC0415
        with media._open_oriented(path) as image:            # noqa: SLF001
            buffer = io.BytesIO()
            if png:
                image.save(buffer, "PNG", optimize=False)
            else:
                image.convert("RGB").save(buffer, "JPEG", quality=95, subsampling=0)
        out.write_bytes(buffer.getvalue())

    def _trim(self) -> None:
        # Read each file's size and time once: a copy being made or used at
        # the same moment can come or go between two looks at it.
        files = []
        for path in self.folder.glob("*"):
            if path.name.startswith("."):
                continue
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            if not stat_module.S_ISREG(stat.st_mode):
                continue
            files.append((stat.st_mtime, stat.st_size, path))
        files.sort(key=lambda entry: entry[0], reverse=True)
        total = 0
        for _, size, path in files:
            total += size
            if total > self.limit:
                path.unlink(missing_ok=True)


def suggest_home(conn, roots: list[str]) -> dict[str, Any] | None:
    """The place with the most photographs, to about a hundred metres — for
    nearly every family, home. Offered, never applied without being asked."""
    marks = ",".join("?" * len(roots)) or "''"
    row = conn.execute(
        "SELECT ROUND(gps_lat, 3) lat, ROUND(gps_lon, 3) lon, COUNT(*) n FROM assets "
        f"WHERE gps_lat IS NOT NULL AND trashed = 0 AND root IN ({marks}) "
        "GROUP BY 1, 2 ORDER BY n DESC LIMIT 1", list(roots)).fetchone()
    if not row or not row["n"]:
        return None
    place = conn.execute(
        "SELECT city, country FROM assets WHERE ROUND(gps_lat, 3) = ? AND ROUND(gps_lon, 3) = ? "
        "AND city IS NOT NULL LIMIT 1", (row["lat"], row["lon"])).fetchone()
    return {"lat": float(row["lat"]), "lon": float(row["lon"]), "photos": int(row["n"]),
            "city": place["city"] if place else "", "country": place["country"] if place else ""}
