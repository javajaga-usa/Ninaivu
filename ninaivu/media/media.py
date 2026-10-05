"""Media probing and derivative generation.

Everything here degrades gracefully: missing ``ffmpeg``, ``cv2`` or
``mutagen`` costs you a feature, never a crash.
"""

from __future__ import annotations

import json
import math
import io
import logging
import os
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, ImageFile

from ..archive import dates as capture_dates

ImageFile.LOAD_TRUNCATED_IMAGES = True
Image.MAX_IMAGE_PIXELS = 512_000_000  # generous, but still guards decompression bombs

try:  # HEIC/HEIF support
    import pillow_heif  # type: ignore

    pillow_heif.register_heif_opener()
    HEIF_OK = True
except Exception:  # pragma: no cover - optional
    HEIF_OK = False

try:
    import numpy as np
except Exception:  # pragma: no cover - optional
    np = None  # type: ignore

try:
    import cv2  # type: ignore
except Exception:  # pragma: no cover - optional
    cv2 = None  # type: ignore
else:
    # The env var above covers the FFmpeg backend; this covers OpenCV's own
    # logger. Neither is load-bearing, so a version that lacks it is fine.
    try:
        cv2.setLogLevel(0)          # SILENT
    except Exception:  # pragma: no cover - older or trimmed builds
        pass

FFPROBE = shutil.which("ffprobe")
FFMPEG = shutil.which("ffmpeg")


log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# EXIF
# ---------------------------------------------------------------------------

_EXIF_TAGS = {
    0x0100: "ImageWidth", 0x0101: "ImageLength", 0x0112: "Orientation",
    0x010F: "Make", 0x0110: "Model", 0x0132: "DateTime",
    0x829A: "ExposureTime", 0x829D: "FNumber", 0x8827: "ISOSpeedRatings",
    0x9003: "DateTimeOriginal", 0x9004: "DateTimeDigitized",
    0x920A: "FocalLength", 0xA002: "PixelXDimension", 0xA003: "PixelYDimension",
    0xA434: "LensModel",
}


def _to_float(value: Any) -> float | None:
    try:
        if isinstance(value, tuple) and len(value) == 2:
            return float(value[0]) / float(value[1]) if value[1] else None
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _parse_exif_datetime(raw: Any) -> float | None:
    if not raw or not isinstance(raw, str):
        return None
    raw = raw.strip().split(".")[0]
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d", "%Y-%m-%d"):
        try:
            when = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        # The archive's rule, so both file a photograph under the same day: a
        # date before 1900 or after tomorrow is a camera clock nobody set, and
        # the next tag down (or the next source down the chain) gets its turn.
        return capture_dates.to_timestamp(when) if capture_dates.plausible(when) else None
    return None


def _gps_to_degrees(value: Any) -> float | None:
    try:
        d, m, s = (_to_float(v) or 0.0 for v in value)
        return d + m / 60.0 + s / 3600.0
    except Exception:
        return None



def created_at(st) -> float:
    """When the file was made, as well as a filesystem can say.

    The rule is *the earliest timestamp the file carries*, and it is that way
    because the two candidates fail in opposite directions:

    * A photograph **edited in place** keeps its creation time and gets a new
      modification time. The creation time is the honest one.
    * A photograph **copied** — which is what the Archive tab does to every
      file it consolidates — gets a new creation time and usually keeps the
      original modification time. Now the modification time is the honest one.

    Taking whichever is older is right in both cases, and it can only ever
    move a photograph earlier in the timeline, never later. That matters: the
    complaint this exists to answer is old photographs surfacing under this
    month because something touched the file.

    Windows reports creation time as ``st_ctime``; macOS and some Linux
    filesystems as ``st_birthtime``; elsewhere ``st_ctime`` is the inode
    change time, which is never older than the modification time and so falls
    out of the comparison on its own.
    """
    candidates = [st.st_mtime]
    for name in ("st_birthtime", "st_ctime"):
        value = getattr(st, name, None)
        if value:
            candidates.append(float(value))
    return min(c for c in candidates if c and c > 0)


def read_exif(img: Image.Image) -> dict[str, Any]:
    """Pull the fields we actually display out of an image's EXIF block."""
    info: dict[str, Any] = {}
    try:
        exif = img.getexif()
    except Exception:
        return info
    if not exif:
        return info

    raw = {_EXIF_TAGS.get(tag, tag): val for tag, val in exif.items()}
    make = str(raw.get("Make", "") or "").strip()
    model = str(raw.get("Model", "") or "").strip()
    if model.startswith(make) and make:
        camera = model
    else:
        camera = f"{make} {model}".strip()
    if camera:
        info["camera"] = camera

    try:
        ifd = exif.get_ifd(0x8769)  # Exif IFD
    except Exception:
        ifd = {}
    merged = {**raw}
    for tag, val in (ifd or {}).items():
        merged[_EXIF_TAGS.get(tag, tag)] = val

    captured = (
        _parse_exif_datetime(merged.get("DateTimeOriginal"))
        or _parse_exif_datetime(merged.get("DateTimeDigitized"))
        or _parse_exif_datetime(merged.get("DateTime"))
    )
    if captured:
        info["captured_at"] = captured

    if lens := merged.get("LensModel"):
        info["lens"] = str(lens).strip() or None
    iso = merged.get("ISOSpeedRatings")
    if isinstance(iso, (list, tuple)):
        iso = iso[0] if iso else None
    if iso:
        try:
            info["iso"] = int(iso)
        except (TypeError, ValueError):
            pass
    if (fnum := _to_float(merged.get("FNumber"))) is not None:
        info["f_number"] = round(fnum, 2)
    if (focal := _to_float(merged.get("FocalLength"))) is not None:
        info["focal_length"] = round(focal, 1)
    exposure = _to_float(merged.get("ExposureTime"))
    if exposure:
        info["exposure"] = (
            f"1/{round(1 / exposure)}s" if exposure < 1 else f"{round(exposure, 1)}s"
        )
    try:
        info["orientation"] = int(raw.get("Orientation") or 1)
    except (TypeError, ValueError):
        info["orientation"] = 1

    try:
        gps = exif.get_ifd(0x8825)
        if gps:
            lat = _gps_to_degrees(gps.get(2))
            lon = _gps_to_degrees(gps.get(4))
            if lat is not None and lon is not None:
                if str(gps.get(1, "N")).upper().startswith("S"):
                    lat = -lat
                if str(gps.get(3, "E")).upper().startswith("W"):
                    lon = -lon
                info["gps_lat"], info["gps_lon"] = round(lat, 6), round(lon, 6)
    except Exception as exc:
        # Not fatal, but not nothing: a library that quietly fails on a
        # thousand photographs looks exactly like one that read them.
        log.debug("%s: %s", __name__, exc)
    return info


# ---------------------------------------------------------------------------
# Perceptual hash (DCT-based, 64 bit)
# ---------------------------------------------------------------------------

_DCT_CACHE: dict[int, Any] = {}


def _dct_matrix(n: int):
    if np is None:
        return None
    if n not in _DCT_CACHE:
        k = np.arange(n).reshape(-1, 1)
        i = np.arange(n).reshape(1, -1)
        m = np.cos(np.pi * (2 * i + 1) * k / (2 * n))
        m[0] = m[0] / math.sqrt(2)
        _DCT_CACHE[n] = m * math.sqrt(2 / n)
    return _DCT_CACHE[n]


def perceptual_hash(img: Image.Image, hash_size: int = 8, factor: int = 4) -> str | None:
    """64-bit DCT perceptual hash, returned as 16 hex chars."""
    if np is None:
        return average_hash(img, hash_size)
    size = hash_size * factor
    try:
        small = img.convert("L").resize((size, size), Image.Resampling.LANCZOS)
        pixels = np.asarray(small, dtype=np.float64)
        m = _dct_matrix(size)
        coeffs = m @ pixels @ m.T
        block = coeffs[:hash_size, :hash_size]
        median = np.median(block[1:].flatten().tolist() + block[0, 1:].tolist())
        bits = (block > median).flatten()
        value = 0
        for bit in bits:
            value = (value << 1) | int(bit)
        return f"{value:016x}"
    except Exception:
        return None


def average_hash(img: Image.Image, hash_size: int = 8) -> str | None:
    try:
        small = img.convert("L").resize((hash_size, hash_size), Image.Resampling.LANCZOS)
        pixels = list(small.tobytes())
        avg = sum(pixels) / len(pixels)
        value = 0
        for px in pixels:
            value = (value << 1) | int(px > avg)
        return f"{value:016x}"
    except Exception:
        return None


def hamming(a: str, b: str) -> int:
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except (TypeError, ValueError):
        return 64


# ---------------------------------------------------------------------------
# BlurHash (compact gradient placeholder)
# ---------------------------------------------------------------------------

def _rgb_pixels(img: Image.Image) -> list[tuple[int, int, int]]:
    """RGB tuples without ``getdata()`` (deprecated in Pillow 14)."""
    raw = img.tobytes()
    return [tuple(raw[i:i + 3]) for i in range(0, len(raw), 3)]


_B83 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz#$%*+,-.:;=?@[]^_{|}~"


def _b83(value: int, length: int) -> str:
    out = ""
    for i in range(1, length + 1):
        digit = (value // (83 ** (length - i))) % 83
        out += _B83[digit]
    return out


def _srgb_to_linear(v: int) -> float:
    x = v / 255.0
    return x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(v: float) -> int:
    x = max(0.0, min(1.0, v))
    s = x * 12.92 if x <= 0.0031308 else 1.055 * (x ** (1 / 2.4)) - 0.055
    return int(round(s * 255 + 0.5))


def blurhash_encode(img: Image.Image, cx: int = 4, cy: int = 3) -> str | None:
    """Encode a tiny BlurHash string (~30 chars) used as a load placeholder."""
    try:
        small = img.convert("RGB").resize((32, 32), Image.Resampling.BILINEAR)
        w, h = small.size
        px = _rgb_pixels(small)
        lin = [
            (_srgb_to_linear(r), _srgb_to_linear(g), _srgb_to_linear(b))
            for r, g, b in px
        ]

        factors = []
        for j in range(cy):
            for i in range(cx):
                norm = 1.0 if (i == 0 and j == 0) else 2.0
                r = g = b = 0.0
                for y in range(h):
                    cos_y = math.cos(math.pi * j * y / h)
                    for x in range(w):
                        basis = norm * math.cos(math.pi * i * x / w) * cos_y
                        pr, pg, pb = lin[y * w + x]
                        r += basis * pr
                        g += basis * pg
                        b += basis * pb
                scale = 1.0 / (w * h)
                factors.append((r * scale, g * scale, b * scale))

        dc, ac = factors[0], factors[1:]
        size_flag = (cx - 1) + (cy - 1) * 9
        out = _b83(size_flag, 1)

        if ac:
            actual_max = max(max(abs(v) for v in f) for f in ac)
            quant_max = max(0, min(82, int(actual_max * 166 - 0.5)))
            max_value = (quant_max + 1) / 166
            out += _b83(quant_max, 1)
        else:
            max_value = 1.0
            out += _b83(0, 1)

        dc_value = (
            (_linear_to_srgb(dc[0]) << 16)
            + (_linear_to_srgb(dc[1]) << 8)
            + _linear_to_srgb(dc[2])
        )
        out += _b83(dc_value, 4)

        for r, g, b in ac:
            def q(v: float) -> int:
                sign = 1 if v >= 0 else -1
                return max(0, min(18, int(round(
                    sign * (abs(v) / max_value) ** 0.5 * 9 + 9.5))))
            out += _b83(q(r) * 19 * 19 + q(g) * 19 + q(b), 2)
        return out
    except Exception:
        return None


def dominant_color(img: Image.Image) -> str | None:
    try:
        small = img.convert("RGB").resize((1, 1), Image.Resampling.BOX)
        r, g, b = small.getpixel((0, 0))
        return f"#{r:02x}{g:02x}{b:02x}"
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Quality statistics (focus and exposure)
# ---------------------------------------------------------------------------

#: Long edge every still is reduced to before sharpness is measured.
#:
#: Laplacian variance scales with resolution: the same scene off a 48MP phone
#: scores several times higher than off a 2MP compact, purely because there
#: are more edges per frame. Measuring every photograph at one size is what
#: makes a single threshold mean the same thing across a library of mixed
#: cameras and decades.
QUALITY_EDGE = 256

#: Bumped when the flags below change in a way worth re-scoring a library
#: for. ``assets.quality_version`` is compared against it, the same contract
#: ``AI_VERSION`` and ``FACE_VERSION`` already keep — without it a threshold
#: change would only ever reach photographs indexed after it.
QUALITY_VERSION = 1

#: Luma at or below this counts as crushed shadow, at or above as blown
#: highlight. Both are 8-bit values, a little inside the actual endpoints so
#: ordinary black and white do not read as clipped.
_SHADOW_FLOOR = 8
_HIGHLIGHT_CEILING = 247


def _laplacian_variance(grey: Image.Image) -> float | None:
    """Variance of the 3x3 Laplacian — the standard focus measure.

    The same kernel OpenCV's ``Laplacian`` applies, written out in numpy so
    this works in every AI tier: cv2 is optional, numpy is not. Returns
    ``None`` rather than 0.0 when it cannot measure, so "too small to judge"
    stays distinguishable from "completely flat".
    """
    if np is None:
        return None
    a = np.asarray(grey, dtype=np.float64)
    if a.ndim != 2 or min(a.shape) < 3:
        return None
    lap = (a[:-2, 1:-1] + a[2:, 1:-1] + a[1:-1, :-2] + a[1:-1, 2:]
           - 4.0 * a[1:-1, 1:-1])
    return float(lap.var())


def quality_stats(img: Image.Image) -> dict[str, float]:
    """Focus and exposure for one still, measured at :data:`QUALITY_EDGE`.

    Keys are omitted rather than zeroed when they cannot be computed, so a
    caller can tell a genuinely dark photograph from one that was never
    measured.
    """
    try:
        grey = img.convert("L")
        grey.thumbnail((QUALITY_EDGE, QUALITY_EDGE), Image.Resampling.BILINEAR)
    except Exception:  # noqa: BLE001 - one odd file must not stop a scan
        return {}

    out: dict[str, float] = {}
    sharp = _laplacian_variance(grey)
    if sharp is not None:
        out["sharpness"] = round(sharp, 3)

    try:
        if np is not None:
            a = np.asarray(grey, dtype=np.float64)
            if not a.size:
                return out
            out["brightness"] = round(float(a.mean()) / 255.0, 4)
            out["contrast"] = round(float(a.std()) / 255.0, 4)
            out["shadow_clip"] = round(float((a <= _SHADOW_FLOOR).mean()), 4)
            out["highlight_clip"] = round(
                float((a >= _HIGHLIGHT_CEILING).mean()), 4)
        else:
            pixels = list(grey.tobytes())
            if pixels:
                out["brightness"] = round(sum(pixels) / len(pixels) / 255.0, 4)
    except Exception:  # noqa: BLE001
        pass
    return out


#: Greyscale standard deviation, 0-1, below which a frame carries no detail
#: to be in focus at all — a wall, a sky, a scan of blank paper. Laplacian
#: variance cannot tell that apart from a soft photograph, and calling a
#: deliberate flat field "blurry" is the false positive people notice.
FLAT_CONTRAST = 0.035


def quality_flags(stats: dict[str, float], width: int | None = None,
                  height: int | None = None, *,
                  blur_below: float, dark_below: float, bright_above: float,
                  clip_above: float, min_pixels: int) -> list[str]:
    """Turn raw statistics into the words the browse filters use.

    Deliberately plain: ``blurry``, ``dark``, ``bright``, ``clipped``,
    ``lowres``. A photograph can carry several. A measurement that is missing
    never produces a flag — Ninaivu says nothing rather than guessing.
    """
    flags: list[str] = []
    sharpness = stats.get("sharpness")
    contrast = stats.get("contrast")
    flat = contrast is not None and contrast < FLAT_CONTRAST
    if sharpness is not None and sharpness < blur_below and not flat:
        flags.append("blurry")
    brightness = stats.get("brightness")
    if brightness is not None:
        if brightness < dark_below:
            flags.append("dark")
        elif brightness > bright_above:
            flags.append("bright")
    if stats.get("highlight_clip", 0.0) >= clip_above:
        flags.append("clipped")
    if width and height and int(width) * int(height) < int(min_pixels):
        flags.append("lowres")
    return flags


# ---------------------------------------------------------------------------
# Video / audio probing
# ---------------------------------------------------------------------------

def ffprobe_info(path: str | Path) -> dict[str, Any]:
    if not FFPROBE:
        return {}
    try:
        proc = subprocess.run(
            [FFPROBE, "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, timeout=30, check=False,
        )
        if proc.returncode != 0:
            return {}
        return json.loads(proc.stdout or b"{}")
    except (subprocess.SubprocessError, ValueError, OSError):
        return {}


def probe_video(path: str | Path) -> dict[str, Any]:
    info: dict[str, Any] = {}
    data = ffprobe_info(path)
    if data:
        fmt = data.get("format", {})
        if dur := fmt.get("duration"):
            try:
                info["duration"] = round(float(dur), 2)
            except ValueError:
                pass
        tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
        for key in ("creation_time", "com.apple.quicktime.creationdate", "date"):
            if raw := tags.get(key):
                ts = _parse_iso(raw)
                if ts:
                    info["captured_at"] = ts
                    break
        if model := tags.get("com.apple.quicktime.model") or tags.get("model"):
            info["camera"] = str(model)
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                info["width"] = stream.get("width")
                info["height"] = stream.get("height")
                rotation = 0
                for sd in stream.get("side_data_list", []) or []:
                    if "rotation" in sd:
                        rotation = int(sd["rotation"])
                if rotation in (90, -90, 270, -270):
                    info["width"], info["height"] = info["height"], info["width"]
                break
    elif cv2 is not None:
        try:
            cap = cv2.VideoCapture(str(path))
            fps = cap.get(cv2.CAP_PROP_FPS) or 0
            frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
            if fps > 0 and frames > 0:
                info["duration"] = round(frames / fps, 2)
            info["width"] = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or None
            info["height"] = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or None
            cap.release()
        except Exception as exc:
            # Not fatal, but not nothing: a library that quietly fails on a
            # thousand photographs looks exactly like one that read them.
            log.debug("%s: %s", __name__, exc)
    return info


def probe_audio(path: str | Path) -> dict[str, Any]:
    info: dict[str, Any] = {}
    try:
        import mutagen  # type: ignore

        audio = mutagen.File(str(path))
        if audio is not None:
            if audio.info and getattr(audio.info, "length", None):
                info["duration"] = round(float(audio.info.length), 2)
            tags = getattr(audio, "tags", None) or {}

            def first(*keys: str) -> str | None:
                for key in keys:
                    val = tags.get(key)
                    if val:
                        return str(val[0] if isinstance(val, list) else val)
                return None

            title = first("TIT2", "title", "\xa9nam")
            artist = first("TPE1", "artist", "\xa9ART")
            album = first("TALB", "album", "\xa9alb")
            caption = " — ".join(x for x in (artist, title) if x)
            if caption:
                info["caption"] = caption
            if album:
                info["lens"] = None
                info.setdefault("tags", []).append(album.lower())
            return info
    except Exception as exc:
        # Not fatal, but not nothing: a library that quietly fails on a
        # thousand photographs looks exactly like one that read them.
        log.debug("%s: %s", __name__, exc)

    data = ffprobe_info(path)
    fmt = data.get("format", {}) if data else {}
    if dur := fmt.get("duration"):
        try:
            info["duration"] = round(float(dur), 2)
        except ValueError:
            pass
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    caption = " — ".join(x for x in (tags.get("artist"), tags.get("title")) if x)
    if caption:
        info["caption"] = caption
    return info


def _parse_iso(raw: str) -> float | None:
    raw = str(raw).strip().replace("Z", "+00:00")
    for candidate in (raw, raw.split(".")[0], raw.split("T")[0]):
        try:
            dt = datetime.fromisoformat(candidate)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Thumbnails
# ---------------------------------------------------------------------------

#: How much of a RAW file to search for an embedded preview. Camera RAWs run
#: 20-60 MB; anything past this is not a photograph from a camera.
RAW_MAX_SCAN_BYTES = 120 * 1024 * 1024

_JPEG_START = b"\xff\xd8\xff"
_JPEG_END = b"\xff\xd9"


def embedded_jpeg(path: str | Path) -> bytes | None:
    """The largest JPEG stored inside a RAW file, if there is one.

    Cameras write a full-size JPEG preview into the RAW so the back of the
    camera has something to show, and it carries the same EXIF as the frame.
    Pulling it out gives Ninaivu a thumbnail *and* a capture date without
    decoding a mosaic or taking a dependency — which matters, because a RAW
    with no preview and no rawpy would otherwise index as a broken tile with
    the file's timestamp for a date.

    The largest is taken deliberately: most RAWs hold two or three previews,
    and the first one in the file is usually the 160-pixel one meant for the
    camera's thumbnail grid.
    """
    try:
        size = os.path.getsize(path)
        if size > RAW_MAX_SCAN_BYTES:
            return None
        with open(path, "rb") as handle:
            blob = handle.read()
    except OSError:
        return None

    best: bytes | None = None
    start = blob.find(_JPEG_START)
    while start != -1:
        end = blob.find(_JPEG_END, start + 3)
        if end == -1:
            break
        candidate = blob[start:end + 2]
        if best is None or len(candidate) > len(best):
            best = candidate
        start = blob.find(_JPEG_START, end + 2)
    return best


def open_raw(path: str | Path, *, edge: int | None = None) -> Image.Image | None:
    """A RAW as a picture, by whatever route works.

    The embedded preview first, because it is free and carries the EXIF. Then
    rawpy, which actually develops the sensor data — better colour, and the
    only option for a file whose preview is missing or tiny. Neither being
    available is not an error: the caller falls back to treating the file as
    unreadable, exactly as before.

    With *edge*, a preview is decoded no smaller than that on either side but
    no larger than it needs to be — see :func:`open_for_index`. Its size before
    that is kept in ``info[FULL_SIZE]``.
    """
    data = embedded_jpeg(path)
    if data:
        try:
            img = Image.open(io.BytesIO(data))
            full = img.size
            if edge:
                img.draft(None, (edge, edge))
            img.load()
            img.info[FULL_SIZE] = full
            # A preview smaller than this is the camera's thumbnail grid
            # image, not the photograph; developing is worth it instead. Judged
            # on the preview's real size, not on what it was decoded at.
            if max(full) >= 640:
                return img
            small = img
        except Exception:  # noqa: BLE001
            small = None
    else:
        small = None

    try:
        import rawpy  # noqa: PLC0415 - optional, and slow to import

        with rawpy.imread(str(path)) as raw:
            return Image.fromarray(raw.postprocess(use_camera_wb=True))
    except Exception as exc:# noqa: BLE001 - not installed, or cannot read this one
        # Not fatal, but not nothing: a library that quietly fails on a
        # thousand photographs looks exactly like one that read them.
        log.debug("%s: %s", __name__, exc)
    return small


def is_raw(path: str | Path) -> bool:
    from ..server.config import RAW_EXTS  # noqa: PLC0415 - avoids an import cycle
    return Path(path).suffix.lower() in RAW_EXTS


#: ``Image.info`` key for a picture's size before it was decoded smaller.
FULL_SIZE = "ninaivu_full_size"

#: EXIF orientations that turn the picture a quarter turn, so width and height
#: trade places once it is stood upright.
_QUARTER_TURNS = frozenset({5, 6, 7, 8})


def open_for_index(path: str | Path, edge: int) -> tuple[Image.Image, tuple[int, int]]:
    """A picture decoded only as large as indexing needs, and its real size.

    Indexing never looks at a photograph at full resolution: the largest
    thumbnail is 640 pixels and orientation detection shrinks its own copy to
    640 before it looks. Decoding a 24-megapixel camera JPEG in full anyway —
    72 MB of pixels, per photo — was most of the processor time a scan spent on
    it. A JPEG can be decoded at a half, a quarter or an eighth of its size
    directly, which skips that work instead of doing it and throwing it away.

    *edge* is the smallest either side may be decoded at. Measured on ten
    6000×4000 Canon JPEGs, a quarter-size decode took 324 ms a photo against
    1,187 ms in full, with identical perceptual hashes (so duplicate groups do
    not move), brightness within 0.001, and sharpness scores 2–4% higher.

    Formats that cannot be decoded smaller (PNG, HEIC, WebP) are decoded in full
    exactly as before. The size returned is the photograph's own, stood upright
    by its EXIF orientation — what the index records as width and height, not
    the size of the image handed back.
    """
    if is_raw(path):
        img = open_raw(path, edge=edge)
        if img is None:
            raise OSError(f"no readable preview inside {path}")
    else:
        img = Image.open(path)
        full = img.size
        img.draft(None, (edge, edge))
        img.load()
        img.info[FULL_SIZE] = full
    width, height = img.info.get(FULL_SIZE) or img.size
    if img.getexif().get(0x0112) in _QUARTER_TURNS:
        width, height = height, width
    return (ImageOps.exif_transpose(img) or img), (width, height)


def _open_oriented(path: str | Path) -> Image.Image:
    if is_raw(path):
        img = open_raw(path)
        if img is None:
            raise OSError(f"no readable preview inside {path}")
        return ImageOps.exif_transpose(img) or img
    img = Image.open(path)
    img.load()
    return ImageOps.exif_transpose(img) or img


def extract_video_frame(path: str | Path, offset: float = 1.0) -> Image.Image | None:
    """Grab a poster frame; prefers ffmpeg, falls back to OpenCV."""
    if FFMPEG:
        try:
            proc = subprocess.run(
                [FFMPEG, "-v", "quiet", "-ss", str(offset), "-i", str(path),
                 "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
                capture_output=True, timeout=45, check=False,
            )
            if proc.returncode == 0 and proc.stdout:
                import io

                return Image.open(io.BytesIO(proc.stdout)).convert("RGB")
            # Retry from the very first frame for very short clips.
            proc = subprocess.run(
                [FFMPEG, "-v", "quiet", "-i", str(path), "-frames:v", "1",
                 "-f", "image2pipe", "-vcodec", "png", "-"],
                capture_output=True, timeout=45, check=False,
            )
            if proc.returncode == 0 and proc.stdout:
                import io

                return Image.open(io.BytesIO(proc.stdout)).convert("RGB")
        except (subprocess.SubprocessError, OSError, ValueError):
            pass
    if cv2 is not None:
        try:
            cap = cv2.VideoCapture(str(path))
            fps = cap.get(cv2.CAP_PROP_FPS) or 25
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(fps * offset))
            ok, frame = cap.read()
            if not ok:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ok, frame = cap.read()
            cap.release()
            if ok:
                return Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        except Exception as exc:
            # Not fatal, but not nothing: a library that quietly fails on a
            # thousand photographs looks exactly like one that read them.
            log.debug("%s: %s", __name__, exc)
    # Both paths gave up. Almost always a container with no index — a file
    # still being copied in, or a download that stopped early. Named here
    # because the decoders themselves say only that something was missing.
    log.debug("no frame could be read from %s", path)
    return None


def thumb_base(root_key: str, rel_path: str) -> str:
    """Stable, sharded base name for a file's derivatives (``ab/abcdef…``)."""
    import hashlib

    # Non-cryptographic use -- this only shards filenames on disk, it is
    # never used to protect anything -- so SHA-1 is fine, and saying so
    # explicitly is what keeps a security scanner from flagging it.
    digest = hashlib.sha1(f"{root_key}|{rel_path}".encode("utf-8"),
                          usedforsecurity=False).hexdigest()
    return f"{digest[:2]}/{digest}"


def thumb_recipe(sizes, fmt: str, quality: int) -> str:
    """A short fingerprint of *how* thumbnails are made.

    It rides in the thumbnail URL beside the asset's own version. Changing the
    sizes, the format or the quality rewrites every thumbnail on disk without
    touching a single photograph — and without this, those new files would sit
    behind URLs the browser was told to keep for a year.
    """
    import hashlib                                   # noqa: PLC0415

    raw = f"{tuple(sizes)}|{fmt.upper()}|{int(quality)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:6]


def thumb_version(row, recipe: str = "") -> str:
    """The token in a thumbnail URL, which changes exactly when the picture does.

    A thumbnail's content is a function of three things: the file that was
    read, the turn applied to it, and the settings it was made with. So the
    version is all three.

    ``indexed_at`` alone is not enough, and the reason is not theoretical: it is
    a timestamp, and truncating it to whole seconds means two rotations in the
    same second produce the same version. Rotating twice in quick succession —
    ninety degrees, then back — is exactly how the button gets used, and it
    would have left the second turn showing the first one's thumbnail.
    """
    def field(name: str) -> int:
        # Rows arrive as dicts from some callers and as sqlite3.Row from
        # others, and those two disagree about missing keys — one raises
        # KeyError, the other IndexError, and neither has `.get`.
        try:
            value = row[name]
        except (KeyError, IndexError, TypeError):
            return 0
        return int(value or 0)

    return f"{field('indexed_at')}r{field('rotation')}" + (f"t{recipe}" if recipe else "")


def thumb_file(base_name: str, size: int, fmt: str = "WEBP") -> str:
    ext = "webp" if fmt.upper() == "WEBP" else "jpg"
    return f"{base_name}_{size}.{ext}"


def write_thumbnails(
    source: Image.Image,
    thumbs_dir: Path,
    base_name: str,
    sizes: tuple[int, ...],
    fmt: str = "WEBP",
    quality: int = 82,
    fast: bool = False,
) -> str:
    """Write one derivative per requested size. Returns the shared base name.

    When *fast* is true, WebP uses its fastest compression (method 0) — the
    visual difference at thumbnail sizes is negligible, and batch operations
    like straighten-apply benefit from the 3–5× encode speedup.

    Sizes are generated largest-first and each subsequent size is downscaled
    from the previous (smaller) result rather than from the full-resolution
    source. Thumbnailing a 640 px image to 256 px is far cheaper than
    thumbnailing a 4000 px image to 256 px.
    """
    rgb = source if source.mode == "RGB" else source.convert("RGB")
    webp = fmt.upper() == "WEBP"
    ext = "webp" if webp else "jpg"
    webp_method = 0 if fast else 4
    # Descending order: start from the largest size so each subsequent
    # thumbnail can be derived from the previous one (pyramid).
    ordered = sorted(sizes, reverse=True)
    prev = rgb
    for size in ordered:
        name = f"{base_name}_{size}.{ext}"
        out = thumbs_dir / name
        out.parent.mkdir(parents=True, exist_ok=True)
        copy = prev.copy()
        copy.thumbnail((size, size), Image.Resampling.LANCZOS)
        # Named for this process and thread: the scan, a turn by hand and
        # Straighten can make the same thumbnail at once, and with one shared
        # name each wrote over and moved away the other's half-written file.
        tmp = out.with_suffix(f"{out.suffix}.{os.getpid()}-{threading.get_ident()}.tmp")
        if webp:
            copy.save(tmp, "WEBP", quality=quality, method=webp_method)
        else:
            copy.save(tmp, "JPEG", quality=quality, optimize=True, progressive=True)
        tmp.replace(out)
        # The thumbnail just made is a good starting point for the next
        # (smaller) size — its pixel dimensions are already close.
        prev = copy
    return base_name


def remove_thumbnails(thumbs_dir: Path, base_name: str, sizes: tuple[int, ...],
                      fmt: str = "WEBP") -> None:
    ext = "webp" if fmt.upper() == "WEBP" else "jpg"
    for size in sizes:
        try:
            (thumbs_dir / f"{base_name}_{size}.{ext}").unlink(missing_ok=True)
        except OSError:
            pass


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024 or unit == "TB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"


def detect_motion_photo(path: Path | str) -> bool:
    """Check for Google Motion Photo markers in JPEG/HEIC files."""
    try:
        p = Path(path)
        if not p.is_file():
            return False
        size = p.stat().st_size
        with open(p, "rb") as f:
            header = f.read(min(size, 65536))
            if b"GCamera:MotionPhoto" in header or b"GCamera:MicroVideo" in header or b"MotionPhoto_Data" in header:
                return True
            if size > 65536:
                f.seek(max(0, size - 65536))
                tail = f.read()
                if b"MotionPhoto_Data" in tail or b"GCamera:MicroVideo" in tail:
                    return True
    except Exception as exc:
        # Not fatal, but not nothing: a library that quietly fails on a
        # thousand photographs looks exactly like one that read them.
        log.debug("%s: %s", __name__, exc)
    return False

