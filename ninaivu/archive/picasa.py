"""What Picasa knew about the photographs the import brought in.

Picasa (Google, 2002-2016) kept everything it was told in a small text file
in each folder it looked at: ``.picasa.ini`` (``Picasa.ini`` in older
versions). A family that used it for ten years has, in those files, the
photographs it starred, the faces it named, the albums it made, the photos
it turned the right way up and the ones it hid. None of that is in the
photographs themselves, so an import that copies the photographs leaves all
of it behind on the old drive.

Picasa also never overwrote a photograph when an edit was saved: the edited
picture took the original's name, and the untouched original was moved into
``.picasaoriginals`` (``Originals`` in Picasa 2) beside it. The import
brings both across as two unrelated photographs.

This reads those files, read-only, and puts what they say onto the library's
own record:

* a **star** becomes a favourite of the administrator who asked;
* a **named face** names the face Ninaivu found in the same place, and the
  person is found by name or made (an "Amma" here and an "Amma" in Picasa are
  one person);
* an **album** is made by its Picasa name, or added to when one has that name;
* a **turn** (``rotate=rotate(1)``) is kept as the photograph's turn, as a
  turn made in the viewer is;
* **hidden** makes the photograph hidden, which administrators still see;
* a **caption** typed into Picasa is kept when the photograph has none;
* a saved edit and its **original** are put in one stack, the edit on top
  as the family saw it in Picasa, the original one tap away (with
  ``photo_stacks`` on, when the two are alike enough to stack).

Nothing is written to the old drive or to any photograph. Each thing is put
on once (``picasa_done``): a favourite somebody takes off afterwards, or an
album they empty, is not put back by the next run. Face names wait for
Ninaivu to have found the faces; a run after the face search places them.

Where the files are comes from two places, so a drive does not have to be
walked again: the import's own record of every file it copied
(``archive.db``, which says where each one went), and the library's folders,
for a Picasa folder that was added to the library as it was.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

log = logging.getLogger(__name__)

__all__ = ["INI_NAMES", "Entry", "Folder", "parse", "read_folder", "rect64",
           "find", "bring_in", "Rescue"]

#: The names Picasa gave its file, newest first. ``.picasa.ini`` from 3.0 on,
#: ``Picasa.ini`` before; ``picasa.ini`` for a drive that went through a
#: program that lowercased names.
INI_NAMES = (".picasa.ini", "Picasa.ini", "picasa.ini")
#: Where Picasa moved an original when an edit was saved over it.
ORIGINALS = (".picasaoriginals", "originals")
#: The largest .picasa.ini read. A folder of 5,000 photographs, every one
#: with faces and albums, is well under a megabyte.
MAX_INI = 4 * 1024 * 1024
#: Picasa's contact for a face somebody told it to ignore.
NO_CONTACT = "ffffffffffffffff"
#: Words a family is unlikely to have meant as a person's name.
_NOT_NAMES = {"", "unknown", "unnamed", "ignore", "ignored"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS picasa_done (
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    what     TEXT NOT NULL,         -- star | album | rotate | hidden | caption | face | top
    value    TEXT NOT NULL DEFAULT '',
    at       REAL NOT NULL,
    PRIMARY KEY (asset_id, what, value)
);
"""


# ---------------------------------------------------------------------------
# Reading one folder's file
# ---------------------------------------------------------------------------

@dataclass
class Entry:
    """What Picasa said about one photograph."""
    star: bool = False
    rotate: int = 0                 # clockwise quarter turns, 0-3
    hidden: bool = False
    caption: str = ""
    #: Each face as (left, top, right, bottom), fractions of the picture as
    #: stored in the file, and Picasa's contact id for who it is.
    faces: list[tuple[tuple[float, float, float, float], str]] = field(default_factory=list)
    albums: list[str] = field(default_factory=list)      # album tokens


@dataclass
class Folder:
    entries: dict[str, Entry] = field(default_factory=dict)   # by lowercased file name
    albums: dict[str, str] = field(default_factory=dict)      # token -> name
    contacts: dict[str, str] = field(default_factory=dict)    # contact id -> name


_ROTATE = re.compile(r"rotate\((\d)\)")
_RECT = re.compile(r"rect64\(([0-9a-fA-F]{1,16})\)")


def rect64(text: str) -> tuple[float, float, float, float] | None:
    """A Picasa ``rect64(...)`` box as (left, top, right, bottom) fractions.

    The sixteen hex digits are four 16-bit numbers, each a fraction of 65535;
    Picasa drops leading zeros, so a face at the very left has fewer digits.
    """
    match = _RECT.fullmatch(text.strip())
    if match is None:
        return None
    digits = match.group(1).zfill(16)
    left, top, right, bottom = (int(digits[i:i + 4], 16) / 65535 for i in range(0, 16, 4))
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _decode(raw: bytes) -> str:
    # Picasa wrote UTF-8 on every platform; a file a Windows tool resaved can
    # be in the old code page, where a name such as "Sébastien" would not
    # decode at all.
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse(text: str) -> Folder:
    """Read the text of a ``.picasa.ini``. Anything not understood is skipped."""
    folder = Folder()
    section: str | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith((";", "#")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if section is None or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        key = key.lower()
        if section.lower() == "contacts2":
            # id=Name;email;  — Picasa 3.9 keeps the names of the faces in
            # this folder here, so the names travel with the drive.
            name = value.split(";", 1)[0].strip()
            if name.lower() not in _NOT_NAMES:
                folder.contacts[key] = name[:120]
            continue
        if section.lower().startswith(".album:"):
            if key == "name" and value:
                folder.albums[section.split(":", 1)[1].strip()] = value[:200]
            continue
        if section.lower() in ("picasa", "encoding"):
            continue
        entry = folder.entries.setdefault(section.lower(), Entry())
        if key == "star":
            entry.star = value.lower() == "yes"
        elif key == "hidden":
            entry.hidden = value.lower() == "yes"
        elif key == "rotate":
            turn = _ROTATE.fullmatch(value)
            if turn:
                entry.rotate = int(turn.group(1)) % 4
        elif key == "caption":
            entry.caption = value[:1000]
        elif key == "albums":
            entry.albums = [token.strip() for token in value.split(",") if token.strip()]
        elif key == "faces":
            for part in value.split(";"):
                if "," not in part:
                    continue
                box_text, contact = part.rsplit(",", 1)
                box = rect64(box_text)
                contact = contact.strip().lower()
                if box is not None and contact and contact != NO_CONTACT:
                    entry.faces.append((box, contact))
    return folder


def read_folder(folder: str) -> Folder | None:
    """The Picasa file in *folder*, read, or None when it has none."""
    from .safety import long_path                         # noqa: PLC0415

    for name in INI_NAMES:
        path = os.path.join(folder, name)
        try:
            if not os.path.isfile(long_path(path)):
                continue
            with open(long_path(path), "rb") as stream:
                raw = stream.read(MAX_INI + 1)
        except OSError:
            continue
        if len(raw) > MAX_INI:
            log.info("%s is larger than any Picasa file; left alone", path)
            return None
        return parse(_decode(raw))
    return None


# ---------------------------------------------------------------------------
# Which library photograph each entry is
# ---------------------------------------------------------------------------

def _key(path: str) -> str:
    """One spelling of *path* for comparing: NFC (a Mac drive hands back NFD
    names), the long-path prefix gone, Windows' case folded."""
    from .safety import strip_long_prefix                 # noqa: PLC0415
    return os.path.normcase(unicodedata.normalize("NFC", strip_long_prefix(str(path))))


def _library(conn) -> tuple[dict[str, int], dict[str, dict[str, int]]]:
    """Every photograph in the index by full path, and by folder then name."""
    by_path: dict[str, int] = {}
    by_folder: dict[str, dict[str, int]] = {}
    for row in conn.execute("SELECT id, root, rel_path FROM assets WHERE trashed=0"):
        full = os.path.join(row["root"], *str(row["rel_path"]).split("/"))
        key = _key(full)
        by_path[key] = int(row["id"])
        folder, name = os.path.split(key)
        by_folder.setdefault(folder, {})[name.lower()] = int(row["id"])
    return by_path, by_folder


def _imported(by_path: dict[str, int]) -> tuple[dict[str, dict[str, int | None]], list[str]]:
    """Every folder the import copied from, with each file in it and the
    library photograph it became (None while not indexed), and the folders
    the imports were started on."""
    from . import database as adb                         # noqa: PLC0415
    from .safety import normalise_sources                 # noqa: PLC0415

    folders: dict[str, dict[str, int | None]] = {}
    conn = adb.get_db()
    for row in conn.execute(
            "SELECT source_path, destination_path FROM files WHERE status IN "
            "('verified', 'duplicate') AND destination_path IS NOT NULL"):
        source = _key(row["source_path"])
        folder, name = os.path.split(source)
        folders.setdefault(folder, {})[name.lower()] = by_path.get(_key(row["destination_path"]))
    starts: set[str] = set()
    for row in conn.execute("SELECT sources FROM jobs"):
        try:
            for source in normalise_sources(json.loads(row["sources"] or "[]")):
                starts.add(_key(source["path"]))
        except (TypeError, ValueError, KeyError):
            continue
    return folders, sorted(starts, key=len, reverse=True)


def _start_of(folder: str, starts: list[str]) -> str | None:
    for start in starts:                  # longest first
        if folder == start or folder.startswith(start.rstrip(os.sep) + os.sep):
            return start
    return None


@dataclass
class Found:
    """Every Picasa folder that could be read, and what each entry became."""
    #: (folder, its file, {lowercased name: asset id or None}) for each.
    folders: list[tuple[str, Folder, dict[str, int | None]]] = field(default_factory=list)
    contacts: dict[str, str] = field(default_factory=dict)
    albums: dict[str, str] = field(default_factory=dict)
    #: (edited asset, original asset) for each original Picasa set aside.
    pairs: list[tuple[int, int]] = field(default_factory=list)
    checked: int = 0
    unreachable: list[str] = field(default_factory=list)


def find(conn, *, should_stop: Callable[[], bool] = lambda: False,
         progress: Callable[[int, int], None] = lambda done, total: None) -> Found:
    """Look in every folder the library came from for a Picasa file.

    Only folders that gave the library a photograph are looked in, and each
    is asked for three file names: no folder is listed, and a drive is not
    walked again. A drive that is not plugged in now is named, not guessed at.
    """
    by_path, by_folder = _library(conn)
    try:
        imported, starts = _imported(by_path)
    except Exception:                                     # noqa: BLE001 - no import ever run
        log.debug("no import record to read", exc_info=True)
        imported, starts = {}, []
    folders: dict[str, dict[str, int | None]] = {}
    for folder, names in imported.items():
        folders.setdefault(folder, {}).update(names)
    for folder, names in by_folder.items():
        merged = folders.setdefault(folder, {})
        for name, asset_id in names.items():
            if merged.get(name) is None:
                merged[name] = asset_id

    found = Found()
    present: dict[str, bool] = {}
    missing: set[str] = set()
    order = sorted(folders)
    for done, folder in enumerate(order, 1):
        if should_stop():
            break
        if done % 200 == 0:
            progress(done, len(order))
        start = _start_of(folder, starts)
        if start is not None:
            if start not in present:
                present[start] = os.path.isdir(start)
            if not present[start]:
                missing.add(start)
                continue
        found.checked += 1
        picasa = read_folder(folder)
        if picasa is None:
            continue
        found.folders.append((folder, picasa, folders[folder]))
        found.contacts.update(picasa.contacts)
        found.albums.update(picasa.albums)
    progress(len(order), len(order))
    found.unreachable = sorted(missing)

    # Each original Picasa set aside, with the edit that took its place.
    readable = {folder for folder, _, _ in found.folders}
    for folder, names in folders.items():
        parent, base = os.path.split(folder)
        if base.lower() not in ORIGINALS or parent not in readable:
            continue
        beside = folders.get(parent, {})
        for name, original in names.items():
            edited = beside.get(name)
            if original is not None and edited is not None and original != edited:
                found.pairs.append((edited, original))
    return found


def summary(found: Found) -> dict[str, Any]:
    """What a run would bring in, counted."""
    counts = {"stars": 0, "faces": 0, "unnamed_faces": 0, "turned": 0, "hidden": 0,
              "captions": 0, "in_albums": 0, "photos": 0, "not_in_library": 0}
    people: set[str] = set()
    albums: set[str] = set()
    for _, picasa, names in found.folders:
        for name, entry in picasa.entries.items():
            if names.get(name) is None:
                counts["not_in_library"] += 1
                continue
            counts["photos"] += 1
            counts["stars"] += entry.star
            counts["turned"] += bool(entry.rotate)
            counts["hidden"] += entry.hidden
            counts["captions"] += bool(entry.caption)
            for _, contact in entry.faces:
                person = found.contacts.get(contact)
                if person:
                    counts["faces"] += 1
                    people.add(person.casefold())
                else:
                    counts["unnamed_faces"] += 1
            named = [found.albums[t] for t in entry.albums if t in found.albums]
            counts["in_albums"] += bool(named)
            albums.update(named)
    return {"folders": len(found.folders), "checked": found.checked,
            "unreachable": found.unreachable[:10], "unreachable_count": len(found.unreachable),
            "people": len(people), "albums": len(albums), "originals": len(found.pairs),
            "sample": [os.path.basename(f) or f for f, _, _ in found.folders[:5]],
            **counts}


# ---------------------------------------------------------------------------
# Putting it on
# ---------------------------------------------------------------------------

def _done(conn, asset_id: int, what: str, value: str = "") -> bool:
    return conn.execute("SELECT 1 FROM picasa_done WHERE asset_id=? AND what=? AND value=?",
                        (asset_id, what, value)).fetchone() is not None


def _mark(conn, asset_id: int, what: str, value: str = "") -> None:
    conn.execute("INSERT OR IGNORE INTO picasa_done(asset_id, what, value, at) VALUES (?,?,?,?)",
                 (asset_id, what, value, time.time()))


_EXIF_TURN = {3: 180, 4: 180, 5: 90, 6: 90, 7: 270, 8: 270}


def _turn_box(box: tuple[float, float, float, float], step: int,
              width: float, height: float) -> tuple[float, float, float, float]:
    """A box (x, y, w, h) of a *width* x *height* picture after a clockwise turn."""
    x, y, w, h = box
    if step == 90:
        return height - y - h, x, h, w
    if step == 180:
        return width - x - w, height - y - h, w, h
    if step == 270:
        return y, width - x - w, h, w
    return box


def _stored_size(row: dict[str, Any]) -> tuple[tuple[int, int], int] | None:
    """The photograph's size as stored in the file and its EXIF turn, read
    from the file's header only."""
    from PIL import Image                                 # noqa: PLC0415
    try:
        with Image.open(Path(row["root"]) / row["rel_path"]) as img:
            orientation = int(img.getexif().get(0x0112) or 1)
            return img.size, _EXIF_TURN.get(orientation, 0)
    except Exception:                                     # noqa: BLE001
        return None


def place_faces(hints: list[tuple[tuple[float, float, float, float], str]],
                faces: list[dict[str, Any]], stored: tuple[int, int],
                expected_turn: int) -> list[tuple[int, int, float]]:
    """Which found face each Picasa box is: ``(hint, face index, overlap)``.

    Picasa's boxes are fractions of the picture as stored; Ninaivu's are in
    pixels of the picture shown the right way up. The turn between the two
    is the EXIF tag's and the photograph's own, *expected_turn*, but tools
    that resaved a photograph sometimes kept Picasa's box and lost the tag,
    so every quarter turn is tried and the one under which the boxes land on
    the faces best is kept, *expected_turn* winning a tie.
    """
    from ..storage import db                              # noqa: PLC0415

    width, height = stored
    raw = [(left * width, top * height, (right - left) * width, (bottom - top) * height)
           for (left, top, right, bottom), _ in hints]
    boxes = []
    for face in faces:
        box = face["bbox"]
        box = json.loads(box) if isinstance(box, str) else box
        boxes.append(tuple(float(v) for v in box))
    best: tuple[float, int, list[tuple[int, int, float]]] | None = None
    for step in sorted((0, 90, 180, 270), key=lambda s: s != expected_turn % 360):
        turned = [_turn_box(box, step, width, height) for box in raw]
        pairs = db._pair_boxes(turned, boxes)             # noqa: SLF001 - the carrying rule
        placed = [(k, j, db._box_iou(turned[k], boxes[j])) for k, j in pairs.items()]  # noqa: SLF001
        score = sum(overlap for _, _, overlap in placed)
        if placed and (best is None or score > best[0] + 1e-9):
            best = (score, step, placed)
    return best[2] if best else []


def bring_in(conn, cfg, found: Found, *, user_id: int | None,
             refresh_person: Callable[[int], None] | None = None,
             should_stop: Callable[[], bool] = lambda: False) -> dict[str, Any]:
    """Put what the Picasa files say onto the library's photographs. Idempotent."""
    from ..media import upright                           # noqa: PLC0415
    from ..storage import db                              # noqa: PLC0415
    from ..storage import stacks                          # noqa: PLC0415

    conn.executescript(SCHEMA)
    out = {"favourites": 0, "albums": 0, "album_photos": 0, "turned": 0, "hidden": 0,
           "captions": 0, "faces": 0, "faces_waiting": 0, "faces_unplaced": 0, "stacked": 0,
           "unreadable": 0}
    album_ids: dict[str, int] = {}
    people: dict[str, int] = {}
    touched: set[int] = set()
    to_hide: list[int] = []

    def entries() -> Iterable[tuple[int, Entry]]:
        for _, picasa, names in found.folders:
            for name, entry in picasa.entries.items():
                asset_id = names.get(name)
                if asset_id is not None:
                    yield asset_id, entry
        # The parent's word for a saved edit is the original's too: the same
        # photograph, starred and in the same albums.
        edits = {edited: original for edited, original in found.pairs}
        for _, picasa, names in found.folders:
            for name, entry in picasa.entries.items():
                original = edits.get(names.get(name))   # type: ignore[arg-type]
                if original is not None:
                    yield original, Entry(star=entry.star, albums=entry.albums,
                                          faces=entry.faces)

    for asset_id, entry in entries():
        if should_stop():
            break
        row = db.get_asset(conn, asset_id)
        if row is None or row.get("trashed"):
            continue
        if entry.star and user_id and not _done(conn, asset_id, "star"):
            db.set_user_asset(conn, user_id, asset_id, favorite=1)
            _mark(conn, asset_id, "star")
            out["favourites"] += 1
        for token in entry.albums:
            name = found.albums.get(token)
            if not name or _done(conn, asset_id, "album", name):
                continue
            if name not in album_ids:
                before = conn.execute("SELECT id FROM albums WHERE name=?", (name,)).fetchone()
                album_ids[name] = db.create_album(conn, name, created_by=user_id)
                out["albums"] += before is None
            db.album_add(conn, album_ids[name], [asset_id])
            _mark(conn, asset_id, "album", name)
            out["album_photos"] += 1
        if entry.caption and not _done(conn, asset_id, "caption"):
            with db._write_lock:                          # noqa: SLF001
                changed = conn.execute(
                    "UPDATE assets SET caption=?, caption_source='manual' "
                    "WHERE id=? AND COALESCE(caption, '')=''", (entry.caption, asset_id)).rowcount
            _mark(conn, asset_id, "caption")
            out["captions"] += changed
        if (entry.hidden and not _done(conn, asset_id, "hidden")
                and int(row.get("visibility") or 0) < 2 and row.get("vis_source") != "item"):
            to_hide.append(asset_id)
            _mark(conn, asset_id, "hidden")
        conn.commit()
        if (entry.rotate and row.get("kind") == "picture" and row.get("rot_source") != "manual"
                and not _done(conn, asset_id, "rotate")):
            try:
                upright.save_turn(conn, cfg, row, entry.rotate * 90)
                row = db.get_asset(conn, asset_id) or row
                out["turned"] += 1
            except Exception:                             # noqa: BLE001 - a turn is not worth the run
                log.info("could not keep Picasa's turn for photograph %s", asset_id, exc_info=True)
            _mark(conn, asset_id, "rotate")
            conn.commit()
        if entry.faces:
            _name_faces(conn, row, entry, found.contacts, people, touched, out)

    if to_hide:
        db.set_visibility(conn, to_hide, 2, source="item", user_id=user_id)
        out["hidden"] = len(to_hide)

    for edited, original in found.pairs:
        if should_stop() or _done(conn, edited, "top"):
            continue
        rows = [conn.execute("SELECT * FROM assets WHERE id=?", (i,)).fetchone()
                for i in (edited, original)]
        if None in rows or rows[0]["dup_group"] is None or rows[1]["dup_group"] is None:
            continue
        key = stacks.key_of(rows[0])
        if key != stacks.key_of(rows[1]):
            continue
        with db._write_lock:                              # noqa: SLF001
            conn.execute(
                "INSERT INTO stack_choices(key, cover, apart, chosen_by, chosen_at) "
                "VALUES (?, ?, 0, ?, ?) ON CONFLICT(key) DO NOTHING",
                (key, edited, user_id, time.time()))
            _mark(conn, edited, "top")
            conn.commit()
        out["stacked"] += 1
    if out["stacked"] and getattr(cfg, "photo_stacks", False):
        stacks.build(conn)

    conn.commit()
    if refresh_person is not None:
        for person_id in sorted(touched):
            try:
                refresh_person(person_id)
            except Exception:                             # noqa: BLE001
                log.info("could not refresh person %s", person_id, exc_info=True)
    out["people"] = len(touched)
    return out


def _name_faces(conn, row: dict[str, Any], entry: Entry, contacts: dict[str, str],
                people: dict[str, int], touched: set[int], out: dict[str, int]) -> None:
    from ..storage import db                              # noqa: PLC0415

    asset_id = int(row["id"])
    hints = [(box, contacts[c]) for box, c in entry.faces
             if c in contacts and not _done(conn, asset_id, "face", contacts[c].casefold())]
    if not hints:
        return
    faces = db.faces_for_asset(conn, asset_id)
    if not faces:
        # Not looked at for faces yet (or none found): the names wait.
        out["faces_waiting"] += len(hints)
        return
    stored = _stored_size(row)
    if stored is None:
        out["unreadable"] += 1
        return
    size, exif_turn = stored
    expected = (exif_turn + int(row.get("rotation") or 0)) % 360
    placed = place_faces(hints, faces, size, expected)
    rejected = {(int(r["face_id"]), int(r["person_id"])) for r in conn.execute(
        "SELECT face_id, person_id FROM face_rejections WHERE face_id IN (%s)"
        % ",".join("?" * len(faces)), [int(f["id"]) for f in faces])}
    named_here = {int(f["person_id"]) for f in faces if f.get("person_id")}
    writes = []
    for k, j, overlap in placed:
        name = hints[k][1]
        face = faces[j]
        key = name.casefold()
        if key not in people:
            people[key] = int(db.create_or_update_person_cluster(conn, name)["id"])
        person_id = people[key]
        _mark(conn, asset_id, "face", key)
        if face.get("source") == "confirmed" or (int(face["id"]), person_id) in rejected:
            continue                    # a person here already said who it is
        if face.get("person_id") == person_id or person_id in named_here:
            continue
        sure = overlap >= db.CARRY_IOU
        writes.append((int(face["id"]), person_id, "confirmed" if sure else "auto",
                       1.0 if sure else round(overlap, 3)))
        named_here.add(person_id)
        touched.add(person_id)
    if writes:
        db.set_faces_person(conn, writes)
        out["faces"] += len(writes)
    out["faces_unplaced"] += len(hints) - len(placed)
    conn.commit()


# ---------------------------------------------------------------------------
# The console's run
# ---------------------------------------------------------------------------

class Rescue:
    """One look, or one bring-in, at a time, in the background."""

    def __init__(self, state_dir: str | os.PathLike[str]):
        self._path = Path(state_dir) / "picasa.json"
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = {"phase": "idle"}
        try:
            saved = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                self._state = {**saved, "phase": saved.get("phase") if saved.get("phase") in (
                    "looked", "brought", "failed") else "idle"}
        except (OSError, ValueError):
            pass

    def state(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._state))

    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, mode: str, work: Callable[["Rescue"], dict[str, Any]]) -> bool:
        with self._lock:
            if self.busy():
                return False
            self._stop.clear()
            self._state = {"phase": "looking" if mode == "look" else "bringing",
                           "done": 0, "total": 0, "started": time.time(),
                           "found": self._state.get("found"), "brought": self._state.get("brought")}
            self._thread = threading.Thread(target=self._run, args=(mode, work),
                                            name="ninaivu-picasa", daemon=True)
            self._thread.start()
            return True

    def stop(self) -> None:
        self._stop.set()

    def stopping(self) -> bool:
        return self._stop.is_set()

    def progress(self, done: int, total: int) -> None:
        with self._lock:
            self._state.update(done=done, total=total)

    def _run(self, mode: str, work: Callable[["Rescue"], dict[str, Any]]) -> None:
        try:
            result = work(self)
            with self._lock:
                self._state.update(result)
                self._state["phase"] = "looked" if mode == "look" else "brought"
                self._state["stopped"] = self._stop.is_set()
                self._state["at"] = time.time()
        except Exception as exc:                          # noqa: BLE001
            log.exception("Picasa run failed")
            with self._lock:
                self._state.update(phase="failed", error=str(exc)[:300], at=time.time())
        finally:
            from ..storage import db                      # noqa: PLC0415
            db.end_open_transactions(failed=True)
            try:
                self._path.write_text(json.dumps(self.state()), encoding="utf-8")
            except OSError:
                log.info("could not keep the Picasa result", exc_info=True)
