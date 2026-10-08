"""Bring old prints back: find each print in a photograph of it, and file it.

A family that wants its albums in the library photographs the prints with a
phone, one at a time or several laid out on a table, or scans a page of them
on a flatbed. What arrives is a picture of a table with photographs on it:
skewed by the angle of the phone, faded by forty years in a drawer, often
sideways. This turns each print in it into a photograph of its own.

The steps, each one plain enough to test with rectangles drawn on a
background:

* **Find the prints.** Edges and two thresholds give outlines; the convex
  ones with four corners, big enough and not too long and thin, are prints.
  A print inside a print (the picture inside its white border) is dropped,
  so a bordered print is cut at its border. When nothing qualifies the whole
  photograph is the print — a close-up of one print, or a picture taken too
  close to tell — and it is only straightened.
* **Flatten it.** Each outline is warped to a rectangle as long and wide as
  its sides, so a print shot at an angle comes out square-on.
* **Stand it up.** The library's own orientation code (:mod:`upright`, with
  the trained model in :mod:`orientnet` where it has been downloaded)
  decides the quarter turn, exactly as it does for a scan of the library.
* **Clean it up.** Fading and a colour cast are fixed here with numpy,
  always available. Face restoration (GFPGAN) and upscaling (Real-ESRGAN) are
  the AI models tab's, reused through :mod:`onnx_tools`, and offered only
  when they are installed. Dust and scratches are not attempted: the
  inpainting helpers here need somebody to paint the damage, and guessing
  which specks are dust is how a freckle disappears.
* **Guess the decade.** Where the picture-search model (OpenCLIP or SigLIP)
  is loaded, each decade is described to it and the prints are compared with
  the descriptions. It is a hint for the person filing them, never a date
  written on its own.

Nothing here leaves the machine: no step talks to Gemini or any other
service, and none of it needs a dependency Ninaivu does not already have.
"""

from __future__ import annotations

import io
import json
import logging
import math
import re
import secrets
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import numpy as np
from PIL import Image, ImageOps

from . import media  # noqa: F401 - registers the HEIF opener for an iPhone's photos

log = logging.getLogger(__name__)

try:
    import cv2
except ImportError:                                   # pragma: no cover
    cv2 = None                                        # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

#: Photographs one batch may carry. Ten pages of prints is an afternoon's
#: sorting; more is better done as a second batch than as one long wait.
MAX_PHOTOS = 10
#: Bytes one photograph may be. A phone's largest JPEG or HEIF is a fraction
#: of this; a flatbed's uncompressed TIFF of a page is about this size.
MAX_PHOTO_BYTES = 60 * 1024 * 1024
#: Pixels one photograph may declare. A 600 dpi scan of a letter-size page is
#: 34 megapixels and a 200 MP phone photograph is the largest a phone takes;
#: the header is checked before anything is decoded (safe_image.py).
MAX_PIXELS = 220_000_000
#: Prints found in one photograph, at most. A table with more on it is
#: better photographed in two halves, where each print gets more pixels.
MAX_PRINTS = 12
#: How long an unsaved batch is kept, in seconds, before it is cleared away.
KEEP_SESSION = 6 * 3600

#: The folder, inside a library (and inside a family member's own folder
#: there), that prints are filed under. Named on disk, so not translated.
FOLDER = "Scanned prints"
ORIGINALS = "originals"
UNKNOWN_YEAR = "Unknown year"


# ---------------------------------------------------------------------------
# Finding the prints
# ---------------------------------------------------------------------------

#: Detection works on a copy this many pixels on its longest side. Enough to
#: place a corner within a few pixels of the original; small enough that the
#: whole search takes a fraction of a second.
DETECT_EDGE = 1024
#: A print is at least this share of the photograph. Smaller outlines are
#: the patterns on a tablecloth, a stamp, a corner of a book.
MIN_AREA = 0.02
#: …and at most this share: an outline that is the whole frame is the frame.
MAX_AREA = 0.97
#: No real print is longer than four times its width; a strip that is, is
#: the edge of a table or a ruler.
MAX_ASPECT = 4.0
#: Corners further from square than this are not a print seen at an angle a
#: person would hold a phone at.
MIN_CORNER, MAX_CORNER = 55.0, 125.0
#: How much of its four-cornered outline a shape must fill. A print with
#: rounded corners fills nearly all of it; a blob or an L-shape does not.
MIN_FILL = 0.85
#: Two outlines sharing this much of the smaller one are the same print (or a
#: picture inside a print's border) and only the larger is kept.
SAME_PRINT = 0.8


@dataclass(frozen=True)
class Found:
    """One print: its corners in the photograph, clockwise from top left."""

    quad: tuple[tuple[float, float], ...]
    #: The whole photograph, because no print could be told apart in it.
    whole: bool = False

    def as_list(self) -> list[list[float]]:
        return [[round(x, 2), round(y, 2)] for x, y in self.quad]


def _order(points: np.ndarray) -> np.ndarray:
    """Four corners clockwise (on screen, y down), from the top-left one.

    Ordered by angle round the centre rather than by the sums and differences
    of the coordinates, which tie for a print turned forty-five degrees and
    then hand two corners the same name.
    """
    pts = np.asarray(points, dtype=np.float64).reshape(4, 2)
    centre = pts.mean(axis=0)
    angles = np.arctan2(pts[:, 1] - centre[1], pts[:, 0] - centre[0])
    pts = pts[np.argsort(angles)]
    start = int(np.argmin(pts[:, 0] + pts[:, 1]))
    return np.roll(pts, -start, axis=0)


def _corner_angles(quad: np.ndarray) -> list[float]:
    out = []
    for i in range(4):
        a, b, c = quad[i - 1], quad[i], quad[(i + 1) % 4]
        u, v = a - b, c - b
        cosine = float(np.dot(u, v) / (np.linalg.norm(u) * np.linalg.norm(v) + 1e-9))
        out.append(math.degrees(math.acos(max(-1.0, min(1.0, cosine)))))
    return out


def _sides(quad: np.ndarray) -> tuple[float, float]:
    """(width, height) of the rectangle a quad flattens to: the mean of each
    pair of opposite sides, so a print seen at an angle keeps its proportions."""
    top = np.linalg.norm(quad[1] - quad[0])
    bottom = np.linalg.norm(quad[2] - quad[3])
    left = np.linalg.norm(quad[3] - quad[0])
    right = np.linalg.norm(quad[2] - quad[1])
    return (top + bottom) / 2, (left + right) / 2


def _quad_of(contour: np.ndarray) -> np.ndarray | None:
    """The four corners of an outline, or None when it is not print-shaped."""
    hull = cv2.convexHull(contour)
    hull_area = cv2.contourArea(hull)
    if hull_area <= 0:
        return None
    perimeter = cv2.arcLength(hull, True)
    approx = cv2.approxPolyDP(hull, 0.02 * perimeter, True)
    if len(approx) == 4 and cv2.isContourConvex(approx):
        quad = approx.reshape(4, 2).astype(np.float64)
    else:
        # Rounded corners and a slightly ragged edge give five or six points;
        # the smallest rectangle round them is the print, provided the shape
        # nearly fills it.
        quad = cv2.boxPoints(cv2.minAreaRect(hull)).astype(np.float64)
    area = cv2.contourArea(quad.astype(np.float32))
    if area <= 0 or cv2.contourArea(contour) / area < MIN_FILL:
        return None
    return _order(quad)


def _outlines(gray: np.ndarray) -> list[np.ndarray]:
    """Candidate outlines from three looks at the picture.

    Edges find a print against a background of any colour; a threshold finds
    a pale print on a dark table whose edge is too soft for the edge finder,
    and its inverse a dark print on a pale one. Each only adds candidates —
    the shape tests below decide.
    """
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blurred, 40, 120)
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    _, light = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    dark = cv2.bitwise_not(light)
    # Opened, so a thread of pixels joining a print to the table's grain
    # does not make the two one shape.
    speckle = np.ones((3, 3), np.uint8)
    light = cv2.morphologyEx(light, cv2.MORPH_OPEN, speckle)
    dark = cv2.morphologyEx(dark, cv2.MORPH_OPEN, speckle)
    found: list[np.ndarray] = []
    for mask in (edges, light, dark):
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        found.extend(contours)
    return found


def _overlap(one: np.ndarray, other: np.ndarray) -> float:
    """How much of the smaller of two convex quads lies inside the other."""
    a = one.astype(np.float32)
    b = other.astype(np.float32)
    shared, _ = cv2.intersectConvexConvex(a, b)
    smaller = min(cv2.contourArea(a), cv2.contourArea(b)) or 1.0
    return float(shared) / smaller


def find_prints(img: Image.Image) -> list[Found]:
    """Every print in *img*, top to bottom and left to right.

    Never empty: when no print can be told apart, the whole photograph is
    returned as one, marked ``whole``. Deterministic — the same picture gives
    the same corners — because every step is, and the order is fixed.
    """
    width, height = img.size
    whole = Found(((0.0, 0.0), (float(width), 0.0), (float(width), float(height)),
                   (0.0, float(height))), whole=True)
    if cv2 is None or width < 32 or height < 32:
        return [whole]
    scale = min(1.0, DETECT_EDGE / max(width, height))
    small = img.convert("L")
    if scale < 1.0:
        small = small.resize((max(1, round(width * scale)), max(1, round(height * scale))),
                             Image.Resampling.BILINEAR)
    gray = np.asarray(small, dtype=np.uint8)
    frame = float(gray.shape[0] * gray.shape[1])

    candidates: list[tuple[float, np.ndarray]] = []
    for contour in _outlines(gray):
        if cv2.contourArea(contour) < MIN_AREA * frame * MIN_FILL:
            continue
        quad = _quad_of(contour)
        if quad is None:
            continue
        area = cv2.contourArea(quad.astype(np.float32)) / frame
        if not MIN_AREA <= area <= MAX_AREA:
            continue
        w, h = _sides(quad)
        if min(w, h) <= 0 or max(w, h) / min(w, h) > MAX_ASPECT:
            continue
        if not all(MIN_CORNER <= a <= MAX_CORNER for a in _corner_angles(quad)):
            continue
        candidates.append((area, quad))

    # Largest first, so a print's border wins over the picture inside it and
    # the edge finder's outline over the threshold's copy of the same print.
    candidates.sort(key=lambda item: (-item[0], tuple(item[1].ravel())))
    kept: list[np.ndarray] = []
    for _, quad in candidates:
        if any(_overlap(quad, other) > SAME_PRINT for other in kept):
            continue
        kept.append(quad)
        if len(kept) >= MAX_PRINTS:
            break
    if not kept:
        return [whole]

    band = gray.shape[0] / 6.0

    def reading_order(quad: np.ndarray) -> tuple[int, float]:
        cx, cy = quad.mean(axis=0)
        return int(cy // band), float(cx)

    kept.sort(key=reading_order)
    return [Found(tuple((float(x / scale), float(y / scale)) for x, y in _inset(quad)))
            for quad in kept]


#: Pixels (at the detection size) each corner is pulled in by. An outline
#: sits on the edge itself, half on the table; losing a hair of the print's
#: border is invisible, a line of tablecloth along it is not.
INSET = 2.0


def _inset(quad: np.ndarray) -> np.ndarray:
    centre = quad.mean(axis=0)
    out = []
    for point in quad:
        towards = centre - point
        length = float(np.linalg.norm(towards)) or 1.0
        out.append(point + towards / length * INSET * math.sqrt(2))
    return np.asarray(out)


# ---------------------------------------------------------------------------
# Flattening, standing up, straightening
# ---------------------------------------------------------------------------

def warp(img: Image.Image, found: Found) -> Image.Image:
    """The print, square-on: its outline pulled out to a rectangle."""
    rgb = img.convert("RGB")
    if found.whole or cv2 is None:
        return rgb.copy()
    quad = np.asarray(found.quad, dtype=np.float32)
    w, h = _sides(quad.astype(np.float64))
    out_w, out_h = max(1, int(round(w))), max(1, int(round(h)))
    target = np.array([[0, 0], [out_w - 1, 0], [out_w - 1, out_h - 1], [0, out_h - 1]],
                      dtype=np.float32)
    matrix = cv2.getPerspectiveTransform(quad, target)
    flat = cv2.warpPerspective(np.asarray(rgb), matrix, (out_w, out_h),
                               flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    return Image.fromarray(flat)


def upright_turn(img: Image.Image, engine: Any = None) -> int:
    """The clockwise quarter turn that stands this print up, or 0.

    The library's own answer (upright.decide), for a picture with no EXIF
    tag — which a print just cut out of a photograph is. Without the
    orientation model it falls back to faces, and says 0 when unsure: a print
    left sideways is one tap to fix, one turned wrongly by a guess is worse.
    """
    from . import upright                               # noqa: PLC0415

    try:
        verdict = upright.decide(img, exif_orientation=None, engine=engine)
    except Exception:                                   # noqa: BLE001 - never fail a save over it
        log.debug("print scan: orientation not decided", exc_info=True)
        return 0
    return verdict.rotation % 360 if verdict.source in ("model", "faces", "ai") else 0


def turn(img: Image.Image, rotation: int) -> Image.Image:
    from . import upright                               # noqa: PLC0415

    return upright.apply(img, rotation % 360)


#: Slight skew only: a page set crooked on a flatbed, a print a little askew.
#: Anything beyond this is a photograph of a tilted scene, left as taken.
MAX_SKEW = 5.0


def skew_angle(img: Image.Image) -> float:
    """Degrees the picture's long straight lines lean, anticlockwise positive.

    Read from the lines a crooked scan is full of — the print's own edges, a
    border, a horizon — and only when enough of them agree. 0 when unsure.
    """
    if cv2 is None:
        return 0.0
    small = img.convert("L")
    small.thumbnail((800, 800), Image.Resampling.BILINEAR)
    gray = np.asarray(small, dtype=np.uint8)
    edges = cv2.Canny(cv2.GaussianBlur(gray, (3, 3), 0), 50, 150)
    shortest = max(20, min(gray.shape) // 4)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 720, threshold=60,
                            minLineLength=shortest, maxLineGap=8)
    if lines is None:
        return 0.0
    angles, weights = [], []
    for x1, y1, x2, y2 in lines[:, 0]:
        angle = math.degrees(math.atan2(-(y2 - y1), x2 - x1))
        # Fold every line onto the nearest of horizontal and vertical.
        folded = (angle + 45) % 90 - 45
        if abs(folded) <= MAX_SKEW:
            angles.append(folded)
            weights.append(math.hypot(x2 - x1, y2 - y1))
    if len(angles) < 3:
        return 0.0
    order = np.argsort(angles)
    cumulative = np.cumsum(np.asarray(weights)[order])
    median = float(np.asarray(angles)[order][np.searchsorted(cumulative, cumulative[-1] / 2)])
    return round(median, 2) if abs(median) >= 0.3 else 0.0


def deskew(img: Image.Image, angle: float | None = None) -> Image.Image:
    """Level a slightly crooked picture, trimmed so no corner is left empty."""
    angle = skew_angle(img) if angle is None else angle
    if not angle:
        return img
    w, h = img.size
    turned = img.rotate(-angle, resample=Image.Resampling.BICUBIC, expand=False)
    a = math.radians(abs(angle))
    keep_w = int(w * math.cos(a) - h * math.sin(a))
    keep_h = int(h * math.cos(a) - w * math.sin(a))
    if keep_w < w // 2 or keep_h < h // 2:
        return img
    left, top = (w - keep_w) // 2, (h - keep_h) // 2
    return turned.crop((left, top, left + keep_w, top + keep_h))


# ---------------------------------------------------------------------------
# Colour
# ---------------------------------------------------------------------------

#: Ends of the tonal range, percent: a speck of dust or a glint must not set
#: the black and white points for the whole print.
CLIP_LOW, CLIP_HIGH = 0.5, 99.5
#: A channel spanning fewer levels than this is a flat field, not a faded one.
MIN_SPAN = 8.0
#: No channel is stretched more than this many times: a nearly blank print is
#: not made into noise.
MAX_GAIN = 5.0
#: Below this average saturation a print is black and white or sepia, and
#: its tone is the photograph, not a cast: the channels are stretched together.
MONOCHROME = 0.08
#: How much of the correction is applied.
STRENGTH = 0.85


def fix_colours(img: Image.Image) -> Image.Image:
    """Undo fading and a colour cast: auto levels, channel by channel.

    A faded print has lost its blacks and whites, and has usually lost them
    unevenly — the cyan dye goes first, which is why old prints turn red. So
    each channel's own darkest and lightest are stretched back to black and
    white, which restores contrast and neutralises the cast in one step. A
    black-and-white or sepia print is stretched as one, so its tone stays.
    """
    rgb = img.convert("RGB")
    arr = np.asarray(rgb, dtype=np.float32)
    sample = rgb.copy()
    sample.thumbnail((512, 512), Image.Resampling.BILINEAR)
    probe = np.asarray(sample, dtype=np.float32)
    chroma = float((probe.max(axis=2) - probe.min(axis=2)).mean()) / 255.0

    if chroma < MONOCHROME:
        luma = probe.mean(axis=2)
        lo, hi = np.percentile(luma, (CLIP_LOW, CLIP_HIGH))
        ranges = [(float(lo), float(hi))] * 3
    else:
        ranges = [tuple(float(v) for v in np.percentile(probe[..., c], (CLIP_LOW, CLIP_HIGH)))
                  for c in range(3)]
    out = arr.copy()
    for channel, (lo, hi) in enumerate(ranges):
        if hi - lo < MIN_SPAN:
            continue
        span = max(hi - lo, 255.0 / MAX_GAIN)
        # Held at its middle when the gain is capped, so a print too flat to
        # stretch all the way is lifted evenly rather than pushed to black.
        low = (lo + hi - span) / 2
        out[..., channel] = (arr[..., channel] - low) * (255.0 / span)
    # Most of the way, not all of it: a print with only a few tones in it (a
    # studio portrait on a plain backdrop) is pushed to garish by a full
    # stretch, and a print a little short of black reads as old, not broken.
    out = arr + STRENGTH * (out - arr)
    return Image.fromarray(np.clip(out + 0.5, 0, 255).astype(np.uint8))


# ---------------------------------------------------------------------------
# The AI models tab's tools, when they are there
# ---------------------------------------------------------------------------

def tools_available() -> dict[str, bool]:
    """Which optional clean-up steps can run on this machine."""
    try:
        from . import onnx_tools                        # noqa: PLC0415
        return {"restore": bool(onnx_tools.available("restore")),
                "upscale": bool(onnx_tools.available("upscale"))}
    except Exception:                                   # noqa: BLE001
        return {"restore": False, "upscale": False}


def _png(img: Image.Image) -> bytes:
    buffer = io.BytesIO()
    img.save(buffer, "PNG")
    return buffer.getvalue()


def restore_faces(img: Image.Image) -> Image.Image:
    """GFPGAN on every face, through the Playground's own code. A print with
    no faces in it comes back as it was."""
    from . import onnx_tools                            # noqa: PLC0415

    if max(img.size) > onnx_tools.MAX_INPUT_SIDE:
        return img
    try:
        out = onnx_tools.restore(_png(img))
    except ValueError:                                  # "No faces were found"
        return img
    with Image.open(io.BytesIO(out)) as restored:
        return restored.convert("RGB")


def upscale(img: Image.Image, factor: int) -> Image.Image:
    """Real-ESRGAN, which works at ×4; ×2 is its answer halved, which is
    sharper than asking for less in the first place. A print already bigger
    than the model takes is left at its size."""
    from . import onnx_tools                            # noqa: PLC0415

    if factor not in (2, 4) or max(img.size) > onnx_tools.MAX_UPSCALE_INPUT:
        return img
    with Image.open(io.BytesIO(onnx_tools.upscale(_png(img)))) as large:
        large = large.convert("RGB")
    if factor == 2:
        large = large.resize((img.width * 2, img.height * 2), Image.Resampling.LANCZOS)
    return large


# ---------------------------------------------------------------------------
# Which decade
# ---------------------------------------------------------------------------

DECADES = tuple(range(1900, 2020, 10))
#: Several wordings per decade, averaged, so one phrase's quirks do not decide.
DECADE_PROMPTS = (
    "a photograph taken in the {d}s",
    "an old family photo from the {d}s",
    "a {d}s photograph",
)
#: CLIP's own logit scale; SigLIP's cosines are smaller, so its answers simply
#: come out less sure, which is the honest direction to err in.
LOGIT_SCALE = 100.0
#: Below this the guess is not shown. Twelve decades means a shrug is 0.08.
MIN_CONFIDENCE = 0.25


def year_model_ready(engine: Any) -> bool:
    return bool(engine is not None and getattr(engine, "semantic", False)
                and hasattr(engine, "embed_images") and hasattr(engine, "embed_texts"))


def guess_decade(images: list[Image.Image], engine: Any) -> dict[str, Any] | None:
    """The likeliest decade for these prints and how sure, or None.

    Zero-shot: the picture-search model is shown each decade in words and
    each print in pixels, and the decades are ranked by how alike they are.
    Printing, film stock and fashion date a photograph better than people
    expect, but it is still a guess, and it is offered as one.
    """
    if not images or not year_model_ready(engine):
        return None
    try:
        texts = [p.format(d=d) for d in DECADES for p in DECADE_PROMPTS]
        words = np.asarray(engine.embed_texts(texts), dtype=np.float32)
        words = words.reshape(len(DECADES), len(DECADE_PROMPTS), -1).mean(axis=1)
        words /= np.linalg.norm(words, axis=1, keepdims=True) + 1e-9
        pictures = np.asarray(engine.embed_images([im.convert("RGB") for im in images]),
                              dtype=np.float32)
        pictures /= np.linalg.norm(pictures, axis=1, keepdims=True) + 1e-9
        logits = LOGIT_SCALE * (pictures @ words.T)
        logits -= logits.max(axis=1, keepdims=True)
        probabilities = np.exp(logits)
        probabilities /= probabilities.sum(axis=1, keepdims=True)
        mean = probabilities.mean(axis=0)
    except Exception:                                   # noqa: BLE001 - a hint, never an error
        log.debug("print scan: decade not guessed", exc_info=True)
        return None
    best = int(np.argmax(mean))
    confidence = float(mean[best])
    if not math.isfinite(confidence) or confidence < MIN_CONFIDENCE:
        return None
    return {"decade": DECADES[best], "confidence": round(confidence, 3)}


# ---------------------------------------------------------------------------
# The date somebody gave
# ---------------------------------------------------------------------------

#: The first photographs on paper; nothing scanned can be older.
EARLIEST_YEAR = 1839


@dataclass(frozen=True)
class When:
    """What a person said about when the prints were taken."""

    moment: datetime | None
    #: The folder under "Scanned prints": "1975", "1970s" or "Unknown year".
    label: str

    @property
    def date_key(self) -> str:
        return self.moment.date().isoformat() if self.moment else ""


_WHEN = re.compile(r"(?P<decade>\d{3}0)s|(?P<year>\d{4})(?:-(?P<month>\d{2})(?:-(?P<day>\d{2}))?)?",
                   re.ASCII)


def parse_when(value: Any) -> When:
    """``""``, ``"1970s"``, ``"1975"``, ``"1975-06"`` or ``"1975-06-12"``.

    A year alone is filed on the first of January and a decade on its first
    year: the gallery needs a day to sort by, and the earliest it can be is
    the honest one. ValueError for anything else, or a date in the future.
    ASCII digits only, because the year becomes a folder's name.
    """
    text = str(value or "").strip()
    if not text:
        return When(None, UNKNOWN_YEAR)
    problem = "Give a year (1975), a decade (1970s) or a date (1975-06-12)."
    match = _WHEN.fullmatch(text)
    if not match:
        raise ValueError(problem)
    try:
        if match["decade"]:
            moment = datetime(int(match["decade"]), 1, 1)
            label = f"{match['decade']}s"
        else:
            moment = datetime(int(match["year"]), int(match["month"] or 1),
                              int(match["day"] or 1))
            label = match["year"]
    except ValueError:
        raise ValueError(problem) from None
    if moment.year < EARLIEST_YEAR or moment > datetime.now():
        raise ValueError(problem)
    return When(moment, label)


def folder_for(scope: str | None, when: When) -> str:
    return "/".join(filter(None, [scope or "", FOLDER, when.label]))


def originals_folder(scope: str | None) -> str:
    return "/".join(filter(None, [scope or "", FOLDER, ORIGINALS]))


# ---------------------------------------------------------------------------
# A batch: the photographs, held until somebody says which prints to keep
# ---------------------------------------------------------------------------

def sessions_dir(cfg: Any) -> Path:
    return Path(cfg.state_dir) / "print-scans"


def sweep(cfg: Any, now: float | None = None) -> None:
    """Clear away batches nobody saved: a closed tab must not leave a stack
    of phone photographs in the state folder for good."""
    base = sessions_dir(cfg)
    if not base.is_dir():
        return
    now = time.time() if now is None else now
    for folder in base.iterdir():
        try:
            if folder.is_dir() and not folder.is_symlink() and \
                    now - folder.stat().st_mtime > KEEP_SESSION:
                shutil.rmtree(folder, ignore_errors=True)
        except OSError:
            continue


def new_session(cfg: Any, owner: int) -> tuple[str, Path]:
    sweep(cfg)
    token = secrets.token_hex(16)
    folder = sessions_dir(cfg) / token
    folder.mkdir(parents=True, exist_ok=False)
    return token, folder


def session_folder(cfg: Any, token: Any) -> Path | None:
    if not isinstance(token, str) or len(token) != 32 or \
            any(c not in "0123456789abcdef" for c in token):
        return None
    folder = sessions_dir(cfg) / token
    if folder.is_symlink() or not folder.is_dir():
        return None
    return folder


def load_session(cfg: Any, token: Any, owner: int) -> tuple[Path, dict[str, Any]] | None:
    """A batch and what was found in it, for the person who started it only."""
    folder = session_folder(cfg, token)
    if folder is None:
        return None
    try:
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if meta.get("owner") != owner:
        return None
    return folder, meta


def save_session(folder: Path, meta: dict[str, Any]) -> None:
    (folder / "meta.json").write_text(json.dumps(meta), encoding="utf-8")


def open_photo(path: Path) -> Image.Image:
    """A photograph from a batch, the way up its camera said."""
    from . import safe_image                            # noqa: PLC0415

    safe_image.check_untrusted_file(path, MAX_PIXELS)
    with Image.open(path) as source:
        source.load()
        return ImageOps.exif_transpose(source).convert("RGB")


PREVIEW_EDGE = 640


def preview(img: Image.Image, edge: int = 320) -> bytes:
    small = img.copy()
    small.thumbnail((edge, edge), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    small.save(buffer, "JPEG", quality=82)
    return buffer.getvalue()


@dataclass(frozen=True)
class Seen:
    """A print as the batch shows it: where, which way up, how crooked."""

    found: Found
    rotation: int
    skew: float
    image: Image.Image

    def as_dict(self) -> dict[str, Any]:
        return {"quad": self.found.as_list(), "whole": self.found.whole,
                "rotation": self.rotation, "skew": self.skew}


def examine(photo: Image.Image, engine: Any = None,
            decide: Callable[[Image.Image, Any], int] | None = None) -> list[Seen]:
    """Every print in one photograph: where it is, its turn, and a small copy.

    The turn and the skew are decided here, on a copy big enough for the
    orientation model and the face detector, and the same ones are used when
    the print is saved, so what was ticked is what is filed.
    """
    decide = decide or upright_turn
    found = find_prints(photo)
    scale = min(1.0, (PREVIEW_EDGE * 2) / max(photo.size))
    small = photo if scale >= 1.0 else photo.resize(
        (max(1, round(photo.width * scale)), max(1, round(photo.height * scale))),
        Image.Resampling.LANCZOS)
    out = []
    for item in found:
        shrunk = item if item.whole else Found(
            tuple((x * scale, y * scale) for x, y in item.quad))
        flat = warp(small, shrunk)
        skew = skew_angle(flat) if item.whole else 0.0
        flat = deskew(flat, skew)
        rotation = int(decide(flat, engine)) % 360
        out.append(Seen(item, rotation, skew, turn(flat, rotation)))
    return out


# ---------------------------------------------------------------------------
# Making one print, and writing it into the library
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Options:
    fix_colours: bool = True
    restore_faces: bool = False
    upscale: int = 0
    keep_original: bool = True


def develop(photo: Image.Image, found: Found, rotation: int, options: Options,
            skew: float = 0.0) -> Image.Image:
    """One print, finished: flattened, straightened, stood up, cleaned."""
    flat = deskew(warp(photo, found), skew)
    flat = turn(flat, rotation)
    if options.fix_colours:
        flat = fix_colours(flat)
    if options.restore_faces:
        flat = restore_faces(flat)
    if options.upscale:
        flat = upscale(flat, options.upscale)
    return flat


_EXIF_IFD = 0x8769


def encode(img: Image.Image, when: When) -> bytes:
    """The finished print as a JPEG carrying its date, so the file says when
    it is from wherever it is copied, not only Ninaivu's index."""
    exif = Image.Exif()
    exif[0x0112] = 1                                    # Orientation: already upright
    exif[0x0131] = "Ninaivu print scan"                 # Software
    exif[0x010E] = "Scanned print"                      # ImageDescription
    if when.moment is not None:
        exif.get_ifd(_EXIF_IFD)[0x9003] = when.moment.strftime("%Y:%m:%d %H:%M:%S")
    buffer = io.BytesIO()
    img.convert("RGB").save(buffer, "JPEG", quality=94, exif=exif)
    return buffer.getvalue()


def date_fields(when: When) -> dict[str, Any]:
    """The index's date columns for a print somebody dated by hand.

    Marked ``manual`` either way, which is what keeps it through a rescan
    (db.upsert_asset). "Unknown year" is a statement too: without it the
    rescan would date the print by the day it was saved.
    """
    from ..archive.dates import to_timestamp            # noqa: PLC0415

    if when.moment is None:
        return {"captured_at": None, "date_key": "", "date_source": "manual"}
    return {"captured_at": to_timestamp(when.moment), "date_key": when.date_key,
            "date_source": "manual"}


def caption(when: When) -> str:
    if when.moment is None:
        return "Scanned print"
    if when.label.endswith("s"):
        return f"Scanned print from the {when.label}"
    return f"Scanned print from {when.label}"


class _Bytes:
    """Bytes with the ``save`` upload staging expects (upload_review.stage)."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.content_length = len(data)

    def save(self, stream) -> None:
        stream.write(self._data)


def _taken(conn, home: str, root: Path, path: Path) -> bool:
    return path.exists() or path.is_symlink() or bool(conn.execute(
        "SELECT 1 FROM assets WHERE root=? AND rel_path=?",
        (home, path.relative_to(root).as_posix())).fetchone())


def _publish(conn, folder: Path, root: Path, home: str, stem: str, suffix: str,
             data: bytes, *, plain_first: bool = False) -> Path:
    """Write *data* under the first free ``stem N`` name (or plain ``stem``
    first, for an original that has a name of its own). Never replaces a
    file: the bytes go to a hidden name first and are linked into place,
    which refuses a name that exists even when two saves race for it."""
    from . import date_edit                             # noqa: PLC0415

    folder.mkdir(parents=True, exist_ok=True)
    staged = folder / f".ninaivu-print-{secrets.token_hex(6)}.part"
    with staged.open("xb") as output:
        output.write(data)
    try:
        number = 0 if plain_first else 1
        while True:
            target = folder / (f"{stem} {number}{suffix}" if number else f"{stem}{suffix}")
            number += 1
            if _taken(conn, home, root, target):
                continue
            try:
                date_edit.publish_exclusive(staged, target)
            except FileExistsError:
                continue
            return target
    finally:
        staged.unlink(missing_ok=True)


def _index(conn, cfg: Any, library: str, home: str, root: Path, target: Path,
           overrides: dict[str, Any], *, manual_turn: int | None) -> int:
    """Index one new file at once, as an approved upload is, so it is in the
    gallery the moment the save says it is."""
    from ..storage import db                            # noqa: PLC0415
    from . import scanner                               # noqa: PLC0415

    rel = target.relative_to(root).as_posix()
    record = scanner.build_record(root, rel, target.stat(), cfg,
                                  rules=db.folder_rules(conn, library),
                                  manual_turn=manual_turn)
    record["root"] = home
    record.update(overrides)
    return db.upsert_asset(conn, record)


@dataclass
class Saver:
    """Where a batch's prints go, and on whose authority.

    An administrator's go straight into the library. Anyone else's wait for
    an administrator, exactly as an upload or an edited copy does, and are
    filed into the same folder when approved (upload_review.approve).
    """

    conn: Any
    cfg: Any
    library: str
    scope: str | None
    user_id: int
    is_admin: bool

    def __post_init__(self) -> None:
        self.home: str | None = None
        self.count = 0

    def _home(self) -> str:
        if self.home is None:
            from ..storage import new_files             # noqa: PLC0415
            # The library itself, or the new-files folder when it cannot be
            # written (an NTFS drive on a Mac) — see new_files.py.
            self.home = new_files.destination(self.cfg, self.library)
        return self.home

    def _folder(self, place: str) -> tuple[Path, Path]:
        root = Path(self._home()).resolve()
        folder = (root / place).resolve()
        if not folder.is_relative_to(root):
            raise ValueError("Scanned prints must stay inside the library.")
        return root, folder

    def print_(self, data: bytes, when: When) -> dict[str, Any]:
        place = folder_for(self.scope, when)
        overrides = {**date_fields(when), "caption": caption(when), "kind": "picture",
                     "rotation": 0, "rot_source": "manual"}
        self.count += 1
        if not self.is_admin:
            # Numbered within the batch; approval finds a free name beside
            # what is already in the folder (upload_review._free_target).
            return self._stage(data, f"Scanned print {self.count}.jpg", place, overrides)
        root, folder = self._folder(place)
        target = _publish(self.conn, folder, root, self._home(), "Scanned print", ".jpg", data)
        # Already stood up, so the scan is told the turn is settled (0) and
        # does not look for one of its own.
        asset_id = _index(self.conn, self.cfg, self.library, self._home(), root, target,
                          overrides, manual_turn=0)
        return {"status": "saved", "id": asset_id, "folder": place, "filename": target.name}

    def original(self, path: Path, name: str) -> dict[str, Any]:
        from ..utils.filenames import portable_name     # noqa: PLC0415

        place = originals_folder(self.scope)
        safe = portable_name(name, "photo.jpg")
        stem, suffix = Path(safe).stem or "photo", Path(safe).suffix.lower() or ".jpg"
        data = path.read_bytes()
        if not self.is_admin:
            return self._stage(data, f"{stem}{suffix}", place, None)
        root, folder = self._folder(place)
        target = _publish(self.conn, folder, root, self._home(), stem, suffix, data,
                          plain_first=True)
        asset_id = _index(self.conn, self.cfg, self.library, self._home(), root, target, {},
                          manual_turn=None)
        return {"status": "saved", "id": asset_id, "folder": place, "filename": target.name}

    def _stage(self, data: bytes, name: str, place: str,
               overrides: dict[str, Any] | None) -> dict[str, Any]:
        from . import upload_review                     # noqa: PLC0415

        staged = upload_review.stage(self.conn, self.cfg, _Bytes(data), name, self.library,
                                     self.scope or "", self.user_id,
                                     overrides=overrides, place=place)
        return {"status": "pending", "pending_id": staged["id"], "folder": place,
                "filename": name}


def run_batch(folder: Path, meta: dict[str, Any], picks: list[tuple[int, int]],
              options: Options, when: When, saver: Saver,
              report: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Develop and file every ticked print in a batch, then clear the batch.

    ``picks`` are (photograph, print) pairs from what :func:`examine` found.
    The originals of the photographs a print was picked from are kept in
    ``Scanned prints/originals`` unless asked not to be. One print that fails
    is reported and the rest are still saved.
    """
    report = report or (lambda update: None)
    photos = meta.get("photos") or []
    used = sorted({p for p, _ in picks})
    total = len(picks) + (len(used) if options.keep_original else 0)
    done = 0
    report({"stage": "running", "value": 0, "max": total})
    saved: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for photo_index in used:
        entry = photos[photo_index]
        path = folder / entry["file"]
        photo = open_photo(path)
        for p, print_index in picks:
            if p != photo_index:
                continue
            spot = entry["prints"][print_index]
            found = Found(tuple(tuple(c) for c in spot["quad"]), whole=bool(spot.get("whole")))
            try:
                finished = develop(photo, found, int(spot.get("rotation") or 0), options,
                                   float(spot.get("skew") or 0.0))
                saved.append(saver.print_(encode(finished, when), when))
            except (OSError, ValueError, RuntimeError) as error:
                log.warning("print scan: one print not saved: %s", error)
                failed.append({"photo": photo_index, "print": print_index,
                               "error": str(error)})
            done += 1
            report({"stage": "running", "value": done, "max": total})
        if options.keep_original:
            try:
                saver.original(path, entry.get("name") or entry["file"])
            except (OSError, ValueError) as error:
                failed.append({"photo": photo_index, "print": None, "error": str(error)})
            done += 1
            report({"stage": "running", "value": done, "max": total})
    shutil.rmtree(folder, ignore_errors=True)
    pending = any(item["status"] == "pending" for item in saved)
    return {"saved": saved, "failed": failed, "pending_approval": pending,
            "folder": folder_for(saver.scope, when),
            "first_id": next((item.get("id") for item in saved if item.get("id")), None)}
