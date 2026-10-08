"""Incremental, parallel library scanner.

Design notes
------------
* A scan is *incremental*: files whose ``(mtime, size)`` signature is unchanged
  are skipped entirely, so a rescan of a 100k-item library costs one
  ``os.scandir`` walk.
* Derivative work (decode, thumbnail, hash) runs on a thread pool; Pillow and
  ffmpeg both release the GIL, so this scales with cores.
* Progress is published on a lock-free snapshot dict the API polls, and the
  worker checks a stop event between items so switching library roots is
  instant.
* The AI pass runs *after* indexing, so the gallery is usable within seconds
  while tagging catches up in the background.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import sqlite3
import tempfile
import threading
import time
import unicodedata
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

import numpy as np
from PIL import Image

from . import audio_art, faces as faces_mod, media, screens, upright
from ..archive import dates as capture_dates
from ..storage import db
from ..server.auth import VIS_FAMILY, VIS_HIDDEN
from ..server.config import Config, media_kind

log = logging.getLogger(__name__)

AI_VERSION = 2  # bump to force re-tagging
#: Bump to re-sample videos. Separate from :data:`AI_VERSION` because the
#: sampling — how many moments, and where in the clip — changes independently
#: of the model doing the describing.
KEYFRAME_VERSION = 1

# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------

#: Phases whose progress is the count of items finished (``tagged`` out of
#: ``tag_total``) rather than the count of files walked.
ITEM_PHASES = frozenset({"tagging", "videos", "naming", "reading", "faces", "covers"})


@dataclass
class ScanProgress:
    root: str = ""
    status: str = "idle"  # idle | walking | indexing | tagging | done | error
    total: int = 0
    processed: int = 0
    added: int = 0
    updated: int = 0
    removed: int = 0
    errors: int = 0
    tagged: int = 0
    tag_total: int = 0
    #: When the pass now running set its own count. The rate has to be measured
    #: from here rather than from the start of the scan: by the time a scan
    #: reaches the video pass it may have been walking and tagging for an hour,
    #: and dividing that hour by the videos described so far would put the
    #: finish a day out.
    phase_started_at: float = 0.0
    message: str = ""
    #: The folder the walk is in *right now*, relative to the library root and
    #: shortened for a status line. Empty whenever nothing is walking — a scan
    #: that has stopped is not "in" anywhere, and leaving the last folder on
    #: screen would make a finished scan read as one frozen mid-walk.
    folder: str = ""
    #: Set when a scan finished but deliberately did not do something.
    warning: str = ""
    #: Why the scan is waiting for the household right now, or "". See
    #: ninaivu/server/workload.py.
    held: str = ""
    started_at: float = 0.0
    ended_at: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            data = {
                k: v for k, v in self.__dict__.items() if not k.startswith("_")
            }
        done = data["status"] in {"idle", "done", "error"}
        total = data["total"] or 1
        data["percent"] = 100 if done else min(99, int(data["processed"] * 100 / total))
        # The passes after indexing measure themselves in items, not in files
        # walked: by then `processed` has already reached `total` and a bar
        # drawn from it sits at 99% for however long tagging, naming, reading
        # or the face pass takes — which on a first scan is most of the wait,
        # and looks exactly like a scan that has hung.
        if not done and data["status"] in ITEM_PHASES and data["tag_total"]:
            data["percent"] = min(
                99, int(data["tagged"] * 100 / data["tag_total"]))
        data["running"] = not done
        data["elapsed"] = round(
            (data["ended_at"] or time.time()) - data["started_at"], 1
        ) if data["started_at"] else 0.0
        data["eta"] = self._eta(data, done)
        return data

    #: Items a pass must have finished before it is allowed to say how long it
    #: has left. The first few are unrepresentative — a model still loading, a
    #: drive still spinning up — and a wrong answer early is worse than none,
    #: because the household believes the first number they are shown.
    ETA_AFTER = 25

    @staticmethod
    def _eta(data: dict[str, Any], done: bool) -> float | None:
        """Seconds until this pass finishes, or None when it cannot be said.

        A count that ticks tells a working scan from a hung one, but it does
        not answer the question people actually have. "Describing 1,452 /
        17,685" is fourteen hours, and the household is entitled to know that
        before they decide whether to leave the machine on.
        """
        if done or data["status"] not in ITEM_PHASES:
            return None
        tagged, total = data["tagged"], data["tag_total"]
        if tagged < ScanProgress.ETA_AFTER or not total or tagged >= total:
            return None
        started = data.get("phase_started_at") or 0.0
        if not started:
            return None
        spent = time.time() - started
        if spent <= 0:
            return None
        return round(spent / tagged * (total - tagged), 1)

    def update(self, **fields: Any) -> None:
        with self._lock:
            # A pass's clock starts when the scan moves on to it — when the
            # phase changes — and not whenever a total arrives. The face pass
            # sends its total with every progress report, every ten
            # photographs, so a clock restarted on each one measured the last
            # ten only and divided that by every photograph done so far. On
            # 141,148 faces still to find it said three minutes, then five,
            # then two, for a pass that had about seventeen hours to run.
            new_status = fields.get("status")
            if new_status is not None and new_status != self.status:
                self.phase_started_at = time.time()
            for key, value in fields.items():
                setattr(self, key, value)

    def bump(self, **fields: int) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(self, key, getattr(self, key) + value)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

#: How many trailing folder names a status line can carry before it stops
#: being a status line. The end of a path is always the informative part.
FOLDER_LINE_PARTS = 3


def took(started: float) -> str:
    """How long something has been going, for a log line."""
    seconds = max(0.0, time.time() - started)
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.2f}h"


def short_folder(path: str | Path, root: str | Path) -> str:
    """Where the scan is, phrased for one line under a progress bar.

    Relative to the library root, because "2019/07/Cornwall" is what somebody
    recognises and ``D:\\Photos\\2019\\07\\Cornwall`` is what they have to read
    past. A path fifteen levels down keeps its tail; anything that cannot be
    made relative — a root that moved, a symlink out of the tree — falls back
    to the same tail treatment rather than throwing or going blank, because a
    status line that disappears is worse than one that is slightly odd.
    """
    text = str(path).replace("\\", "/").rstrip("/")
    base = str(root).replace("\\", "/").rstrip("/")

    if text == base:
        return "the top folder"
    if base and text.startswith(base + "/"):
        text = text[len(base) + 1:]

    parts = [p for p in text.split("/") if p and p != ".."]
    if not parts:
        return "the top folder"
    if len(parts) <= FOLDER_LINE_PARTS:
        return "/".join(parts)
    return "…/" + "/".join(parts[-FOLDER_LINE_PARTS:])


def date_from_path(rel_path: str) -> tuple[float | None, str]:
    """A capture date named by a dated filename or ``YYYY/MM/DD`` folders.

    The filename is the more specific of the two, so it is asked first. A
    folder that names only a month or a year gives its first day.
    """
    when = capture_dates.filename_date(rel_path)
    if when is None:
        period = capture_dates.folder_period(rel_path.replace("\\", "/"))
        when = period[0] if period else None
    if when is None:
        return None, ""
    return capture_dates.to_timestamp(when), when.strftime("%Y-%m-%d")


def takeout_date(abs_path: Path) -> float | None:
    """A capture time from a Google Takeout sidecar, or None.

    Google Photos strips EXIF from a lot of what it exports and puts the
    real capture time in a companion ``<name>.json`` instead — without this,
    a whole Takeout import lands under the day it was downloaded rather than
    the years the photos were actually taken.
    """
    when = capture_dates.takeout_date(abs_path)
    return capture_dates.to_timestamp(when) if when else None


#: Windows marks a file hidden with an attribute, not a leading dot. Both
#: conventions have to count, because a folder copied from a Mac onto an NTFS
#: drive carries the dot and a folder hidden in Explorer carries the bit.
_FILE_ATTRIBUTE_HIDDEN = 0x2
_FILE_ATTRIBUTE_SYSTEM = 0x4


def _stat_quiet(entry: Any) -> os.stat_result | None:
    """``entry.stat()`` that returns None rather than raising on a bad entry."""
    try:
        return entry.stat(follow_symlinks=False)
    except OSError:
        return None


@lru_cache(maxsize=8192)
def _dir_is_hidden(path: str) -> bool:
    """Is this directory hidden? Memoised — a library asks about the same
    folder once per file in it, and on Windows the answer costs a stat."""
    drive, rest = os.path.splitdrive(path)
    if rest in ("\\", "/", ""):
        return False
    name = os.path.basename(path.rstrip(os.sep))
    if not name:
        return False
    if name.startswith("."):
        return True
    try:
        return is_hidden(name, os.stat(path))
    except OSError:
        return False


def path_is_hidden(root: Path | str, rel_path: str,
                   st: os.stat_result | None = None) -> bool:
    """Whether a file, or any folder on the way down to it, is hidden.

    A photograph inside ``.private/`` is hidden whatever its own name says, so
    the whole chain has to be checked — and on Windows "hidden" is an attribute
    on each directory, not something the relative path can reveal.
    """
    if is_hidden(os.path.basename(rel_path), st):
        return True
    parts = rel_path.replace("\\", "/").split("/")[:-1]
    current = str(root)
    for part in parts:
        if part.startswith("."):
            return True
        current = os.path.join(current, part)
        if _dir_is_hidden(current):
            return True
    return False


def is_hidden(name: str, st: os.stat_result | None = None) -> bool:
    """Whether this entry was deliberately put out of sight.

    Dot-prefixed on every platform, plus the Windows hidden and system
    attributes when the stat result carries them. ``os.stat_result`` only has
    ``st_file_attributes`` on Windows, so the getattr is doing real work.
    """
    if name.startswith("."):
        return True
    attributes = getattr(st, "st_file_attributes", 0) if st is not None else 0
    return bool(attributes & (_FILE_ATTRIBUTE_HIDDEN | _FILE_ATTRIBUTE_SYSTEM))


class WalkReport:
    """Whether a walk actually saw the whole tree.

    A walk that swallows errors and a walk that found nothing look identical
    from the outside, and the difference decides whether it is safe to delete
    rows for the files that were not seen. Recording it is what stops an
    unreadable folder — or an unplugged drive whose mountpoint survives — from
    being read as "these photos are gone".
    """

    __slots__ = ("complete", "unreadable", "root_readable")

    def __init__(self) -> None:
        self.complete = True
        self.unreadable: list[str] = []
        self.root_readable = True

    def failed(self, path: Path | str, exc: BaseException) -> None:
        self.complete = False
        if len(self.unreadable) < 50:        # enough to diagnose, not a flood
            self.unreadable.append(f"{path}: {exc}")

    def __bool__(self) -> bool:
        return self.complete


class FileSignature:
    """The four stat fields indexing reads, and nothing else.

    A first scan holds one of these per new file until indexing reaches it. A
    full ``os.stat_result`` is a few hundred bytes; at a million files the list
    of them alone was most of a gigabyte. Attribute names match ``stat_result``
    so everything that reads a stat reads this unchanged.
    """

    __slots__ = ("st_size", "st_mtime", "st_ctime", "st_birthtime")

    def __init__(self, st: Any) -> None:
        self.st_size = st.st_size
        self.st_mtime = st.st_mtime
        self.st_ctime = getattr(st, "st_ctime", None)
        self.st_birthtime = getattr(st, "st_birthtime", None)


def walk_media(root: Path, cfg: Config,
               report: "WalkReport | None" = None,
               ) -> Iterator[tuple[str, os.stat_result]]:
    """Yield ``(rel_path, stat)`` for every supported media file under *root*.

    Pass a :class:`WalkReport` to learn whether anything was skipped. Without
    one the behaviour is unchanged, so existing callers that only want the
    files are unaffected.

    Hidden files and folders are yielded like any other when
    ``cfg.index_hidden`` is on; it is :func:`build_record` that turns that into
    a visibility. The names in ``cfg.ignore_dirs`` are skipped regardless —
    those are caches and recycle bins, not somebody's photographs.
    """
    pending_root = (Path(cfg.state_dir) / "pending-uploads").resolve()
    private_roots = (pending_root, cfg.thumbs_dir.resolve())
    here = root.resolve()
    if any(here.is_relative_to(private) for private in private_roots):
        return
    # Resolving a folder so it can be compared with those two costs a system
    # call, and the walk would pay it for every folder in the library. It can
    # only ever match when one of them is *inside* this library — or when a link
    # in the library could lead into one — so when neither is true the check is
    # skipped rather than answered.
    guarded = tuple(private for private in private_roots
                    if cfg.follow_symlinks or private.is_relative_to(here))
    stack = [(root, False)]
    first = True
    while stack:
        current, inherited_hidden = stack.pop()
        try:
            entries = list(os.scandir(current))
        except (PermissionError, OSError) as exc:
            if report is not None:
                report.failed(current, exc)
                if first:
                    # The root itself could not be listed. Everything below it
                    # is unknown, not absent.
                    report.root_readable = False
            first = False
            continue
        first = False
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=cfg.follow_symlinks):
                    # Pending uploads are private, even when state lives below
                    # a library root and hidden-file indexing is enabled.
                    if guarded:
                        resolved = Path(entry.path).resolve()
                        if any(resolved.is_relative_to(private)
                               for private in guarded):
                            continue
                    # Caches, recycle bins and version-control directories are
                    # never anybody's media, hidden or not.
                    if entry.name in cfg.ignore_dirs:
                        continue
                    hidden_dir = is_hidden(entry.name, _stat_quiet(entry))
                    if hidden_dir and not cfg.index_hidden:
                        continue
                    stack.append((Path(entry.path), inherited_hidden or hidden_dir))
                elif entry.is_file(follow_symlinks=cfg.follow_symlinks):
                    # entry.path, not entry.name: the ambiguous extensions are
                    # settled by reading the first bytes, which needs a real path.
                    if media_kind(entry.path) == "unknown":
                        continue
                    st = entry.stat()
                    if (not cfg.index_hidden
                            and (inherited_hidden or is_hidden(entry.name, st))):
                        continue
                    # Icons, sprites, signatures and other software's leftover
                    # thumbnails are all technically images. None of them are
                    # anybody's photographs, and letting them in pads the
                    # library with noise nobody wants to scroll past.
                    if st.st_size < cfg.min_media_bytes:
                        continue
                    rel = os.path.relpath(entry.path, root).replace("\\", "/")
                    yield rel, st
            except (PermissionError, OSError) as exc:
                if report is not None:
                    report.failed(entry.path, exc)
                continue


# ---------------------------------------------------------------------------
# Per-file processing
# ---------------------------------------------------------------------------

#: Lowercased clip name -> the name as the folder actually spells it.
#:
#: Cached per folder because a live photo asks about its neighbours once per
#: still, and a holiday folder holds hundreds of them; one listing answers for
#: all of them. Small on purpose: a scan works through a library folder by
#: folder, so a handful of entries covers the whole walk, and a listing that
#: goes stale mid-scan is picked up by the watcher like any other change.
@lru_cache(maxsize=64)
def _clips_beside(folder: str) -> dict[str, str]:
    found: dict[str, str] = {}
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                name = entry.name
                if name[-4:].lower() in (".mov", ".mp4") and entry.is_file():
                    found[name.lower()] = name
    except OSError:
        pass
    return found


def _rule_reaches_inside(rule_folder: str, folder: str, root: Path | str) -> bool:
    """Was this rule set at or below the hidden boundary it is overruling?

    The boundary is the highest hidden folder on the way down. A rule set on or
    inside it is an admin deliberately opening that folder up; a rule set above
    it is not.
    """
    parts = folder.replace("\\", "/").split("/") if folder else []
    current = str(root)
    boundary = None
    for index, part in enumerate(parts):
        current = os.path.join(current, part)
        if part.startswith(".") or _dir_is_hidden(current):
            boundary = "/".join(parts[: index + 1])
            break
    if boundary is None:
        return True                       # only the file itself is hidden
    return rule_folder == boundary or rule_folder.startswith(f"{boundary}/")


def build_record(root: Path, rel_path: str, st: os.stat_result, cfg: Config,
                 rules: list[dict[str, Any]] | None = None,
                 engine: Any | None = None,
                 manual_turn: int | None = None) -> dict[str, Any]:
    """Probe one file and produce its database record (plus derivatives).

    *manual_turn* is a turn somebody set by hand for this file. The index
    keeps it over a rescan (``db.upsert_asset``); it is applied here as well,
    in place of what the scan would decide, so the thumbnails and the
    width and height written now agree with it instead of quietly undoing it.
    """
    abs_path = root / rel_path
    kind = media_kind(abs_path)
    name = os.path.basename(rel_path)
    folder = os.path.dirname(rel_path)

    record: dict[str, Any] = {
        "root": str(root),
        "rel_path": rel_path,
        "filename": name,
        "folder": folder,
        "ext": os.path.splitext(name)[1].lower().lstrip("."),
        "kind": kind,
        "size": st.st_size,
        "mtime": st.st_mtime,
        "orientation": 1,
        "tags": [],
        "ai_version": 0,
        "indexed_at": time.time(),
    }

    # A folder the admin already marked public or hidden passes that decision
    # on to anything new that lands inside it. Failing that, something the
    # household deliberately hid on disk stays hidden here: an explicit rule an
    # admin set in the console outranks the filesystem, but nothing else does,
    # so a private folder cannot become visible merely by being scanned.
    found = db.folder_rule_for(rules or [], folder)
    hidden = cfg.index_hidden and path_is_hidden(root, rel_path, st)

    if found is not None and (not hidden or _rule_reaches_inside(found[1], folder, root)):
        # An admin's rule wins — but only one they set on the hidden folder
        # itself or below it. A rule set on an ordinary parent long ago was not
        # a decision about the private folder that appeared inside it later,
        # and treating it as one would publish exactly what was hidden.
        record["visibility"] = found[0]
        record["vis_source"] = "folder"
    elif hidden:
        record["visibility"] = VIS_HIDDEN
        record["vis_source"] = "hidden"
    else:
        record["visibility"] = VIS_FAMILY
        record["vis_source"] = "default"

    # Audio is administrator-only, and no folder rule lifts that. A media
    # library swept up from old drives and phone backups is full of voice
    # notes, voicemail and recordings nobody chose to put in a gallery; a
    # photograph is looked at on purpose, a sound file plays the moment it is
    # tapped, in a room with other people in it. The read path enforces this
    # regardless of what is stored — writing it here as well means the console
    # shows the truth rather than a level that is silently overridden.
    if record.get("kind") in db.ADMIN_ONLY_KINDS:
        record["visibility"] = VIS_HIDDEN
        record["vis_source"] = "kind"

    # A screenshot is hidden from the moment it is indexed, by its name. What
    # the picture looks like is judged later, once the search model has seen
    # it (Scanner._judge_screens). Only new rows take this: a rescan keeps
    # whatever level an item already has.
    if (getattr(cfg, "hide_screens", False) and kind == "picture"
            and record["vis_source"] in screens.REPLACEABLE_SOURCES
            and record["visibility"] < VIS_HIDDEN
            and screens.named_like_screenshot(rel_path)):
        record["visibility"] = VIS_HIDDEN
        record["vis_source"] = screens.SOURCE

    base = media.thumb_base(str(root), rel_path)
    source = None

    try:
        if kind == "picture":
            # Decoded no larger than the largest thing made from it: the
            # thumbnails, or the working copy orientation detection shrinks to.
            source, full_size = media.open_for_index(
                abs_path, max(max(cfg.thumb_sizes), upright.WORK_SIZE))
            # The EXIF was read by open_for_index from the file as it is —
            # before the picture was stood upright, which removes the tag —
            # so the file is not opened a second time for it (for a RAW, a
            # second extraction of its preview).
            exif_info: dict[str, Any] = source.info.get(media.INDEX_EXIF) or {}
            record.update(exif_info)

            # Which way up does this go?
            #
            # exif_transpose above has already honoured the camera's tag, so
            # this only ever has something to say about a file that carried
            # none — a scanned print, a photograph a messaging app stripped.
            # The turn is baked into the thumbnails below and recorded so the
            # browser can show the untouched original the same way up.
            #
            # The tag is read from what the file actually held, not from
            # `record`: the record carries a *default* orientation of 1, which
            # is indistinguishable from a camera saying "this one is upright" —
            # and treating that default as a real tag switched detection off
            # for every photograph with no EXIF at all, which is exactly the
            # set this exists for.
            if manual_turn is not None:
                if manual_turn % 360:
                    source = upright.apply(source, manual_turn % 360)
                record["rotation"] = manual_turn % 360
                record["rot_source"] = "manual"
            verdict = None if manual_turn is not None else upright.decide(
                source,
                exif_orientation=exif_info.get("orientation"),
                # CLIP is only consulted when the faces found nothing, and only
                # when asked for: scoring four rotations of every landscape in
                # a fifty-thousand-photograph library is minutes of GPU time
                # spent on pictures it has little to say about.
                engine=engine if cfg.orientation_ai else None,
                enabled=cfg.detect_orientation,
            )
            if verdict is not None and verdict.source in ("model", "faces", "ai") \
                    and verdict.turns:
                source = upright.apply(source, verdict.rotation)
                record["rotation"] = verdict.rotation
                record["rot_source"] = verdict.source
            # The photograph's size, not the decoded image's: that can be a
            # quarter of it, and the low-resolution check and the viewer's
            # layout both read these.
            if record.get("rotation", 0) % 180 == 90:
                full_size = full_size[::-1]
            record["width"], record["height"] = full_size

            # Live Photo & Motion Photo detection
            #
            # `is_live` is only ever set when there's an actual playable
            # companion clip: an iPhone-style pairing (a same-stem
            # .mov/.mp4 beside this file) is the only case that qualifies
            # today. A Google Motion Photo muxes its video *inside* the
            # JPEG/HEIC itself -- `media.detect_motion_photo()` can tell one
            # is there, but nothing extracts it, so there is no companion to
            # serve. Setting `is_live` for that case used to show a LIVE
            # badge that failed silently on tap (`/api/live-video/<id>` 404s
            # with no `live_video_path`). Not setting it means these simply
            # don't show as live yet, which is honest. `media.detect_motion_
            # photo()` is still there and still cheap (a header/tail byte
            # scan), so a future session that adds real embedded-clip
            # extraction only has to call it here and wire the result back
            # into `is_live`/`live_video_path`.
            # Matched against what is actually in the folder rather than by
            # guessing at the spelling. Trying ``.mov`` and then ``.MOV``
            # against Path.exists() answers yes to either on Windows, so what
            # got written down was the guess rather than the file — and on the
            # library this was found on, all 5,869 of them were wrong. NTFS
            # does not care; a case-sensitive filesystem would 404 every one.
            stem = abs_path.stem.lower()
            siblings = _clips_beside(str(abs_path.parent))
            for vext in (".mov", ".mp4"):
                real = siblings.get(f"{stem}{vext}")
                if real:
                    candidate = abs_path.parent / real
                    rel_video = os.path.relpath(candidate, root).replace("\\", "/")
                    record["is_live"] = 1
                    record["live_video_path"] = rel_video
                    break
        elif kind == "video":
            record.update(media.probe_video(abs_path))
            if cfg.video_thumbs:
                source = media.extract_video_frame(abs_path, cfg.video_thumb_offset)
                if source and not record.get("width"):
                    record["width"], record["height"] = source.size
        elif kind == "audio":
            info = media.probe_audio(abs_path)
            record["duration"] = info.get("duration")
            if info.get("caption"):
                record["caption"] = info["caption"]
            if info.get("tags"):
                record["tags"] = info["tags"]
            # Its cover, or a tile that says what it is: a blank square in a
            # grid of pictures reads as something broken.
            source = audio_art.picture_for(abs_path, rel_path,
                                           duration=record["duration"])
    except Exception as exc:  # noqa: BLE001 - one bad file must not stop a scan
        record["error"] = str(exc)

    if source is not None:
        try:
            # The placeholder and the grid colour are averages of the whole
            # picture; the smallest thumbnail gives the same answer at a
            # thousandth of the pixels. The duplicate fingerprint and the
            # focus measure stay on the decoded picture: both are compared
            # against values already in the index.
            smallest = media.write_thumbnails(
                source, cfg.thumbs_dir, base, cfg.thumb_sizes,
                cfg.thumb_format, cfg.thumb_quality, return_smallest=True,
            )
            record["thumb"] = base
            record["blurhash"] = media.blurhash_encode(smallest)
            record["color"] = media.dominant_color(smallest)
            grey = None
            # Not for sound files: their tiles are drawn, and an album's tracks
            # look alike by design — they would be offered as duplicates.
            if cfg.perceptual_hash and kind != "audio":
                grey = source.convert("L")
                record["phash"] = media.perceptual_hash(grey)
            if cfg.quality_scan and kind == "picture":
                stats = media.quality_stats(grey if grey is not None else source)
                record["sharpness"] = stats.get("sharpness")
                record["brightness"] = stats.get("brightness")
                record["quality"] = media.quality_flags(
                    stats, record.get("width"), record.get("height"),
                    blur_below=cfg.blur_threshold,
                    dark_below=cfg.dark_threshold,
                    bright_above=cfg.bright_threshold,
                    clip_above=cfg.highlight_clip_threshold,
                    min_pixels=cfg.low_res_pixels,
                )
                record["quality_version"] = media.QUALITY_VERSION
        except Exception as exc:  # noqa: BLE001
            record["error"] = str(exc)
        finally:
            try:
                source.close()
            except Exception as exc:
                # Not fatal, but not nothing: a library that quietly fails on a
                # thousand photographs looks exactly like one that read them.
                log.debug("%s: %s", __name__, exc)

    record.update(capture_date_fields(abs_path, kind, st, record.get("captured_at")))
    # A Google Takeout sidecar beside the file: the export strips the location
    # from a lot of what it holds and keeps it only there, along with the
    # description typed under the photograph. The file's own EXIF wins when it
    # has either; the sidecar only fills what is missing.
    if kind in ("picture", "video") and (record.get("gps_lat") is None or not record.get("caption")):
        try:
            from ..archive import takeout                # noqa: PLC0415
            extra = takeout.extras(abs_path)
        except Exception:                                # noqa: BLE001 — never the file's fault
            extra = {}
        if extra:
            if record.get("gps_lat") is None and "lat" in extra:
                record["gps_lat"], record["gps_lon"] = extra["lat"], extra["lon"]
            if not record.get("caption") and extra.get("description"):
                record["caption"] = extra["description"]
    return record


def capture_date_fields(abs_path: Path, kind: str, st: Any,
                        read: float | None) -> dict[str, Any]:
    """``captured_at``, ``date_key`` and ``date_source`` for one file.

    The same chain the Archive tab files by (``ninaivu.archive.dates``), so a
    photograph is shown under the day its archive folder says: EXIF >
    recording date inside the video > Google Takeout sidecar > date in the
    filename > dated folder weighed against the file's timestamp > the
    timestamp > undated.

    ``read`` is what probing the file already found: EXIF for a picture, or
    ffprobe's reading for a video, which stands in for containers the shared
    parser cannot read (MKV, WMV, MTS).

    Not ``st.st_mtime`` alone. A file that was edited, re-saved, copied off a
    card or restored from a backup has a modification time that says when that
    happened, not when the photograph was taken — measured across one library,
    the ones that carry both are a median of ten years apart.
    """
    if kind == "picture" and read:
        when = capture_dates.from_timestamp(read)
        return {"captured_at": read, "date_source": "exif",
                "date_key": when.strftime("%Y-%m-%d")}

    probed = capture_dates.from_timestamp(read) if read else None
    when, found = capture_dates.fallback_date(abs_path, st=st, container=probed)
    if when is None:
        # No believable date at all. Undated is honest; the gallery groups these
        # under "Undated" rather than inventing a day from a dead clock.
        return {"captured_at": None, "date_source": "none", "date_key": ""}
    if found == "filesystem":
        # The raw timestamp, not a round trip through local time, which is
        # ambiguous for the hour clocks go back.
        captured = capture_dates.file_timestamp(st)
        return {"captured_at": captured,
                "date_source": "created" if captured < st.st_mtime else "mtime",
                "date_key": when.strftime("%Y-%m-%d")}
    return {"captured_at": capture_dates.to_timestamp(when),
            "date_source": "path" if found == "folder" else found,
            "date_key": when.strftime("%Y-%m-%d")}


# ---------------------------------------------------------------------------
# Near-duplicate grouping
# ---------------------------------------------------------------------------

#: Bits in a perceptual hash (:func:`media.perceptual_hash` returns 16 hex
#: characters).
PHASH_BITS = 64

#: Roughly how many hashes a candidate bucket should hold. Comparing a bucket
#: is quadratic in its size, so this — not the number of buckets — is what
#: decides whether grouping a large library costs seconds or minutes.
DUP_BUCKET_TARGET = 32

try:
    _popcount = int.bit_count                            # Python 3.10+
except AttributeError:                                   # pragma: no cover
    def _popcount(value: int) -> int:
        return bin(value).count("1")


def dup_bands(threshold: int, count: int) -> list[tuple[int, int]]:
    """``(shift, mask)`` for each slice of the hash a bucket key is cut from.

    Two hashes within *threshold* bits of each other differ in at most
    *threshold* places, so if the hash is cut into ``threshold + 1``
    non-overlapping bands then at least one band must be **identical** — which
    makes bucketing by each band in turn a candidate search that cannot miss a
    pair. Bucketing by a single prefix, as this used to, misses most of them:
    at the default distance of six bits, roughly seven pairs in ten differ
    somewhere in the prefix and so were never compared at all.

    The guarantee needs narrow bands, and narrow bands mean few distinct keys,
    so on a large library every bucket would end up holding thousands of hashes
    to compare pairwise. Rather than let that happen the bands are widened until
    a bucket is expected to hold about :data:`DUP_BUCKET_TARGET` hashes, and
    however many of them still fit are used. A big library therefore keeps the
    guarantee for close duplicates and loses it gradually for distant ones —
    and is still both faster and more thorough than one prefix was.

    The bands need not cover all 64 bits. A bit in none of them is simply a bit
    no bucket key depends on, which can only put *more* pairs together.
    """
    wanted = max(1, int(threshold) + 1)
    width = PHASH_BITS // wanted
    # Enough bits that `count` hashes spread into buckets of about the target
    # size, i.e. 2**width >= count / target.
    spread = max(1, (max(0, count) // DUP_BUCKET_TARGET).bit_length())
    width = min(PHASH_BITS, max(width, spread))
    mask = (1 << width) - 1
    return [(index * width, mask)
            for index in range(min(wanted, PHASH_BITS // width))]


class _Merged:
    """Disjoint sets of hashes, so a chain of near-duplicates is one group.

    A burst of shots links up a pair at a time — the first to the second, the
    second to the third — and the group they belong to is only right if every
    one of those findings is folded into the same set. Pairing them off without
    this left photographs that belong together in two half-groups, and which
    half a photograph landed in depended on the order the pairs were found.

    A set is named by its smallest member, so the group key a scan writes does
    not depend on the order the library was read in, and a rescan that finds the
    same duplicates has nothing to write.
    """

    __slots__ = ("parent",)

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def root(self, item: str) -> str:
        parent = self.parent
        found = item
        while parent.setdefault(found, found) != found:
            found = parent[found]
        while parent[item] != found:                     # flatten the path
            parent[item], item = found, parent[item]
        return found

    def merge(self, one: str, other: str) -> None:
        first, second = self.root(one), self.root(other)
        if first == second:
            return
        if second < first:
            first, second = second, first
        self.parent[second] = first


def group_duplicates(rows: Iterable[tuple[int, str]],
                     threshold: int) -> dict[int, str]:
    """``asset id -> group key`` for every picture that has a near-twin.

    Takes ``(id, phash)`` pairs. Identical hashes are collapsed before any
    comparing happens: a library swept up from old drives and phone backups
    holds the same file many times over, and those copies are the one case that
    needs no distance at all. It also keeps the quadratic part of the work
    proportional to how many *different* pictures there are rather than to how
    many files.
    """
    by_hash: dict[str, list[int]] = {}
    for asset_id, phash in rows:
        if phash:
            by_hash.setdefault(phash, []).append(int(asset_id))
    if not by_hash:
        return {}

    values: dict[str, int] = {}
    for phash in by_hash:
        try:
            values[phash] = int(phash, 16)
        except (TypeError, ValueError):
            continue          # not a hash this scanner wrote; it stands alone

    merged = _Merged()
    threshold = max(0, int(threshold))
    if threshold and len(values) > 1:
        for shift, mask in dup_bands(threshold, len(values)):
            buckets: dict[int, list[str]] = {}
            for phash, value in values.items():
                buckets.setdefault((value >> shift) & mask, []).append(phash)
            for bucket in buckets.values():
                if len(bucket) < 2:
                    continue
                for index, one in enumerate(bucket):
                    first = values[one]
                    for other in bucket[index + 1:]:
                        # Compared as integers: parsing the hex once per
                        # picture rather than once per comparison is the
                        # difference between a few thousand conversions and a
                        # few million.
                        if _popcount(first ^ values[other]) <= threshold:
                            merged.merge(one, other)

    groups: dict[str, list[int]] = {}
    for phash, ids in by_hash.items():
        groups.setdefault(merged.root(phash), []).extend(ids)
    return {asset_id: key for key, ids in groups.items() if len(ids) > 1
            for asset_id in ids}


# ---------------------------------------------------------------------------
# Scanner service
# ---------------------------------------------------------------------------

#: How recent a file's modification time must be for a "modified" event to
#: count as a change to it.
WATCH_FRESH_SECONDS = 600.0

#: How many unreadable files one scan names in the log before only counting.
FAILED_FILES_NAMED = 50

#: How long starting a scan waits for the previous one to notice its stop.
STOP_WAIT_SECONDS = 10.0
#: A scan asked for while the running one is in these phases waits for it to
#: finish rather than stopping it: it is still reading files, and starting
#: over would throw that reading away.
HAND_OVER_AFTER = ("walking", "indexing")
#: How long a watcher runs before it is stopped, at least: long enough for a
#: native watcher's own thread to have finished starting (see _stop_watch).
WATCH_SETTLE_SECONDS = 0.25

#: Jobs that hold the indexer down while they read the whole library. Each
#: reads every original once, from the same disk the indexer walks, and on a
#: processor with no GPU each wants every core: run beside a scan, both take
#: twice as long. They only read, so an approval does not have to wait on
#: them the way it waits on a consolidation.
CLAIM_STRAIGHTEN = "straightening photographs"
CLAIM_STORAGE_CHECK = "checking the library's storage"
CLAIM_IMPORT = "an import is copying into the library"

#: When the place-name list was last fetched for, and how often to try again.
PLACES_TRIED = "places_fetch_tried"
PLACES_RETRY_SECONDS = 24 * 3600
READ_ONLY_CLAIMS = frozenset({CLAIM_STRAIGHTEN, CLAIM_STORAGE_CHECK})


def _worth_a_rescan(event) -> bool:  # noqa: ANN001
    """Whether a watcher event means the library changed, not that it was read.

    Windows reports a last-access update as "modified", and watchdog asks for
    those. Every photograph the cloud upload sent, every video the keyframe
    pass sampled, every file the storage check hashed came back as a change —
    and each one scheduled a full rescan, whose own reads scheduled the next:
    1,071 scans in one day on a library where nothing had changed. A real
    edit moves the modification time to now; a read leaves it where it was.
    """
    kind = getattr(event, "event_type", "")
    if kind in ("opened", "closed_no_write"):
        return False
    if kind != "modified":
        return True
    try:
        mtime = os.stat(getattr(event, "src_path", "")).st_mtime
    except OSError:
        return True                  # gone already: that is a change
    return mtime >= time.time() - WATCH_FRESH_SECONDS


class Scanner:
    """Owns the background scan thread and the optional filesystem watcher."""

    def __init__(self, cfg: Config, ai: Any | None = None) -> None:
        self.cfg = cfg
        self.ai = ai
        # Scanners are made on the main thread (build_services, the tests) and
        # do their face-based orientation work on pool threads; see there.
        if cfg.detect_orientation:
            upright.prime_on_main_thread()
        self.progress = ScanProgress()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        #: One watchdog observer per watched folder, keyed by that folder.
        self._observers: dict[str, Any] = {}
        #: A scan asked for while the last one had not yet let go; the last
        #: one starts it on its way out. See :meth:`start`.
        self._after_stop: tuple[list[Path], bool] | None = None
        #: The quiet-period timer per watched folder, so stopping the watcher
        #: can stop them too. Its own lock: :meth:`stop` cancels them while it
        #: holds ``_lock``.
        self._watch_timers: dict[str, threading.Timer] = {}
        self._timer_lock = threading.Lock()
        #: Held while a watcher is started or the watchers are stopped, so a
        #: stop never meets a watcher half started (see _stop_watch).
        self._watch_lock = threading.Lock()
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        #: Everything the indexer is standing down for, in the order asked.
        #: More than one job can hold the disk at once — a consolidation, a
        #: storage check, a straightening pass — and the indexer comes back
        #: only when the last of them lets go.
        self._claims: list[str] = []
        self._defer_pending: tuple[list[Path], bool] | None = None
        #: How many scans have been asked for and queued rather than started.
        #: A job holding the indexer for hours (the storage check) watches it
        #: to let a scan through for files that arrived meanwhile.
        self._queued_requests = 0
        #: Set once the image model has loaded, when start-up scans before it
        #: (Services.boot). None: there is no model to wait for.
        self.model_ready: threading.Event | None = None
        #: What the scan thread last started was asked to do — its folders and
        #: whether it was a full rescan — so a scan paused by :meth:`defer`
        #: comes back as the same scan, not as a quick one of everything.
        self._current: tuple[list[Path], bool] | None = None
        #: The last prune this scanner declined to carry out, if any. Kept so
        #: the console can say why the index still lists files that are not on
        #: the disk it just read.
        self.skipped_prune: db.PruneRefused | None = None
        #: The household's claim on the machine (ninaivu/server/workload.py),
        #: when this scanner belongs to a running Ninaivu.
        self.workload = None

    # -- yielding to the archive engine -----------------------------------
    #
    # Indexing the library and consolidating drives both walk the disk and hash
    # what they find. Run together on a spinning drive or a USB enclosure and
    # each halves the other's throughput while making both ETAs a lie. Archiving
    # wins, because it is moving real data with a verification step, whereas
    # indexing is cheap to resume: the walk skips anything whose size and mtime
    # already match, so a stopped scan restarts almost where it stopped.

    def defer(self, reason: str) -> bool:
        """Stand down until every holder has called :meth:`resume`.

        True if a running scan was stopped. While anything holds the indexer,
        :meth:`start` records what was asked for instead of doing it, so
        nothing is lost — a scan requested mid-consolidation simply happens
        when the consolidation ends.

        Claims are by *reason*, and asking twice for the same one is one
        claim. There used to be a single slot: a job that finished released
        the indexer even while another still had the disk, and approving an
        upload in the middle of a consolidation put the indexer back on the
        drive being written to.
        """
        with self._lock:
            if reason not in self._claims:
                self._claims.append(reason)
            was_running = self.running
            if was_running:
                # The scan being stopped, as it was asked for, merged with
                # anything already queued. It used to be replaced with a quick
                # scan of every library, so a full rescan paused for a
                # consolidation came back as an incremental one and never
                # re-read what it had set out to — and a scan queued behind the
                # running one, which stop() drops, was lost with it.
                roots, full = self._current or (
                    [Path(r) for r in self.cfg.libraries], False)
                self._defer_pending = self._merge_pending(roots, full)
                if self._after_stop is not None:
                    self._defer_pending = self._merge_pending(*self._after_stop)
        if was_running:
            reached = self.progress.snapshot()
            self.stop(join=False)
            self.progress.update(
                status="paused",
                message=f"Paused — {reason}. Indexing resumes when that finishes.",
            )
            self._notify({"phase": "paused"})
            # Logged on the way down and on the way back up, because the two
            # together are the only record of who had the disk. A consolidation
            # and an indexer that are both running halve each other's speed,
            # and from the outside that looks like one slow disk.
            log.info("indexing stood down — %s; it had reached %s of %s files "
                     "in the %s phase",
                     reason, f"{reached.get('processed', 0):,}",
                     f"{reached.get('total', 0):,}", reached.get("status"))
        else:
            log.info("indexing stood down — %s; nothing was running", reason)
        return was_running

    def resume(self, reason: str | None = None) -> bool:
        """Let go of one claim, or all of them. True if a scan was started.

        The queued scan starts only once nothing holds the indexer.
        """
        with self._lock:
            if reason is None:
                self._claims.clear()
            elif reason in self._claims:
                self._claims.remove(reason)
            if self._claims:
                log.debug("released by %s; still held by %s",
                          reason, "; ".join(self._claims))
                return False
            pending = self._defer_pending
            self._defer_pending = None
        if not pending or not pending[0]:
            log.debug("nothing was queued, so there is nothing to resume")
            return False
        log.info("indexing resumed — the disk is free again")
        # The folders that were asked for, not every library: a watcher event
        # queued for one drive used to come back as a walk of all of them.
        self.start(pending[0], full=pending[1])
        return True

    @contextmanager
    def held(self, reason: str):
        """Hold the indexer down for the length of a ``with`` block."""
        self.defer(reason)
        try:
            yield
        finally:
            self.resume(reason)

    @property
    def deferred(self) -> str | None:
        """Why the indexer is standing down, or ``None`` when it is free."""
        claims = list(self._claims)
        return "; ".join(claims) if claims else None

    @property
    def waiting(self) -> bool:
        """True when a scan is queued until the indexer is let go."""
        return self._defer_pending is not None

    @property
    def queued_requests(self) -> int:
        """How many scans have been queued rather than started, ever."""
        return self._queued_requests

    # -- lifecycle --------------------------------------------------------
    def start(self, root: str | Path | Iterable[str | Path] | None = None, *,
              full: bool = False) -> None:
        """Scan one folder, several, or every configured library folder if none
        is given. Every library folder is watched afterwards either way."""
        if root is None:
            roots = [Path(r) for r in self.cfg.libraries]
        elif isinstance(root, (str, Path)):
            roots = [Path(root).expanduser().resolve()]
        else:
            # Library folders as the configuration spells them, which is how
            # their rows are keyed and how Rescan passes them.
            roots = [Path(r) for r in root]
        if not roots:
            return
        with self._lock:
            # Checked under the same lock as the thread is started with, so a
            # defer() cannot land in between: it would find nothing running to
            # stop, and the scan started a moment later would run straight
            # through the consolidation that had asked for the disk.
            held_by = self.deferred
            if held_by:
                # Queue it rather than refuse it: the caller asked for a scan
                # and will get one — just not while the disk is busy elsewhere.
                self._defer_pending = self._merge_pending(roots, full)
                self._queued_requests += 1
            elif self.running:
                # A scan is under way. The new one is handed over to without
                # waiting here: this used to stop the running scan and join it
                # for up to ten seconds while holding this lock — which the
                # scan's own way out needs, so the wait always ran its full
                # length, in a web request, with every other start, defer and
                # resume queued behind it.
                queued = self._after_stop
                self._after_stop = (
                    (queued[0] + [r for r in roots if r not in queued[0]],
                     queued[1] or full) if queued else (list(roots), full))
                self._queued_requests += 1
                status = self.progress.snapshot().get("status")
                if status in HAND_OVER_AFTER and not self._stop.is_set():
                    # Still reading files: it finishes, and the one asked for
                    # follows straight after. Stopping it threw away a walk of
                    # the whole library each time — and a full scan restarted
                    # by every import or upload that finished could never end.
                    log.info("a scan was asked for while one is %s; it follows "
                             "when that one is done", status)
                else:
                    # Analysing, which can wait hours for its turn: the files
                    # just added are indexed now, and the analysis carries on
                    # from where it got to in the next scan.
                    self._stop.set()
                    log.info("a scan was asked for while the last one was %s; "
                             "it takes over as soon as that one lets go",
                             "stopping" if status not in HAND_OVER_AFTER else status)
                return
            else:
                self._stop.clear()
                self._current = (list(roots), full)
                self._thread = threading.Thread(
                    target=self._run_all, args=(roots, full), name="mv-scan",
                    daemon=True,
                )
                self._thread.start()
        if held_by:
            self.progress.update(
                status="paused", message=f"Queued — {held_by}.")
            log.info("a scan was asked for and queued rather than started — %s",
                     held_by)
            return
        self.watch(roots)

    def watch(self, roots: Iterable[str | Path] = ()) -> None:
        """Watch every library folder, and *roots*, for files arriving.

        Separate from scanning because the two used to be one: only a folder
        that had just been scanned got a watcher. Scanning one library stopped
        the watchers on the others, and a start-up that skipped its scan — the
        library had been walked moments before a restart — watched nothing at
        all, so photographs copied in afterwards waited for somebody to press
        Rescan.
        """
        # One watcher per folder, however it is spelled, and the library's own
        # spelling when there is one: a change is rescanned under the path the
        # watcher was given, and the index keys its rows by that path.
        wanted: dict[Path, Path] = {}
        for folder in [*self.cfg.libraries, *roots]:
            try:
                same = Path(folder).expanduser().resolve()
            except OSError:
                same = Path(folder)
            wanted.setdefault(same, Path(folder))
        for path in wanted.values():
            self._start_watch(path)

    def _run_all(self, roots: list[Path], full: bool) -> None:
        """Scan several library folders in turn, reporting as one job."""
        self._roots_done: list[Path] = []
        try:
            self._run_each(roots, full)
        finally:
            with self._lock:
                queued, self._after_stop = self._after_stop, None
                if queued:
                    # Handed over part-way: the folders this scan had not
                    # finished come along with the ones asked for, or they
                    # waited for somebody to press Rescan.
                    unfinished = [r for r in roots if r not in self._roots_done
                                  and r not in queued[0]]
                    queued = (queued[0] + unfinished, queued[1] or (full and bool(unfinished)))
            if queued:
                self.start(queued[0], full=queued[1])

    def _run_each(self, roots: list[Path], full: bool) -> None:
        total_added = total_removed = total_errors = 0
        for index, root in enumerate(roots, start=1):
            if self._stop.is_set():
                break
            self._run(root, full, label=(index, len(roots)))
            if not self._stop.is_set():
                self._roots_done.append(root)
            total_added += self.progress.added
            total_removed += self.progress.removed
            total_errors += self.progress.errors
        if not self._stop.is_set():
            self.progress.update(
                added=total_added, removed=total_removed, errors=total_errors,
                status="done", ended_at=time.time(),
                message=(f"{len(roots)} library folders up to date"
                         if len(roots) > 1 else "Library up to date"),
            )
            self._notify({"phase": "done"})

    def stop(self, join: bool = False) -> None:
        self._stop.set()
        # Stopping means stopping: a scan queued behind this one is dropped too.
        self._after_stop = None
        thread = self._thread
        if (join and thread and thread.is_alive()
                and thread is not threading.current_thread()):
            thread.join(timeout=STOP_WAIT_SECONDS)
        self._stop_watch()

    @property
    def running(self) -> bool:
        # The scan thread starting the scan queued behind it is on its way out.
        thread = self._thread
        return bool(thread and thread.is_alive()
                    and thread is not threading.current_thread())

    # -- main loop --------------------------------------------------------
    def _run(self, root: Path, full: bool,
             label: tuple[int, int] | None = None) -> None:
        cfg = self.cfg
        # A folder that was hidden when the last scan asked about it may not be
        # hidden now. On Windows the answer costs a stat, so it is memoised —
        # but for the length of one scan only, or unhiding a folder in Explorer
        # would take a restart of the server to be noticed.
        _dir_is_hidden.cache_clear()
        prefix = f"[{label[0]}/{label[1]}] " if label and label[1] > 1 else ""
        self.progress = ScanProgress(
            root=str(root), status="walking", started_at=time.time(),
            message=f"{prefix}Reading {root.name or root}…",
        )
        self._run_id = None
        try:
            # Inside the try: a full disk or an index busy past its timeout
            # here used to end the thread with the scan still saying
            # "walking", and nothing would start another until a restart.
            cfg.ensure_dirs()
            conn = db.ready_connection(cfg.db_path)
            self._run_id = db.start_scan_run(conn, str(root))
            known = {} if full else db.existing_signatures(conn, str(root))
            found: list[tuple[str, os.stat_result]] = []
            present: set[str] = set()

            report = WalkReport()
            # Where the walk is. Reported when it changes folder rather than
            # every N files: it is exactly the moment the answer is new, it
            # costs one string compare per file, and on a folder holding forty
            # thousand photographs the line stays put instead of flickering.
            here = ""
            for rel, st in walk_media(root, cfg, report):
                if self._stop.is_set():
                    return self._finish("idle", "Cancelled")
                present.add(rel)

                folder = rel.replace("\\", "/").rsplit("/", 1)[0] if "/" in rel else ""
                if folder != here:
                    here = folder
                    self.progress.update(
                        folder=short_folder(f"{root}/{folder}" if folder else root,
                                            root))

                sig = known.get(rel)
                if sig is not None:
                    # A file already in the index is left alone when re-checking
                    # is off — that setting exists to do *less* work, and taking
                    # it the other way round meant switching it off re-indexed
                    # every file in the library on every scan.
                    if not cfg.rescan_on_change:
                        continue
                    if abs(sig[0] - st.st_mtime) < 1e-6 and sig[1] == st.st_size:
                        continue
                found.append((rel, FileSignature(st)))
                if len(present) % 500 == 0:
                    self.progress.update(
                        total=len(found), message=f"Found {len(present):,} files…"
                    )

            # A cancelled walk has not seen the whole tree either, so it must
            # never be the evidence for a deletion.
            if self._stop.is_set():
                return self._finish("idle", "Cancelled")

            # Before anything is called missing: a file still here under its
            # name spelled in another Unicode form is the same file.
            self._respell_moved_names(conn, str(root), present, found)
            self._retry_unread(conn, str(root), present, found)

            try:
                removed_thumbs = db.delete_missing(
                    conn, str(root), present, complete=report.complete)
            except db.PruneRefused as refusal:
                removed_thumbs = []
                self.skipped_prune = refusal
                for line in report.unreadable[:5]:
                    log.warning("unreadable during scan of %s — %s", root, line)
                log.warning(
                    "kept %d index entries for %s: %s",
                    refusal.stale, root, refusal.reason)
                self.progress.update(
                    warning=f"Nothing was removed from the index — {refusal.reason}.")
            for base in removed_thumbs:
                media.remove_thumbnails(
                    cfg.thumbs_dir, base, cfg.thumb_sizes, cfg.thumb_format
                )
            self.progress.update(
                removed=len(removed_thumbs), total=len(found), status="indexing",
                message=f"{prefix}Indexing {len(found):,} new or changed files…",
            )

            # Whether the library changed since the duplicates and live photos
            # were last linked. A scan that found nothing new rebuilt both over
            # the whole library anyway: seconds on a computer, half a minute
            # on a Pi, every time a single photograph arrived somewhere else.
            # Told by what the scan found and by the folder's count and newest
            # id, so items deleted or put back from the bin between scans, and
            # a scan stopped before linking, still leave the linking owed.
            if found:
                self._index(conn, root, found, known)
            if self._stop.is_set():
                return self._finish("idle", "Cancelled")
            linked_key = f"linked:{root}"
            shape = conn.execute(
                "SELECT COUNT(*), COALESCE(MAX(id), 0) FROM assets WHERE root=? AND trashed=0",
                (str(root),)).fetchone()
            shape = f"{shape[0]}:{shape[1]}"
            owed = bool(found or removed_thumbs) or db.get_meta(conn, linked_key) != shape
            if owed:
                # A first scan takes the table from empty to its full size, so
                # the statistics gathered at start-up describe a library that no
                # longer exists. Refreshed here rather than at the end: AI
                # tagging can run for hours after this, and the gallery is in
                # use the whole time.
                try:
                    db.refresh_statistics(conn)
                except sqlite3.Error as exc:
                    log.warning("could not refresh query statistics: %s", exc)

            self._rescore_quality(conn, str(root))
            if owed:
                self.progress.update(message=f"{prefix}Matching duplicates and live photos…")
                self._link_duplicates(conn, str(root))
                self._link_live_photos(conn, str(root))
                if not self._stop.is_set():
                    db.set_meta(conn, linked_key, shape)
                    conn.commit()
            self._group_occasions(conn, str(root))
            self._notify({"phase": "indexed"})

            # Before tagging, which on a processor can take hours: screenshots
            # found by name, and everything already tagged judged from the
            # vectors it has, are hidden now rather than when tagging ends.
            self._judge_screens(conn, str(root))
            # And whatever an earlier release, or a pass before the item was
            # hidden, made of an administrator-only item is taken back now.
            forgotten = db.forget_ai_reading(conn, root=str(root))
            if forgotten:
                log.info("hidden items: AI tags, text and faces removed from %s",
                         f"{forgotten:,}")

            # Each pass returns early when the scan is stopped, and this used to
            # carry straight on to _finish("done") regardless. A restart in the
            # middle of analysis was recorded as a scan that had finished, so
            # start-up skipped the scan that would have carried the analysis on.
            #
            # Naming places goes early, ahead of the passes that take hours.
            #
            # It is a local lookup against a list on disk — twenty thousand
            # photographs in about a minute — and it used to run *after*
            # describing videos, which on a library with seventeen thousand
            # clips in it is most of a day. A household that started a scan in
            # the morning got searchable locations the following morning,
            # stuck behind a pass with nothing to do with them.
            #
            # The three slow passes keep the order they had. Which of them is
            # longest depends on the library — videos cost five decodes each
            # but there are fewer of them; text and faces are cheaper each and
            # run over every photograph — so there is no ordering among them
            # that is right for everybody, and guessing would be worse than
            # leaving them where households already expect them.
            self._wait_for_the_model()
            # Ahead of tagging too, which in overnight mode waits for the night:
            # place names and sound-file tiles waited all day behind it.
            passes = [self._name_places, self._draw_audio_art]
            if self.ai is not None and self.cfg.ai_enabled:
                passes.append(self._tag)
                passes.append(self._tag_video_keyframes)
            passes.append(self._read_text)
            if getattr(self.cfg, "faces_enabled", False):
                passes.append(self._find_faces)
            for run_pass in passes:
                if self._stop.is_set():
                    break
                # Each pass says what it is doing once it knows it has work.
                # Without this, one that returns straight away — no videos to
                # describe, place names off, no face models — left the pass
                # before it on screen: "Analysing 512 / 512" at 100% for as
                # long as the rest of the scan took.
                self.progress.update(status="finishing", message="Finishing up…",
                                     tagged=0, tag_total=0)
                self._notify({"phase": "finishing"})
                run_pass(conn, str(root))
            if self._stop.is_set():
                return self._finish("idle", "Cancelled")

            self._finish("done", "Library up to date")
        except Exception as exc:  # noqa: BLE001
            self.progress.update(message=f"Scan failed: {exc}", errors=1)
            self._finish("error", f"Scan failed: {exc}")

    def _wait_for_the_model(self) -> None:
        """Wait, saying so, until the image model start-up is loading is there.

        Start-up walks and indexes before the model has loaded; only what
        uses the model waits for it, here.
        """
        ready = self.model_ready
        if ready is None or ready.is_set():
            return
        self.progress.update(message="Waiting for the image model to load…")
        while not ready.wait(0.5):
            if self._stop.is_set():
                return

    def _finish(self, status: str, message: str) -> None:
        # Close the run row, if this scan opened one. A row without an
        # `ended_at` is a scan that died, and the next start can tell.
        run_id = getattr(self, "_run_id", None)
        if run_id:
            self._run_id = None
            try:
                db.finish_scan_run(
                    db.connect(self.cfg.db_path), run_id, status=status,
                    total=self.progress.total, processed=self.progress.processed,
                    added=self.progress.added, updated=self.progress.updated,
                    removed=self.progress.removed, errors=self.progress.errors)
            except Exception as exc:                    # noqa: BLE001
                log.debug("could not close the scan run: %s", exc)

        # Clear the folder: a scan that has stopped is not *in* anywhere, and
        # leaving the last one it touched on screen makes a finished scan read
        # as one frozen mid-walk — the exact confusion this reporting exists
        # to remove.
        self.progress.update(status=status, message=message, folder="",
                             ended_at=time.time())
        done = self.progress.snapshot()
        # One line per scan. Before this the only record was a database row, so
        # a scan cut short — by a consolidation taking the disk, by a restart,
        # by an error — left nothing anybody could read afterwards, and a
        # library that was not finishing looked the same as one that was.
        log.info("scan of %s: %s after %ss — %s of %s files indexed "
                 "(%s added, %s updated), %s removed, %s errors",
                 done.get("root") or "the library", status,
                 done.get("elapsed", 0.0), f"{done.get('processed', 0):,}",
                 f"{done.get('total', 0):,}", f"{done.get('added', 0):,}",
                 f"{done.get('updated', 0):,}", f"{done.get('removed', 0):,}",
                 f"{done.get('errors', 0):,}")
        self._notify({"phase": status})

    # -- indexing ---------------------------------------------------------
    def _index(self, conn, root: Path, found: list[tuple[str, Any]],
               known: dict[str, tuple[float, int, int]]) -> None:
        cfg = self.cfg
        rules = db.folder_rules(conn, str(root))
        # Turns set by hand, so a rescan makes the thumbnails with them.
        manual_turns = {
            row[0]: int(row[1] or 0) for row in conn.execute(
                "SELECT rel_path, rotation FROM assets "
                "WHERE root=? AND rot_source='manual'", (str(root),))}
        batch: list[dict[str, Any]] = []
        batch_size = 64

        # Indexing is the long half of a first scan — reading every file and
        # building its thumbnails — so it has to say where it is too. Work
        # finishes out of order across the pool, so reporting the folder of
        # whichever file happened to land would jump backwards several times a
        # second. Reporting the *furthest* file finished so far never does:
        # it is monotonic, it is true ("we have got as far as here"), and it
        # costs one integer compare. Each file's position travels with its
        # future rather than in a dictionary of every path, which at a million
        # files was another hundred megabytes held for all of indexing.
        furthest = -1
        here = None

        # Files go to the pool a few at a time rather than all at once. A first
        # scan of a million files submitted up front holds a million futures
        # before the first thumbnail is written, and a cancel has to walk them.
        workers = max(1, cfg.workers)
        started = time.time()
        log.info("indexing %s new or changed files on %d workers",
                 f"{len(found):,}", workers)
        queue = iter(enumerate(found))
        if cfg.orientation_ai:
            self._wait_for_the_model()
        engine = getattr(self.ai, "engine", self.ai)
        futures: dict[Any, tuple[int, str]] = {}
        failed = 0

        with ThreadPoolExecutor(max_workers=workers) as pool:
            while not self._stop.is_set():
                if not self._take_turn("index"):
                    break
                for position, (rel, st) in itertools.islice(queue, workers * 4 - len(futures)):
                    futures[pool.submit(build_record, root, rel, st, cfg, rules,
                                        engine, manual_turns.get(rel))] = (position, rel)
                if not futures:
                    break
                done, _ = wait(futures, return_when=FIRST_COMPLETED)
                for future in done:
                    if self._stop.is_set():
                        break
                    position, rel = futures.pop(future)

                    if position > furthest:
                        furthest = position
                        folder = rel.replace("\\", "/").rsplit("/", 1)[0] if "/" in rel else ""
                        if folder != here:
                            here = folder
                            self.progress.update(
                                folder=short_folder(
                                    f"{root}/{folder}" if folder else root, root))

                    try:
                        record = future.result()
                    except Exception as exc:  # noqa: BLE001
                        self.progress.bump(errors=1, processed=1)
                        failed = self._failed_file(root, rel, exc, failed)
                        continue

                    problem = record.pop("error", None)
                    if problem:
                        self.progress.bump(errors=1)
                        failed = self._failed_file(root, rel, problem, failed)
                    batch.append(record)
                    self.progress.bump(processed=1)
                    self.progress.bump(**{"updated" if rel in known else "added": 1})

                    if len(batch) >= batch_size:
                        db.bulk_upsert(conn, batch)
                        batch.clear()
                        rules = self._rules_now(conn, root, rules)
                        self._notify({"phase": "progress"})
                if self._stop.is_set():
                    for pending in futures:
                        pending.cancel()
                    break

        if batch:
            db.bulk_upsert(conn, batch)
        self._rules_now(conn, root, rules)
        if failed > FAILED_FILES_NAMED:
            log.warning("%s more files in %s could not be fully read; the first "
                        "%d are named above", f"{failed - FAILED_FILES_NAMED:,}",
                        root, FAILED_FILES_NAMED)

        # The rate, not just the count: "slow" is only answerable against a
        # number, and this is the phase people mean when they say indexing.
        indexed = self.progress.processed
        elapsed = max(0.001, time.time() - started)
        log.info("indexed %s files in %s (%.1f a second), %s errors",
                 f"{indexed:,}", took(started), indexed / elapsed,
                 f"{self.progress.errors:,}")

    @staticmethod
    def _rules_now(conn, root: Path, rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The folder rules as they stand, after each batch is written.

        Indexing a first library takes hours, and the rules were read once at
        its start. A folder an administrator hid in the meantime went on
        taking new files at the family level, and since an upsert keeps a
        row's visibility, no later scan put that right. Files written before
        the change was seen are brought under it here.
        """
        now = db.folder_rules(conn, str(root))
        if now != rules:
            moved = db.apply_folder_rules_to_new(conn, str(root), now)
            if moved:
                log.info("%d files indexed while a folder's visibility was being "
                         "changed now follow its new setting", moved)
        return now

    @staticmethod
    def _failed_file(root: Path, rel: str, why: object, failed: int) -> int:
        """Name a file indexing could not read, up to a point. Returns the count.

        These went to ``print``, which a server started by the control panel
        has nowhere to send, so a scan summary could say "12 errors" with no
        way to find out which twelve. A library with thousands of damaged
        files names the first few dozen and counts the rest, so it cannot
        rotate the rest of the log away.
        """
        if failed < FAILED_FILES_NAMED:
            log.warning("could not fully read %s in %s: %s", rel, root, why)
        return failed + 1

    def _take_turn(self, job: str) -> bool:
        """Wait while the household comes first. False if the scan was stopped.

        Asked between pieces of work — a batch of files, a photograph, a clip
        — so a video somebody starts watching gets the disk back within one of
        them. See ninaivu/server/workload.py for when that is.
        """
        workload = self.workload
        if workload is None:
            return not self._stop.is_set()

        def say(reason: str | None) -> None:
            self.progress.update(held=reason or "")
            if reason:
                log.info("%s is making way: %s", job, reason)
            self._notify({"phase": "held" if reason else "resumed"})

        return workload.wait_turn(job, self._stop, on_hold=say)

    # -- names spelled differently on another computer ---------------------
    def _respell_moved_names(self, conn, root: str, present: set[str],
                             found: list) -> None:
        """Index rows whose file is here under another Unicode spelling.

        Windows keeps a name as it was typed, often with é as one character;
        a Mac can list the same file with the e and its accent apart, and on
        an NTFS drive cannot open it by the old spelling at all. After a move
        the index held 126 such names from Windows, each looking missing,
        and the first scan on the Mac had indexed every one again as new:
        126 pairs, one with the history and one without. The spellings are
        compared in one Unicode form, and the row that has the history takes
        the name the disk uses (db.respell_asset).
        """
        stale = [rel for rel in db.live_paths(conn, root) if rel not in present]
        if not stale:
            return
        wanted = {unicodedata.normalize("NFC", rel): rel for rel in stale}
        here: dict[str, str] = {}
        for rel in present:
            key = unicodedata.normalize("NFC", rel)
            if key in wanted and rel != wanted[key]:
                here.setdefault(key, rel)
        if not here:
            return
        respelled = set()
        for key, now in here.items():
            gone = db.respell_asset(conn, root, wanted[key], now)
            if gone:
                media.remove_thumbnails(self.cfg.thumbs_dir, gone, self.cfg.thumb_sizes,
                                        self.cfg.thumb_format)
            respelled.add(now)
        # Already indexed under the old spelling: nothing to read again.
        found[:] = [item for item in found if item[0] not in respelled]
        log.info("%d files in %s are indexed under the spelling of their names "
                 "this computer uses", len(respelled), root)

    def _retry_unread(self, conn, root: str, present: set[str], found: list) -> None:
        """Read again, once, the files the indexer never managed to read that
        claim a thumbnail they do not have.

        A photograph too damaged to open has no thumbnail and no size. The
        move from Windows gave 156 of them thumbnail names all the same (see
        storage/reroot.py), and with a name each one was put to the image model
        on every scan and passed over, and the gallery asked for its picture.
        Read again, a file that still cannot be read is written back with no
        thumbnail — so it is not asked about again — and one that can be read
        now gets its picture.
        """
        rows = conn.execute(
            "SELECT rel_path, thumb FROM assets WHERE root=? AND trashed=0 "
            "AND thumb IS NOT NULL AND width IS NULL AND kind IN ('picture', 'video')",
            (root,)).fetchall()
        queued = {rel for rel, _ in found}
        size, fmt = max(self.cfg.thumb_sizes), self.cfg.thumb_format
        retried = 0
        for row in rows:
            rel = row["rel_path"]
            if rel in queued or rel not in present:
                continue
            if (self.cfg.thumbs_dir / media.thumb_file(row["thumb"], size, fmt)).is_file():
                continue
            try:
                st = os.stat(Path(root) / rel)
            except OSError:
                continue
            found.append((rel, FileSignature(st)))
            retried += 1
        if retried:
            log.info("reading %d files again that were never read and claim a "
                     "thumbnail they do not have", retried)

    # -- duplicates -------------------------------------------------------
    def _link_duplicates(self, conn, root: str) -> None:
        """Group the pictures that are the same shot, writing only what moved.

        Clearing every row's group and putting the survivors back was one write
        per picture in the library on every scan — and because the search index
        is rebuilt row by row from a trigger on ``assets``, it was a full
        rewrite of that index too, for a library where usually nothing has
        changed. Working the groups out first and writing the differences costs
        one read, and on a rescan that finds what the last scan found, no writes
        at all.
        """
        if not self.cfg.perceptual_hash:
            return
        started = time.time()
        rows = conn.execute(
            "SELECT id, phash, dup_group FROM assets "
            "WHERE root=? AND phash IS NOT NULL AND trashed=0",
            (root,),
        ).fetchall()
        if not rows:
            return

        wanted = group_duplicates(
            ((row["id"], row["phash"]) for row in rows),
            self.cfg.duplicate_distance,
        )
        # Rows that have *left* a group are in here too, with ``None``: a
        # picture whose only twin was deleted is no longer a duplicate.
        changed = [(wanted.get(row["id"]), row["id"]) for row in rows
                   if wanted.get(row["id"]) != row["dup_group"]]
        if not changed:
            log.info("duplicates: %s hashes, no group changed",
                     f"{len(rows):,}")
            return
        with db._write_lock:
            conn.executemany(
                "UPDATE assets SET dup_group=? WHERE id=?", changed)
            conn.commit()
        log.info("duplicates: %s hashes compared, %s pictures in %s groups, "
                 "%s rows rewritten in %s", f"{len(rows):,}", f"{len(wanted):,}",
                 f"{len(set(wanted.values())):,}", f"{len(changed):,}",
                 took(started))

    # -- live photos -------------------------------------------------------
    def _link_live_photos(self, conn, root: str) -> None:
        """Join each live photo to its clip, and mark the clip as the clip.

        A live photo is one thing the household took and two files on disk: a
        still, and a short clip beside it with the same stem. Both were indexed
        as ordinary items, so the pair counted once as a photograph and again
        as a video — 5,674 of 19,636 "videos" on the library this was found on.

        This also repairs the join itself. The still's ``live_video_path`` was
        built by trying ``.mov`` and then ``.MOV`` against
        :meth:`Path.exists`, which on Windows answers yes to either — so the
        path written down was the one that had been *guessed*, not the one on
        disk, and on that library every single one of them was wrong. NTFS does
        not care and the clips played anyway; a case-sensitive filesystem
        would have 404ed all of them, and the one query that matches on this
        column (``date_edit``) missed them everywhere, because SQLite compares
        text exactly even where the filesystem does not.
        """
        started = time.time()
        clips: dict[str, tuple[int, str]] = {}
        for row in conn.execute(
            "SELECT id, rel_path FROM assets "
            "WHERE root=? AND kind='video' AND trashed=0", (root,),
        ):
            clips[row["rel_path"].lower()] = (int(row["id"]), row["rel_path"])

        paired: set[int] = set()
        repaired = 0
        for row in conn.execute(
            "SELECT id, live_video_path FROM assets "
            "WHERE root=? AND is_live=1 AND trashed=0 "
            "AND COALESCE(live_video_path,'')<>''", (root,),
        ).fetchall():
            stored = row["live_video_path"]
            found = clips.get(stored.lower())
            if not found:
                continue
            clip_id, real = found
            paired.add(clip_id)
            if real != stored:
                conn.execute("UPDATE assets SET live_video_path=? WHERE id=?",
                             (real, int(row["id"])))
                repaired += 1

        # Written as a difference so a clip whose still has since been removed
        # goes back to being an ordinary video, rather than staying hidden from
        # the count for ever.
        marked = {int(r["id"]) for r in conn.execute(
            "SELECT id FROM assets WHERE root=? AND live_clip=1", (root,))}
        for clip_id in paired - marked:
            conn.execute("UPDATE assets SET live_clip=1 WHERE id=?", (clip_id,))
        for clip_id in marked - paired:
            conn.execute("UPDATE assets SET live_clip=0 WHERE id=?", (clip_id,))
        conn.commit()

        if paired or repaired:
            log.info("live photos: %s clips paired (%s newly, %s released), "
                     "%s paths repaired in %s", f"{len(paired):,}",
                     f"{len(paired - marked):,}", f"{len(marked - paired):,}",
                     f"{repaired:,}", took(started))

    # -- picture quality ---------------------------------------------------
    def _rescore_quality(self, conn, root: str) -> None:
        """Re-measure photographs scored by an older version of the rules.

        A threshold that moves is worth nothing if it only ever reaches
        photographs indexed afterwards — a library would end up with two
        vocabularies and no way to tell which frame was judged by which.

        This measures the largest thumbnail rather than the original. The
        measurement normalises to :data:`media.QUALITY_EDGE` regardless of
        what it is handed, so a thumbnail lands within the same tolerance as
        the resolution independence the measure already relies on — and it
        costs one small decode instead of re-reading forty megapixels.
        """
        if not getattr(self.cfg, "quality_scan", True):
            return
        rows = conn.execute(
            "SELECT id, thumb, width, height FROM assets "
            "WHERE root=? AND kind='picture' AND trashed=0 "
            "AND thumb IS NOT NULL AND quality_version < ?",
            (root, media.QUALITY_VERSION),
        ).fetchall()
        if not rows:
            return

        log.info("re-measuring quality for %s photographs scored by older "
                 "rules", f"{len(rows):,}")
        thumb_ext = "webp" if self.cfg.thumb_format.upper() == "WEBP" else "jpg"
        preview = max(self.cfg.thumb_sizes)
        updates: list[tuple] = []
        for row in rows:
            if self._stop.is_set():
                return
            path = self.cfg.thumbs_dir / f"{row['thumb']}_{preview}.{thumb_ext}"
            if not path.exists():
                continue
            try:
                with Image.open(path) as img:
                    stats = media.quality_stats(img)
            except Exception as exc:  # noqa: BLE001 - one unreadable thumb
                log.debug("%s: %s", path, exc)
                continue
            flags = media.quality_flags(
                stats, row["width"], row["height"],
                blur_below=self.cfg.blur_threshold,
                dark_below=self.cfg.dark_threshold,
                bright_above=self.cfg.bright_threshold,
                clip_above=self.cfg.highlight_clip_threshold,
                min_pixels=self.cfg.low_res_pixels,
            )
            updates.append((stats.get("sharpness"), stats.get("brightness"),
                            json.dumps(flags), media.QUALITY_VERSION,
                            int(row["id"])))

        if updates:
            with db._write_lock:
                conn.executemany(
                    "UPDATE assets SET sharpness=?, brightness=?, quality=?, "
                    "quality_version=? WHERE id=?", updates)
                conn.commit()

    # -- occasions --------------------------------------------------------
    def _group_occasions(self, conn, root: str) -> None:
        """Rebuild the auto-albums for this root.

        Never fatal: an occasion that failed to group is a missing convenience,
        not a missing photograph, and a scan that indexed the whole library
        must not report failure over it.
        """
        if not getattr(self.cfg, "occasion_scan", True):
            return
        try:
            db.rebuild_occasions(
                conn, root,
                gap_seconds=float(self.cfg.occasion_gap_hours) * 3600.0,
                radius_km=float(self.cfg.occasion_radius_km),
                min_items=int(self.cfg.occasion_min_items),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("could not group occasions for %s: %s", root, exc)

    # -- AI ---------------------------------------------------------------
    def _nsfw_threshold(self) -> float:
        """The engine's own line when its scores are on a different scale (SigLIP)."""
        own = getattr(self.ai, "nsfw_threshold", None)
        return float(own) if own is not None else float(self.cfg.nsfw_threshold)

    def _retag_if_the_model_changed(self, conn) -> None:
        """A different search model means every vector has to be made again.

        Vectors from two models cannot be compared — they are not even the
        same width (ViT-B-32 512, SigLIP 2 768) — so switching models clears
        the old ones and marks the library for tagging, which then runs in
        the background as it does for new photos. An install whose vectors
        already came from this model is left alone.
        """
        model_id = getattr(self.ai, "model_id", "")
        if not model_id or not getattr(self.ai, "semantic", False):
            return
        previous = db.get_meta(conn, "ai_model_id", "") or ""
        stale = conn.execute("SELECT 1 FROM embeddings WHERE model != ? LIMIT 1",
                             (model_id,)).fetchone()
        if previous == model_id or (not previous and not stale):
            if previous != model_id:
                with db._write_lock:
                    db.set_meta(conn, "ai_model_id", model_id)
                    conn.commit()
            return
        log.info("search model changed (%s -> %s): re-indexing the library for search",
                 previous or "earlier model", model_id)
        with db._write_lock:
            conn.execute("DELETE FROM embeddings WHERE model != ?", (model_id,))
            # The screen scores were read off those vectors, on that model's
            # scale; they are made again with the new ones.
            conn.execute("UPDATE assets SET ai_version = 0, screen_version = 0")
            db.set_meta(conn, "ai_model_id", model_id)
            conn.commit()
        db.forget_embeddings()

    def _tag(self, conn, root: str) -> None:
        self._retag_if_the_model_changed(conn)
        rows = conn.execute(
            "SELECT id, rel_path, thumb, kind FROM assets "
            "WHERE root=? AND trashed=0 AND thumb IS NOT NULL AND ai_version < ? "
            # A sound file's picture is a cover or a drawn tile. Described as
            # a photograph, "green" and "phone" would find its colour and sign.
            "AND kind != 'audio' "
            # Nothing at the administrator-only level is analysed; see
            # db.AI_MAY_READ.
            f"AND {db.AI_MAY_READ} "
            "ORDER BY COALESCE(captured_at, mtime) DESC",
            (root, AI_VERSION),
        ).fetchall()
        if not rows:
            # Not "done": the passes after this one are still to come, and
            # saying the library is up to date takes the progress strip away
            # while the scan is still working.
            return

        self.progress.update(
            status="tagging", tag_total=len(rows), tagged=0,
            message=f"Analysing {len(rows):,} items with {self.ai.name}…",
        )
        # The phase that dominates a first scan, and the one nobody can see
        # into. On a processor-only install this is hours where indexing was
        # minutes, so the count belongs in the log where it can be believed.
        tagging_started = time.time()
        log.info("tagging %s items with %s (%s)", f"{len(rows):,}",
                 self.ai.name, getattr(self.ai, "device", "") or "unknown device")
        thumb_ext = "webp" if self.cfg.thumb_format.upper() == "WEBP" else "jpg"
        preview_size = max(self.cfg.thumb_sizes)

        def thumb_path(row) -> Path:
            return self.cfg.thumbs_dir / f"{row['thumb']}_{preview_size}.{thumb_ext}"

        # With the model on a graphics processor, the next batch is opened on
        # one helper thread while this one is with the model (see
        # ClipEngine.reads_ahead). One batch ahead at most; the results are
        # still written here, on this thread, in order.
        ahead = (ThreadPoolExecutor(max_workers=1, thread_name_prefix="ninaivu-tag-ahead")
                 if getattr(self.ai, "reads_ahead", False) is True else None)
        waiting: tuple[list[tuple[int, Path]], Any] | None = None

        def hand_over(full: list[tuple[int, Path]]) -> None:
            nonlocal waiting
            opened = (ahead.submit(self.ai.prepare, [p for _, p in full])
                      if ahead is not None else None)
            if waiting is not None:
                self._tag_and_judge(conn, root, *waiting)
            waiting = (full, opened)

        batch: list[tuple[int, Path]] = []
        try:
            for row in rows:
                if self._stop.is_set():
                    return
                path = thumb_path(row)
                if path.exists():
                    batch.append((int(row["id"]), path))
                if len(batch) >= self.cfg.clip_batch_size:
                    if not self._take_turn("analysis"):
                        return
                    hand_over(batch)
                    batch = []
                    self._notify({"phase": "tagging"})
            if batch:
                hand_over(batch)
            if waiting is not None and not self._stop.is_set():
                self._tag_and_judge(conn, root, *waiting)
        finally:
            if ahead is not None:
                ahead.shutdown(wait=True, cancel_futures=True)
        log.info("tagged %s of %s items in %s",
                 f"{self.progress.tagged:,}", f"{len(rows):,}",
                 took(tagging_started))

    def _tag_and_judge(self, conn, root: str, batch: list[tuple[int, Path]],
                       opened: Any = None) -> None:
        self._tag_batch(conn, batch, opened)
        self._judge_screens(conn, root, ids=[i for i, _ in batch])

    def _tag_batch(self, conn, batch: list[tuple[int, Path]], opened: Any = None) -> None:
        try:
            paths = [p for _, p in batch]
            results = (self.ai.analyse(paths, prepared=opened.result())
                       if opened is not None else self.ai.analyse(paths))
        except Exception as exc:  # noqa: BLE001
            log.warning("could not analyse %d items (the first is %s): %s",
                        len(batch), batch[0][1], exc)
            self.progress.bump(errors=1, tagged=len(batch))
            return

        for (asset_id, _), result in zip(batch, results):
            fields: dict[str, Any] = {"ai_version": AI_VERSION}
            if result.get("tags"):
                fields["tags"] = result["tags"]
            if result.get("caption"):
                fields["caption"] = result["caption"]
            if self.cfg.nsfw_filter and result.get("nsfw_score") is not None:
                fields["nsfw_score"] = result["nsfw_score"]
                fields["nsfw"] = int(result["nsfw_score"] >= self._nsfw_threshold())
            # Not update_asset: that marks what it writes as an admin's own
            # choice. This keeps an admin's tags, caption and content flag
            # instead of replacing them — a model change re-tags the whole
            # library, and it must not undo what somebody set by hand.
            db.store_ai_fields(conn, asset_id, **fields)
            if (vector := result.get("embedding")) is not None:
                db.store_embedding(
                    conn, asset_id, self.ai.model_id, len(vector) // 4, vector
                )
        self.progress.bump(tagged=len(batch))

    # -- screenshots and documents ----------------------------------------
    #: Vectors read, judged and written per transaction.
    SCREEN_BATCH = 1000

    def apply_screen_rule(self) -> None:
        """Bring every library folder in line with the setting, now.

        For the console's switch, and for a start-up that does not scan: on,
        it judges whatever has a vector and no score and hides what matches;
        off, it puts back everything the rule hid.
        """
        conn = db.connect(self.cfg.db_path)
        if not getattr(self.cfg, "hide_screens", False):
            restored = db.restore_screens(conn)
            log.info("screenshots and documents: %s items shown again", f"{restored:,}")
            return
        for root in self.cfg.libraries:
            self._judge_screens(conn, str(root), stoppable=False)

    def _judge_screens(self, conn, root: str, ids: list[int] | None = None,
                       *, stoppable: bool = True) -> None:
        """Score pictures that have a vector but no screen score, then hide.

        Reads the vectors tagging already stored, so no image is opened. With
        *ids* it looks only at those items, which is how tagging hands over
        each batch as it finishes; without, it catches up on the whole folder.
        """
        if not getattr(self.cfg, "hide_screens", False):
            return
        scorer = None
        try:
            scorer = screens.scorer_for(self.ai)
        except Exception as exc:                          # noqa: BLE001
            log.warning("could not prepare the screenshot check: %s", exc)
        started = time.time()
        judged = 0
        if scorer is not None:
            where = "a.root=? AND a.kind='picture'"
            params: list[Any] = [root]
            if ids is not None:
                # Just tagged, so just given a new vector: judged again even if
                # an older one was scored — the file may have changed.
                if not ids:
                    return
                where += f" AND a.id IN ({','.join('?' * len(ids))})"
                params += [int(i) for i in ids]
            else:
                where += " AND a.screen_version < ?"
                params.append(screens.SCREEN_VERSION)
            while not (stoppable and self._stop.is_set()):
                rows = conn.execute(
                    "SELECT a.id, e.vector FROM assets a JOIN embeddings e "
                    "ON e.asset_id=a.id AND e.model=? "
                    f"WHERE {where} LIMIT ?",
                    [scorer.model_id, *params, self.SCREEN_BATCH]).fetchall()
                if not rows:
                    break
                vectors = np.frombuffer(b"".join(r["vector"] for r in rows),
                                        dtype=np.float32)
                scores = scorer.score(vectors)
                db.store_screen_scores(
                    conn, [(int(r["id"]), float(s)) for r, s in zip(rows, scores)])
                judged += len(rows)
                if ids is not None:
                    break
        hidden = db.hide_screens(
            conn, root, ids=ids,
            threshold=scorer.threshold if scorer is not None else None)
        if ids is None:
            log.info("screenshots and documents: %s judged by picture, %s hidden "
                     "in %s", f"{judged:,}", f"{hidden:,}", took(started))

    # -- place names -------------------------------------------------------
    #: Photographs named per transaction. Looking a coordinate up in the
    #: gazetteer is a dictionary lookup; committing — and rebuilding that row's
    #: entry in the search index, which a trigger on ``assets`` does row by row
    #: — is not. One commit per photograph made the writing, rather than the
    #: naming, almost the whole cost of this pass.
    PLACE_BATCH = 500

    def _name_places(self, conn, root: str) -> None:
        """Fill in where each photograph was taken, from its coordinates.

        Runs before the occasions are grouped, so a run that all happened in
        one town gets that town in its title on the same scan.

        The gazetteer is fetched the first time this runs and never again.
        If that fetch fails — no network, a moved URL — the scan carries on
        without names rather than failing over a convenience.
        """
        if not getattr(self.cfg, "place_names", False):
            return
        from ..utils import places                        # noqa: PLC0415

        if not places.installed(self.cfg.state_dir):
            # Tried at most once a day. Offline, every scan tried again, and
            # each try could hold the rest of the scan for two minutes under
            # a message that never moved.
            tried = float(db.get_meta(conn, PLACES_TRIED, "0") or 0)
            if time.time() - tried < PLACES_RETRY_SECONDS:
                return
            db.set_meta(conn, PLACES_TRIED, str(time.time()))
            conn.commit()
            self.progress.update(status="naming",
                                 message="Fetching the place-name list…")
            try:
                places.install(self.cfg.state_dir)
            except Exception as exc:  # noqa: BLE001 - offline, or a moved file
                log.warning("could not fetch the place-name list: %s", exc)
                return

        book = places.gazetteer(self.cfg.state_dir)
        if book is None:
            return

        rows = conn.execute(
            "SELECT id, gps_lat, gps_lon FROM assets "
            "WHERE root=? AND trashed=0 AND gps_lat IS NOT NULL "
            "AND gps_lon IS NOT NULL AND place_version < ? ORDER BY id",
            (root, places.PLACE_VERSION),
        ).fetchall()
        if not rows:
            return

        self.progress.update(
            status="naming", tag_total=len(rows), tagged=0,
            message=f"Naming where {len(rows):,} photographs were taken…")
        log.info("naming where %s photographs with coordinates were taken",
                 f"{len(rows):,}")
        named = 0
        placed: list[tuple[Any, ...]] = []
        stamped: list[tuple[Any, ...]] = []

        def flush() -> None:
            if not placed and not stamped:
                return
            with db._write_lock:
                if placed:
                    conn.executemany(
                        "UPDATE assets SET city=?, country=?, place_version=? "
                        "WHERE id=?", placed)
                if stamped:
                    # Stamped, but their city left as it was: a gazetteer with
                    # nothing to say about a coordinate is not evidence that a
                    # name already stored against it is wrong.
                    conn.executemany(
                        "UPDATE assets SET place_version=? WHERE id=?", stamped)
                conn.commit()
            self.progress.bump(tagged=len(placed) + len(stamped))
            placed.clear()
            stamped.clear()

        for row in rows:
            if self._stop.is_set():
                break
            found = book.nearest(float(row["gps_lat"]), float(row["gps_lon"]),
                                 max_km=float(self.cfg.place_max_km))
            if found:
                # Only the name: the distance was how it was chosen, not
                # something anybody needs to see afterwards.
                placed.append((found["city"], found["country"],
                               places.PLACE_VERSION, int(row["id"])))
                named += 1
            else:
                stamped.append((places.PLACE_VERSION, int(row["id"])))
            if len(placed) + len(stamped) >= self.PLACE_BATCH:
                # Whatever a stopped scan had already named is kept, so the
                # next one starts from there rather than doing it again.
                flush()
                self._notify({"phase": "naming"})
        flush()
        log.info("named %d of %d photographs that carry coordinates",
                 named, len(rows))

    # -- text in pictures --------------------------------------------------
    def _read_text(self, conn, root: str) -> None:
        """Read the words in photographs and put them in the search index.

        Off unless asked for, and skipped without complaint when the optional
        reader is not installed — a household that never turns this on should
        never see it mentioned.

        Reads the largest thumbnail rather than the original: it is already
        written, already the right size for the reader, and re-decoding forty
        megapixels to find a shop sign is time spent for nothing.
        """
        if not getattr(self.cfg, "ocr_enabled", False):
            return
        from . import ocr as ocr_mod                      # noqa: PLC0415

        if not ocr_mod.available():
            log.info("text reading is on, but the reader is not installed")
            return
        rows = conn.execute(
            "SELECT id, thumb FROM assets "
            "WHERE root=? AND kind='picture' AND trashed=0 "
            "AND thumb IS NOT NULL AND ocr_version < ? "
            # Text in a hidden picture is what it was hidden for: a passport
            # number, an account, a diagnosis. It is never read.
            f"AND {db.AI_MAY_READ} ORDER BY id",
            (root, ocr_mod.OCR_VERSION),
        ).fetchall()
        if not rows:
            return

        self.progress.update(
            status="reading", tag_total=len(rows), tagged=0,
            message=f"Reading text in {len(rows):,} photographs…")
        log.info("reading the text in %s photographs", f"{len(rows):,}")
        thumb_ext = "webp" if self.cfg.thumb_format.upper() == "WEBP" else "jpg"
        preview = max(self.cfg.thumb_sizes)
        found = 0
        for index, row in enumerate(rows, start=1):
            # The console's switch, honoured here rather than at the next scan
            # — reading every photograph once is hours, and turning it off in
            # the middle of those hours means now.
            if self._stop.is_set() or not getattr(self.cfg, "ocr_enabled", False):
                return
            if not self._take_turn("analysis"):
                return
            path = self.cfg.thumbs_dir / f"{row['thumb']}_{preview}.{thumb_ext}"
            text = ""
            if path.exists():
                text = ocr_mod.read(
                    path,
                    min_score=float(self.cfg.ocr_min_score),
                    max_chars=int(self.cfg.ocr_max_chars),
                )["text"]
            if text:
                found += 1
            # Stamped either way: a photograph with no text in it must not be
            # read again on every scan for the rest of its life.
            # Stored with the same check as the tags: an item hidden while
            # this pass was reading its way down the list keeps no text.
            db.store_ai_fields(conn, int(row["id"]), ocr_text=text or None,
                               ocr_version=ocr_mod.OCR_VERSION)
            # Reading a photograph takes long enough that this is the only
            # thing telling the household the scan is still working.
            self.progress.bump(tagged=1)
            if index % 20 == 0:
                self._notify({"phase": "reading"})
        log.info("read text in %d of %d photographs", found, len(rows))

    # -- pictures for sound files ------------------------------------------
    def _draw_audio_art(self, conn, root: str) -> None:
        """A picture for every sound file that has none yet.

        New files get theirs as they are indexed. This is for the ones indexed
        before sound files had pictures, and for any whose picture has gone.
        Whether one is done is whether its picture is on disk, so there is
        nothing to record: once drawn, a file is not read again. A file on a
        drive that is not plugged in waits for it, rather than being given a
        lesser tile for good.
        """
        rows = conn.execute(
            "SELECT id, rel_path, thumb, duration FROM assets "
            "WHERE root=? AND kind='audio' AND trashed=0 ORDER BY id", (root,),
        ).fetchall()
        sizes, fmt = self.cfg.thumb_sizes, self.cfg.thumb_format
        todo = []
        for row in rows:
            base = row["thumb"] or media.thumb_base(root, row["rel_path"])
            if not (self.cfg.thumbs_dir / media.thumb_file(base, max(sizes), fmt)).is_file():
                todo.append((row, base))
        if not todo:
            return

        self.progress.update(
            status="covers", tag_total=len(todo), tagged=0,
            message=f"Drawing pictures for {len(todo):,} sound files…")
        log.info("drawing pictures for %s sound files", f"{len(todo):,}")

        def draw(item):
            row, _ = item
            path = Path(root) / row["rel_path"]
            if not path.is_file():
                return item, None
            return item, audio_art.picture_for(path, row["rel_path"],
                                               duration=row["duration"])

        # Most of each is ffmpeg decoding, in a process of its own, so a few
        # at once use the processor rather than wait on one another.
        workers = max(1, min(4, int(self.cfg.workers or 1)))
        # Taking turns as indexing, not analysis: it is making thumbnails, a
        # third of a second a file, and in the overnight mode analysis waits
        # for 23:00 — leaving blank tiles all day for no reason.
        drawn = 0
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="ninaivu-audio-art") as pool:
            for start in range(0, len(todo), workers * 2):
                if self._stop.is_set() or not self._take_turn("index"):
                    return
                for (row, base), image in pool.map(draw, todo[start:start + workers * 2]):
                    if image is not None:
                        try:
                            media.write_thumbnails(image, self.cfg.thumbs_dir, base, sizes,
                                                   fmt, self.cfg.thumb_quality)
                            # indexed_at is the thumbnail's version in its URL:
                            # moved, so no browser keeps the old blank.
                            db.update_asset(
                                conn, int(row["id"]), thumb=base,
                                blurhash=media.blurhash_encode(image),
                                color=media.dominant_color(image),
                                indexed_at=time.time())
                            drawn += 1
                        except Exception:                        # noqa: BLE001
                            log.exception("no picture for %s", row["rel_path"])
                        finally:
                            image.close()
                    self.progress.bump(tagged=1)
                self._notify({"phase": "covers"})
        log.info("drew pictures for %d of %d sound files", drawn, len(todo))

    # -- video keyframes ---------------------------------------------------
    #: Where in a clip to look, as fractions of its length. Never the very
    #: first or last moment: those are lens caps, fades and black frames far
    #: more often than they are the subject.
    KEYFRAME_POINTS = (0.1, 0.3, 0.5, 0.7, 0.9)

    def _tag_video_keyframes(self, conn, root: str) -> None:
        """Describe a video by several moments instead of its poster frame.

        A holiday clip is a beach, then a restaurant, then a car park. One
        frame at the one-second mark describes none of them, and searching
        for "beach" would miss the video of the beach.

        Cheap in the tier that has no model — the light engine reads
        composition and colour off each frame just the same — and it needs no
        dependency the poster frame did not already need.
        """
        wanted = int(getattr(self.cfg, "video_keyframes", 0) or 0)
        if self.ai is None or not self.cfg.ai_enabled or wanted < 2:
            return

        # Clips written off for a reason that has since stopped being true.
        #
        # A video indexed on a machine with no ffmpeg has no duration, and this
        # pass used to read that as "nothing to sample here" and stamp it done
        # for ever. Installing ffmpeg afterwards did not rescue it: duration is
        # written when a file is *indexed*, and a file whose size and mtime have
        # not moved is never indexed again. So the stamp is lifted from the ones
        # that were never actually described — they have no duration to this
        # day — and they get another go below, where the probe is retried.
        #
        # Once per decoder, though, not once per scan. A clip that the probe
        # below still cannot read is stamped again, and lifting that stamp
        # every scan re-probed the same 46 broken files every ninety seconds —
        # reads that, until the watcher learned to ignore them, set off the
        # next scan too.
        decoder = "ffmpeg" if media.FFMPEG else ("opencv" if media.cv2 is not None else "")
        lifted_key = f"keyframe_lift:{root}"
        if decoder and db.get_meta(conn, lifted_key, "") != decoder:
            with db._write_lock:
                conn.execute(
                    "UPDATE assets SET keyframe_version=0 "
                    "WHERE root=? AND kind='video' AND trashed=0 "
                    "AND keyframe_version>0 AND COALESCE(duration,0)<=0",
                    (root,),
                )
                db.set_meta(conn, lifted_key, decoder)
                conn.commit()

        # The clip half of a live photo is not described. It is three seconds
        # of a photograph that the tagging pass has already read, so sampling
        # it at five moments costs five decodes and five passes through the
        # model to learn what is already known — 4,571 clips and about five
        # hours of the fourteen, on the library this was found on.
        rows = conn.execute(
            "SELECT id, rel_path, duration FROM assets "
            "WHERE root=? AND kind='video' AND trashed=0 AND live_clip=0 "
            f"AND keyframe_version < ? AND {db.AI_MAY_READ} ORDER BY id",
            (root, KEYFRAME_VERSION),
        ).fetchall()
        if not rows:
            return

        # Its own phase, with its own count. Sampling a clip means decoding
        # it, which without ffmpeg is seconds each: on a library of twenty
        # thousand videos this pass is hours, and it used to run under the
        # previous phase's words with the bar sitting at 100%.
        self.progress.update(status="videos", tag_total=len(rows), tagged=0,
                             message=f"Describing {len(rows):,} videos…")
        self._notify({"phase": "videos"})
        started = time.time()
        log.info("describing %s videos by their keyframes", f"{len(rows):,}")

        points = self.KEYFRAME_POINTS[:wanted]
        # Reading the moments out of a clip is ffmpeg's work, in processes of
        # its own, and was nearly all of this pass: one clip at a time left a
        # many-core machine with one core decoding and the model waiting on
        # it. So the next few clips are read ahead on a small pool while the
        # model describes this one. The database is still written from here
        # alone, one clip after another, in the same order as before.
        workers = max(1, min(4, int(self.cfg.workers or 1)))
        ahead = workers * 2
        pending: deque = deque()
        queued = iter(rows)

        def top_up(pool) -> None:
            while len(pending) < ahead:
                row = next(queued, None)
                if row is None:
                    return
                pending.append((row, pool.submit(self._read_keyframes, root, row, points)))

        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="ninaivu-keyframes") as pool:
            try:
                top_up(pool)
                index = 0
                while pending:
                    row, reading = pending.popleft()
                    index += 1
                    if self._stop.is_set() or not self._take_turn("analysis"):
                        return
                    # The console's switch takes effect here, not at the next
                    # scan. This pass runs for hours on a library with
                    # thousands of clips in it, and somebody turning it off
                    # part-way through those hours means now — otherwise the
                    # only way to be rid of it is to stop the whole scan,
                    # losing the passes that come after this one.
                    if int(getattr(self.cfg, "video_keyframes", 0) or 0) < 2:
                        log.info("keyframes turned off after %s of %s videos",
                                 f"{index - 1:,}", f"{len(rows):,}")
                        return
                    try:
                        self._tag_one_video(conn, root, row, points, read=reading.result())
                    except Exception as exc:  # noqa: BLE001 - one bad clip, not a scan
                        log.debug("keyframes for %s: %s", row["rel_path"], exc)
                        db.update_asset(conn, int(row["id"]),
                                        keyframe_version=KEYFRAME_VERSION)
                    self.progress.bump(tagged=1)
                    if index % 5 == 0:
                        self._notify({"phase": "videos"})
                    top_up(pool)
            finally:
                # Stopped or switched off: clips not yet started are not read.
                for _, reading in pending:
                    reading.cancel()
        log.info("described %s videos in %s", f"{len(rows):,}", took(started))

    #: How large a moment is read out of a clip. The image model looks at 224
    #: or 384 pixels and the light engine at 32, so the 4K frame a phone
    #: records was being decoded, written out as a PNG, read back and saved
    #: again only to be shrunk. This is comfortably above what any of them use.
    KEYFRAME_SIDE = 768

    def _read_keyframes(self, root: str, row, points) -> dict[str, Any]:
        """Read a clip's moments, on a pool thread. Touches no database.

        Returns ``fields`` to store on the clip (a length found by probing),
        and ``frames``: the images read, or None when the clip is to be
        stamped done without being described.
        """
        source = Path(root) / row["rel_path"]
        if not source.exists():
            return {"fields": {}, "frames": None}

        fields: dict[str, Any] = {}
        duration = float(row["duration"] or 0)
        if duration <= 0:
            # Asked again rather than given up on. A clip with no duration was
            # usually indexed before this machine had ffmpeg, and nothing else
            # ever goes back to look: the length is read when a file is indexed,
            # and an unchanged file is not indexed twice. One probe here is
            # cheap next to the five decodes it unlocks, and the answer is kept
            # so no later pass has to ask again.
            info = media.probe_video(source)
            duration = float(info.get("duration") or 0)
            if duration <= 0:
                # Genuinely unreadable — a truncated download, a container with
                # no index. Stamped so every future scan does not retry it.
                return {"fields": {}, "frames": None}
            fields = {k: v for k, v in info.items()
                      if k in {"duration", "width", "height", "camera"}
                      and v is not None}

        frames: list = []
        for fraction in points:
            if self._stop.is_set():
                break
            image = media.extract_video_frame(source, duration * fraction,
                                              max_side=self.KEYFRAME_SIDE)
            if image is None:
                # A clip whose container has no index fails at every
                # offset, not just this one. Trying the other four costs
                # four more decoder start-ups and four more complaints
                # for a file that was never going to answer.
                if not frames:
                    break
                continue
            frames.append(image)
        return {"fields": fields, "frames": frames or None}

    def _tag_one_video(self, conn, root: str, row, points, *,
                       read: dict[str, Any] | None = None) -> None:
        if read is None:
            read = self._read_keyframes(root, row, points)
        if self._stop.is_set():
            # Part-read because the scan is stopping: left for the next scan
            # rather than described from half its moments, or stamped done.
            for image in read["frames"] or ():
                image.close()
            return
        if read["fields"]:
            db.update_asset(conn, int(row["id"]), **read["fields"])
        images = read["frames"]
        if not images:
            db.update_asset(conn, int(row["id"]),
                            keyframe_version=KEYFRAME_VERSION)
            return

        try:
            with tempfile.TemporaryDirectory(prefix="ninaivu-keyframes-") as work:
                frames: list[Path] = []
                for index, image in enumerate(images):
                    path = Path(work) / f"{index}.jpg"
                    image.save(path, "JPEG", quality=88)
                    frames.append(path)
                results = self.ai.analyse(frames)
        finally:
            for image in images:
                image.close()

        fields = self._merge_keyframes(results)
        fields["keyframe_version"] = KEYFRAME_VERSION
        embedding = fields.pop("embedding", None)
        # As in _tag_batch: an admin's own tags, caption and flag are kept.
        db.store_ai_fields(conn, int(row["id"]), **fields)
        if embedding is not None:
            db.store_embedding(conn, int(row["id"]), self.ai.model_id,
                               len(embedding) // 4, embedding)

    def _merge_keyframes(self, results: list[dict]) -> dict[str, Any]:
        """Fold several frames' verdicts into one description of the clip.

        Tags are unioned and ordered by how many moments agreed, so a subject
        that persists outranks one that flickered past in a single frame.

        The explicit-content score is the *worst* moment, not the average. A
        clip that is unobjectionable for thirty seconds and not for one is
        still a clip a household would want caught, and an average would bury
        exactly that.
        """
        counts: dict[str, int] = {}
        order: list[str] = []
        worst = None
        caption = None
        embedding = None

        for index, result in enumerate(results):
            for tag in result.get("tags") or []:
                if tag not in counts:
                    order.append(tag)
                counts[tag] = counts.get(tag, 0) + 1
            score = result.get("nsfw_score")
            if score is not None:
                worst = score if worst is None else max(worst, score)
            # The middle of a clip represents it better than either end, so
            # its caption and its vector are the ones kept for search.
            if index == len(results) // 2:
                caption = result.get("caption") or caption
                embedding = result.get("embedding")

        ranked = sorted(order, key=lambda t: (-counts[t], order.index(t)))
        fields: dict[str, Any] = {"ai_version": AI_VERSION}
        if ranked:
            fields["tags"] = ranked[:self.cfg.max_tags]
        if caption:
            fields["caption"] = caption
        if self.cfg.nsfw_filter and worst is not None:
            fields["nsfw_score"] = worst
            fields["nsfw"] = int(worst >= self._nsfw_threshold())
        if embedding is not None:
            fields["embedding"] = embedding
        return fields

    # -- faces ------------------------------------------------------------
    def _find_faces(self, conn, root: str) -> None:
        """Detect faces, then regroup.

        Deliberately the last thing a scan does. It is the slowest pass and
        the least urgent — the gallery is complete and browsable before it
        starts, and stopping the scan half way through leaves the library
        perfectly usable with some faces not yet found. ``face_version``
        records how far it got, so the next scan resumes rather than restarts.
        """
        indexer = self._face_indexer()
        if indexer is None or not indexer.engine.available:
            return
        total_hint = db.count_assets_needing_faces(conn, root, faces_mod.FACE_VERSION)
        owed_key = f"faces_ungrouped:{root}"
        if not total_hint:
            # A pass stopped after its last photograph found faces and never
            # grouped them; with nothing left to look at, the next scan
            # returned here and the people never appeared.
            if db.get_meta(conn, owed_key) == "1" and not self._stop.is_set():
                indexer.regroup(conn, root)
                db.set_meta(conn, owed_key, "0")
                conn.commit()
                self._notify({"phase": "faces-grouped"})
            return
        db.set_meta(conn, owed_key, "1")
        conn.commit()

        self.progress.update(
            status="faces", tag_total=total_hint, tagged=0,
            message=f"Looking for faces in {total_hint:,} photographs…")
        faces_started = time.time()
        log.info("looking for faces in %s photographs", f"{total_hint:,}")

        def on_progress(done: int, total: int) -> None:
            self.progress.update(tagged=done, tag_total=total)
            self._notify({"phase": "faces"})

        try:
            # Stops for the console's switch as well as for a stopped scan. The
            # face pass is most of a day on a large library, and progress is
            # recorded per photograph, so stopping part-way loses nothing — the
            # next scan with it on carries on from where this one left off.
            result = indexer.detect_pass(
                conn, root,
                should_stop=lambda: (self._stop.is_set()
                                     or not getattr(self.cfg, "faces_enabled", False)
                                     or not self._take_turn("analysis")),
                on_progress=on_progress)
            if not self._stop.is_set():
                indexer.regroup(conn, root)
                db.set_meta(conn, owed_key, "0")
                conn.commit()
                self._notify({"phase": "faces-grouped"})
            log.info("face pass over %s photographs took %s: %s",
                     f"{total_hint:,}", took(faces_started), result)
        except Exception:  # noqa: BLE001
            # A face pass that fails must never fail the scan. The library is
            # already indexed by this point; faces are an addition to it.
            log.exception("the face pass over %s failed", root)
            self.progress.bump(errors=1)

    def _face_indexer(self):
        indexer = getattr(self, "_faces", None)
        if indexer is None:
            try:
                from .faceindex import FaceIndexer  # noqa: PLC0415
                indexer = FaceIndexer(self.cfg)
            except Exception as exc:  # noqa: BLE001
                log.warning("faces cannot be found on this machine: %s", exc)
                return None
            self._faces = indexer
        return indexer

    # -- watcher ----------------------------------------------------------
    def _start_watch(self, root: Path) -> None:
        if not self.cfg.watch:
            return
        with self._watch_lock:
            if str(root) in self._observers:
                return
            self._start_watch_locked(root)

    def _start_watch_locked(self, root: Path) -> None:
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler
        except Exception:
            return

        scanner = self

        class Handler(FileSystemEventHandler):
            def on_any_event(self, event) -> None:  # noqa: ANN001
                if event.is_directory or not _worth_a_rescan(event):
                    return
                paths = (getattr(event, "src_path", ""),
                         getattr(event, "dest_path", ""))
                if any(p and media_kind(p) != "unknown" for p in paths):
                    scanner._schedule_rescan(root)

        try:
            observer = Observer()
            observer.schedule(Handler(), str(root), recursive=True)
            observer.daemon = True
            observer.start()
            observer.ninaivu_started = time.monotonic()
            self._observers[str(root)] = observer
        except Exception as exc:
            # Not fatal, but not nothing: a library that quietly fails on a
            # thousand photographs looks exactly like one that read them.
            log.warning("not watching %s for new files; they appear after the next scan: %s", root, exc)

    def _schedule_rescan(self, root: Path, delay: float | None = None) -> None:
        """Start the quiet period again after a change the watcher noticed."""
        with self._timer_lock:
            waiting = self._watch_timers.pop(str(root), None)
            if waiting is not None:
                waiting.cancel()
            timer = threading.Timer(self.cfg.watch_debounce if delay is None else delay,
                                    lambda: self._rescan_quiet(root))
            timer.daemon = True
            self._watch_timers[str(root)] = timer
            timer.start()

    def _cancel_rescans(self) -> None:
        """Drop the quiet-period timers that have not fired yet.

        A timer outlives the watcher that scheduled it, and a couple of seconds
        later starts exactly the scan that stopping the watcher was there to
        prevent. That is how a consolidation came to share the disk — and the
        processor — with the indexer it had just stood down: the archive's own
        writes into the library had armed a timer, and it fired one second after
        the deferral.
        """
        with self._timer_lock:
            for timer in self._watch_timers.values():
                timer.cancel()
            self._watch_timers.clear()

    def _stop_watch(self) -> None:
        """Stop every watcher, and wait for each to finish stopping.

        On macOS a watcher is an FSEvents stream run by a native thread, and
        a stop could take the whole process down (a segmentation fault in
        watchdog's ``on_thread_stop``) when it came while that thread was
        still starting, or while another thread was starting a watcher for
        the same folder. So: one lock for starting and stopping; a watcher
        younger than WATCH_SETTLE_SECONDS is given that long before it is
        stopped; and each is joined, so nothing is still tearing down after
        the scanner says it has stopped.
        """
        self._cancel_rescans()
        with self._watch_lock:
            observers, self._observers = self._observers, {}
            for observer in observers.values():
                started = getattr(observer, "ninaivu_started", None)
                if started is not None:
                    young = WATCH_SETTLE_SECONDS - (time.monotonic() - started)
                    if young > 0:
                        time.sleep(young)
                try:
                    observer.stop()
                except Exception as exc:
                    # Not fatal, but not nothing: a library that quietly fails on a
                    # thousand photographs looks exactly like one that read them.
                    log.debug("%s: %s", __name__, exc)
            for observer in observers.values():
                if observer is threading.current_thread():
                    continue
                try:
                    observer.join(timeout=STOP_WAIT_SECONDS)
                except Exception as exc:                     # noqa: BLE001
                    log.debug("%s: %s", __name__, exc)

    def _rescan_quiet(self, root: Path) -> None:
        with self._lock:
            # Under the lock the thread is started with, as in start(): a
            # defer() between the check and the start would otherwise find
            # nothing to stop, and this rescan would run on regardless.
            held_by = self.deferred
            if held_by:
                # Something else has the disk, and this is the watcher noticing
                # *its* writes — a consolidation copying into the library.
                # Indexing them now would put the indexer back on the drive
                # being written to and the processor being hashed on, and
                # nothing would stand it down again: whatever deferred it has
                # already done so once. Queue it the way :meth:`start` does, so
                # the work happens when the disk is free.
                queued = (self._defer_pending is not None
                          and root in self._defer_pending[0])
                self._defer_pending = self._merge_pending([root], False)
                self._queued_requests += 1
                # Said once. A consolidation writes all day, and this line
                # every few seconds was most of the log while one ran.
                (log.debug if queued else log.info)(
                    "the watcher saw changes under %s while %s — queued rather "
                    "than started", root, held_by)
                return
            if self.running:
                # Not dropped: the walk may already be past the folder that
                # changed. Queued behind the running scan, as start() does —
                # and an analysis waiting for its turn (all day, in overnight
                # mode) is stood down for it: it used to be asked again every
                # half minute until the analysis ended, so files copied in
                # during the day were not indexed until the night was over.
                queued = self._after_stop
                self._after_stop = (
                    (queued[0] + [root] if root not in queued[0] else queued[0], queued[1])
                    if queued else ([root], False))
                self._queued_requests += 1
                status = self.progress.snapshot().get("status")
                if status not in HAND_OVER_AFTER:
                    self._stop.set()
                return
            thread = threading.Thread(
                target=self._run_all, args=([root], False),
                name="mv-rescan", daemon=True,
            )
            self._stop.clear()
            self._current = ([root], False)
            self._thread = thread
            thread.start()

    def _merge_pending(self, roots: list[Path], full: bool
                       ) -> tuple[list[Path], bool]:
        """Fold another request into the queued one. Called holding ``_lock``."""
        if self._defer_pending is None:
            return list(roots), full
        queued, queued_full = self._defer_pending
        merged = list(queued)
        merged += [r for r in roots if r not in merged]
        return merged, queued_full or full

    # -- listeners --------------------------------------------------------
    def add_listener(self, fn: Callable[[dict[str, Any]], None]) -> None:
        self._listeners.append(fn)

    def remove_listener(self, fn: Callable[[dict[str, Any]], None]) -> None:
        if fn in self._listeners:
            self._listeners.remove(fn)

    def _notify(self, payload: dict[str, Any]) -> None:
        """Tell the listeners where the scan has got to.

        A scan that has been asked to stop has nothing to announce. It used
        to: `stop()` sets an event, and a walk between two batches carries on
        to the end of whatever it was doing and announces that — so a scanner
        somebody had already stopped went on pushing phases into an event
        stream that had outlived it. That is how a progress-stream test came
        to fail in a full suite and pass on its own: a previous test's
        scanner, stopped but not silent.

        `done` is the exception, and deliberately: a scan that finished is
        allowed to say so even if the stop arrived while it was finishing.
        """
        if self._stop.is_set() and payload.get("phase") != "done":
            return
        for fn in list(self._listeners):
            try:
                fn({**payload, **self.progress.snapshot()})
            except Exception as exc:
                # Not fatal, but not nothing: a library that quietly fails on a
                # thousand photographs looks exactly like one that read them.
                log.debug("%s: %s", __name__, exc)
