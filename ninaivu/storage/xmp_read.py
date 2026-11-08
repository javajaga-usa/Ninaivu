"""Reading back the sidecars Ninaivu wrote, for photographs new to this index.

A photograph that arrives with a sidecar Ninaivu wrote somewhere else
(``IMG_1.jpg.xmp``, storage/xmp.py) carries what the household told that
Ninaivu: the people named in it and where their faces are, its albums, its
stars and whether it was a favourite, a description somebody wrote, a date
somebody corrected, and where it was taken. This gives that to the
photograph here, so moving photographs between two Ninaivus (bringing in what
only the Secondary had, mugil/bring_in.py, or a library copied by hand) does
not lose the family's work on them.

The rules:

* **Only Ninaivu's own sidecars** (the ``ninaivu:Writer`` mark) are read.
  One Lightroom or a camera wrote is not; its words were never the family's
  in Ninaivu.
* **Only photographs new to this index**: each sidecar is read once, when
  its photograph is first indexed here. A photograph this Ninaivu already
  knew has the household's answers here already.
* **It only fills in; it never overwrites** what a person here said. A
  description or a date somebody set here stays; albums, stars and names are
  added to, never taken away.
* A name goes to the face detected here where the sidecar says that face
  was. Faces are found after indexing, so names wait (``xmp_people_waiting``)
  until the photograph's faces have been looked for, and a face somebody
  here already confirmed as someone is never renamed.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
import xml.etree.ElementTree as ET  # nosec B405 — Ninaivu's own small files; expat resists expansion
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

__all__ = ["parse", "XmpReader"]

MAX_BYTES = 1024 * 1024
#: How close a face here must be to where the sidecar says it was
#: (intersection over union of the two boxes).
OVERLAP = 0.4
#: How long a name waits for its photograph's faces before it is let go.
WAIT_DAYS = 30

_NS = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "ninaivu": "https://github.com/javajaga-usa/Ninaivu/ns/1.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "xmp": "http://ns.adobe.com/xap/1.0/",
    "photoshop": "http://ns.adobe.com/photoshop/1.0/",
    "exif": "http://ns.adobe.com/exif/1.0/",
    "lr": "http://ns.adobe.com/lightroom/1.0/",
    "Iptc4xmpExt": "http://iptc.org/std/Iptc4xmpExt/2008-02-29/",
    "mwg-rs": "http://www.metadataworkinggroup.com/schemas/regions/",
    "stArea": "http://ns.adobe.com/xmp/sType/Area#",
}
_GPS = re.compile(r"^\s*(\d+(?:\.\d+)?),(\d+(?:\.\d+)?)([NSEW])\s*$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS xmp_people_waiting (
    asset_id INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
    people   TEXT NOT NULL,
    since    REAL NOT NULL
);
"""
UPTO = "xmp_read_upto"


def _q(prefix: str, name: str) -> str:
    return f"{{{_NS[prefix]}}}{name}"


def _gps(value: str | None) -> float | None:
    found = _GPS.match(value or "")
    if not found:
        return None
    degrees, minutes, ref = float(found.group(1)), float(found.group(2)), found.group(3)
    number = degrees + minutes / 60
    return -number if ref in "SW" else number


def parse(text: str) -> dict[str, Any] | None:
    """What a sidecar Ninaivu wrote says; None for any other sidecar."""
    if len(text) > MAX_BYTES or "<!DOCTYPE" in text or "<!ENTITY" in text:
        return None
    try:
        tree = ET.fromstring(text.encode("utf-8"))  # nosec B314 — size-capped, no DTD
    except ET.ParseError:
        return None
    about = tree.find(f".//{_q('rdf', 'Description')}")
    if about is None or about.get(_q("ninaivu", "Writer")) != "Ninaivu":
        return None
    info: dict[str, Any] = {"people": [], "albums": [], "favorite": False, "rating": 0,
                            "caption": "", "created": "", "city": "", "country": "",
                            "lat": None, "lon": None}
    try:
        info["rating"] = max(0, min(5, int(about.get(_q("xmp", "Rating")) or 0)))
    except ValueError:
        info["rating"] = 0
    info["created"] = about.get(_q("photoshop", "DateCreated")) or ""
    info["city"] = about.get(_q("photoshop", "City")) or ""
    info["country"] = about.get(_q("photoshop", "Country")) or ""
    info["lat"] = _gps(about.get(_q("exif", "GPSLatitude")))
    info["lon"] = _gps(about.get(_q("exif", "GPSLongitude")))
    caption = about.find(f"{_q('dc', 'description')}/{_q('rdf', 'Alt')}/{_q('rdf', 'li')}")
    if caption is not None and caption.text:
        info["caption"] = caption.text.strip()
    names: list[str] = []
    for li in about.findall(f"{_q('lr', 'hierarchicalSubject')}/{_q('rdf', 'Bag')}/{_q('rdf', 'li')}"):
        tag = (li.text or "").strip()
        if tag.startswith("People|"):
            names.append(tag[len("People|"):])
        elif tag.startswith("Albums|"):
            info["albums"].append(tag[len("Albums|"):])
        elif tag == "Favourites":
            info["favorite"] = True
    for li in about.findall(f"{_q('Iptc4xmpExt', 'PersonInImage')}/{_q('rdf', 'Bag')}/{_q('rdf', 'li')}"):
        if li.text and li.text.strip() not in names:
            names.append(li.text.strip())
    boxes: dict[str, list[float]] = {}
    for region in about.iter(_q("rdf", "Description")):
        name = region.get(_q("mwg-rs", "Name"))
        area = region.find(_q("mwg-rs", "Area"))
        if not name or area is None:
            continue
        try:
            cx, cy, w, h = (float(area.get(_q("stArea", k)) or "nan") for k in ("x", "y", "w", "h"))
        except ValueError:
            continue
        if all(0 <= v <= 1 for v in (cx, cy, w, h)) and w > 0 and h > 0:
            boxes.setdefault(name, [cx, cy, w, h])
    info["people"] = [{"name": n, "area": boxes.get(n)} for n in names if n]
    info["albums"] = sorted({a for a in info["albums"] if a})
    return info


def _face_version() -> int | None:
    try:
        from .db_faces import face_schema_version              # noqa: PLC0415
        return face_schema_version()
    except Exception:                                     # noqa: BLE001 — no face models here
        return None


def _iou(a: list[float], b: list[float]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x0, y0 = max(ax, bx), max(ay, by)
    x1, y1 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    inter = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


class XmpReader:
    """Reads the sidecars of photographs new to this index, after each scan."""

    def __init__(self, cfg, connect_db: Callable[[], sqlite3.Connection], *,
                 confirm: Callable[[sqlite3.Connection, int, int], Any] | None = None,
                 face_version: Callable[[], int | None] | None = None):
        self.cfg = cfg
        self._connect = connect_db
        self._confirm = confirm
        self._face_version = face_version or _face_version
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        #: The supervisor its timer runs on; Ninaivu gives it its own.
        self.supervisor = None
        self.last: dict[str, Any] = {}

    def _db(self) -> sqlite3.Connection:
        conn = self._connect()
        conn.executescript(SCHEMA)
        return conn

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def wake(self) -> None:
        """After a scan, or when faces may have been found: read and name."""
        with self._lock:
            if self.running:
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run_logged, daemon=True,
                                            name="ninaivu-xmp-read")
            self._thread.start()

    def baseline(self) -> None:
        """Mark where "new" starts, once: the photographs indexed before this
        Ninaivu first read sidecars are not new, and are not read."""
        from .db import get_meta, set_meta                      # noqa: PLC0415
        try:
            conn = self._db()
        except sqlite3.Error:
            return
        try:
            if get_meta(conn, UPTO) is None:
                top = conn.execute("SELECT COALESCE(MAX(id), 0) FROM assets").fetchone()[0]
                set_meta(conn, UPTO, str(int(top)))
                conn.commit()
        except sqlite3.Error:
            pass

    def schedule(self, every: float = 1800.0) -> None:
        """Look again every *every* seconds: names wait for faces found later."""
        self.baseline()
        # Kept by the supervisor (server/supervisor.py); a second schedule()
        # is no second timer.
        from ..server.supervisor import Job, every as every_, supervisor_of  # noqa: PLC0415
        supervisor_of(self).add(Job(
            self.TIMER, self.wake, every_(every), log=log, retry_most=0,
            failure="could not wake the reading of the sidecars"))

    #: The timer's thread, as before it moved to the supervisor.
    TIMER = "ninaivu-xmp-read-timer"

    def stop(self, join: bool = False) -> None:
        self._stop.set()
        if join:
            from ..server.supervisor import supervisor_of        # noqa: PLC0415
            # Asked, not waited for, as before: a tick only wakes a pass.
            supervisor_of(self).stop_job(self.TIMER, timeout=0)
            if self._thread is not None:
                self._thread.join(timeout=30)

    def _run_logged(self) -> None:
        try:
            self.last = self.run()
        except Exception:                                 # noqa: BLE001 — logged, not raised
            log.exception("Reading the photographs' sidecars stopped")

    def run(self) -> dict[str, int]:
        conn = self._db()
        try:
            read = self.read_new(conn)
            named = self.name_faces(conn)
        finally:
            if threading.current_thread() is self._thread:
                # A pass of its own thread: the connection goes with it
                # (db.connect keeps one a thread).
                conn.close()
        if read["read"] or named["named"]:
            log.info("Sidecars: %s read, %s faces named from them", read["read"], named["named"])
        return {**read, **named}

    # -- reading -----------------------------------------------------------

    def read_new(self, conn: sqlite3.Connection) -> dict[str, int]:
        """Read the sidecar of every photograph indexed since the last time."""
        from .db import get_meta, set_meta                      # noqa: PLC0415
        if get_meta(conn, UPTO) is None:
            # Never marked (baseline() runs at start): only photographs
            # indexed from now on are new.
            top = conn.execute("SELECT COALESCE(MAX(id), 0) FROM assets").fetchone()[0]
            set_meta(conn, UPTO, str(int(top)))
            conn.commit()
            return {"read": 0}
        upto = int(get_meta(conn, UPTO, "0") or 0)
        roots = list(self.cfg.library_roots)
        marks = ",".join("?" * len(roots)) or "''"
        done = 0
        while not self._stop.is_set():
            rows = conn.execute(
                "SELECT id, root, rel_path, width, height FROM assets WHERE id > ? AND trashed = 0 "
                f"AND kind IN ('picture', 'video') AND root IN ({marks}) ORDER BY id LIMIT 500",
                [upto, *roots]).fetchall()
            if not rows:
                break
            for row in rows:
                sidecar = Path(row["root"]) / f"{row['rel_path']}.xmp"
                try:
                    if sidecar.is_file() and not sidecar.is_symlink() \
                            and sidecar.stat().st_size <= MAX_BYTES:
                        info = parse(sidecar.read_text(encoding="utf-8", errors="replace"))
                        if info:
                            self.apply(conn, int(row["id"]), info)
                            done += 1
                except OSError:
                    pass
                upto = int(row["id"])
            set_meta(conn, UPTO, str(upto))
            conn.commit()
        return {"read": done}

    def apply(self, conn: sqlite3.Connection, asset_id: int, info: dict[str, Any]) -> None:
        """Fill in *asset_id* from *info*; never overwrite what a person here set."""
        asset = conn.execute("SELECT caption_source, date_source, city, country, gps_lat, gps_lon "
                             "FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if asset is None:
            return
        now = time.time()
        if info["caption"] and asset["caption_source"] != "manual":
            conn.execute("UPDATE assets SET caption = ?, caption_source = 'manual' WHERE id = ?",
                         (info["caption"], asset_id))
        if info["created"] and asset["date_source"] != "manual":
            try:
                when = time.mktime(time.strptime(info["created"][:19], "%Y-%m-%dT%H:%M:%S"))
                conn.execute("UPDATE assets SET captured_at = ?, date_key = ?, date_source = 'manual' "
                             "WHERE id = ?", (when, info["created"][:10], asset_id))
            except (ValueError, OverflowError):
                pass
        if info["city"] and not asset["city"]:
            conn.execute("UPDATE assets SET city = ?, country = ? WHERE id = ?",
                         (info["city"], info["country"] or asset["country"], asset_id))
        elif info["country"] and not asset["country"]:
            conn.execute("UPDATE assets SET country = ? WHERE id = ?", (info["country"], asset_id))
        if info["lat"] is not None and info["lon"] is not None and asset["gps_lat"] is None:
            conn.execute("UPDATE assets SET gps_lat = ?, gps_lon = ? WHERE id = ?",
                         (info["lat"], info["lon"], asset_id))
        for name in info["albums"]:
            conn.execute("INSERT OR IGNORE INTO albums (name, created_at) VALUES (?, ?)", (name, now))
            album = conn.execute("SELECT id FROM albums WHERE name = ?", (name,)).fetchone()
            if album:
                conn.execute("INSERT OR IGNORE INTO album_items (album_id, asset_id, added_at) "
                             "VALUES (?, ?, ?)", (int(album["id"]), asset_id, now))
        if info["rating"] or info["favorite"]:
            owner = conn.execute("SELECT id FROM users WHERE role = 'admin' AND active = 1 "
                                 "ORDER BY id LIMIT 1").fetchone()
            anybody = conn.execute("SELECT 1 FROM user_assets WHERE asset_id = ? "
                                   "AND (favorite = 1 OR rating > 0)", (asset_id,)).fetchone()
            if owner and not anybody:
                conn.execute("INSERT INTO user_assets (user_id, asset_id, favorite, rating) "
                             "VALUES (?, ?, ?, ?) ON CONFLICT (user_id, asset_id) DO UPDATE SET "
                             "favorite = excluded.favorite, rating = excluded.rating",
                             (int(owner["id"]), asset_id, int(info["favorite"]), info["rating"]))
        people = [p for p in info["people"] if p.get("area")]
        if people:
            conn.execute("INSERT OR REPLACE INTO xmp_people_waiting (asset_id, people, since) "
                         "VALUES (?, ?, ?)", (asset_id, json.dumps(people), now))
        conn.commit()

    # -- naming faces --------------------------------------------------------

    def name_faces(self, conn: sqlite3.Connection) -> dict[str, int]:
        """Give waiting names to the faces found since, where the sidecar
        says each face was."""
        from . import db                                         # noqa: PLC0415
        version = self._face_version()
        named = 0
        for row in conn.execute("SELECT w.asset_id, w.people, w.since, a.width, a.height, "
                                "a.face_version FROM xmp_people_waiting w "
                                "JOIN assets a ON a.id = w.asset_id").fetchall():
            if self._stop.is_set():
                break
            asset_id, width, height = int(row["asset_id"]), int(row["width"] or 0), int(row["height"] or 0)
            looked = version is not None and int(row["face_version"] or 0) >= int(version)
            faces = conn.execute("SELECT id, bbox, person_id, source FROM faces WHERE asset_id = ?",
                                 (asset_id,)).fetchall()
            if not faces and not looked:
                if time.time() - float(row["since"]) > WAIT_DAYS * 86400:
                    conn.execute("DELETE FROM xmp_people_waiting WHERE asset_id = ?", (asset_id,))
                    conn.commit()
                continue
            taken: set[int] = set()
            for person in json.loads(row["people"]):
                cx, cy, w, h = person["area"]
                wanted = [(cx - w / 2) * width, (cy - h / 2) * height, w * width, h * height]
                best, score = None, OVERLAP
                for face in faces:
                    if int(face["id"]) in taken:
                        continue
                    try:
                        box = [float(v) for v in json.loads(face["bbox"])][:4]
                    except (TypeError, ValueError):
                        continue
                    if len(box) == 4 and _iou(wanted, box) >= score:
                        best, score = face, _iou(wanted, box)
                if best is None:
                    continue
                taken.add(int(best["id"]))
                if best["source"] == "confirmed" and best["person_id"] is not None:
                    continue                    # somebody here said who it is
                found = db.create_or_update_person_cluster(conn, person["name"])
                person_id = int(found["id"])
                if self._confirm is not None:
                    self._confirm(conn, int(best["id"]), person_id)
                else:
                    db.set_face_person(conn, int(best["id"]), person_id,
                                       source="confirmed", confidence=1.0)
                named += 1
            conn.execute("DELETE FROM xmp_people_waiting WHERE asset_id = ?", (asset_id,))
            conn.commit()
        return {"named": named}
