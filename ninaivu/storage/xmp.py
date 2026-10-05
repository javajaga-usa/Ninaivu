"""What the household told Ninaivu, written beside each photograph as XMP.

Faces named, albums made, favourites and stars, a corrected date, the place a
photograph was taken — all of it lives in Ninaivu's index. That is fine for as
long as Ninaivu is the program the family uses, and a problem the day it is
not: another photo manager opening the same folders sees none of that work.

XMP sidecars are the standard answer. ``IMG_1234.jpg.xmp`` beside
``IMG_1234.jpg`` is read by digiKam, darktable, Lightroom (Classic reads
``IMG_1234.xmp`` too, and this writes the ``.jpg.xmp`` form every one of them
understands), Immich, PhotoPrism and ExifTool. The photograph itself is never
opened for writing.

What goes in, in the vocabularies those programs read:

* **people** — ``Iptc4xmpExt:PersonInImage``, digiKam's ``People/<name>``
  tags, and Metadata Working Group face regions with where each face is;
* **albums** — ``lr:hierarchicalSubject`` ``Albums|<name>`` (and digiKam's);
* **favourites and stars** — ``xmp:Rating`` (the highest anybody gave it),
  and ``Favourites`` for a photograph somebody marked;
* **when** — ``photoshop:DateCreated``, only when Ninaivu knows better than
  the file's own clock (a date read from a name, a folder or set by hand);
* **where** — the town and country, and the coordinates when known;
* **a description** somebody wrote (``caption_source`` is ``manual``).
  Descriptions and tags the AI made are left out on purpose: they are
  guesses, and another program would show them as keywords a person chose.

Two rules keep this from damaging anything:

* **Only Ninaivu's own sidecars are ever rewritten.** Each carries a
  ``ninaivu:Writer`` mark. A sidecar that is already there without it — one
  Lightroom or a camera wrote — is left exactly as it is and counted.
* **Nothing is written that has nothing to say**, and a sidecar of Ninaivu's
  that has nothing left to say is removed rather than left empty.

Off unless the household turns it on (``xmp_sidecars``): it puts a file
beside every photograph that has any of the above, and that is a decision
about the library folders, not Ninaivu's. The sidecars carry the names of
the people in each photograph and where it was taken, readable by anybody who
can read the library folders, whatever the photograph's visibility here.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable
from xml.sax.saxutils import escape

log = logging.getLogger(__name__)

__all__ = ["XmpWriter", "sidecar_for", "render", "WRITER"]

WRITER = "Ninaivu"
MARK = 'ninaivu:Writer="Ninaivu"'
#: Dates Ninaivu worked out rather than read from the file itself — set by
#: hand, or read from its name or folder. Worth telling another program
#: about; the camera's own date (and the file system's) it already has.
_BETTER_DATES = {"manual", "filename", "path"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS xmp_written (
    asset_id    INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
    fingerprint TEXT NOT NULL,
    written_at  REAL NOT NULL
);
"""


def sidecar_for(path: Path) -> Path:
    return path.with_name(path.name + ".xmp")


def _attr(value: Any) -> str:
    return escape(str(value), {'"': "&quot;"})


def _bag(tag: str, items: list[str]) -> str:
    if not items:
        return ""
    lis = "".join(f"<rdf:li>{escape(i)}</rdf:li>" for i in items)
    return f"   <{tag}><rdf:Bag>{lis}</rdf:Bag></{tag}>\n"


def _gps(value: float, positive: str, negative: str) -> str:
    """XMP's GPS form: degrees, decimal minutes and the hemisphere."""
    ref = positive if value >= 0 else negative
    value = abs(value)
    degrees = int(value)
    minutes = (value - degrees) * 60
    return f"{degrees},{minutes:.6f}{ref}"


def render(info: dict[str, Any]) -> str:
    """The XMP document for one photograph's facts; "" when there are none."""
    people: list[dict[str, Any]] = info.get("people") or []
    names = sorted({p["name"] for p in people})
    albums = sorted(set(info.get("albums") or []))
    favourite = bool(info.get("favorite"))
    rating = int(info.get("rating") or 0)
    caption = (info.get("caption") or "").strip()
    created = info.get("created") or ""
    city, country = info.get("city") or "", info.get("country") or ""
    lat, lon = info.get("lat"), info.get("lon")
    if not (names or albums or favourite or rating or caption or created or city
            or country or lat is not None):
        return ""

    hierarchy = ([f"People|{n}" for n in names] + [f"Albums|{a}" for a in albums]
                 + (["Favourites"] if favourite else []))
    digikam = ([f"People/{n}" for n in names] + [f"Albums/{a}" for a in albums]
               + (["Favourites"] if favourite else []))
    attrs = [MARK]
    if rating:
        attrs.append(f'xmp:Rating="{max(0, min(rating, 5))}"')
    if created:
        attrs.append(f'photoshop:DateCreated="{_attr(created)}"')
    if city:
        attrs.append(f'photoshop:City="{_attr(city)}"')
    if country:
        attrs.append(f'photoshop:Country="{_attr(country)}"')
    if lat is not None and lon is not None:
        attrs.append(f'exif:GPSLatitude="{_gps(float(lat), "N", "S")}"')
        attrs.append(f'exif:GPSLongitude="{_gps(float(lon), "E", "W")}"')
        attrs.append('exif:GPSVersionID="2.2.0.0"')
    body = ""
    if caption:
        body += ('   <dc:description><rdf:Alt><rdf:li xml:lang="x-default">'
                 f"{escape(caption)}</rdf:li></rdf:Alt></dc:description>\n")
    body += _bag("dc:subject", names + albums + (["Favourites"] if favourite else []))
    body += _bag("lr:hierarchicalSubject", hierarchy)
    body += _bag("digiKam:TagsList", digikam)
    body += _bag("Iptc4xmpExt:PersonInImage", names)
    width, height = int(info.get("width") or 0), int(info.get("height") or 0)
    regions = [p for p in people if p.get("box") and width and height]
    if regions:
        lis = []
        for person in regions:
            x, y, w, h = person["box"]
            lis.append(
                '<rdf:li><rdf:Description mwg-rs:Type="Face" '
                f'mwg-rs:Name="{_attr(person["name"])}">'
                '<mwg-rs:Area stArea:unit="normalized" '
                f'stArea:x="{(x + w / 2) / width:.5f}" stArea:y="{(y + h / 2) / height:.5f}" '
                f'stArea:w="{w / width:.5f}" stArea:h="{h / height:.5f}"/>'
                "</rdf:Description></rdf:li>")
        body += ("   <mwg-rs:Regions rdf:parseType=\"Resource\">\n"
                 f'    <mwg-rs:AppliedToDimensions stDim:w="{width}" stDim:h="{height}" '
                 'stDim:unit="pixel"/>\n'
                 f"    <mwg-rs:RegionList><rdf:Bag>{''.join(lis)}</rdf:Bag></mwg-rs:RegionList>\n"
                 "   </mwg-rs:Regions>\n")
    return (
        '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="Ninaivu">\n'
        ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
        '  <rdf:Description rdf:about=""\n'
        '    xmlns:ninaivu="https://github.com/javajaga-usa/Ninaivu/ns/1.0/"\n'
        '    xmlns:dc="http://purl.org/dc/elements/1.1/"\n'
        '    xmlns:xmp="http://ns.adobe.com/xap/1.0/"\n'
        '    xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/"\n'
        '    xmlns:exif="http://ns.adobe.com/exif/1.0/"\n'
        '    xmlns:lr="http://ns.adobe.com/lightroom/1.0/"\n'
        '    xmlns:digiKam="http://www.digikam.org/ns/1.0/"\n'
        '    xmlns:Iptc4xmpExt="http://iptc.org/std/Iptc4xmpExt/2008-02-29/"\n'
        '    xmlns:mwg-rs="http://www.metadataworkinggroup.com/schemas/regions/"\n'
        '    xmlns:stArea="http://ns.adobe.com/xmp/sType/Area#"\n'
        '    xmlns:stDim="http://ns.adobe.com/xap/1.0/sType/Dimensions#"\n'
        f"    {' '.join(attrs)}>\n"
        f"{body}"
        "  </rdf:Description>\n"
        " </rdf:RDF>\n"
        "</x:xmpmeta>\n"
        '<?xpacket end="w"?>\n')


def gather(conn: sqlite3.Connection, roots: list[str], asset_ids: list[int] | None = None
           ) -> dict[int, dict[str, Any]]:
    """Every photograph's facts, in a handful of queries rather than one per file."""
    marks = ",".join("?" * len(roots)) or "''"
    where = f"a.trashed = 0 AND a.live_clip = 0 AND a.kind IN ('picture', 'video') AND a.root IN ({marks})"
    params: list[Any] = list(roots)
    if asset_ids:
        where += f" AND a.id IN ({','.join('?' * len(asset_ids))})"
        params += [int(i) for i in asset_ids]
    facts: dict[int, dict[str, Any]] = {}
    for r in conn.execute(
            "SELECT a.id, a.root, a.rel_path, a.width, a.height, a.caption, a.caption_source, "
            "a.captured_at, "
            f"a.date_source, a.city, a.country, a.gps_lat, a.gps_lon FROM assets a WHERE {where}",
            params):
        caption = r["caption"] or ""
        created = ""
        if r["captured_at"] and r["date_source"] in _BETTER_DATES:
            created = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(float(r["captured_at"])))
        facts[int(r["id"])] = {
            "root": r["root"], "rel_path": r["rel_path"], "width": r["width"], "height": r["height"],
            # Only what a person wrote: the index says who wrote each one.
            "caption": caption if r["caption_source"] == "manual" else "",
            "created": created, "city": r["city"] or "", "country": r["country"] or "",
            "lat": r["gps_lat"], "lon": r["gps_lon"], "people": [], "albums": [],
            "favorite": False, "rating": 0,
        }
    if not facts:
        return facts
    people: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for r in conn.execute(
            "SELECT f.asset_id, f.bbox, p.name FROM faces f JOIN people_clusters p ON p.id = f.person_id "
            f"JOIN assets a ON a.id = f.asset_id WHERE {where} AND f.source IN ('confirmed', 'auto') "
            "AND COALESCE(p.name, '') != ''", params):
        try:
            box = [float(v) for v in json.loads(r["bbox"])][:4]
        except (TypeError, ValueError):
            box = []
        people[int(r["asset_id"])].append({"name": r["name"], "box": box if len(box) == 4 else None})
    for aid, found in people.items():
        if aid in facts:
            # One name once, keeping the first box seen for it.
            seen: dict[str, dict[str, Any]] = {}
            for person in found:
                seen.setdefault(person["name"], person)
            facts[aid]["people"] = list(seen.values())
    for r in conn.execute(
            "SELECT ai.asset_id, al.name FROM album_items ai JOIN albums al ON al.id = ai.album_id "
            f"JOIN assets a ON a.id = ai.asset_id WHERE {where}", params):
        if int(r["asset_id"]) in facts:
            facts[int(r["asset_id"])]["albums"].append(r["name"])
    for r in conn.execute(
            "SELECT ua.asset_id, MAX(ua.favorite) fav, MAX(ua.rating) rating FROM user_assets ua "
            f"JOIN assets a ON a.id = ua.asset_id WHERE {where} GROUP BY ua.asset_id", params):
        if int(r["asset_id"]) in facts:
            facts[int(r["asset_id"])].update(favorite=bool(r["fav"]), rating=int(r["rating"] or 0))
    return facts


class XmpWriter:
    """Keeps a sidecar beside every photograph that has something to say."""

    def __init__(self, cfg, connect_db: Callable[[], sqlite3.Connection], *,
                 hold: Callable[[], Any] | None = None, every: float = 3600.0):
        self.cfg = cfg
        self._connect = connect_db
        self._hold = hold
        self._every = every
        self._lock = threading.Lock()
        self._stop = threading.Event()
        #: The schedule's own stop, apart from a pass's (see stop()).
        self._asleep = threading.Event()
        self._thread: threading.Thread | None = None
        self._timer: threading.Thread | None = None
        self._state: dict[str, Any] = {"running": False, "written": 0, "removed": 0,
                                       "unchanged": 0, "left_alone": 0, "failed": 0,
                                       "message": "", "finished_at": 0.0, "problems": []}

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "xmp_sidecars", False))

    @property
    def roots(self) -> list[str]:
        return list(self.cfg.roots or ([self.cfg.active_root] if self.cfg.active_root else []))

    def _db(self) -> sqlite3.Connection:
        conn = self._connect()
        conn.executescript(SCHEMA)
        return conn

    def status(self) -> dict[str, Any]:
        with self._lock:
            snap = dict(self._state, problems=list(self._state["problems"]))
        try:
            snap["kept"] = int(self._db().execute("SELECT COUNT(*) FROM xmp_written").fetchone()[0])
        except sqlite3.Error:
            snap["kept"] = 0
        snap["enabled"] = self.enabled
        return snap

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self, asset_ids: list[int] | None = None, *, force: bool = False) -> dict[str, Any]:
        with self._lock:
            if self.running:
                raise ValueError("The sidecars are already being written.")
            self._stop.clear()
            self._state.update(running=True, written=0, removed=0, unchanged=0, left_alone=0,
                               failed=0, message="Writing…", problems=[])
            self._thread = threading.Thread(target=self.run, args=(asset_ids, force),
                                            name="ninaivu-xmp", daemon=True)
            self._thread.start()
        return self.status()

    def stop(self, join: bool = False) -> None:
        """Stop the pass in progress; with *join*, at shutdown, the schedule too."""
        self._stop.set()
        if join:
            self._asleep.set()
            for thread in (self._thread, self._timer):
                if thread is not None and thread is not threading.current_thread():
                    thread.join(10)

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def keep(self) -> None:
        """Bring the sidecars up to date every hour while the setting is on."""
        if self._timer is not None and self._timer.is_alive():
            return
        self._asleep.clear()

        def loop() -> None:
            while not self._asleep.wait(self._every):
                if self.enabled and not self.running:
                    try:
                        self.start()
                    except ValueError:
                        pass

        self._timer = threading.Thread(target=loop, name="ninaivu-xmp-schedule", daemon=True)
        self._timer.start()

    def _pause_for_the_household(self) -> None:
        """Wait while people are using Ninaivu (the workload's say), or until stopped."""
        while self._hold is not None and not self._stop.is_set():
            reason = self._hold()
            if not reason:
                break
            with self._lock:
                self._state["message"] = f"Waiting: {reason}"
            self._stop.wait(5)

    def _bump(self, key: str, n: int = 1) -> None:
        with self._lock:
            self._state[key] += n

    def run(self, asset_ids: list[int] | None = None, force: bool = False) -> None:
        """One pass: write what changed, remove what has nothing left to say."""
        message = ""
        try:
            conn = self._db()
            facts = gather(conn, self.roots, asset_ids)
            known = {int(r[0]): r[1] for r in conn.execute(
                "SELECT asset_id, fingerprint FROM xmp_written")}
            for index, (aid, info) in enumerate(facts.items()):
                if self._stop.is_set():
                    message = "Stopped."
                    break
                if index % 200 == 0:
                    self._pause_for_the_household()
                text = render(info)
                print_ = hashlib.sha1(text.encode()).hexdigest() if text else ""  # noqa: S324
                if not text and aid not in known:
                    continue                     # nothing to say, and nothing said before
                if not force and known.get(aid) == print_:
                    self._bump("unchanged")
                    continue
                try:
                    self._write(conn, aid, info, text, print_)
                except OSError as exc:
                    self._bump("failed")
                    with self._lock:
                        if len(self._state["problems"]) < 50:
                            self._state["problems"].append({"file": info["rel_path"], "why": str(exc)})
            conn.commit()
            if not message:
                snap = self.status()
                message = (f"{snap['written']:,} sidecar(s) written, {snap['removed']:,} removed, "
                           f"{snap['unchanged']:,} already up to date.")
                if snap["left_alone"]:
                    message += (f" {snap['left_alone']:,} photograph(s) already had a sidecar "
                                "another program wrote; those were left as they are.")
        except Exception as exc:                            # noqa: BLE001
            log.exception("writing the XMP sidecars stopped")
            message = f"Writing the sidecars stopped: {exc}"
        finally:
            with self._lock:
                self._state.update(running=False, message=message, finished_at=time.time())

    def _write(self, conn: sqlite3.Connection, aid: int, info: dict[str, Any],
               text: str, print_: str) -> None:
        root = Path(info["root"])
        path = root.joinpath(*str(info["rel_path"]).replace("\\", "/").split("/"))
        if not Path(os.path.abspath(path)).is_relative_to(os.path.abspath(root)):
            raise OSError("refusing a path outside the library")
        if not path.is_file():
            return
        sidecar = sidecar_for(path)
        if sidecar.exists():
            try:
                head = sidecar.read_text(encoding="utf-8", errors="replace")[:8192]
            except OSError:
                head = ""
            if MARK not in head:
                self._bump("left_alone")
                return
        if not text:
            sidecar.unlink(missing_ok=True)
            conn.execute("DELETE FROM xmp_written WHERE asset_id=?", (aid,))
            self._bump("removed")
            return
        temp = sidecar.with_name(f".{sidecar.name}.ninaivu-part")
        try:
            temp.write_text(text, encoding="utf-8")
            os.replace(temp, sidecar)
        finally:
            temp.unlink(missing_ok=True)
        conn.execute("INSERT INTO xmp_written(asset_id, fingerprint, written_at) VALUES(?, ?, ?) "
                     "ON CONFLICT(asset_id) DO UPDATE SET fingerprint=excluded.fingerprint, "
                     "written_at=excluded.written_at", (aid, print_, time.time()))
        self._bump("written")
