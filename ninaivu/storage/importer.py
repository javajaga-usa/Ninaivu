"""Bringing in a Google Photos, iCloud or WhatsApp export.

A family's photographs are rarely all on its own disks. A good share are in
Google Photos or iCloud, and both will hand them over as a download: Google
Takeout's ``takeout-….zip`` files, Apple's ``iCloud Photos Part 1 of N.zip``.
This reads those — the zips as they downloaded, or folders they were unpacked
into — and files what they hold into the library by date.

What makes an export worth a module of its own rather than a folder to point
the archive at is what travels *beside* the photographs:

* **Google** writes a JSON sidecar for each file with when it was taken —
  often the only reliable date, because a photograph saved from a chat has
  none of its own — where, its description, and whether it was a favourite.
  The albums are the folders. Every photograph in an album is also in its
  year's folder, so each comes twice.
* **Apple** writes ``Photo Details.csv`` — when, favourite, hidden, deleted —
  and a CSV per album naming its photographs.
* **WhatsApp**'s "Export chat, with media" writes the chat as text beside its
  photographs and videos. Each chat becomes an album, "WhatsApp: <name>", and
  a file is dated from its name (``IMG-20190704-WA0012.jpg``). The same
  picture forwarded to five chats is one file here, in five albums.

All of that is kept. Dates set the file's time, so the library files it on
the right day; the place, the description, the favourite, "hidden" and the
albums are applied to the library's own record once the new files have been
indexed. Something marked deleted in iCloud is not brought back.

**Nothing is duplicated.** A file already in the library — the same bytes,
checked by hash, only ever against files of the same size — is not copied
again, and the same photograph arriving twice in one export arrives once.
What the export says about a file that was already here (it was in an album,
it was a favourite) is still applied to it.

**Nothing is lost or half-written.** The export is only read: the zips are
not unpacked in place and nothing in them is changed or deleted. Each file is
written under a temporary name inside the library folder, hashed as it goes,
and only then given its real name — never over another file. What has been
done is written down per file, so an import stopped part-way carries on where
it stopped.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import posixpath
import re
import shutil
import tarfile
import threading
import time
import zipfile
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from ..utils.filenames import portable_name

log = logging.getLogger(__name__)

__all__ = ["Importer", "look", "STAGING"]

#: Where a file is written before it has its name, inside the library folder.
#: A dot folder: the library's scanner does not look in one.
STAGING = ".ninaivu-import"

CHUNK = 4 * 1024 * 1024
#: The largest sidecar or CSV read into memory. Anything bigger is not one.
MAX_METADATA = 8 * 1024 * 1024

#: What reading a damaged export raises. A corrupt zip member fails in zlib
#: and a .tgz cut short by an interrupted download ends in EOFError; neither
#: is an OSError, and either one used to stop the whole import at the same
#: file every time it was started again.
UNREADABLE = (OSError, zipfile.BadZipFile, tarfile.TarError, ValueError, EOFError, zlib.error)

SCHEMA = """
CREATE TABLE IF NOT EXISTS imports (
    id        INTEGER PRIMARY KEY,
    key       TEXT NOT NULL UNIQUE,
    state     TEXT NOT NULL,                 -- copied | duplicate | skipped | failed
    root      TEXT NOT NULL DEFAULT '',
    rel_path  TEXT NOT NULL DEFAULT '',
    sha256    TEXT NOT NULL DEFAULT '',
    source    TEXT NOT NULL DEFAULT '',      -- google | icloud | files
    taken     REAL,
    lat       REAL,
    lon       REAL,
    caption   TEXT NOT NULL DEFAULT '',
    favorite  INTEGER NOT NULL DEFAULT 0,
    hidden    INTEGER NOT NULL DEFAULT 0,
    albums    TEXT NOT NULL DEFAULT '[]',
    user_id   INTEGER,
    applied   INTEGER NOT NULL DEFAULT 0,
    reason    TEXT NOT NULL DEFAULT '',
    at        REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_imports_pending ON imports(applied, state);
"""


def _media_exts() -> frozenset[str]:
    from ..server.config import (AMBIGUOUS_EXTS, AUDIO_EXTS, IMAGE_EXTS,  # noqa: PLC0415
                                 RAW_EXTS, VIDEO_EXTS)
    return frozenset(IMAGE_EXTS | RAW_EXTS | VIDEO_EXTS | AUDIO_EXTS | AMBIGUOUS_EXTS)


def is_media(name: str) -> bool:
    base = posixpath.basename(name)
    return not base.startswith(".") and posixpath.splitext(base)[1].lower() in _media_exts()


# ---------------------------------------------------------------------------
# Where the files are: zips, tars, and folders
# ---------------------------------------------------------------------------

@dataclass
class Member:
    source: "Source"
    name: str            # POSIX path inside the source
    size: int
    mtime: float
    #: The tar's own entry for it. Opening by name searches the tar's whole
    #: list of members each time: over a Takeout export of 100,000 files, a
    #: search through all of them for every file.
    info: Any = field(default=None, repr=False, compare=False)

    @property
    def key(self) -> str:
        return f"{self.source.path}|{self.name}|{self.size}"


class Source:
    """One export file, or one unpacked folder."""

    kind = "folder"

    def __init__(self, path: Path):
        self.path = Path(path)

    def members(self) -> Iterator[Member]:
        raise NotImplementedError

    def read(self, member: Member) -> bytes:
        with self.open(member) as handle:
            return handle.read(MAX_METADATA + 1)

    def open(self, member: Member):
        raise NotImplementedError

    def close(self) -> None:
        pass


class FolderSource(Source):
    def __init__(self, path: Path, skip: set[Path]):
        super().__init__(path)
        self._skip = skip

    def members(self) -> Iterator[Member]:
        for dirpath, dirnames, filenames in os.walk(self.path):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for filename in sorted(filenames):
                full = Path(dirpath) / filename
                if full in self._skip or filename.startswith("."):
                    continue
                try:
                    stat = full.stat()
                except OSError:
                    continue
                yield Member(self, full.relative_to(self.path).as_posix(), stat.st_size,
                             stat.st_mtime)

    def open(self, member: Member):
        return open(self.path / member.name, "rb")


class ZipSource(Source):
    kind = "zip"

    def __init__(self, path: Path):
        super().__init__(path)
        self._zip = zipfile.ZipFile(path)

    def members(self) -> Iterator[Member]:
        for info in self._zip.infolist():
            if info.is_dir():
                continue
            try:
                when = time.mktime(info.date_time + (0, 0, -1))
            except (OverflowError, ValueError):
                when = 0.0
            yield Member(self, info.filename, info.file_size, when)

    def open(self, member: Member):
        return self._zip.open(member.name)

    def close(self) -> None:
        self._zip.close()


class TarSource(Source):
    """A ``.tgz`` export. Read front to back; a tar has no index to jump by."""

    kind = "tar"

    def __init__(self, path: Path):
        super().__init__(path)
        self._tar = tarfile.open(path, "r:*")

    def members(self) -> Iterator[Member]:
        for info in self._tar:
            if info.isfile():
                yield Member(self, info.name, info.size, float(info.mtime or 0), info)

    def open(self, member: Member):
        handle = self._tar.extractfile(member.info if member.info is not None else member.name)
        if handle is None:
            raise OSError(f"{member.name} is not a file")
        return handle

    def close(self) -> None:
        self._tar.close()


ARCHIVE_SUFFIXES = (".zip", ".tgz", ".tar.gz", ".tar")


def _is_archive(path: Path) -> bool:
    name = path.name.lower()
    return any(name.endswith(s) for s in ARCHIVE_SUFFIXES)


def sources(folder: Path | str) -> list[Source]:
    """Every export file in *folder* (not below it), and the folder itself for
    anything already unpacked there."""
    folder = Path(folder)
    found: list[Source] = []
    archives = sorted(p for p in folder.iterdir() if p.is_file() and _is_archive(p))
    for path in archives:
        try:
            found.append(ZipSource(path) if path.name.lower().endswith(".zip")
                         else TarSource(path))
        except (zipfile.BadZipFile, tarfile.TarError, OSError) as exc:
            log.warning("could not open %s: %s", path.name, exc)
    found.append(FolderSource(folder, set(archives)))
    return found


# ---------------------------------------------------------------------------
# What travels beside the photographs
# ---------------------------------------------------------------------------

_DUPLICATE = re.compile(r"^(.*)\((\d+)\)(\.[^.]*)?$")
_SUPPLEMENTAL = ".supplemental-metadata"
#: Takeout cuts long sidecar names short; a cut this long is never a full name.
_TRUNCATED_MIN = 40
_YEAR_FOLDER = re.compile(r"^Photos from \d{4}$", re.IGNORECASE)
#: A WhatsApp chat export's text: ``_chat.txt`` (iPhone) or
#: ``WhatsApp Chat with Amma.txt`` (Android), and the zip it came in.
_WHATSAPP_TXT = re.compile(r"^WhatsApp Chat (?:with |- )(.+)\.txt$", re.IGNORECASE)
_WHATSAPP_ZIP = re.compile(r"^WhatsApp Chat (?:with |- )(.+?)(?: \(\d+\))?\.zip$", re.IGNORECASE)


def _google_sidecar(names: dict[str, str], base: str) -> str | None:
    """The sidecar name for *base* among a folder's JSON *names* (lowercased →
    real), by the rules Takeout names them with. See archive/dates.py, which
    applies the same rules to files on disk."""
    stem = posixpath.splitext(base)[0]
    wanted = [f"{base}.json", f"{base}{_SUPPLEMENTAL}.json", f"{stem}.json"]
    original, number = base, None
    dup = _DUPLICATE.match(base)
    if dup:
        original, number = dup.group(1) + (dup.group(3) or ""), dup.group(2)
        wanted += [f"{original}({number}).json", f"{original}{_SUPPLEMENTAL}({number}).json"]
    if stem.endswith("-edited"):
        original_base = stem[:-len("-edited")] + posixpath.splitext(base)[1]
        wanted += [f"{original_base}.json", f"{original_base}{_SUPPLEMENTAL}.json"]
    for name in wanted:
        if name.lower() in names:
            return names[name.lower()]
    target = (original + _SUPPLEMENTAL).lower()
    suffix = f"({number}).json" if number else ".json"
    cut = [real for low, real in names.items()
           if low.endswith(suffix) and len(low) - len(suffix) >= _TRUNCATED_MIN
           and target.startswith(low[:-len(suffix)])]
    return cut[0] if len(cut) == 1 else None


def _sidecar_facts(data: dict[str, Any]) -> dict[str, Any]:
    """What Metadata.about() will say from one Takeout sidecar: when, where,
    the caption and whether it was a favourite. Everything else the sidecar
    carries is let go of here, so the export is not held in memory twice."""
    facts: dict[str, Any] = {}
    for key in ("photoTakenTime", "creationTime"):
        block = data.get(key)
        if isinstance(block, dict) and str(block.get("timestamp") or "").isdigit():
            facts["taken"] = float(block["timestamp"])
            break
    for key in ("geoDataExif", "geoData"):
        geo = data.get(key)
        if isinstance(geo, dict):
            lat, lon = geo.get("latitude"), geo.get("longitude")
            if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) \
                    and (lat or lon) and -90 <= lat <= 90 and -180 <= lon <= 180:
                facts["lat"], facts["lon"] = float(lat), float(lon)
                break
    facts["caption"] = str(data.get("description") or "").strip()[:2000]
    facts["favorite"] = bool(data.get("favorited"))
    return facts


def _icloud_date(text: str) -> float | None:
    """``Tuesday December 17,2019 5:31 PM GMT`` and the like."""
    text = " ".join(str(text or "").replace(",", ", ").split())
    for pattern in ("%A %B %d, %Y %I:%M %p %Z", "%A %B %d, %Y %H:%M %Z",
                    "%B %d, %Y %I:%M %p %Z", "%Y-%m-%d %H:%M:%S"):
        try:
            when = datetime.strptime(text, pattern)
        except ValueError:
            continue
        return when.replace(tzinfo=timezone.utc).timestamp()
    return None


def _yes(value: Any) -> bool:
    return str(value or "").strip().lower() in ("yes", "true", "1")


class Metadata:
    """Everything the export says about its files, read before any is copied —
    Takeout puts a photograph and its sidecar in different zips often enough."""

    def __init__(self) -> None:
        #: folder → {sidecar name lowercased → the few fields about() reads}.
        #: A Takeout sidecar is a kilobyte or two of JSON, and a large export
        #: has one per photograph; keeping each one whole until its photograph
        #: turned up in some later zip held hundreds of megabytes for nothing.
        self.google: dict[str, dict[str, dict[str, Any]]] = {}
        self.google_names: dict[str, dict[str, str]] = {}
        #: album folder → its title, from the folder's metadata.json
        self.album_titles: dict[str, str] = {}
        #: file name lowercased → details from Photo Details.csv
        self.icloud: dict[str, dict[str, Any]] = {}
        #: file name lowercased → album names
        self.icloud_albums: dict[str, list[str]] = {}
        #: (source path, folder) → the WhatsApp chat's name
        self.whatsapp: dict[tuple[str, str], str] = {}

    def take(self, source: Source, member: Member) -> None:
        name = member.name
        low = name.lower()
        base = posixpath.basename(name)
        folder = posixpath.dirname(name)
        if member.size > MAX_METADATA:
            return
        if low.endswith(".txt"):
            chat = _WHATSAPP_TXT.match(base)
            if chat or base.lower() == "_chat.txt":
                zipped = _WHATSAPP_ZIP.match(source.path.name) if source.kind != "folder" else None
                name = (chat.group(1) if chat else zipped.group(1) if zipped
                        else posixpath.basename(folder) or source.path.stem)
                self.whatsapp[(str(source.path), folder)] = name.strip()[:120] or "chat"
            return
        if low.endswith(".json"):
            try:
                data = json.loads(source.read(member).decode("utf-8", "replace"))
            except UNREADABLE:
                return
            if not isinstance(data, dict):
                return
            if base.lower() == "metadata.json" and data.get("title"):
                self.album_titles[folder] = str(data["title"])[:200]
                return
            self.google.setdefault(folder, {})[base.lower()] = _sidecar_facts(data)
            self.google_names.setdefault(folder, {})[base.lower()] = base
        elif low.endswith(".csv"):
            try:
                text = source.read(member).decode("utf-8-sig", "replace")
            except UNREADABLE:
                return
            rows = list(csv.DictReader(io.StringIO(text)))
            if base.lower().startswith("photo details"):
                for row in rows:
                    image = str(row.get("imgName") or "").strip()
                    if image:
                        self.icloud[image.lower()] = row
            elif "/albums/" in f"/{low}" or low.startswith("albums/"):
                album = posixpath.splitext(base)[0]
                for row in rows:
                    for value in row.values():
                        for image in str(value or "").split(","):
                            image = image.strip()
                            if image and "." in image:
                                self.icloud_albums.setdefault(image.lower(), []).append(album)

    def about(self, member: Member) -> dict[str, Any]:
        """What is known about one file: its source, when, where, and more."""
        base = posixpath.basename(member.name)
        folder = posixpath.dirname(member.name)
        found: dict[str, Any] = {"source": "files", "albums": []}
        names = self.google_names.get(folder)
        sidecar = _google_sidecar(names, base) if names else None
        if sidecar:
            found["source"] = "google"
            found.update(self.google[folder][sidecar.lower()])
            album = posixpath.basename(folder)
            if album and not _YEAR_FOLDER.match(album) and album.lower() not in (
                    "google photos", "google foto", "takeout", "archive", "trash", "bin"):
                found["albums"] = [self.album_titles.get(folder, album)]
        chat = self.whatsapp.get((str(member.source.path), folder))
        if chat is not None:
            found["source"] = "whatsapp"
            found["albums"] = [f"WhatsApp: {chat}"]
            from ..archive.dates import filename_date, to_timestamp  # noqa: PLC0415
            named = filename_date(base)
            if named is not None:
                found["taken"] = to_timestamp(named)
        details = self.icloud.get(base.lower())
        if details is not None:
            found["source"] = "icloud"
            when = _icloud_date(details.get("originalCreationDate", ""))
            if when:
                found["taken"] = when
            found["favorite"] = _yes(details.get("favorite"))
            found["hidden"] = _yes(details.get("hidden"))
            found["deleted"] = _yes(details.get("deleted"))
            found["albums"] = list(dict.fromkeys(self.icloud_albums.get(base.lower(), [])))
        return found


def look(folder: Path | str) -> dict[str, Any]:
    """What an import from *folder* would find, without copying anything.

    Zips are counted from their index; a tar has none, so its contents are
    only counted when it is imported.
    """
    folder = Path(folder)
    found = sources(folder)
    media = size = sidecars = 0
    kinds = {"google": False, "icloud": False}
    archives = []
    try:
        for source in found:
            if source.kind == "tar":
                archives.append({"name": source.path.name, "kind": "tar", "media": None})
                continue
            here = 0
            for member in source.members():
                low = member.name.lower()
                if is_media(member.name):
                    media += 1
                    here += 1
                    size += member.size
                elif low.endswith(".json"):
                    sidecars += 1
                    if "takeout/" in low or "google photos" in low:
                        kinds["google"] = True
                elif low.endswith(".csv") and "photo details" in low:
                    kinds["icloud"] = True
                if "takeout/" in low:
                    kinds["google"] = True
                if "icloud photos" in low:
                    kinds["icloud"] = True
            if source.kind != "folder":
                archives.append({"name": source.path.name, "kind": source.kind, "media": here})
            elif here:
                archives.append({"name": "(files already unpacked here)", "kind": "folder",
                                 "media": here})
    finally:
        for source in found:
            source.close()
    return {"folder": str(folder), "media": media, "bytes": size, "sidecars": sidecars,
            "google": kinds["google"], "icloud": kinds["icloud"], "parts": archives}


# ---------------------------------------------------------------------------
# Doing it
# ---------------------------------------------------------------------------

class _Stop(Exception):
    pass


#: Library files' hashes kept between duplicate checks, at most.
HASH_CACHE_MAX = 50_000


class Importer:
    """One import at a time, on its own thread."""

    def __init__(self, cfg, connect_db: Callable[[], Any], *, scanner=None,
                 hold: Callable[[], str | None] | None = None):
        self.cfg = cfg
        self._connect = connect_db
        self.scanner = scanner
        self._hold = hold
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._state: dict[str, Any] = self._fresh()
        self._hash_cache: dict[tuple[str, str, float], str] = {}
        self._hints_guard = threading.Lock()
        self._hints_running = False
        self._hints_again = False
        if scanner is not None:
            scanner.add_listener(self._scan_heard)

    @staticmethod
    def _fresh() -> dict[str, Any]:
        return {"running": False, "phase": "", "folder": "", "destination": "",
                "found": 0, "done": 0, "copied": 0, "duplicates": 0, "skipped": 0,
                "failed": 0, "bytes": 0, "current": "", "message": "", "error": "",
                "waiting": "", "problems": []}

    def _db(self):
        conn = self._connect()
        conn.executescript(SCHEMA)
        return conn

    def status(self) -> dict[str, Any]:
        with self._lock:
            state = dict(self._state)
            state["problems"] = list(self._state["problems"])
        row = self._db().execute(
            "SELECT COUNT(*) FROM imports WHERE applied=0 AND state IN ('copied', 'duplicate')"
        ).fetchone()
        state["waiting_for_index"] = int(row[0])
        return state

    def _update(self, **fields: Any) -> None:
        with self._lock:
            self._state.update(fields)

    def _bump(self, **fields: int) -> None:
        with self._lock:
            for key, value in fields.items():
                self._state[key] += value

    def _problem(self, name: str, why: str) -> None:
        with self._lock:
            self._state["failed"] += 1
            if len(self._state["problems"]) < 100:
                self._state["problems"].append({"file": name, "why": why})

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self, folder: str, root: str, user_id: int | None) -> dict[str, Any]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {"started": False, "reason": "An import is already running."}
            self._stop.clear()
            self._state = self._fresh()
            self._state.update(running=True, phase="reading", folder=str(folder),
                               message="Reading what the export says about its photographs…")
            self._thread = threading.Thread(target=self._run, args=(Path(folder), root, user_id),
                                            name="ninaivu-import", daemon=True)
            self._thread.start()
        return {"started": True}

    def stop(self, join: bool = False) -> None:
        self._stop.set()
        if join and self._thread:
            self._thread.join(30)

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def _pause_for_the_household(self) -> None:
        while self._hold is not None:
            reason = self._hold()
            if not reason:
                break
            self._update(waiting=reason)
            if self._stop.wait(5):
                raise _Stop()
        self._update(waiting="")
        if self._stop.is_set():
            raise _Stop()

    def _run(self, folder: Path, root: str, user_id: int | None) -> None:
        from . import new_files                             # noqa: PLC0415

        found: list[Source] = []
        try:
            destination = new_files.destination(self.cfg, root)
            self._update(destination=destination)
            conn = self._db()
            found = sources(folder)
            meta = Metadata()
            media: list[Member] = []
            for source in found:
                # One damaged part of an export (a .tgz whose download was cut
                # short) is reported and passed over: the other parts are
                # still worth importing.
                try:
                    for member in source.members():
                        if self._stop.is_set():
                            raise _Stop()
                        if is_media(member.name):
                            media.append(member)
                        elif member.name.lower().endswith((".json", ".csv", ".txt")):
                            meta.take(source, member)
                except UNREADABLE as exc:
                    log.warning("could not read all of %s: %s", source.path.name, exc)
                    self._problem(source.path.name, f"could not be read to the end: {exc}")
            # What an earlier import did counts only while its result is still
            # in the library. After losing the library, importing the same
            # export again brought nothing back: every file was "done before",
            # or a "duplicate" of a copy that no longer exists.
            done = {row[0]: (row[1], row[2], row[3]) for row in conn.execute(
                "SELECT key, state, root, rel_path FROM imports "
                "WHERE state IN ('copied', 'duplicate', 'skipped')")}
            self._update(phase="copying", found=len(media), message="Copying into the library…")
            seen_here: dict[str, tuple[str, str]] = {}
            for row in conn.execute(
                    "SELECT sha256, root, rel_path FROM imports WHERE state='copied' AND sha256 != ''"):
                seen_here[row[0]] = (row[1], row[2])
            for member in media:
                self._pause_for_the_household()
                earlier = done.get(member.key)
                if earlier is not None and (earlier[0] == "skipped"
                                            or self._still_there(earlier[1], earlier[2])):
                    self._bump(done=1, skipped=1)
                    continue
                self._update(current=member.name)
                try:
                    self._one(conn, member, meta.about(member), destination, user_id, seen_here)
                except _Stop:
                    raise
                except UNREADABLE as exc:
                    log.warning("could not import %s: %s", member.name, exc)
                    self._problem(member.name, str(exc))
                    conn.execute(
                        "INSERT INTO imports(key, state, reason, at) VALUES(?, 'failed', ?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET state='failed', reason=excluded.reason",
                        (member.key, str(exc)[:300], time.time()))
                    conn.commit()
                self._bump(done=1)
            self._tidy(destination)
            snap = self.status()
            copied, dupes = snap["copied"], snap["duplicates"]
            message = (f"Copied {copied:,} file{'' if copied == 1 else 's'}; {dupes:,} "
                       f"{'was' if dupes == 1 else 'were'} already in the library")
            if snap["skipped"]:
                message += f"; {snap['skipped']:,} skipped (deleted in iCloud, or done before)"
            if snap["failed"]:
                message += f"; {snap['failed']:,} could not be read"
            message += "."
            if snap["waiting_for_index"]:
                message += (" Places, favourites, descriptions and albums are applied as "
                            "soon as the library has indexed the new files.")
                self._update(phase="indexing")
                if self.scanner is not None:
                    self.scanner.start([destination])
            else:
                self._update(phase="done")
            self._update(running=False, current="", message=message)
            self.apply_hints()
        except _Stop:
            self._update(running=False, phase="stopped", current="",
                         message="Stopped. Starting the same import again carries on from here.")
        except Exception as exc:                            # noqa: BLE001
            log.exception("the import stopped")
            self._update(running=False, phase="stopped", current="",
                         error=f"The import stopped: {exc}")
        finally:
            for source in found:
                source.close()

    def _tidy(self, destination: str) -> None:
        try:
            (Path(destination) / STAGING).rmdir()
        except OSError:
            pass

    # -- one file ---------------------------------------------------------

    def _one(self, conn, member: Member, about: dict[str, Any], destination: str,
             user_id: int | None, seen_here: dict[str, tuple[str, str]]) -> None:
        if about.get("deleted"):
            conn.execute("INSERT OR REPLACE INTO imports(key, state, source, reason, at) "
                         "VALUES(?, 'skipped', ?, ?, ?)",
                         (member.key, about["source"], "deleted in iCloud", time.time()))
            conn.commit()
            self._bump(skipped=1)
            return
        base = Path(destination)
        staging = base / STAGING
        staging.mkdir(parents=True, exist_ok=True)
        partial = staging / f"{hashlib.sha1(member.key.encode()).hexdigest()}.part"  # noqa: S324
        digest = hashlib.sha256()
        written = 0
        try:
            with member.source.open(member) as src, open(partial, "wb") as out:
                while chunk := src.read(CHUNK):
                    if self._stop.is_set():
                        raise _Stop()
                    out.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
            if written != member.size:
                raise OSError(f"only {written:,} of {member.size:,} bytes could be read")
            sha = digest.hexdigest()
            same = seen_here.get(sha)
            if same is not None and not self._still_there(*same):
                same = None
            same = same or self._already_here(conn, member.size, sha)
            if same is not None:
                partial.unlink()
                self._record(conn, member, about, "duplicate", same[0], same[1], sha, user_id)
                self._bump(duplicates=1)
                return
            taken = about.get("taken") or self._date_inside(partial) or member.mtime or time.time()
            day = time.localtime(taken)
            name = _safe_name(posixpath.basename(member.name))
            folder = base / f"{day.tm_year:04d}" / f"{day.tm_mon:02d}" / f"{day.tm_mday:02d}"
            folder.mkdir(parents=True, exist_ok=True)
            # Synced only now it is kept: a duplicate, dropped above, need
            # never have been pushed to the disk.
            with open(partial, "rb+") as out:
                os.fsync(out.fileno())
            os.utime(partial, (taken, taken))
            about = {**about, "taken": taken}

            def claim(candidate: Path) -> None:
                # Written down before the file appears under its name, so a
                # scan that finds it at once already knows whether the export
                # had it hidden (db.bulk_upsert hides it as it is indexed).
                self._record(conn, member, about, "copied", str(base),
                             candidate.relative_to(base).as_posix(), sha, user_id)

            target = _publish(partial, folder / name, claim)
            rel = target.relative_to(base).as_posix()
            seen_here[sha] = (str(base), rel)
            self._bump(copied=1, bytes=written)
        finally:
            partial.unlink(missing_ok=True)

    @staticmethod
    def _date_inside(path: Path) -> float | None:
        """The date written inside the file, when the export gave none."""
        try:
            from ..archive import scanner as archive_scanner     # noqa: PLC0415
            from ..archive.dates import to_timestamp             # noqa: PLC0415
            when, label = archive_scanner.capture_date(str(path))
            if when is not None and label in ("exif", "container"):
                return to_timestamp(when)
        except Exception:                                   # noqa: BLE001
            return None
        return None

    def _still_there(self, root: str, rel: str) -> bool:
        """Whether an earlier import's file is still in this library.

        A library folder that is away answers yes: its files are not gone, and
        importing them again would make a second copy of each.
        """
        from . import roots as roots_kit                    # noqa: PLC0415

        if not root or not rel or root not in (getattr(self.cfg, "roots", None) or []):
            return False
        if not roots_kit.available(root):
            return True
        return (Path(root) / rel).is_file()

    def _already_here(self, conn, size: int, sha: str) -> tuple[str, str] | None:
        """A library file with these bytes, looked for only among its size."""
        # Named by root as well as size, because the index on size leads with
        # root: asked for a size alone, SQLite read the whole assets table for
        # every file imported. The library's folders are the only ones an
        # import can be a duplicate of anyway.
        roots = [str(root) for root in (getattr(self.cfg, "roots", None) or [])]
        where = "size=? AND trashed=0"
        params: list[Any] = [int(size)]
        if roots:
            where += f" AND root IN ({','.join('?' * len(roots))})"
            params += roots
        for row in conn.execute(
                f"SELECT root, rel_path, mtime FROM assets WHERE {where} LIMIT 50", params):
            key = (row["root"], row["rel_path"], float(row["mtime"] or 0))
            known = self._hash_cache.get(key)
            if known is None:
                try:
                    known = _hash(Path(row["root"]) / row["rel_path"])
                except OSError:
                    continue
                if len(self._hash_cache) >= HASH_CACHE_MAX:
                    # Kept for the life of the server, across every import:
                    # bounded, so a household importing for years does not
                    # hold a hash of every file it ever compared.
                    self._hash_cache.clear()
                self._hash_cache[key] = known
            if known == sha:
                return row["root"], row["rel_path"]
        return None

    def _record(self, conn, member: Member, about: dict[str, Any], state: str, root: str,
                rel: str, sha: str, user_id: int | None) -> None:
        conn.execute(
            "INSERT INTO imports(key, state, root, rel_path, sha256, source, taken, lat, lon, "
            "caption, favorite, hidden, albums, user_id, applied, at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?) "
            "ON CONFLICT(key) DO UPDATE SET state=excluded.state, root=excluded.root, "
            "rel_path=excluded.rel_path, sha256=excluded.sha256, applied=0",
            (member.key, state, root, rel, sha, about.get("source", "files"), about.get("taken"),
             about.get("lat"), about.get("lon"), about.get("caption", ""),
             int(bool(about.get("favorite"))), int(bool(about.get("hidden"))),
             json.dumps(about.get("albums") or []), user_id, time.time()))
        conn.commit()

    # -- after the library has indexed them ---------------------------------

    def _scan_heard(self, payload: dict[str, Any]) -> None:
        # From "indexed", not only "done": the files are in the index once
        # indexing ends, and a first scan's analysis after it is hours. Until
        # the hints went on, photographs hidden in iCloud showed in the family
        # gallery and were read by every AI pass.
        if payload.get("phase") not in ("indexed", "done"):
            return
        # One at a time. Each scan announces "done" for every library folder
        # and once more for the whole, and every one of them started its own
        # thread over the same rows.
        with self._hints_guard:
            if self._hints_running:
                self._hints_again = True
                return
            self._hints_running = True
        threading.Thread(target=self._apply_hints_once, name="ninaivu-import-hints",
                         daemon=True).start()

    def _apply_hints_once(self) -> None:
        try:
            while True:
                try:
                    self.apply_hints()
                except Exception:                           # noqa: BLE001
                    log.exception("could not apply what the export said")
                with self._hints_guard:
                    if not self._hints_again:
                        self._hints_running = False
                        return
                    self._hints_again = False
        finally:
            from . import db                                # noqa: PLC0415
            db.end_open_transactions(failed=True)

    def apply_hints(self) -> int:
        """Put what the export said onto the library's own record. Idempotent.

        Only onto what is not already known: a place the photograph's own data
        gave, or a description somebody wrote here, is not replaced.
        """
        from . import db                                    # noqa: PLC0415

        conn = self._db()
        rows = conn.execute(
            "SELECT * FROM imports WHERE applied=0 AND state IN ('copied', 'duplicate')").fetchall()
        applied = 0
        albums: dict[str, int] = {}
        for row in rows:
            asset = conn.execute("SELECT id FROM assets WHERE root=? AND rel_path=? AND trashed=0",
                                 (row["root"], row["rel_path"])).fetchone()
            if asset is None:
                continue                                    # not indexed yet
            asset_id = int(asset["id"])
            if row["lat"] is not None and row["lon"] is not None:
                conn.execute("UPDATE assets SET gps_lat=?, gps_lon=? WHERE id=? "
                             "AND gps_lat IS NULL", (row["lat"], row["lon"], asset_id))
            if row["caption"]:
                # Somebody typed it, in Google Photos: as much theirs as one
                # typed here, so a later automatic caption does not replace it.
                conn.execute("UPDATE assets SET caption=?, caption_source='manual' "
                             "WHERE id=? AND COALESCE(caption, '')=''",
                             (row["caption"], asset_id))
            conn.commit()
            if row["favorite"] and row["user_id"]:
                db.set_user_asset(conn, int(row["user_id"]), asset_id, favorite=1)
            if row["hidden"] and row["state"] == "copied":
                db.set_visibility(conn, [asset_id], 2, source="item",
                                  user_id=row["user_id"], record_undo=False)
            for name in json.loads(row["albums"] or "[]"):
                name = str(name).strip()[:200]
                if not name:
                    continue
                if name not in albums:
                    albums[name] = db.create_album(conn, name, created_by=row["user_id"])
                db.album_add(conn, albums[name], [asset_id])
            conn.execute("UPDATE imports SET applied=1 WHERE id=?", (row["id"],))
            conn.commit()
            applied += 1
        if applied:
            log.info("applied what the export said to %s imported files", applied)
            with self._lock:
                if self._state.get("phase") == "indexing" and not self._pending(conn):
                    self._state["phase"] = "done"
        return applied

    @staticmethod
    def _pending(conn) -> int:
        return int(conn.execute("SELECT COUNT(*) FROM imports WHERE applied=0 AND state IN "
                                "('copied', 'duplicate')").fetchone()[0])


def _safe_name(name: str) -> str:
    # One name for every disk the library may be on: a ":" from a Mac export
    # wrote into an NTFS alternate data stream, and "?" or "*" could not be
    # created on exFAT at all. The export's own name stays in the import's key.
    return portable_name(name, "photo")


def _publish(partial: Path, target: Path,
             claim: Callable[[Path], None] | None = None) -> Path:
    """Give the staged file its name, never over another file. *claim* is
    told the name about to be taken, just before it is."""
    stem, suffix = target.stem, target.suffix
    for n in range(1, 10000):
        candidate = target if n == 1 else target.with_name(f"{stem} ({n}){suffix}")
        if candidate.exists():
            continue
        if claim is not None:
            claim(candidate)
        try:
            os.link(partial, candidate)
            partial.unlink()
            return candidate
        except FileExistsError:
            continue
        except OSError:
            # A filesystem without hard links (exFAT). Checked a moment ago.
            if candidate.exists():
                continue
            shutil.move(str(partial), str(candidate))
            return candidate
    raise OSError(f"no free name for {target.name}")


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()
