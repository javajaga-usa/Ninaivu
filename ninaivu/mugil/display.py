"""On the Primary: a display copy of each original the Secondary keeps.

With two computers the Mac is to work from light copies, and send for an
original only when someone needs the original itself. This makes those
copies. A display copy is the original made smaller and nothing else:

* a camera JPEG, a PNG or a RAW becomes an AVIF no larger than
  :data:`PHOTO_EDGE` pixels on its long side;
* a video other than an iPhone's own becomes HEVC in MP4, no larger than
  :data:`VIDEO_LINES` lines;
* an iPhone's HEIC photo or video, and every kind of file not listed, is
  already efficient and gets none: the original is its own display copy.

**Only the size and the format change** (the household's rule). The EXIF of
a photograph goes into its copy byte for byte, with its colour profile and
XMP; the orientation tag is kept and the pixels are not turned. A video's
container and stream tags (when and where it was filmed, the camera) and its
rotation go into the copy as they were. Each copy is given its original's
modified time. A copy is checked before it is kept: it has to open, carry the
same metadata, run as long, and be smaller; one that is not is thrown away
and the original stays its own display copy.

Copies are made only of originals the Secondary has confirmed
(``mugil-sent.db``), so a display copy never stands for a file that exists
in one place only. They live in the state folder under ``display/``, named
by the original's fingerprint, so a file that moves keeps its copy. They are
a cache: nothing in the library is touched, and deleting the folder costs
only the time to make them again. Nothing here frees space either; that is a
later step, and only on the household's word.

One file at a time, at a low priority, making way for the household like the
copying does.
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import PRIMARY, pairing, role
from .courier import SENT, sent_record

log = logging.getLogger(__name__)

#: The longest side of a photograph's display copy, in pixels.
PHOTO_EDGE = 2560
#: AVIF quality: high enough that the copy looks the same on a large screen.
AVIF_QUALITY = 80
#: The most lines of picture a video's display copy keeps (its short side).
VIDEO_LINES = 1080
#: libx265's constant quality, and the Mac video engine's quality, for 1080p.
X265_CRF = 24
VIDEOTOOLBOX_QUALITY = 62

#: The folder in the state folder, and the record beside it.
FOLDER = "display"
LEDGER = "mugil-display.db"

#: How often the worker looks for originals without a display copy.
LOOK_EVERY = 10 * 60.0
#: How many it takes at a time.
BATCH = 50

#: A video longer than this many times its own length to encode is given up.
TIME_ALLOWANCE = 8
MIN_SECONDS = 10 * 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS copies (
    digest        TEXT PRIMARY KEY,
    algo          TEXT NOT NULL,
    kind          TEXT NOT NULL,
    file          TEXT,
    size          INTEGER NOT NULL DEFAULT 0,
    original_size INTEGER NOT NULL,
    outcome       TEXT NOT NULL,
    made_at       REAL NOT NULL
);
"""

#: What became of an original: a copy was made, it needs none, the copy came
#: out no smaller, or it could not be made (the log says why).
OUTCOMES = ("made", "kept", "larger", "failed")

PHOTO_EXTS = {"jpg", "jpeg", "jfif", "png"}
#: Tags ffmpeg writes or rewrites itself; any other tag must come through.
_OWN_TAGS = {"encoder", "major_brand", "minor_version", "compatible_brands",
             "handler_name", "vendor_id", "duration", "language"}


def plan(ext: str, kind: str, camera: str | None) -> str:
    """``photo``, ``video`` or ``keep`` for a file, from what the index has."""
    from ..storage.space import classify                        # noqa: PLC0415
    found = classify(ext, kind, camera)
    if found in ("jpeg", "png", "raw"):
        return "photo"
    if found in ("video", "legacy_video"):
        return "video"
    return "keep"


def folder(state_dir: str | Path) -> Path:
    return Path(state_dir) / FOLDER


def copy_path(state_dir: str | Path, digest: str, how: str) -> Path:
    suffix = ".avif" if how == "photo" else ".mp4"
    return folder(state_dir) / digest[:2] / f"{digest}{suffix}"


@contextlib.contextmanager
def ledger(state_dir: str | Path):
    conn = sqlite3.connect(Path(state_dir) / LEDGER, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def find(state_dir: str | Path, digest: str) -> Path | None:
    """The display copy of the original with *digest*, when there is one."""
    if not (Path(state_dir) / LEDGER).exists():
        return None
    with ledger(state_dir) as conn:
        row = conn.execute("SELECT file FROM copies WHERE digest=? AND outcome='made'",
                           (digest,)).fetchone()
    if not row or not row["file"]:
        return None
    path = folder(state_dir) / row["file"]
    return path if path.is_file() else None


def summary(state_dir: str | Path) -> dict[str, Any]:
    out: dict[str, Any] = {o: 0 for o in OUTCOMES}
    out.update({"bytes": 0, "original_bytes": 0})
    if not (Path(state_dir) / LEDGER).exists():
        return out
    with ledger(state_dir) as conn:
        for row in conn.execute("SELECT outcome, COUNT(*) AS n, COALESCE(SUM(size), 0) AS b, "
                                "COALESCE(SUM(original_size), 0) AS o FROM copies "
                                "GROUP BY outcome"):
            out[row["outcome"]] = row["n"]
            if row["outcome"] == "made":
                out["bytes"] = row["b"]
                out["original_bytes"] = row["o"]
    return out


# -- a photograph -------------------------------------------------------------

class NotMade(Exception):
    """A copy that was not kept; ``outcome`` is ``larger`` or ``failed``."""

    def __init__(self, outcome: str, why: str) -> None:
        super().__init__(why)
        self.outcome = outcome


def _picture_and_metadata(source: Path):
    from PIL import Image                                        # noqa: PLC0415
    from ..media import media                                    # noqa: PLC0415
    if media.is_raw(source):
        picture = media.open_raw(source)
        if picture is None:
            raise NotMade("failed", "the RAW could not be developed")
        # The camera's own preview carries the frame's EXIF.
        preview = media.embedded_jpeg(source)
        info = Image.open(io.BytesIO(preview)).info if preview else {}
        return picture, info
    picture = Image.open(source)
    picture.load()
    return picture, dict(picture.info)


def make_photo(source: Path, target: Path) -> int:
    """Write *source* as an AVIF display copy at *target*; its size."""
    from PIL import Image                                        # noqa: PLC0415
    picture, info = _picture_and_metadata(source)
    keep = {k: info[k] for k in ("exif", "icc_profile", "xmp") if info.get(k)}
    if picture.mode not in ("RGB", "RGBA", "L", "LA"):
        picture = picture.convert("RGBA" if "A" in picture.getbands() else "RGB")
    picture = picture.copy()
    # Smaller, never turned: the orientation tag in the EXIF still says how.
    picture.thumbnail((PHOTO_EDGE, PHOTO_EDGE), Image.Resampling.LANCZOS)
    partial = target.with_name(target.name + ".partial")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        picture.save(partial, "AVIF", quality=AVIF_QUALITY, **keep)
        with Image.open(partial) as back:
            back.load()
            for key, value in keep.items():
                if back.info.get(key) != value:
                    raise NotMade("failed", f"the copy lost the original's {key}")
        _finish(partial, target, source)
    finally:
        partial.unlink(missing_ok=True)
    return target.stat().st_size


# -- a video ------------------------------------------------------------------

_hevc: str | bool | None = False


def hevc_encoder() -> str | None:
    """The HEVC encoder this ffmpeg has: the Mac's video engine, else libx265."""
    global _hevc
    if _hevc is False:
        from ..media import media                                # noqa: PLC0415
        _hevc = None
        if media.FFMPEG:
            try:
                listing = subprocess.run([media.FFMPEG, "-hide_banner", "-v", "quiet", "-encoders"],
                                         capture_output=True, timeout=20,
                                         check=False).stdout.decode("utf-8", "replace")
            except (OSError, subprocess.SubprocessError):
                listing = ""
            if sys.platform == "darwin" and " hevc_videotoolbox " in listing:
                _hevc = "hevc_videotoolbox"
            elif " libx265 " in listing:
                _hevc = "libx265"
    return _hevc or None


def video_command(ffmpeg: str, source: Path, target: Path, encoder: str, *,
                  lines: int | None = VIDEO_LINES, audio: bool = True) -> list[str]:
    """The ffmpeg command for an HEVC copy of *source*: its short side at most
    *lines*, or the picture's own size with ``lines=None`` (the smaller-videos
    trial, media/video_trial.py). *audio* says it has sound: ffmpeg refuses
    to copy the tags of a sound stream that is not there."""
    from ..media.media import LOCAL_ONLY                         # noqa: PLC0415
    # The short side at most *lines*, the picture as it is stored: with
    # -noautorotate the rotation goes into the copy as the original has it.
    # Even sizes either way: HEVC in 4:2:0 needs them.
    scale = ("scale='trunc(iw/2)*2':'trunc(ih/2)*2'" if lines is None else
             f"scale='if(gte(iw,ih),-2,min({lines},iw))'"
             f":'if(gte(iw,ih),min({lines},ih),-2)'")
    quality = (["-q:v", str(VIDEOTOOLBOX_QUALITY)] if encoder == "hevc_videotoolbox"
               else ["-crf", str(X265_CRF), "-preset", "medium", "-x265-params", "log-level=error"])
    sound = ["-map_metadata:s:a", "0:s:a"] if audio else []
    return [ffmpeg, "-v", "error", "-nostdin", "-y", "-noautorotate", *LOCAL_ONLY,
            "-i", str(source), "-map", "0:v:0", "-map", "0:a?",
            "-map_metadata", "0", "-map_metadata:s:v", "0:s:v:0", *sound,
            "-vf", scale, "-c:v", encoder, *quality, "-tag:v", "hvc1", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k",
            "-movflags", "+faststart+use_metadata_tags", "-f", "mp4", str(target)]


def _probe(path: Path) -> dict[str, Any]:
    from ..media import media                                    # noqa: PLC0415
    return media.ffprobe_info(path) or {}


def _tags(probe: dict[str, Any]) -> dict[str, str]:
    return {k.lower(): _once(str(v))
            for k, v in ((probe.get("format") or {}).get("tags") or {}).items()
            if k.lower() not in _OWN_TAGS}


def _once(value: str) -> str:
    """A tag said twice, as one. FFmpeg 9 writes the date a video was filmed
    both in the MP4's own header and among the ``use_metadata_tags`` keys,
    and ffprobe reads back ``2015-06-01T10:00:00Z;2015-06-01T10:00:00Z``: the
    same date, kept. Taken for a changed tag, Compress made a second copy of
    a video it had already done and Replace encoded it all again. Different
    values joined by ``;`` are left as they are, so a changed tag still is."""
    parts = value.split(";")
    return parts[0] if len(parts) > 1 and len(set(parts)) == 1 else value


def _rotation(probe: dict[str, Any]) -> int:
    for stream in probe.get("streams") or []:
        if stream.get("codec_type") != "video":
            continue
        for side in stream.get("side_data_list") or []:
            if "rotation" in side:
                return int(float(side["rotation"])) % 360
        rotate = (stream.get("tags") or {}).get("rotate")
        return int(float(rotate)) % 360 if rotate else 0
    return 0


def has_sound(probe: dict[str, Any]) -> bool:
    """Whether ffprobe saw a sound stream; yes when it saw no streams at all."""
    streams = probe.get("streams") or []
    return not streams or any(s.get("codec_type") == "audio" for s in streams)


def _duration(probe: dict[str, Any]) -> float:
    try:
        return float((probe.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        return 0.0


def make_video(source: Path, target: Path) -> int:
    """Write *source* as an HEVC display copy at *target*; its size."""
    from ..media import media                                    # noqa: PLC0415
    encoder = hevc_encoder()
    if not media.FFMPEG or not encoder:
        raise NotMade("failed", "this computer has no HEVC encoder")
    before = _probe(source)
    length = _duration(before)
    partial = target.with_name(target.name + ".partial")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        def lower():
            if hasattr(os, "nice"):
                with contextlib.suppress(OSError):
                    os.nice(10)
        done = subprocess.run(video_command(media.FFMPEG, source, partial, encoder,
                                            audio=has_sound(before)),
                              capture_output=True, check=False,
                              timeout=max(MIN_SECONDS, length * TIME_ALLOWANCE),
                              preexec_fn=lower if os.name == "posix" else None)
        if done.returncode != 0 or not partial.is_file():
            said = done.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or ["ffmpeg"]
            raise NotMade("failed", said[0][:200])
        after = _probe(partial)
        if length and abs(_duration(after) - length) > max(1.0, length * 0.01):
            raise NotMade("failed", "the copy does not run as long as the original")
        if _tags(after) != _tags(before):
            raise NotMade("failed", "the copy lost some of the original's tags")
        if _rotation(after) != _rotation(before):
            raise NotMade("failed", "the copy lost the original's rotation")
        _finish(partial, target, source)
    except subprocess.TimeoutExpired:
        raise NotMade("failed", "the copy took too long") from None
    finally:
        partial.unlink(missing_ok=True)
    return target.stat().st_size


def _finish(partial: Path, target: Path, source: Path) -> None:
    if partial.stat().st_size >= source.stat().st_size:
        raise NotMade("larger", "the copy came out no smaller than the original")
    stamp = source.stat()
    os.utime(partial, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    os.replace(partial, target)


def make(state_dir: str | Path, source: Path, *, how: str, algo: str, digest: str) -> str:
    """Make and record the display copy of *source*; the outcome."""
    size = source.stat().st_size
    file: str | None = None
    made = 0
    if how == "keep":
        outcome = "kept"
    else:
        target = copy_path(state_dir, digest, how)
        try:
            made = (make_photo if how == "photo" else make_video)(source, target)
            outcome = "made"
            file = target.relative_to(folder(state_dir)).as_posix()
        except NotMade as exc:
            outcome = exc.outcome
            log.info("No display copy of %s: %s", source, exc)
        except Exception as exc:                                 # noqa: BLE001 - one file
            outcome = "failed"
            log.warning("Could not make a display copy of %s: %s", source, exc)
    with ledger(state_dir) as conn:
        conn.execute("INSERT OR REPLACE INTO copies (digest, algo, kind, file, size, "
                     "original_size, outcome, made_at) VALUES (?,?,?,?,?,?,?,?)",
                     (digest, algo, how, file, made, size, outcome, time.time()))
    return outcome


# -- the worker -----------------------------------------------------------------

class DisplayCopies:
    """Makes display copies by itself while this computer is a paired Primary."""

    def __init__(self, cfg, connect_db: Callable[[], sqlite3.Connection], *,
                 hold: Callable[[], str | None] | None = None) -> None:
        self.cfg = cfg
        self.connect_db = connect_db
        self.hold = hold or (lambda: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.doing: dict[str, Any] = {}
        #: Originals passed over in this round (gone, or changed since sent).
        self._passed: set[str] = set()

    def wanted(self) -> bool:
        return role(self.cfg) == PRIMARY and pairing.load(self.cfg.state_dir) is not None

    def keep(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._keep, name="mugil-display", daemon=True)
        self._thread.start()

    def stop(self, join: bool = False) -> None:
        self._stop.set()
        if join and self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=10)
        self._thread = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            doing = dict(self.doing)
        return {**summary(self.cfg.state_dir), "doing": doing, "wanted": self.wanted(),
                "owed": self.owed_count()}

    def _keep(self) -> None:
        while not self._stop.is_set():
            self._passed = set()
            try:
                while self.wanted() and not self._stop.is_set() and self.run_once():
                    pass
            except Exception:                                    # noqa: BLE001 - keep looking
                log.exception("Display copies stopped")
            self._stop.wait(LOOK_EVERY)

    def owed(self, limit: int = BATCH) -> list[dict[str, Any]]:
        """Originals the Secondary confirmed that have no display copy yet."""
        state = Path(self.cfg.state_dir)
        if not (state / SENT).exists():
            return []
        with sent_record(state) as conn:
            conn.execute("ATTACH DATABASE ? AS display", (str(state / LEDGER),))
            conn.executescript(_SCHEMA.replace("copies", "display.copies"))
            rows = [dict(r) for r in conn.execute(
                "SELECT s.root, s.rel_path, s.size, s.mtime, s.algo, s.digest FROM sent s "
                "LEFT JOIN display.copies c ON c.digest=s.digest WHERE c.digest IS NULL "
                "ORDER BY s.sent_at LIMIT ?", (int(limit) + len(self._passed),))]
            conn.commit()
            conn.execute("DETACH DATABASE display")
        return [r for r in rows if r["digest"] not in self._passed][:limit]

    def owed_count(self) -> int:
        state = Path(self.cfg.state_dir)
        if not (state / SENT).exists():
            return 0
        with sent_record(state) as conn:
            conn.execute("ATTACH DATABASE ? AS display", (str(state / LEDGER),))
            conn.executescript(_SCHEMA.replace("copies", "display.copies"))
            n = conn.execute("SELECT COUNT(*) FROM sent s LEFT JOIN display.copies c "
                             "ON c.digest=s.digest WHERE c.digest IS NULL").fetchone()[0]
            conn.commit()
            conn.execute("DETACH DATABASE display")
        return int(n)

    def run_once(self) -> int:
        """Make the copies of one batch; how many originals it went through."""
        rows = self.owed()
        if not rows:
            return 0
        conn = self.connect_db()
        try:
            for row in rows:
                if self._stop.is_set():
                    break
                while self.hold() and not self._stop.wait(30):
                    pass
                if self._stop.is_set():
                    break
                found = conn.execute("SELECT ext, kind, camera FROM assets WHERE root=? "
                                     "AND rel_path=? AND trashed=0",
                                     (row["root"], row["rel_path"])).fetchone()
                source = Path(row["root"]) / row["rel_path"]
                try:
                    unchanged = source.stat().st_size == row["size"]
                except OSError:
                    unchanged = False
                if found is None or not unchanged:
                    # Gone, or changed since it was sent: the next copy run
                    # sends it again, and its display copy follows that.
                    self._passed.add(row["digest"])
                    continue
                how = plan(found["ext"], found["kind"], found["camera"])
                with self._lock:
                    self.doing = {"path": row["rel_path"], "how": how, "since": time.time()}
                make(self.cfg.state_dir, source, how=how, algo=row["algo"], digest=row["digest"])
        finally:
            conn.close()
            with self._lock:
                self.doing = {}
        return len(rows)


__all__ = ["DisplayCopies", "NotMade", "plan", "make", "make_photo", "make_video", "find",
           "summary", "copy_path", "PHOTO_EDGE", "VIDEO_LINES"]
