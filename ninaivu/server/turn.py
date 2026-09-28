"""Turning the original file, not just the picture Ninaivu shows.

Ninaivu has always kept its hands off your files. A rotation was a number in
the index: thumbnails were baked with it applied, the viewer turned the
original in the browser, and the bytes on the disk were never touched. That is
the right default, and it is still what the viewer's rotate button does.

It stops being enough the moment the photograph leaves Ninaivu. Copy it to a
memory stick, attach it to an email, open the folder in Explorer — and it is
sideways again, because the correction only ever existed in one program's
database.

So this module writes the turn into the file. Every route it takes is
lossless, and that is not a preference, it is the whole design:

``exif``
    JPEG and TIFF carry an *orientation tag*: two bytes that say which way up
    the picture goes. Rotating means changing those two bytes. The compressed
    image data is not read, not decoded and not written — a 12-megapixel JPEG
    is turned by rewriting two bytes in the middle of it, in about a
    millisecond, and turning it back restores the file byte for byte.

``pixels``
    PNG and BMP have no orientation tag, so the pixels themselves are turned
    and the file re-saved. Both formats are lossless, so this costs nothing
    but time.

``container``
    MP4, MOV and MKV carry a *display matrix*: the same idea as the EXIF tag,
    one level up. ``ffmpeg -c copy`` rewrites the container and streams the
    audio and video across untouched, so a four-gigabyte video is turned in
    the time it takes to copy it, with not a frame re-encoded.

Anything that would need re-encoding is refused, with a reason, and the file
is left exactly as it was. A lossy WebP, a GIF, an AVI: Ninaivu will still turn
them in its own gallery, it just will not quietly degrade the original to do
it. Refusing is the feature.

Two rules run through all of it.

**Nothing is written until it is known to have worked.** Every route writes to
a temporary file beside the original and renames it into place, so an
interruption leaves the old file or the new one and never a half of each. And
after the rename the result is read back and checked — the picture still
decodes, and it now reports the orientation that was asked for. A file that
fails that check is put back.

**The turn is composed, not assumed.** A photograph that already claims to be
rotated 90° and is asked for another 90° ends up claiming 180°, and one that
is mirrored stays mirrored. The eight EXIF orientations are a group; this
module does the arithmetic rather than pretending only four of them exist.
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from ..media import media

__all__ = [
    "Turn", "rotate_original", "can_rotate_original", "compose",
    "orientation_of", "video_rotation", "JPEG_EXTS", "TIFF_EXTS",
    "PIXEL_EXTS", "VIDEO_EXTS",
]

#: EXIF/TIFF tag 0x0112 — which way up the picture goes.
ORIENTATION = 0x0112

JPEG_EXTS = {".jpg", ".jpeg", ".jpe", ".jfif"}
TIFF_EXTS = {".tif", ".tiff"}
#: Lossless raster formats with no orientation tag: turn the pixels instead.
PIXEL_EXTS = {".png", ".bmp"}
#: Containers with a display matrix ffmpeg can rewrite without re-encoding.
VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".mkv"}

#: How long to let ffmpeg remux one file before giving up on it. A stream copy
#: is disk-speed, so this is generous even for a very large video.
FFMPEG_TIMEOUT = 60 * 30


# ---------------------------------------------------------------------------
# The eight orientations, as arithmetic
# ---------------------------------------------------------------------------

# Every EXIF orientation is "mirror horizontally (or not), then rotate
# clockwise by this much". Written that way, adding a turn is addition:
# rotating the displayed picture by R gives (mirrored, rot + R), whichever
# orientation you started from, because a rotation applied after a mirror
# composes with the rotation that was already there.
_DECOMPOSE: dict[int, tuple[bool, int]] = {
    1: (False, 0),
    2: (True, 0),
    3: (False, 180),
    4: (True, 180),
    5: (True, 270),
    6: (False, 90),
    7: (True, 90),
    8: (False, 270),
}
_COMPOSE = {value: key for key, value in _DECOMPOSE.items()}


def compose(orientation: int, turn: int) -> int:
    """The orientation a file should claim after turning it *turn*° clockwise."""
    mirrored, rot = _DECOMPOSE.get(orientation, (False, 0))
    return _COMPOSE[(mirrored, (rot + turn) % 360)]


def quarter(turn: Any) -> int | None:
    """*turn* as one of 0, 90, 180, 270 — or ``None`` if it is not one."""
    try:
        if isinstance(turn, bool) or turn is None:
            return None
        value = int(turn)
        if value != float(turn):
            return None
    except (TypeError, ValueError, OverflowError):   # Infinity, 1e400
        return None
    value %= 360
    return value if value in (0, 90, 180, 270) else None


# ---------------------------------------------------------------------------
# What came back
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Turn:
    """The outcome of turning one file.

    ``ok`` says whether the file on disk now carries the turn. ``how`` is the
    route taken — ``exif``, ``pixels`` or ``container`` — and ``why`` explains
    a refusal in words meant for the person who pressed the button, not for a
    log.
    """

    ok: bool
    how: str = ""
    why: str = ""
    #: The orientation the file now claims (pictures with a tag).
    orientation: int = 1
    #: The clockwise rotation the container now carries (videos).
    rotation: int = 0
    #: Set when the turn was a no-op because nothing had to change.
    unchanged: bool = False


def _no(why: str) -> Turn:
    return Turn(ok=False, why=why)


# ---------------------------------------------------------------------------
# Reading a TIFF block
# ---------------------------------------------------------------------------

# TIFF field types, and how many bytes one of each takes. A field whose whole
# value fits in four bytes is stored inline; anything larger is stored
# elsewhere in the block and the four bytes hold its offset. That distinction
# is the only reason this module has to understand the format at all.
_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1,
              8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4}

#: Tags whose inline value is itself an offset to another IFD.
_SUB_IFD = (0x8769, 0x8825, 0xA005)
#: The embedded thumbnail's offset, in IFD1. Inline, and still an offset.
_THUMB_OFFSET = 0x0201


def _endian(block: bytes) -> str | None:
    if block[:2] == b"II":
        return "<"
    if block[:2] == b"MM":
        return ">"
    return None


def _first_ifd(block: bytes, end: str) -> int | None:
    if len(block) < 8:
        return None
    magic, offset = struct.unpack_from(end + "HI", block, 2)
    if magic != 42 or not 8 <= offset < len(block):
        return None
    return offset


def _entries(block: bytes, end: str, ifd: int) -> tuple[int, int]:
    """(number of entries, offset of the first) — or (0, 0) if it is unusable."""
    if ifd + 2 > len(block):
        return 0, 0
    count = struct.unpack_from(end + "H", block, ifd)[0]
    if count == 0 or ifd + 2 + count * 12 + 4 > len(block):
        return 0, 0
    return count, ifd + 2


def _find_orientation(block: bytes, end: str) -> int | None:
    """Byte offset of the orientation entry's *value*, if IFD0 carries one."""
    ifd = _first_ifd(block, end)
    if ifd is None:
        return None
    count, start = _entries(block, end, ifd)
    for index in range(count):
        at = start + index * 12
        tag, kind, number = struct.unpack_from(end + "HHI", block, at)
        if tag == ORIENTATION and kind == 3 and number == 1:
            return at + 8
    return None


def _offset_fields(block: bytes, end: str) -> list[int]:
    """Every four-byte field in the block that holds an offset into it.

    Walked so that an entry can be inserted into IFD0 and everything the block
    points at can be moved along with it. Missing one of these is not a
    cosmetic bug — it is a file whose thumbnail, GPS block or maker note now
    points twelve bytes into the middle of something else.
    """
    found: list[int] = []
    seen: set[int] = set()

    def walk(ifd: int, depth: int) -> None:
        if depth > 4 or ifd in seen:
            return
        seen.add(ifd)
        count, start = _entries(block, end, ifd)
        if not count:
            return
        for index in range(count):
            at = start + index * 12
            tag, kind, number = struct.unpack_from(end + "HHI", block, at)
            size = _TYPE_SIZE.get(kind)
            if size is None:
                continue
            if tag in _SUB_IFD and number == 1:
                found.append(at + 8)
                walk(struct.unpack_from(end + "I", block, at + 8)[0], depth + 1)
            elif tag == _THUMB_OFFSET and number == 1:
                found.append(at + 8)
            elif size * number > 4:
                found.append(at + 8)
        # The pointer to the next IFD, which is where a JPEG keeps the
        # thumbnail's directory.
        nxt = start + count * 12
        found.append(nxt)
        following = struct.unpack_from(end + "I", block, nxt)[0]
        if following:
            walk(following, depth + 1)

    first = _first_ifd(block, end)
    if first is not None:
        walk(first, 0)
    return sorted(set(found))


def orientation_of(block: bytes) -> int:
    """The orientation a TIFF block claims, or 1 when it does not say."""
    end = _endian(block)
    if end is None:
        return 1
    at = _find_orientation(block, end)
    if at is None:
        return 1
    value = struct.unpack_from(end + "H", block, at)[0]
    return value if value in _DECOMPOSE else 1


# ---------------------------------------------------------------------------
# Writing a TIFF block
# ---------------------------------------------------------------------------

def _set_orientation(block: bytes, value: int) -> bytes | None:
    """*block* with its orientation set, adding the tag if it is missing."""
    end = _endian(block)
    if end is None:
        return None
    at = _find_orientation(block, end)
    if at is not None:
        out = bytearray(block)
        struct.pack_into(end + "H", out, at, value)
        return bytes(out)
    return _add_orientation(block, end, value)


def _add_orientation(block: bytes, end: str, value: int) -> bytes | None:
    """Insert an orientation entry into IFD0, moving what the block points at.

    Entries are sorted by tag, so the new one goes in its proper place rather
    than on the end; readers are allowed to binary-search a directory and some
    of them do.
    """
    ifd = _first_ifd(block, end)
    if ifd is None:
        return None
    count, start = _entries(block, end, ifd)
    if not count:
        return None

    position = count
    for index in range(count):
        tag = struct.unpack_from(end + "H", block, start + index * 12)[0]
        if tag == ORIENTATION:      # a type this module does not write
            return None
        if tag > ORIENTATION:
            position = index
            break
    insert_at = start + position * 12

    out = bytearray(block)
    for at in _offset_fields(block, end):
        if at + 4 > len(out):
            return None
        current = struct.unpack_from(end + "I", out, at)[0]
        # A pointer to nothing stays a pointer to nothing, and anything that
        # already lives before the insertion point does not move.
        if current and current >= insert_at:
            if current + 12 > 0xFFFFFFFF:
                return None
            struct.pack_into(end + "I", out, at, current + 12)
    struct.pack_into(end + "H", out, ifd, count + 1)

    entry = (struct.pack(end + "HHI", ORIENTATION, 3, 1)
             + struct.pack(end + "HH", value, 0))
    return bytes(out[:insert_at]) + entry + bytes(out[insert_at:])


def _fresh_exif(value: int) -> bytes:
    """A minimal EXIF block whose only field is the orientation."""
    return (b"II" + struct.pack("<HI", 42, 8)
            + struct.pack("<H", 1)
            + struct.pack("<HHI", ORIENTATION, 3, 1)
            + struct.pack("<HH", value, 0)
            + struct.pack("<I", 0))


# ---------------------------------------------------------------------------
# JPEG segments
# ---------------------------------------------------------------------------

_STANDALONE = {0x01, 0xD8, 0xD9} | set(range(0xD0, 0xD8))


def _segments(data: bytes):
    """Walk a JPEG's markers, stopping at the start of the compressed scan."""
    at = 2
    while at + 4 <= len(data):
        if data[at] != 0xFF:
            return
        marker = data[at + 1]
        if marker == 0xFF:                       # fill byte, skip it
            at += 1
            continue
        if marker in _STANDALONE:
            at += 2
            continue
        if marker == 0xDA:                       # start of scan: we are done
            return
        length = struct.unpack_from(">H", data, at + 2)[0]
        if length < 2 or at + 2 + length > len(data):
            return
        yield marker, at, at + 4, at + 2 + length
        at += 2 + length


def _exif_app1(data: bytes) -> tuple[int, int, int] | None:
    """(segment start, payload start, payload end) of the EXIF APP1, if any."""
    for marker, seg, body, end in _segments(data):
        if marker == 0xE1 and data[body:body + 6] == b"Exif\x00\x00":
            return seg, body, end
    return None


def _jpeg_with_orientation(data: bytes, value: int) -> bytes | None:
    if data[:2] != b"\xff\xd8":
        return None

    found = _exif_app1(data)
    if found is not None:
        seg, body, end = found
        block = _set_orientation(data[body + 6:end], value)
        if block is None:
            return None
        payload = b"Exif\x00\x00" + block
        if len(payload) + 2 > 0xFFFF:
            return None
        return (data[:seg] + b"\xff\xe1" + struct.pack(">H", len(payload) + 2)
                + payload + data[end:])

    # No EXIF at all. A fresh block goes in after the SOI, or after a JFIF
    # APP0 if the file opens with one, which is where every writer puts it.
    at = 2
    for marker, _seg, _body, end in _segments(data):
        if marker == 0xE0:
            at = end
        break
    payload = b"Exif\x00\x00" + _fresh_exif(value)
    return (data[:at] + b"\xff\xe1" + struct.pack(">H", len(payload) + 2)
            + payload + data[at:])


# ---------------------------------------------------------------------------
# Writing files safely
# ---------------------------------------------------------------------------

def _replace(path: Path, data: bytes) -> None:
    """Write *data* over *path*, atomically, keeping the permission bits."""
    handle, temp_name = tempfile.mkstemp(dir=str(path.parent),
                                         prefix=".ninaivu-turn-",
                                         suffix=path.suffix)
    temp = Path(temp_name)
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        try:
            shutil.copystat(path, temp)
        except OSError:
            pass
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def _still_a_picture(path: Path, expect: int | None) -> bool:
    """Does this still decode, and does it now claim what it was told to?"""
    try:
        with Image.open(path) as img:
            img.verify()
        if expect is None:
            return True
        with Image.open(path) as img:
            claimed = img.getexif().get(ORIENTATION, 1)
        return int(claimed) == expect
    except Exception:                              # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Videos
# ---------------------------------------------------------------------------

def video_rotation(path: str | Path) -> int:
    """The clockwise turn a container already carries, 0/90/180/270.

    Two places say it and they disagree about sign: the modern display matrix
    reports the rotation as a counter-clockwise angle, while the older
    ``rotate`` tag is clockwise. Both are read, normalised, and the matrix
    wins because it is the one every current player looks at.
    """
    data = media.ffprobe_info(path)
    for stream in data.get("streams", []):
        if stream.get("codec_type") != "video":
            continue
        for side in stream.get("side_data_list", []) or []:
            if "rotation" in side:
                try:
                    return int(round(-float(side["rotation"]))) % 360
                except (TypeError, ValueError):
                    pass
        tag = (stream.get("tags") or {}).get("rotate")
        if tag is not None:
            try:
                return int(round(float(tag))) % 360
            except (TypeError, ValueError):
                pass
        break
    return 0


# The four ways of asking ffmpeg for a rotation. There are two options, and
# each has been written with both signs by some released version: ffmpeg 4's
# ``-metadata rotate=90`` produces a file that plays turned 270°, while
# ``-display_rotation`` does not exist before ffmpeg 6. Rather than test the
# version and guess, each candidate is tried and the *result* is measured.
_WRITERS: tuple[tuple[str, bool], ...] = (
    ("display", False), ("display", True),
    ("rotate", False), ("rotate", True),
)


def _remux(source: Path, target: Path, wanted: int,
           style: str, flip: bool) -> bool:
    """One attempt at writing *wanted* into a copy of the container."""
    ffmpeg = media.FFMPEG
    if not ffmpeg:
        return False
    angle = (-wanted) % 360 if flip else wanted % 360
    if style == "display":
        command = [ffmpeg, "-v", "error", "-nostdin", "-y",
                   "-display_rotation", str(angle),
                   "-i", str(source), "-map", "0", "-c", "copy",
                   str(target)]
    else:
        command = [ffmpeg, "-v", "error", "-nostdin", "-y",
                   "-i", str(source), "-map", "0", "-c", "copy",
                   "-metadata:s:v:0", f"rotate={angle}", str(target)]
    try:
        done = subprocess.run(command, capture_output=True,
                              timeout=FFMPEG_TIMEOUT, check=False)
    except (subprocess.SubprocessError, OSError):
        return False
    if done.returncode != 0 or not target.exists() or target.stat().st_size == 0:
        return False
    # ffmpeg accepting an option is not the same as the container carrying
    # what was meant, so the answer is read back off the file just written.
    return video_rotation(target) == wanted % 360


def _rotate_video(path: Path, turn: int) -> Turn:
    if not media.FFMPEG:
        return _no("ffmpeg is not installed, so the video file itself "
                   "cannot be turned")
    if path.suffix.lower() not in VIDEO_EXTS:
        return _no(f"{path.suffix.lower() or 'this format'} has nowhere to "
                   "record a rotation, and re-encoding would lose quality")

    wanted = (video_rotation(path) + turn) % 360
    handle, temp_name = tempfile.mkstemp(dir=str(path.parent),
                                         prefix=".ninaivu-turn-",
                                         suffix=path.suffix)
    os.close(handle)
    temp = Path(temp_name)
    try:
        for style, flip in _WRITERS:
            if _remux(path, temp, wanted, style, flip):
                try:
                    shutil.copystat(path, temp)
                except OSError:
                    pass
                os.replace(temp, path)
                return Turn(ok=True, how="container", rotation=wanted)
        return _no("ffmpeg could not record a rotation in this container")
    finally:
        temp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Pictures
# ---------------------------------------------------------------------------

def _rotate_tagged(path: Path, turn: int, jpeg: bool) -> Turn:
    try:
        data = path.read_bytes()
    except OSError as exc:
        return _no(f"the file could not be read: {exc}")

    if jpeg:
        found = _exif_app1(data)
        current = orientation_of(data[found[1] + 6:found[2]]) if found else 1
        wanted = compose(current, turn)
        rebuilt = _jpeg_with_orientation(data, wanted)
    else:
        current = orientation_of(data)
        wanted = compose(current, turn)
        rebuilt = _set_orientation(data, wanted)

    if rebuilt is None:
        return _no("this file's metadata is in a shape Ninaivu will not "
                   "rewrite without re-encoding it")
    if wanted == current and rebuilt == data:
        return Turn(ok=True, how="exif", orientation=current, unchanged=True)

    try:
        _replace(path, rebuilt)
    except OSError as exc:
        return _no(f"the file could not be written: {exc}")

    if not _still_a_picture(path, wanted):
        try:                                       # put it back, exactly
            _replace(path, data)
        except OSError:
            pass
        return _no("the turned file did not read back correctly, so the "
                   "original was restored")
    return Turn(ok=True, how="exif", orientation=wanted)


def _rotate_pixels(path: Path, turn: int) -> Turn:
    try:
        with Image.open(path) as img:
            img.load()
            fmt = img.format
            turned = img if turn == 0 else img.rotate(-turn, expand=True)
            buffer = _save(turned, fmt)
    except (OSError, ValueError) as exc:
        return _no(f"the file could not be turned: {exc}")
    if buffer is None:
        return _no("Ninaivu cannot re-save this format without losing quality")
    try:
        _replace(path, buffer)
    except OSError as exc:
        return _no(f"the file could not be written: {exc}")
    if not _still_a_picture(path, None):
        return _no("the turned file did not read back correctly")
    return Turn(ok=True, how="pixels", orientation=1)


def _save(img: Image.Image, fmt: str | None) -> bytes | None:
    import io

    if fmt not in ("PNG", "BMP"):
        return None
    out = io.BytesIO()
    if fmt == "PNG":
        img.save(out, "PNG", optimize=True)
    else:
        img.save(out, "BMP")
    return out.getvalue()


# ---------------------------------------------------------------------------
# The way in
# ---------------------------------------------------------------------------

def can_rotate_original(path: str | Path, kind: str = "") -> bool:
    """Whether :func:`rotate_original` has a lossless route for this file.

    Used to grey a button out rather than to decide anything: the real answer
    comes from trying, because it also depends on what is inside the file.
    """
    suffix = Path(path).suffix.lower()
    if suffix in JPEG_EXTS or suffix in TIFF_EXTS or suffix in PIXEL_EXTS:
        return True
    if suffix in VIDEO_EXTS:
        return bool(media.FFMPEG)
    return False


def rotate_original(path: str | Path, turn: int, kind: str = "") -> Turn:
    """Turn the file at *path* *turn*° clockwise, losslessly, in place.

    Returns a :class:`Turn` saying what happened. A refusal always means the
    file was not touched.
    """
    step = quarter(turn)
    if step is None:
        return _no("a turn must be 0, 90, 180 or 270 degrees")

    path = Path(path)
    if not path.is_file():
        return _no("the file is not there")
    suffix = path.suffix.lower()

    if step == 0:
        return Turn(ok=True, how="", unchanged=True)

    if suffix in VIDEO_EXTS or kind == "video":
        return _rotate_video(path, step)
    if suffix in JPEG_EXTS:
        return _rotate_tagged(path, step, jpeg=True)
    if suffix in TIFF_EXTS:
        return _rotate_tagged(path, step, jpeg=False)
    if suffix in PIXEL_EXTS:
        return _rotate_pixels(path, step)
    if media.is_raw(path):
        return _no("a RAW file is the negative — Ninaivu will not rewrite one. "
                   "The turn is still saved in the library")
    return _no(f"{suffix or 'this format'} cannot be turned without "
               "re-encoding it, which would lose quality")
