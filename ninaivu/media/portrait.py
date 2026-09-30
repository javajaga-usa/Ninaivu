"""Finding the people in a photograph, so the retouching tools know where they are.

The Photo Studio's Skin and Hair tools used to offer one thing: a selection of
the largest face, made by seeding GrabCut with an ellipse. That selection
covered the eyes, the eyebrows and the lips, so smoothing it blurred them; it
ignored everybody else in the picture, which in a family photograph is most of
the people; and it never found any hair at all. A tool that needs you to paint
round every face by hand is a tool people open once.

This does what the tools need in order to be useful on the photographs a family
actually takes:

* **Every face**, each with its own model of its own skin. Skin is not one
  colour. A fair face and a dark one in the same frame need different
  treatment, and a single "skin colour" would either miss one of them or turn
  both towards the same tone. So each face samples itself — cheeks and
  forehead, located from the detector's landmarks — and everything below is
  judged against that person's own colour, lightness and variation.
* **Only skin**. Eyes, brows, lips, teeth and facial hair are taken out by
  geometry and by how far they are from *this* person's skin. So are the marks
  that are there on purpose: a bindi or kumkum, sindoor in a parting, sacred
  ash or sandal paste across a forehead. A smoothing tool that erases a
  vermilion dot has not retouched anything; it has made a mistake about what
  the photograph is of.
* **Hair and beard**, found from colour and position, with an honest
  "not sure" when the head is covered, bald, or the hair cannot be told from
  what is behind it.
* **What each face needs**, measured: whether it is in shadow (judged by the
  whites of that person's own eyes, never by how dark their skin is, so that
  a dark face in good light is not mistaken for an under-exposed one), how
  unevenly it is lit from one side, how shiny it is, how blotchy, how far its
  colour sits from natural skin. The measurements become the starting values
  the editor proposes, per person.

Nothing here decides anything for the person editing. The result is a set of
soft maps and some numbers; every one of them can be painted over, erased or
thrown away, and nothing is applied until a slider moves. No new model is
needed: the face detector the household already downloaded gives a box and five
landmarks, and the rest is colour and geometry. A face-parsing network
(:mod:`ninaivu.media.face_parser`), if the household has installed one, is used
for *where skin and hair are* at the places colour cannot tell — black hair
against a black wall — and is checked against each face before it is believed.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

try:
    import cv2
except ImportError:                                    # pragma: no cover
    cv2 = None                                         # type: ignore[assignment]
try:
    import numpy as np
except ImportError:                                    # pragma: no cover
    np = None                                          # type: ignore[assignment]

__all__ = ["available", "analyse", "engine_of", "pack_planes", "Analysis", "Frame", "Face", "SkinModel",
           "frame_of", "face_of", "fit_skin", "skin_likelihood", "WORK_SIZE", "MAX_FACES"]

#: The longest edge the analysis works at. Faces are found at this size; the
#: maps it returns are then scaled to whatever the editor is showing.
WORK_SIZE = 2400

#: Faces per photograph the retouching tools will take on.
MAX_FACES = 24

#: The eye distance, in pixels, above which a picture is looked at smaller, and the
#: least the smallest face in it may be brought down to.
BIG_FACE_D = 160.0
MIN_FACE_D = 34.0

#: Below this, a hair selection is more likely to be a stretch of wall than a head
#: of hair, and is not offered; the person paints it in instead.
MIN_HAIR_CONFIDENCE = 0.25


def available() -> bool:
    return cv2 is not None and np is not None


# ---------------------------------------------------------------------------
# Small maths
# ---------------------------------------------------------------------------

def _smoothstep(edge0: float, edge1: float, x):
    """0 at *edge0*, 1 at *edge1*, smooth between; works in either direction."""
    span = edge1 - edge0
    if abs(span) < 1e-9:
        return (x >= edge1).astype(np.float32)
    t = np.clip((x - edge0) / span, 0.0, 1.0)
    return (t * t * (3.0 - 2.0 * t)).astype(np.float32)


def _lab(rgb):
    """CIE Lab of an RGB uint8 picture: L in 0-100, a and b roughly -110..110."""
    return cv2.cvtColor(rgb.astype(np.float32) / 255.0, cv2.COLOR_RGB2LAB)


def _guided(guide, source, radius: int, eps: float = 2e-3):
    """Edge-aware smoothing of *source*, steered by *guide* (He et al.).

    It is what makes a soft mask snap to a real edge — the jaw line, the
    hairline — instead of bleeding across it, and it is a handful of box
    filters, so it costs almost nothing.
    """
    radius = max(1, int(radius))
    k = (2 * radius + 1, 2 * radius + 1)
    mean_i = cv2.boxFilter(guide, -1, k)
    mean_p = cv2.boxFilter(source, -1, k)
    cov = cv2.boxFilter(guide * source, -1, k) - mean_i * mean_p
    var = cv2.boxFilter(guide * guide, -1, k) - mean_i * mean_i
    a = cov / (var + eps)
    b = mean_p - a * mean_i
    return cv2.boxFilter(a, -1, k) * guide + cv2.boxFilter(b, -1, k)


def _masked_blur(plane, weight, sigma: float):
    """A Gaussian blur that only averages over where *weight* is: skin blurred
    over skin, and never over the eye, the hair or the wall beside it."""
    weight = weight.astype(np.float32)
    top = cv2.GaussianBlur(plane * weight, (0, 0), sigma)
    bottom = cv2.GaussianBlur(weight, (0, 0), sigma)
    return top / np.maximum(bottom, 1e-3)


def _soft_ellipse(u, v, centre_u: float, centre_v: float, radius_u: float,
                  radius_v: float, feather: float = 0.1):
    """1 inside the ellipse, 0 outside, a smooth edge *feather* wide (in radii)."""
    r = np.sqrt(((u - centre_u) / radius_u) ** 2 + ((v - centre_v) / radius_v) ** 2)
    return _smoothstep(1.0 + feather, 1.0 - feather, r)


def _fill_small_holes(binary, largest: int):
    """Close holes in a 0/255 mask that are smaller than *largest* pixels.

    A specular highlight on a forehead is a hole in a colour model — it has no
    colour to speak of — but it is unmistakably skin, because it is wholly
    surrounded by it. A gap as big as an eye is not, which is why the size is
    bounded.
    """
    h, w = binary.shape
    flooded = 255 - binary
    scratch = np.zeros((h + 2, w + 2), np.uint8)
    for seed in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        if flooded[seed[1], seed[0]] == 255:
            cv2.floodFill(flooded, scratch, seed, 0)
    count, labels, stats, _ = cv2.connectedComponentsWithStats((flooded > 0).astype(np.uint8), 8)
    filled = binary.copy()
    for label in range(1, count):
        if stats[label, cv2.CC_STAT_AREA] <= largest:
            filled[labels == label] = 255
    return filled


# ---------------------------------------------------------------------------
# A face's own coordinate system
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Frame:
    """Coordinates measured in *eye distances*, from the middle of the eyes.

    ``u`` runs along the line between the eyes (towards the subject's left,
    the right-hand side of the picture when the head is level) and ``v`` runs
    down the face. Expressed this way a face is the same shape at any size and
    at any tilt: the brows are always about -0.36 above the eye line, the
    mouth about +1.0 below it, and "the forehead" is a place rather than a
    pixel range — which is what lets one set of proportions serve a child, an
    adult, a face in profile and a head tipped over a shoulder.
    """

    x: float
    y: float
    d: float
    cos: float
    sin: float

    def coords(self, X, Y):
        dx, dy = X - self.x, Y - self.y
        return ((dx * self.cos + dy * self.sin) / self.d,
                (-dx * self.sin + dy * self.cos) / self.d)

    def point(self, u: float, v: float) -> tuple[float, float]:
        return (self.x + (u * self.cos - v * self.sin) * self.d,
                self.y + (u * self.sin + v * self.cos) * self.d)


def frame_of(landmarks) -> Frame | None:
    """The frame from the detector's five landmarks, or None if they are nonsense."""
    try:
        (rx, ry), (lx, ly) = landmarks[0], landmarks[1]
    except (TypeError, ValueError, IndexError):
        return None
    d = math.hypot(lx - rx, ly - ry)
    if not math.isfinite(d) or d < 6:
        return None
    angle = math.atan2(ly - ry, lx - rx)
    # A head tipped far past the shoulder has its landmarks in an order the
    # detector cannot be trusted about; do not build a frame on a guess.
    if abs(angle) > math.radians(55):
        return None
    return Frame((rx + lx) / 2, (ry + ly) / 2, d, math.cos(angle), math.sin(angle))


@dataclass(frozen=True)
class Face:
    """One detected face, in its own frame."""

    frame: Frame
    mouth: tuple[float, float, float]   # centre u, centre v, half-width — in eye distances
    box: tuple[float, float, float, float]
    score: float


def face_of(detected: dict[str, Any]) -> Face | None:
    """A :class:`Face` from one entry of ``FaceEngine.locate``."""
    points = detected.get("landmarks") or []
    frame = frame_of(points)
    if frame is None or len(points) < 5:
        return None
    (u1, v1) = frame.coords(np.float32(points[3][0]), np.float32(points[3][1]))
    (u2, v2) = frame.coords(np.float32(points[4][0]), np.float32(points[4][1]))
    half = float(math.hypot(u2 - u1, v2 - v1)) / 2.0
    if not (0.15 <= half <= 1.2):          # a mouth two or three times too wide is a bad landmark
        half = 0.42
    centre_v = float((v1 + v2) / 2)
    if not (0.4 <= centre_v <= 2.0):
        centre_v = 1.05
    box = tuple(float(v) for v in detected.get("box", (0, 0, 0, 0)))
    return Face(frame, (float((u1 + u2) / 2), centre_v, half), box, float(detected.get("score", 1.0)))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# One person's skin
# ---------------------------------------------------------------------------

@dataclass
class SkinModel:
    """What *this* face's skin looks like, so the rest can be judged against it."""

    lightness: float                 # median L*
    spread: float                    # robust spread of L* (lighting and shading)
    ab: Any                          # median (a*, b*)
    inverse: Any                     # inverse covariance of (a*, b*), regularised
    chroma: float
    hue: float                       # degrees, in the a*-b* plane
    samples: int


def _disc(shape, cx: float, cy: float, radius: float):
    h, w = shape[:2]
    x0, x1 = max(0, int(cx - radius - 1)), min(w, int(cx + radius + 2))
    y0, y1 = max(0, int(cy - radius - 1)), min(h, int(cy + radius + 2))
    if x1 <= x0 or y1 <= y0:
        return None
    yy, xx = np.mgrid[y0:y1, x0:x1]
    keep = (xx - cx) ** 2 + (yy - cy) ** 2 <= radius ** 2
    return yy[keep], xx[keep]


#: Where to read a face's skin: a cheek on each side and the middle of the
#: forehead, in eye distances. They are where skin is least likely to have
#: anything else on it.
SEEDS = ((-0.46, 0.58), (0.46, 0.58), (0.0, -0.72))


def fit_skin(lab, frame: Frame) -> SkinModel | None:
    """Sample the cheeks and forehead, throw out what is not skin, describe the rest.

    The patches are read *robustly* — a fringe over the forehead, a stud in the
    nose, a bindi or a shadow takes a few samples out of the pool, and the
    median and the median-absolute-deviation ignore them — so the model
    describes the skin and not the most recent thing that happened to it.
    """
    radius = max(2.0, 0.11 * frame.d)
    rows, cols = [], []
    for u, v in SEEDS:
        cx, cy = frame.point(u, v)
        hit = _disc(lab.shape, cx, cy, radius)
        if hit is not None:
            rows.append(hit[0])
            cols.append(hit[1])
    if not rows:
        return None
    ys, xs = np.concatenate(rows), np.concatenate(cols)
    if ys.size < 12:
        return None
    pixels = lab[ys, xs]
    median = np.median(pixels, axis=0)
    mad = 1.4826 * np.median(np.abs(pixels - median), axis=0)
    chroma_gap = np.hypot(pixels[:, 1] - median[1], pixels[:, 2] - median[2])
    keep = (chroma_gap <= 3.0 * max(float(mad[1]), float(mad[2]), 1.0) + 3.0) \
        & (np.abs(pixels[:, 0] - median[0]) <= 2.5 * max(float(mad[0]), 2.0) + 8.0)
    if keep.sum() >= max(10, 0.25 * keep.size):
        pixels = pixels[keep]
    ab = np.median(pixels[:, 1:], axis=0)
    covariance = np.cov((pixels[:, 1:] - ab).T) if len(pixels) > 3 else np.eye(2)
    covariance = covariance + np.eye(2) * 2.0 ** 2     # never tighter than two units of a* and b*
    try:
        inverse = np.linalg.inv(covariance)
    except np.linalg.LinAlgError:
        return None
    lightness = float(np.median(pixels[:, 0]))
    spread = float(1.4826 * np.median(np.abs(pixels[:, 0] - lightness)))
    return SkinModel(
        lightness=lightness, spread=max(spread, 3.0), ab=ab, inverse=inverse,
        chroma=float(np.hypot(ab[0], ab[1])),
        hue=float(math.degrees(math.atan2(ab[1], ab[0]))), samples=int(len(pixels)))


def skin_likelihood(lab, model: SkinModel, relax=None):
    """How much each pixel looks like this person's skin, 0 to 1.

    Two things decide it. Colour: distance from the person's own (a*, b*),
    tolerant near the centre and decaying away from it. And darkness *relative
    to their own skin*: hair, brows, lashes and the inside of a mouth are much
    darker than the face around them whatever the skin tone, which is what
    separates black hair from dark-brown skin where colour alone cannot (the
    two sit almost on top of each other in chromaticity). Brighter than the
    skin is not penalised — a highlight is still skin.
    """
    ab = lab[..., 1:]
    diff = ab - model.ab
    d2 = np.einsum("...i,ij,...j->...", diff, model.inverse, diff)
    colour = np.exp(-0.5 * np.maximum(d2 - 1.5, 0.0) / 6.0)
    sd = max(model.spread, 5.0)

    # A highlight is skin with less of its colour: oil and sweat reflect the
    # light as it is, and what comes back is brighter and paler along the very
    # line from the skin's colour towards white. Left to the test above it
    # would be the one place on a face that is "not skin", and so the one place
    # a shine control could not reach. So, for pixels brighter than the skin,
    # distance is also measured from that line — with a floor on how much of the
    # colour must remain, which is what keeps a white wall, collar or sari
    # beside the face from being taken for a highlight on it.
    own = np.asarray(model.ab, np.float32)
    own_sq = float(own @ own)
    if own_sq > 1.0:
        bright = _smoothstep(model.lightness + 4.0, model.lightness + 12.0, lab[..., 0])
        kept = np.clip((ab @ own) / own_sq, 0.4, 1.0)
        residual = ab - kept[..., None] * own
        d2_line = np.einsum("...i,ij,...j->...", residual, model.inverse, residual)
        paler = np.exp(-0.5 * np.maximum(d2_line - 1.5, 0.0) / 3.0)      # steeper: grey and white hair sit near the line too
        colour = np.maximum(colour, paler * bright)
    dark = _smoothstep(model.lightness - 3.2 * sd - 4.0, model.lightness - 1.3 * sd - 1.0, lab[..., 0])
    if relax is not None:
        # Where the only thing that can be dark is skin — the hollow under an
        # eye, which is the very thing someone wants to lift — darkness says
        # nothing and colour is left to decide.
        dark = np.maximum(dark, relax)
    return (colour * dark).astype(np.float32)


# ---------------------------------------------------------------------------
# The maps, face by face
# ---------------------------------------------------------------------------

def _window(frame: Frame, shape) -> tuple[int, int, int, int]:
    """The part of the picture around one face worth looking at: y0, y1, x0, x1.

    Generous on purpose: long hair and a full beard fall well below the face,
    and a turban or a bun rises well above it.
    """
    cx, cy = frame.point(0.0, 0.55)
    half = 3.7 * frame.d
    h, w = shape[:2]
    return (max(0, int(cy - half)), min(h, int(cy + half) + 1),
            max(0, int(cx - half)), min(w, int(cx + half) + 1))


def _keep_seeded(binary, frame: Frame, origin: tuple[int, int]):
    """Keep only the skin-coloured region the face's own seed patches sit in.

    A patch of wall, a neighbour's arm or a skin-coloured sofa may touch the
    oval without being part of the person. What belongs to the face is what is
    joined to the cheeks and forehead the model was read from.
    """
    count, labels, _, _ = cv2.connectedComponentsWithStats(binary, 8)
    if count <= 2:
        return binary
    votes: dict[int, int] = {}
    for u, v in SEEDS:
        cx, cy = frame.point(u, v)
        x, y = int(round(cx)) - origin[1], int(round(cy)) - origin[0]
        if 0 <= y < labels.shape[0] and 0 <= x < labels.shape[1] and labels[y, x]:
            votes[int(labels[y, x])] = votes.get(int(labels[y, x]), 0) + 1
    if not votes:
        return binary
    keep = np.isin(labels, list(votes))
    return np.where(keep, binary, 0).astype(np.uint8)


def _features(lab, u, v, model: SkinModel, face: Face):
    """The parts of a face that are not skin, found by where they are and how far they are from this person's skin: 0 to 1."""
    L = lab[..., 0]
    eyes = np.maximum(_soft_ellipse(u, v, -0.5, 0.0, 0.37, 0.22, 0.14),
                      _soft_ellipse(u, v, 0.5, 0.0, 0.37, 0.22, 0.14))
    sd = max(model.spread, 5.0)
    brow_zone = np.maximum(_soft_ellipse(u, v, -0.5, -0.36, 0.52, 0.16, 0.18),
                           _soft_ellipse(u, v, 0.5, -0.36, 0.52, 0.16, 0.18))
    # Dark brows are found by being dark, anywhere in the zone. A grey or white
    # brow — an elder's — is not darker than the skin and is found by where it
    # is: a band a brow's width, which costs a little skin no tool would have
    # improved by smoothing anyway.
    brow_band = np.maximum(_soft_ellipse(u, v, -0.5, -0.36, 0.38, 0.085, 0.3),
                           _soft_ellipse(u, v, 0.5, -0.36, 0.38, 0.085, 0.3))
    brows = np.maximum(brow_zone * _smoothstep(0.8 * sd + 3.0, 2.0 * sd + 8.0, model.lightness - L), brow_band)
    mu, mv, half = face.mouth
    mouth = _soft_ellipse(u, v, mu, mv, half * 1.22 + 0.05, 0.14 + 0.32 * half, 0.14)
    # Lipstick and the lip itself run past the ellipse: redder than the skin, near the mouth.
    redder = _smoothstep(7.0, 15.0, lab[..., 1] - model.ab[0]) \
        * _soft_ellipse(u, v, mu, mv, 0.95, 0.7, 0.25)
    excluded = 1.0 - (1.0 - eyes) * (1.0 - brows) * (1.0 - mouth) * (1.0 - redder)
    return excluded


def _skin_map(lab, u, v, model: SkinModel, face: Face, origin: tuple[int, int]):
    """Skin only: no eyes, brows, lips, teeth, hair or beard."""
    frame = face.frame
    L = lab[..., 0]
    chroma = np.hypot(lab[..., 1], lab[..., 2])
    oval = _soft_ellipse(u, v, 0.0, 0.34, 1.08, 1.52, 0.12)
    hollows = np.maximum(_soft_ellipse(u, v, -0.5, 0.30, 0.36, 0.14, 0.25),
                         _soft_ellipse(u, v, 0.5, 0.30, 0.36, 0.14, 0.25))
    # Skin turned away from the light — the far cheek of a face seen a little from the
    # side — is darker than the skin the model was read from, but it is the same
    # colour and it is still skin. Darkness is judged as a share of this skin's own
    # lightness: hair and brows are a third of it or less and stay out; a cheek in
    # shade is three-quarters and comes in.
    shade = _smoothstep(0.50, 0.68, L / max(model.lightness, 1.0))
    p = skin_likelihood(lab, model, relax=np.maximum(hollows, shade)) * oval

    # Pure white is not skin: the whites of the eyes beyond their ellipse, teeth,
    # a streak of paste. It has to be *pure*, and far brighter than the skin,
    # because a highlight on skin keeps some of the skin's colour and is exactly
    # the shine a person wants calmed — a tool cannot calm what it was told is
    # not skin. The eyes, mouth and brows are kept out by where they are.
    colourless = _smoothstep(model.lightness + 28.0, model.lightness + 40.0, L) \
        * (1.0 - _smoothstep(0.08 * model.chroma, 0.2 * model.chroma, chroma))
    p = p * (1.0 - 0.9 * colourless)

    face_area = math.pi * 1.08 * 1.52 * frame.d ** 2
    binary = _fill_small_holes((p > 0.35).astype(np.uint8) * 255, int(0.012 * face_area))
    binary = _keep_seeded(binary, frame, origin)
    guide = (L / 100.0).astype(np.float32)
    soft = np.clip(_guided(guide, binary.astype(np.float32) / 255.0,
                           max(2, int(round(0.045 * frame.d)))), 0.0, 1.0)
    soft = np.where(soft < 0.04, 0.0, soft) * oval

    excluded = _features(lab, u, v, model, face)
    return np.clip(soft * (1.0 - excluded), 0.0, 1.0).astype(np.float32)


def _protect_marks(lab, u, v, model: SkinModel, frame: Frame):
    """Marks worn on purpose, which no tool may smooth, fade or recolour.

    Four kinds, found by what sets them apart from skin and from hair:

    * a **red mark** between the brows — bindi, kumkum, a tilak: *saturated*
      red and *compact*. Both matter. Flushed skin is redder than the rest of
      a face but broadly so, and so is a henna-dyed fringe; a bindi is a small
      spot of colour skin never reaches, so it is a peak in the redness
      (a difference of Gaussians) as well as a high value of it;
    * a **black bindi** — the dark dot put on a child's forehead: a compact
      dark spot in the same place;
    * **vermilion** along a parting — sindoor: a narrow, strongly red line up
      the middle above the forehead, told from coloured hair by being a line;
    * **sacred ash or sandal paste** in stripes across the forehead — thin,
      bright and pale next to the skin around them, a ridge in lightness where
      a shiny highlight (broad and soft) is not one.
    """
    L, a, b = lab[..., 0], lab[..., 1], lab[..., 2]
    chroma = np.hypot(a, b)
    sd = max(model.spread, 5.0)

    def peak(plane, fine_sigma: float, wide_sigma: float):
        return cv2.GaussianBlur(plane, (0, 0), max(1.0, fine_sigma * frame.d)) \
            - cv2.GaussianBlur(plane, (0, 0), max(2.0, wide_sigma * frame.d))

    between_brows = _soft_ellipse(u, v, 0.0, -0.66, 0.20, 0.34, 0.25)
    red_peak = peak(a, 0.05, 0.16)
    red_mark = (_smoothstep(6.0, 14.0, red_peak) * _smoothstep(13.0, 23.0, a - model.ab[0])
                * _smoothstep(33.0, 47.0, chroma) * between_brows)
    # A black dot is far darker than the skin around it *in proportion to that
    # skin*: on light skin it is a long way down, on dark skin not as far in
    # absolute terms, so the test is a ratio.
    dark_peak = peak(-L, 0.05, 0.16)
    dark_dot = (_smoothstep(12.0, 24.0, dark_peak) * _smoothstep(0.72, 0.5, L / max(model.lightness, 1.0))
                * between_brows)

    parting = _soft_ellipse(u, v, 0.0, -1.6, 0.22, 0.85, 0.25)
    vermilion = (_smoothstep(5.0, 13.0, peak(a, 0.035, 0.12)) * _smoothstep(22.0, 32.0, a)
                 * _smoothstep(28.0, 42.0, chroma) * parting)

    # Ash and sandal paste go on in bands: *horizontal* lines, pale and bright
    # well beyond any shine, a hand's width long. Blurring hard along the line
    # and lightly across it makes a band a ridge and leaves a wrinkle, a
    # highlight or a fringe of hair — which are short, or soft, or dark — flat.
    def band_ridge(plane):
        along = 0.2 * frame.d
        fine = cv2.GaussianBlur(plane, (0, 0), sigmaX=max(1.0, along), sigmaY=max(1.0, 0.03 * frame.d))
        wide = cv2.GaussianBlur(plane, (0, 0), sigmaX=max(1.0, along), sigmaY=max(2.0, 0.13 * frame.d))
        return fine - wide

    band = _soft_ellipse(u, v, 0.0, -0.78, 0.6, 0.4, 0.2)
    ash = (_smoothstep(6.0, 14.0, band_ridge(L)) * _smoothstep(model.lightness + 12.0, model.lightness + 22.0, L)
           * (1.0 - _smoothstep(0.30 * model.chroma, 0.55 * model.chroma, chroma)) * band)

    marks = np.maximum.reduce([red_mark, dark_dot, vermilion, ash]).astype(np.float32)
    grow = max(1, int(round(0.04 * frame.d)))
    marks = cv2.dilate(marks, np.ones((2 * grow + 1, 2 * grow + 1), np.uint8))
    return np.clip(cv2.GaussianBlur(marks, (0, 0), float(grow)), 0.0, 1.0).astype(np.float32)


def _under_eye(u, v, skin):
    """The hollow under each eye, where dark circles are, limited to skin."""
    zone = np.maximum(_soft_ellipse(u, v, -0.5, 0.30, 0.38, 0.17, 0.3),
                      _soft_ellipse(u, v, 0.5, 0.30, 0.38, 0.17, 0.3))
    return (zone * skin).astype(np.float32)


def _light_zone(lab, u, v, model: SkinModel, face: Face, origin: tuple[int, int]):
    """Where "face light" may reach: the face, and the skin joined to it, as one piece.

    Light that is brought to a face has to go everywhere the face goes — the eyes
    and lips as well as the skin, the ears, the neck under the chin — or there is
    a seam where the lift stops. And it must stop where the person does: a zone
    drawn as an ellipse round the face lifts the wall beside the jaw as well, and
    leaves a pale halo that is the first thing anybody sees. So it is found the way
    the skin is, from colour and from what is joined to the cheeks, with the eyes,
    brows and mouth counted in (they are holes in the skin, wholly surrounded by
    it), and is snapped to the real edge of the face. Where colour fails — heavy
    make-up, a turned or shadowed cheek — the middle of the face is a fallback.
    """
    frame = face.frame
    L = lab[..., 0]
    reach = _soft_ellipse(u, v, 0.0, 0.55, 1.5, 2.3, 0.2)
    # Skin in shadow — the far cheek, the neck under the chin — is darker than the
    # skin the model was read from but still skin. Dark is judged here as a share
    # of the skin's own lightness: hair, at a third of it or less, is out; a neck
    # at two-thirds is in.
    shade = _smoothstep(0.42, 0.62, L / max(model.lightness, 1.0))
    p = skin_likelihood(lab, model, relax=shade) * reach
    face_area = math.pi * 1.08 * 1.52 * frame.d ** 2
    binary = _fill_small_holes((p > 0.3).astype(np.uint8) * 255, int(0.06 * face_area))
    binary = _keep_seeded(binary, frame, origin)
    guide = (L / 100.0).astype(np.float32)
    joined = np.clip(_guided(guide, binary.astype(np.float32) / 255.0, max(2, int(round(0.05 * frame.d)))), 0.0, 1.0)
    joined = np.where(joined < 0.04, 0.0, joined) * reach
    middle = 0.9 * _soft_ellipse(u, v, 0.0, 0.34, 0.85, 1.25, 0.3)
    return np.maximum(joined, middle).astype(np.float32)


def _skin_body(lab, u, v, model: SkinModel, face: Face, origin: tuple[int, int]):
    """All of this person's exposed skin that goes with the face: the ears, the neck, and the face itself.

    The skin map is the face, and only the face: it stops at the jaw, because
    that is where smoothing should stop. But a *colour* — a cast turned back, a
    little more richness — that stops at the jaw leaves a line round the face
    where the neck is another colour. So colour is applied to this, which is found
    the way the skin is, from the cheeks outward through everything skin-coloured
    that is joined to them, and which keeps the eyes, brows and lips out (they are
    holes in it, left as holes).
    """
    frame = face.frame
    L = lab[..., 0]
    reach = _soft_ellipse(u, v, 0.0, 0.55, 1.5, 2.3, 0.2)
    shade = _smoothstep(0.42, 0.62, L / max(model.lightness, 1.0))
    binary = _keep_seeded(((skin_likelihood(lab, model, relax=shade) * reach) > 0.3).astype(np.uint8) * 255, frame, origin)
    guide = (L / 100.0).astype(np.float32)
    soft = np.clip(_guided(guide, binary.astype(np.float32) / 255.0, max(2, int(round(0.04 * frame.d)))), 0.0, 1.0)
    # The edge-snapping blurs a little across an eye, which is a few pixels high.
    return (np.where(soft < 0.04, 0.0, soft) * reach * (1.0 - _features(lab, u, v, model, face))).astype(np.float32)


def _parsed_body(lab, u, v, planes, frame: Frame):
    """The same, from a network: skin, ears and neck, less the features."""
    guide = (lab[..., 0] / 100.0).astype(np.float32)
    claim = np.clip(planes["skin"] + planes["neck"], 0.0, 1.0) * (1.0 - planes["features"])
    soft = np.clip(_guided(guide, claim.astype(np.float32), max(2, int(round(0.04 * frame.d)))), 0.0, 1.0)
    return (np.where(soft < 0.04, 0.0, soft) * (1.0 - planes["features"])
            * _soft_ellipse(u, v, 0.0, 0.55, 1.5, 2.3, 0.2)).astype(np.float32)


# ---------------------------------------------------------------------------
# Hair and beard
# ---------------------------------------------------------------------------

@dataclass
class HairResult:
    weight: Any                       # float32 map, 0-1
    confidence: float                 # 0 (nothing reliable) to 1
    lightness: float                  # median L* of the hair
    chroma: float
    grey: float                       # share of the hair that is grey or white
    colour: tuple[int, int, int]      # its median colour as RGB


def _head_prior(u, v):
    """Where hair can be: round the head, and down beside the face for long hair.

    Generous and soft, and with the face itself cut out. It does not say where
    the hair *is*, only where it would be plausible to look for it, so that a
    dark wall behind the head cannot become hair simply by being the same
    colour, and eyes and brows cannot become hair by being dark. Bangs over the
    forehead are allowed: the keep-out starts at the brows. Most hair rises
    less than a face-height above the hairline, so the cap is that tall and no
    taller; a bun, a turban or a great deal of volume will be cut short, and the
    brush is for that.
    """
    cap = _soft_ellipse(u, v, 0.0, -0.95, 1.5, 1.15, 0.18)
    side = np.maximum(_soft_ellipse(u, v, -1.3, 1.3, 0.75, 2.6, 0.25),
                      _soft_ellipse(u, v, 1.3, 1.3, 0.75, 2.6, 0.25))
    face = _soft_ellipse(u, v, 0.0, 0.55, 1.02, 1.15, 0.12)
    return np.maximum(cap, 0.9 * side) * (1.0 - face)


def _hairline(u, v, skin) -> float:
    """The top of this person's forehead, in eye distances (negative is up)."""
    column = (np.abs(u) <= 0.3) & (skin > 0.5)
    return float(np.min(v[column])) if column.any() else -1.2


def _grabcut_hair(rgb, region, prior, skin, scale_to: int = 380):
    """Refine a rough hair region to the real edges, on a small copy.

    Colour alone cannot tell dark-brown hair from a black wall a little darker
    than it, but the edge between them is there to be found, and GrabCut finds
    edges. It is run small — it is superlinear in pixels and the result is
    snapped to the full-size edges afterwards by a guided filter — and only on
    an area the prior allows, with the skin and the face marked as definitely
    not hair.
    """
    # GrabCut starts its colour models from random clusters, so the same face gave
    # a slightly different edge on every look. A fixed seed makes it the same one.
    cv2.setRNGSeed(20260930)
    h, w = region.shape
    scale = min(1.0, scale_to / float(max(h, w)))
    size = (max(8, int(round(w * scale))), max(8, int(round(h * scale))))
    small = cv2.resize(rgb, size, interpolation=cv2.INTER_AREA)
    region_s = cv2.resize(region, size, interpolation=cv2.INTER_NEAREST) > 0
    prior_s = cv2.resize(prior, size, interpolation=cv2.INTER_AREA)
    skin_s = cv2.resize(skin, size, interpolation=cv2.INTER_AREA)
    mask = np.full(size[::-1], cv2.GC_PR_BGD, np.uint8)
    mask[prior_s < 0.05] = cv2.GC_BGD
    mask[skin_s > 0.35] = cv2.GC_BGD
    mask[region_s] = cv2.GC_PR_FGD
    core = cv2.erode(region_s.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
    mask[core & (prior_s > 0.6)] = cv2.GC_FGD
    if not (mask == cv2.GC_FGD).any() or not (mask == cv2.GC_BGD).any():
        return None
    try:
        cv2.grabCut(cv2.cvtColor(small, cv2.COLOR_RGB2BGR), mask, None,
                    np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64),
                    3, cv2.GC_INIT_WITH_MASK)
    except cv2.error:
        return None
    refined = np.where((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)
    return cv2.resize(refined, (w, h), interpolation=cv2.INTER_LINEAR)


def _hair_map(lab, rgb, u, v, model: SkinModel, face: Face, skin) -> HairResult | None:
    """Hair, from the colour just above this person's hairline and where hair goes."""
    frame = face.frame
    L, a, b = lab[..., 0], lab[..., 1], lab[..., 2]
    chroma = np.hypot(a, b)
    likelihood = skin_likelihood(lab, model)

    line = _hairline(u, v, skin)
    band = (v >= line - 0.5) & (v <= line - 0.1) & (np.abs(u) <= 0.6)
    sample = band & (likelihood < 0.25)
    if sample.sum() < max(24, 0.3 * band.sum()):
        return None                               # bald, capped, or the crown is out of the picture
    pixels = lab[sample]
    median = np.median(pixels, axis=0)
    mad = 1.4826 * np.median(np.abs(pixels - median), axis=0)
    hair_chroma = float(np.hypot(median[1], median[2]))
    # A bright, strongly coloured crown is cloth, a cap or a scarf, not hair.
    if hair_chroma > 38.0 and median[0] > 38.0:
        return None

    covariance = np.cov(pixels.T) + np.diag([6.0 ** 2, 3.0 ** 2, 3.0 ** 2])   # sheen and shade vary, but not like a room
    try:
        inverse = np.linalg.inv(covariance)
    except np.linalg.LinAlgError:
        return None
    diff = lab - median
    d2 = np.einsum("...i,ij,...j->...", diff, inverse, diff)
    prior = _head_prior(u, v)
    p = np.exp(-0.5 * np.maximum(d2 - 2.0, 0.0) / 6.0) * (1.0 - likelihood) * prior

    # Can this hair be told from what is behind it? Look at a ring just outside
    # where hair can be: if that is also hair-coloured, no amount of cleverness
    # with colour will say where one stops — a grey moon behind brown hair, a
    # black wall behind black hair — and the honest answer is to say so.
    distance = np.hypot(u, v - 0.3)
    ring = (prior < 0.04) & (distance > 1.9) & (distance < 3.3) & (likelihood < 0.25)
    # Above the head is where hair meets what is behind it. In a group, the sides
    # of the ring are other people, whose hair and faces say nothing about the wall
    # behind this one — so the test is made overhead where it can, and all round
    # only when there is not enough overhead to read.
    overhead = ring & (v < -0.6) & (np.abs(u) < 1.6)
    chosen = overhead if overhead.sum() > 50 else ring
    background_like = float(np.mean(np.exp(-0.5 * np.maximum(d2[chosen] - 2.0, 0.0) / 6.0))) if chosen.sum() > 50 else 0.0
    if background_like > 0.6:
        return None

    binary = (p > 0.32).astype(np.uint8) * 255
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    count, labels, _, _ = cv2.connectedComponentsWithStats(binary, 8)
    seeds = labels[sample]
    seeds = seeds[seeds > 0]
    if seeds.size == 0:
        return None
    region = (labels == int(np.bincount(seeds).argmax())).astype(np.uint8) * 255
    refined = _grabcut_hair(rgb, region, prior.astype(np.float32), skin.astype(np.float32))
    agreement = 0.0
    if refined is not None:
        union = np.count_nonzero((region > 0) | (refined > 0))
        agreement = float(np.count_nonzero((region > 0) & (refined > 0))) / union if union else 0.0
        area_ratio = np.count_nonzero(refined) / max(1.0, float(np.count_nonzero(region)))
        # The colour region over-claims wherever the background is about the
        # colour of the hair, so a refinement that is smaller is the point of
        # it. What GrabCut does wrong is claim *more* than the colour allowed,
        # silently: then the colour is believed and the two are said to disagree.
        if area_ratio <= 1.25 and np.count_nonzero(refined) > 0:
            region = refined
        else:
            agreement *= 0.5
    region = _fill_small_holes(region, int(0.05 * math.pi * frame.d ** 2))

    face_area = math.pi * 1.08 * 1.52 * frame.d ** 2
    area = float(np.count_nonzero(region))
    if area < 0.12 * face_area or area > 7.0 * face_area:
        return None                              # nothing there, or a wall the colour of hair
    edge = np.concatenate([region[0], region[-1], region[:, 0], region[:, -1]])
    leaked = float(np.count_nonzero(edge)) / max(1, edge.size)
    guide = (L / 100.0).astype(np.float32)
    soft = np.clip(_guided(guide, region.astype(np.float32) / 255.0,
                           max(2, int(round(0.05 * frame.d)))), 0.0, 1.0)
    soft = np.where(soft < 0.04, 0.0, soft) * prior.clip(0.0, 1.0).astype(np.float32) ** 0.35 \
        * (1.0 - np.clip(skin * 1.2, 0.0, 1.0))

    confidence = float(np.clip(1.0 - 2.5 * leaked, 0.0, 1.0)
                       * (0.45 + 0.55 * (agreement if refined is not None else 0.5))
                       * (0.65 + 0.35 * float(np.clip(1.0 - float(mad[0]) / 25.0, 0.0, 1.0)))
                       * float(np.clip(1.25 - 1.6 * background_like, 0.25, 1.0)))
    return _hair_result(lab, soft, confidence)


def _hair_result(lab, soft, confidence: float) -> HairResult | None:
    """What a hair selection looks like: how light, how coloured, how much of it is grey."""
    L = lab[..., 0]
    chroma = np.hypot(lab[..., 1], lab[..., 2])
    inside = soft > 0.5
    if inside.sum() < 12:
        return None
    in_l, in_c = L[inside], chroma[inside]
    # Grey or white: light for hair and all but colourless. Judged in absolute
    # terms, because hair that is entirely grey has a grey median and would
    # otherwise look like hair with nothing to cover. Blonde and henna-red are
    # light and coloured, and black hair's highlights are a thin tail; neither
    # is counted.
    grey = float(np.mean((in_l > 48.0) & (in_c < 12.0)))
    colour = cv2.cvtColor(np.array([[np.median(lab[inside], axis=0)]], np.float32), cv2.COLOR_LAB2RGB)[0, 0]
    return HairResult(soft.astype(np.float32), float(confidence), float(np.median(in_l)),
                      float(np.median(in_c)), grey,
                      tuple(int(round(float(c) * 255)) for c in colour))


def _beard_map(lab, u, v, model: SkinModel, face: Face, hair_weight=None):
    """Moustache and beard: much darker than the skin, much less coloured, in the lower face.

    Both tests are needed. A neck in shadow is darker than the face but is still
    skin-coloured; lips are coloured more, not less. "Much darker" is judged as a
    share of *this* skin's lightness, because a black beard on very dark skin is
    a few tens of units of lightness down where on fair skin it is sixty. Stubble
    is partly skin, so the map is graded rather than on or off — darker means
    more beard — and is only believed when enough of the lower face is covered to
    be a beard at all. Long hair falling beside the jaw is dark and colourless
    too, which is why the hair that was found is taken out first, and the region
    stops at the edge of the face rather than running out to meet it.
    """
    frame = face.frame
    L = lab[..., 0]
    chroma = np.hypot(lab[..., 1], lab[..., 2])
    lower = _soft_ellipse(u, v, 0.0, 1.25, 0.98, 0.78, 0.2)
    share = L / max(model.lightness, 1.0)
    darker = _smoothstep(0.82, 0.56, share) * _smoothstep(6.0, 14.0, model.lightness - L)
    hairlike = 1.0 - _smoothstep(0.35 * model.chroma, 0.7 * model.chroma, chroma)
    mu, mv, half = face.mouth
    mouth_core = _soft_ellipse(u, v, mu, mv + 0.02, half * 0.95 + 0.04, 0.10 + 0.22 * half, 0.25)
    candidate = darker * hairlike * lower * (1.0 - mouth_core)
    if hair_weight is not None:
        candidate = candidate * (1.0 - np.clip(hair_weight * 1.5, 0.0, 1.0))
    guide = (L / 100.0).astype(np.float32)
    soft = np.clip(_guided(guide, candidate.astype(np.float32), max(2, int(round(0.05 * frame.d)))), 0.0, 1.0)
    # A beard covers the chin and jaw; hair falling beside the face covers only
    # the edges of the region. So the centre is what counts. A moustache on its
    # own is a band above the lip, too little of the region to count, and is
    # judged where it is.
    inner = (lower > 0.5) & (np.abs(u) < 0.7)
    coverage = float(soft[inner].mean()) if inner.any() else 0.0
    band = _soft_ellipse(u, v, mu, mv - 0.17, half + 0.1, 0.11, 0.1) > 0.5
    moustache = float(soft[band].mean()) if band.any() else 0.0
    if coverage < 0.15 and moustache < 0.45:
        return None, coverage
    return soft.astype(np.float32), max(coverage, 0.5 * moustache)


# ---------------------------------------------------------------------------
# With a face-parsing network, when there is one
# ---------------------------------------------------------------------------

def _parse(parser, rgb, face: Face, window: tuple[int, int, int, int]):
    """What a face-parsing network makes of one face, as soft planes over its window — or None.

    The network is shown a square of the picture round the face, at the size
    faces fill in what it was trained on, and what it says is checked against the
    face before it is believed: the cheeks the skin model was read from must be
    skin to it, and the amount of skin it found must be about a face's worth.
    A face it gets wrong is left to the built-in method.
    """
    from . import face_parser                          # noqa: PLC0415

    frame = face.frame
    cx, cy = frame.point(0.0, 0.35)
    half = 2.3 * frame.d
    height, width = rgb.shape[:2]
    cy0, cy1 = max(0, int(cy - half)), min(height, int(cy + half) + 1)
    cx0, cx1 = max(0, int(cx - half)), min(width, int(cx + half) + 1)
    if cy1 - cy0 < 32 or cx1 - cx0 < 32:
        return None
    crop = np.ascontiguousarray(rgb[cy0:cy1, cx0:cx1])
    try:
        probabilities = parser.probabilities(crop, origin=(cy0, cx0))
    except Exception as exc:                            # noqa: BLE001
        log.debug("face parsing failed: %s", exc)
        return None
    if probabilities is None or probabilities.shape[1:] != crop.shape[:2]:
        return None

    y0, y1, x0, x1 = window

    def place(plane):
        out = np.zeros((y1 - y0, x1 - x0), np.float32)
        oy0, oy1, ox0, ox1 = max(y0, cy0), min(y1, cy1), max(x0, cx0), min(x1, cx1)
        if oy1 > oy0 and ox1 > ox0:
            out[oy0 - y0:oy1 - y0, ox0 - x0:ox1 - x0] = plane[oy0 - cy0:oy1 - cy0, ox0 - cx0:ox1 - cx0]
        return out

    planes = {name: place(probabilities[list(labels)].sum(axis=0))
              for name, labels in (("skin", face_parser.SKIN), ("features", face_parser.FEATURES),
                                   ("hair", face_parser.HAIR), ("neck", face_parser.NECK))}
    face_area = math.pi * 1.08 * 1.52 * frame.d ** 2
    found = float(planes["skin"].sum())
    if not (0.25 * face_area <= found <= 1.5 * face_area):
        return None
    cheeks = []
    for u_, v_ in SEEDS[:2]:
        px, py = frame.point(u_, v_)
        ix, iy = int(round(px)) - x0, int(round(py)) - y0
        if 0 <= iy < planes["skin"].shape[0] and 0 <= ix < planes["skin"].shape[1]:
            cheeks.append(float(planes["skin"][iy, ix]))
    if not cheeks or float(np.mean(cheeks)) < 0.5:
        return None
    return planes


def _parsed_skin(lab, u, v, planes, frame: Frame):
    """Skin from the network: its skin, less the features, snapped to the real edge of the face."""
    guide = (lab[..., 0] / 100.0).astype(np.float32)
    claim = np.clip(planes["skin"] * (1.0 - planes["features"]), 0.0, 1.0).astype(np.float32)
    soft = np.clip(_guided(guide, claim, max(2, int(round(0.04 * frame.d)))), 0.0, 1.0)
    # The edge-snapping blurs a little across the small things (an eye is a few
    # pixels high); what the network said was a feature stays out.
    return (np.where(soft < 0.04, 0.0, soft) * (1.0 - planes["features"])
            * _soft_ellipse(u, v, 0.0, 0.34, 1.2, 1.65, 0.12)).astype(np.float32)


def _parsed_zone(lab, u, v, planes, frame: Frame, beard=None):
    """Where light may reach, from the network: the face, its features and the neck, as one piece."""
    guide = (lab[..., 0] / 100.0).astype(np.float32)
    whole = np.clip(planes["skin"] + planes["features"] + planes["neck"], 0.0, 1.0).astype(np.float32)
    soft = np.clip(_guided(guide, whole, max(2, int(round(0.05 * frame.d)))), 0.0, 1.0)
    zone = np.where(soft < 0.04, 0.0, soft) * _soft_ellipse(u, v, 0.0, 0.55, 1.5, 2.3, 0.2)
    return zone.astype(np.float32)


def _parsed_hair(lab, planes, frame: Frame) -> HairResult | None:
    """Hair from the network. Its confidence is how sure the network was: a selection it was torn about is not trusted."""
    guide = (lab[..., 0] / 100.0).astype(np.float32)
    claim = np.clip(planes["hair"] * (1.0 - np.clip(planes["skin"] + planes["features"], 0.0, 1.0)), 0.0, 1.0).astype(np.float32)
    soft = np.clip(_guided(guide, claim, max(2, int(round(0.04 * frame.d)))), 0.0, 1.0)
    soft = np.where(soft < 0.04, 0.0, soft).astype(np.float32)
    settled = soft > 0.5
    if int(settled.sum()) < 0.12 * math.pi * 1.08 * 1.52 * frame.d ** 2:
        return None
    # Sure where the choice between hair and not-hair was lopsided.
    sureness = float(np.mean(np.abs(2.0 * planes["hair"][settled] - 1.0)))
    return _hair_result(lab, soft, 0.55 + 0.45 * sureness)


# ---------------------------------------------------------------------------
# What each face needs
# ---------------------------------------------------------------------------

#: Where natural skin sits on the hue circle of the a*-b* plane, in degrees.
#: Skin of every complexion falls along one line in colour — the lightness and
#: the richness change from person to person, the direction hardly does — so a
#: face that sits well off it is a colour cast, not a complexion.
SKIN_LINE = 48.0


def _lightness_to_luminance(l):
    """Relative luminance of a CIE lightness, for working out how many stops a lift is."""
    l = np.asarray(l, np.float64)
    return np.where(l > 8.0, ((l + 16.0) / 116.0) ** 3, l / 903.3)


def _stops_between(l_from: float, l_to: float) -> float:
    return float(math.log2(max(float(_lightness_to_luminance(l_to)), 1e-6)
                           / max(float(_lightness_to_luminance(l_from)), 1e-6)))


def _eye_white(lab, u, v, model: SkinModel, face: Face) -> float | None:
    """How bright the whites of this person's eyes are, or None if they cannot be read.

    The one part of a face whose brightness does not depend on the person's
    complexion. A dark-skinned face in front of a white wall is darker than the
    wall, and so is a medium-skinned face in a backlit doorway; skin alone cannot
    say which of them is under-exposed, and a tool that guessed from skin would
    lighten dark skin and call it a correction. The whites can: in a well
    exposed photograph they are near the brightest thing on the face whoever the
    face belongs to, so when they are dim the light is what is wrong. Too small
    a face, closed eyes or a shut-eyed blink give no reading, and then nothing
    is suggested.
    """
    if face.frame.d < 30.0:
        return None
    L = lab[..., 0]
    chroma = np.hypot(lab[..., 1], lab[..., 2])
    strip = (np.abs(np.abs(u) - 0.5) < 0.24) & (np.abs(v) < 0.09)
    # Well above the skin, in *light* and not in lightness (which bunches up in
    # the dark, where a white is only a few units above the skin but still twice
    # as bright): an eyelid's shine or the edge of an iris is not a white.
    candidate = strip & (chroma < 13.0) \
        & (_lightness_to_luminance(L) > 1.6 * float(_lightness_to_luminance(model.lightness)))
    if int(candidate.sum()) < 8:
        return None
    return float(np.percentile(L[candidate], 85))


def measure_face(lab, u, v, model: SkinModel, skin, under, hair: HairResult | None,
                 beard_coverage: float, face: Face, scene_lightness: float, ring_lightness: float | None):
    """Numbers about one face, and the starting values they suggest.

    A suggestion is deliberately modest and only made when the measurement is
    clear. Nothing here decides: these become the initial slider positions,
    marked as suggested, and every one is the person's to change.
    """
    L = lab[..., 0]
    a, b = lab[..., 1], lab[..., 2]
    chroma = np.hypot(a, b)
    inside = skin > 0.6
    if inside.sum() < 40:
        return {}, {}
    skin_l = float(np.median(L[inside]))
    skin_c = float(np.median(chroma[inside]))
    skin_h = float(math.degrees(math.atan2(float(np.median(b[inside])), float(np.median(a[inside])))))

    reference = ring_lightness if ring_lightness is not None else scene_lightness
    backlit = float(reference - skin_l)
    white = _eye_white(lab, u, v, model, face)

    left, right = inside & (u < -0.05), inside & (u > 0.05)
    asymmetry = 0.0
    if left.sum() > 30 and right.sum() > 30:
        asymmetry = float(np.median(L[right]) - np.median(L[left]))

    # Shine: skin that is lighter than the skin around it and has lost its colour.
    bright = inside & (L > skin_l + 9.0) & (chroma < 0.8 * skin_c)
    shine = float(bright.sum()) / float(inside.sum())
    forehead = inside & (v < -0.3)
    cheeks = inside & (v > 0.3) & (v < 1.0)
    forehead_gap = float(np.median(L[forehead]) - np.median(L[cheeks])) \
        if forehead.sum() > 30 and cheeks.sum() > 30 else 0.0

    # Blotchiness: how much colour wanders at the scale of a patch — bigger
    # than a pore or a grain of sensor noise, smaller than the shading of the
    # face — taking the skin alone into account.
    fine, wide = max(1.0, 0.05 * face.frame.d), max(1.5, 0.22 * face.frame.d)
    wander = []
    for plane in (a, b):
        wander.append((_masked_blur(plane, skin, fine) - _masked_blur(plane, skin, wide))[inside])
    blotch = float(np.sqrt(np.mean(np.concatenate(wander) ** 2)))

    under_zone = under > 0.4
    dark_circles = 0.0
    if under_zone.sum() > 20 and cheeks.sum() > 30:
        dark_circles = float(np.median(L[cheeks]) - np.median(L[under_zone]))

    reliable_hue = skin_l > 28.0 and skin_c > 8.0
    hue_offset = skin_h - SKIN_LINE if reliable_hue else 0.0

    measure = {
        "lightness": round(skin_l, 1), "chroma": round(skin_c, 1), "hue": round(skin_h, 1),
        "backlit": round(backlit, 1), "asymmetry": round(asymmetry, 1),
        "shine": round(shine, 3), "foreheadGap": round(forehead_gap, 1),
        "blotch": round(blotch, 2), "underEye": round(dark_circles, 1),
        "hueOffset": round(hue_offset, 1), "beard": round(beard_coverage, 2),
        "eyeWhite": round(white, 1) if white is not None else None,
    }
    suggest: dict[str, float] = {}

    # A face whose own whites are dim is in shadow, and is brought up by the
    # number of stops that puts them where a well lit face has them — the same
    # exposure change for skin of any colour, so a dark face stays as dark as
    # it is. Nothing is suggested when the whites cannot be read, and never
    # from how dark the skin is: skin that is simply dark is not under-exposed.
    if white is not None and white < 66.0 and backlit > 8.0:
        stops = min(1.4, _stops_between(white, 76.0))
        if stops > 0.2:
            suggest["faceLight"] = float(min(90.0, 100.0 * stops / 1.5))
    if abs(asymmetry) > 16.0:
        suggest["balance"] = float(min(70.0, 3.0 * (abs(asymmetry) - 8.0)))
    # The thresholds below sit near where one face in ten stands out from the
    # rest (measured on a family library of some ten thousand faces), so that a
    # suggestion means this face differs from most, not that it is a face.
    if shine > 0.15 or forehead_gap > 18.0:
        suggest["shine"] = float(min(60.0, 250.0 * shine + 2.0 * max(0.0, forehead_gap - 11.0)))
    if blotch > 2.5:
        suggest["even"] = float(min(45.0, 18.0 * (blotch - 1.9)))
    if dark_circles > 8.0:
        suggest["underEye"] = float(min(55.0, 5.0 * (dark_circles - 4.0)))
    # Skin sits in a band of hues, and complexions differ within it. A cast is
    # a face well outside the band — the excess beyond it is what is offered to
    # take away, and never the whole distance to an average.
    if reliable_hue and abs(hue_offset) > 18.0:
        excess = abs(hue_offset) - 6.0
        suggest["tone"] = float(math.copysign(min(80.0, 0.7 * excess / 0.12), -hue_offset))
    # Washed out or ashen: little colour for skin this light. Skin that is dark
    # has less chroma in absolute terms and is not ashen for it.
    if skin_l > 38.0 and skin_c < 11.5 and skin_h == skin_h:
        suggest["richness"] = float(min(40.0, 5.0 * (14.0 - skin_c)))
    if hair is not None and hair.confidence > 0.45:
        if hair.grey > 0.15:
            suggest["greyCover"] = float(min(85.0, 250.0 * hair.grey + 25.0))
        if hair.lightness < 20.0:
            suggest["hairDetail"] = 30.0
    return measure, {k: round(v, 1) for k, v in suggest.items() if v >= 5.0}


@dataclass
class Analysis:
    """Every face in a picture, with the soft maps the editor works from."""

    size: tuple[int, int]                           # (width, height) of the picture analysed
    faces: list[dict[str, Any]]
    planes: dict[str, Any]                          # name -> float32 (H, W), 0-1
    skin_id: Any                                    # uint8 (H, W): which face's skin and face zone this is
    hair_id: Any                                    # uint8 (H, W): which face's hair and beard this is
    cast: dict[str, float]


def analyse(rgb, located: list[dict[str, Any]], *, limit: int = MAX_FACES, parser=None) -> Analysis:
    """Everything the retouching tools need to know about the people in *rgb*.

    *located* is ``FaceEngine.locate``'s answer for the same picture. Each face
    is worked out in its own window, so the cost grows with the number of faces
    and their size, not with the size of the photograph.

    *parser*, if given, is a :class:`~ninaivu.media.face_parser.FaceParser`. Where
    it gives a sensible answer for a face, its skin, zone and hair replace the
    built-in ones; marks, beard and everything measured stay the built-in
    method's. Where it does not, that face is done the built-in way.
    """
    candidates = []
    for detected in located:
        face = face_of(detected)
        if face is not None:
            candidates.append(face)
    candidates.sort(key=lambda f: f.frame.d, reverse=True)
    chosen = sorted(candidates[:limit], key=lambda f: f.frame.x)

    # A close-up fills the picture: its window is the whole of it, and the work
    # needs a few dozen planes of that size at once. A face has nothing more to
    # tell at a hundred and sixty pixels between the eyes than at four hundred, so
    # the picture is looked at smaller — as small as that, but never so small
    # that the smallest face in it stops being findable — and the maps come back
    # at that size. They are only ever resampled to what is on screen.
    if chosen:
        scale = min(1.0, max(MIN_FACE_D / min(f.frame.d for f in chosen),
                             BIG_FACE_D / max(f.frame.d for f in chosen)))
        if scale < 0.97:
            rgb = cv2.resize(rgb, (max(1, round(rgb.shape[1] * scale)), max(1, round(rgb.shape[0] * scale))),
                             interpolation=cv2.INTER_AREA)
            chosen = [Face(Frame(f.frame.x * scale, f.frame.y * scale, f.frame.d * scale, f.frame.cos, f.frame.sin),
                           (f.mouth[0], f.mouth[1], f.mouth[2]), tuple(v * scale for v in f.box), f.score)  # type: ignore[arg-type]
                      for f in chosen]
    height, width = rgb.shape[:2]
    lab = _lab(rgb)
    scene_lightness = float(np.median(lab[::4, ::4, 0]))

    names = ("skin", "zone", "protect", "hair", "beard", "under", "body")
    planes = {name: np.zeros((height, width), np.float32) for name in names}
    skin_id = np.zeros((height, width), np.uint8)
    hair_id = np.zeros((height, width), np.uint8)
    report: list[dict[str, Any]] = []
    hue_offsets: list[float] = []

    for index, face in enumerate(chosen, start=1):
        frame = face.frame
        entry: dict[str, Any] = {
            "id": index,
            "box": [face.box[0] / width, face.box[1] / height, face.box[2] / width, face.box[3] / height],
            "landmarks": None,
            "score": round(face.score, 3),
            "frame": {"x": frame.x / width, "y": frame.y / height, "d": frame.d / width,
                      "cos": round(frame.cos, 5), "sin": round(frame.sin, 5)},
        }
        report.append(entry)
        try:
            model = fit_skin(lab, frame)
            if model is None:
                entry["usable"] = False
                continue
            y0, y1, x0, x1 = _window(frame, rgb.shape)
            roi = lab[y0:y1, x0:x1]
            ys, xs = np.mgrid[y0:y1, x0:x1].astype(np.float32)
            u, v = frame.coords(xs, ys)

            parsed = _parse(parser, rgb, face, (y0, y1, x0, x1)) if parser is not None else None
            protect = _protect_marks(roi, u, v, model, frame)
            if parsed is not None:
                skin = _parsed_skin(roi, u, v, parsed, frame)
                zone = _parsed_zone(roi, u, v, parsed, frame)
                hair = _parsed_hair(roi, parsed, frame)
            else:
                skin = _skin_map(roi, u, v, model, face, (y0, x0))
                zone = _light_zone(roi, u, v, model, face, (y0, x0))
                hair = _hair_map(roi, rgb[y0:y1, x0:x1], u, v, model, face, skin)
            under = _under_eye(u, v, skin)
            body = (_parsed_body(roi, u, v, parsed, frame) if parsed is not None
                    else _skin_body(roi, u, v, model, face, (y0, x0)))
            hair_unsure = hair is not None and hair.confidence < MIN_HAIR_CONFIDENCE
            if hair_unsure:
                hair = None                      # a selection that is probably wrong is worse than none
            beard, coverage = _beard_map(roi, u, v, model, face, hair.weight if hair is not None else None)
            zone = zone * (1.0 - 0.85 * (hair.weight if hair is not None else 0.0))
            if beard is not None:
                zone = np.maximum(zone, beard)           # a beard is lit with the face it is on

            distance = np.hypot(u, v - 0.3)
            ring = (distance > 2.0) & (distance < 3.4) & (skin < 0.05)
            ring_l = float(np.median(roi[..., 0][ring])) if ring.sum() > 200 else None
            measure, suggest = measure_face(roi, u, v, model, skin, under, hair, coverage, face,
                                            scene_lightness, ring_l)

            # Where faces touch, each pixel goes to the face whose light zone reaches
            # it furthest, and skin, marks and hollows follow that one choice so a
            # pixel is never half one person's and half another's.
            sl = (slice(y0, y1), slice(x0, x1))
            own = zone > planes["zone"][sl]
            skin_id[sl][own] = index
            for name, local in (("skin", skin), ("zone", zone), ("protect", protect), ("under", under), ("body", body)):
                planes[name][sl] = np.where(own, local, planes[name][sl])
            # Hair and beard share one owner map, so they are compared together.
            mine = np.maximum(hair.weight if hair is not None else 0.0, beard if beard is not None else 0.0)
            if np.ndim(mine):
                claim = mine > np.maximum(planes["hair"][sl], planes["beard"][sl])
                hair_id[sl][claim] = index
                for name, local in (("hair", hair.weight if hair is not None else 0.0),
                                    ("beard", beard if beard is not None else 0.0)):
                    planes[name][sl] = np.where(claim, local, planes[name][sl])

            entry.update({
                "usable": True,
                "engine": "parser" if parsed is not None else "builtin",
                "skin": {"lightness": round(model.lightness, 1), "hue": round(model.hue, 1),
                         "chroma": round(model.chroma, 1)},
                "hair": ({"found": True, "confidence": round(hair.confidence, 2), "grey": round(hair.grey, 3),
                          "colour": list(hair.colour), "lightness": round(hair.lightness, 1)}
                         if hair is not None else {"found": False, "unsure": bool(hair_unsure)}),
                "beard": round(coverage, 2) if beard is not None else 0.0,
                "marks": bool(protect.max() > 0.5),
                "measure": measure,
                "suggest": suggest,
            })
            landmarks = []
            for point in face_landmarks(face):
                landmarks.append([point[0] / width, point[1] / height])
            entry["landmarks"] = landmarks
            if measure and abs(measure.get("hueOffset", 0.0)) > 0:
                hue_offsets.append(measure["hueOffset"])
        except Exception:                                   # noqa: BLE001
            # One face that cannot be worked out must not take the others with it.
            log.exception("portrait analysis failed for one face")
            entry.clear()
            entry.update({"id": index, "usable": False})

    cast = {"hueOffset": round(float(np.median(hue_offsets)), 1) if hue_offsets else 0.0,
            "faces": len(hue_offsets)}
    return Analysis((width, height), report, planes, skin_id, hair_id, cast)


def engine_of(analysis: Analysis) -> str:
    """Which method made the maps: ``parser`` if a face-parsing network made any of them."""
    return "parser" if any(face.get("engine") == "parser" for face in analysis.faces) else "builtin"


def face_landmarks(face: Face) -> list[tuple[float, float]]:
    """The two eyes and the mouth corners, recovered from the frame (nose is not needed)."""
    f = face.frame
    mu, mv, half = face.mouth
    return [f.point(-0.5, 0.0), f.point(0.5, 0.0), f.point(0.0, 0.55),
            f.point(mu - half, mv), f.point(mu + half, mv)]


def pack_planes(analysis: Analysis) -> dict[str, Any]:
    """The maps as three 8-bit, three-channel pictures, ready to be sent to a browser.

    ``a``: skin, light zone, protected marks. ``b``: hair, beard, under-eyes.
    ``c``: all of a person's exposed skin, face and neck together, for colour.
    ``ids``: which face owns each pixel's skin, and which its hair, so that a
    person's own settings reach only that person. The pictures are opaque RGB,
    not RGBA: a canvas premultiplies alpha when it decodes an image, and a plane
    hidden in a channel next to an alpha below one comes back changed.
    """
    def pack(*channels):
        return np.dstack([np.clip(c * 255.0 + 0.5, 0, 255).astype(np.uint8) for c in channels])

    planes = analysis.planes
    return {
        "a": pack(planes["skin"], planes["zone"], planes["protect"]),
        "b": pack(planes["hair"], planes["beard"], planes["under"]),
        "c": pack(planes["body"], np.zeros_like(planes["body"]), np.zeros_like(planes["body"])),
        "ids": np.dstack([analysis.skin_id, analysis.hair_id, np.zeros_like(analysis.skin_id)]),
    }
