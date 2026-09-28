"""Finding the person in a photograph, so nobody has to paint round them.

The Photo Studio's skin and hair adjustments have always been honest about
what they are: they apply to an area you painted, and nothing else. That is
the right guarantee — an edit that decides for itself which pixels are
somebody's face is an edit you cannot predict. But painting hair round a
child's flyaways with a mouse is a chore, and it is the reason most people
open the tool once.

So this proposes a starting selection and never more than that. The masks it
returns are handed to the brush as a first pass; every one of them can be
painted over, erased, or thrown away, and nothing is applied until a slider
moves. The tool keeps its promise, and the chore goes.

**No new model.** The face detector the household already downloaded for the
Faces tab gives a box and five landmarks. Everything below is geometry from
those, refined by GrabCut — the same segmentation OpenCV has shipped for
fifteen years. Nothing is downloaded and nothing is sent anywhere.

Hair suggestions deliberately cover only distinct, dark crown regions. They
must be bounded in the image itself, not by clipping a background selection
to a head-shaped fence. Low contrast, light hair and ambiguous backgrounds
are left to the brush. This is a starting selection, not face parsing.
"""

from __future__ import annotations

import logging
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

__all__ = ["available", "auto_masks", "WORK_SIZE"]

#: Segmentation runs on a small copy. GrabCut is superlinear in the pixel
#: count and the result is feathered and scaled up anyway, so working larger
#: buys nothing a person could see and costs seconds they would feel.
WORK_SIZE = 512

#: GrabCut iterations. Three is where the mask stops visibly improving on a
#: 512-pixel portrait.
_ITERATIONS = 3


def available() -> bool:
    return cv2 is not None and np is not None


def _faces(bgr, state_dir) -> list[tuple[float, float, float, float]]:
    """Face boxes, from the detector the Faces tab already uses."""
    try:
        from . import faces as faces_mod                # noqa: PLC0415

        engine = faces_mod.FaceEngine(state_dir)
        if not engine.available:
            return []
        found = engine.detect(bgr)
    except Exception as exc:                            # noqa: BLE001
        log.debug("portrait: face detection unavailable — %s", exc)
        return []
    boxes = []
    for face in found:
        box = getattr(face, "box", None) or getattr(face, "bbox", None)
        if box is not None and len(box) >= 4:
            boxes.append(tuple(float(v) for v in box[:4]))
    return boxes


def _largest(boxes):
    return max(boxes, key=lambda b: b[2] * b[3]) if boxes else None


def _grabcut(bgr, definite_fg, probable_fg, definite_bg):
    """Refine three painted hints into a mask that follows the actual edges."""
    mask = np.full(bgr.shape[:2], cv2.GC_PR_BGD, np.uint8)
    mask[probable_fg > 0] = cv2.GC_PR_FGD
    mask[definite_fg > 0] = cv2.GC_FGD
    mask[definite_bg > 0] = cv2.GC_BGD
    if not (mask == cv2.GC_FGD).any():
        return np.zeros(bgr.shape[:2], np.uint8)
    try:
        cv2.grabCut(bgr, mask, None,
                    np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64),
                    _ITERATIONS, cv2.GC_INIT_WITH_MASK)
    except Exception as exc:                            # noqa: BLE001
        log.debug("portrait: grabcut declined this image — %s", exc)
        return ((probable_fg > 0) | (definite_fg > 0)).astype(np.uint8) * 255
    return np.where((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)


def _ellipse(shape, cx, cy, rx, ry) -> "np.ndarray":
    canvas = np.zeros(shape[:2], np.uint8)
    cv2.ellipse(canvas, (int(cx), int(cy)), (max(1, int(rx)), max(1, int(ry))),
                0, 0, 360, 255, -1)
    return canvas



def _outside(shape, x, y, w, h) -> "np.ndarray":
    """Definite background: everything beyond this rectangle."""
    canvas = np.full(shape[:2], 255, np.uint8)
    x0, y0 = max(0, int(x)), max(0, int(y))
    x1, y1 = min(shape[1], int(x + w)), min(shape[0], int(y + h))
    if x1 > x0 and y1 > y0:
        canvas[y0:y1, x0:x1] = 0
    return canvas


#: Above this share of the frame, a "face" mask is not a face.
#:
#: GrabCut fails in one direction — it over-claims — and it fails silently. A
#: mask covering half the photograph is not a slightly wrong selection, it is
#: the segmentation having latched onto the background, and applying skin
#: smoothing to it would be the single worst thing this tool could do. When
#: that happens the geometric hint is used instead: cruder, but always about
#: the right part of the picture.
_RUNAWAY = 0.34


def _bounded(mask, fallback):
    """The segmented mask, unless it ran away — then the shape it started from."""
    if mask.mean() / 255.0 > _RUNAWAY:
        log.debug("portrait: segmentation over-claimed; using the geometry")
        return fallback.copy()
    if mask.mean() < 2:                      # found essentially nothing
        return fallback.copy()
    return mask



def _feather(mask, radius: int = 9):
    """Soft edges, because a hard cut-out betrays itself the moment it moves."""
    blurred = cv2.GaussianBlur(mask, (radius | 1, radius | 1), 0)
    return blurred


def _hair(bgr, face):
    """Suggest a stable crown component, or abstain without a geometric fallback.

    Check components across the *whole image* before applying any head bounds.
    Otherwise a dark wall can become a plausible cap simply by being clipped.
    Two brightness thresholds must agree, ruling out weak background edges.
    """
    x, y, w, h = face
    ih, iw = bgr.shape[:2]
    if not np.isfinite(face).all() or min(w, h) < 24:
        return None
    if x < 0 or y - .65 * h < 2 or x + w >= iw or y + h >= ih:
        return None
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    cheek = _ellipse(bgr.shape, x + w / 2, y + h * .60, w * .22, h * .20) > 0
    skin_level = float(np.median(gray[cheek]))
    threshold = min(105., skin_level - 35.)
    if threshold < 20:
        return None
    crown = _ellipse(bgr.shape, x + w / 2, y - h * .12, w * .28, h * .12) > 0
    bounds = _outside(bgr.shape, x - w * .40, y - h * .65, w * 1.8, h * 1.25) > 0
    candidates = []
    for cutoff in (threshold, threshold + 15):
        count, labels, stats, _ = cv2.connectedComponentsWithStats(
            (gray < cutoff).astype(np.uint8), 8)
        ids, hits = np.unique(labels[crown], return_counts=True)
        choices = [(int(hit), int(label)) for label, hit in zip(ids, hits) if label]
        if not choices:
            return None
        hit, label = max(choices)
        if hit < .55 * np.count_nonzero(crown):
            return None
        component = labels == label
        area = stats[label, cv2.CC_STAT_AREA]
        # Never trim a leaking region into an apparently safe proposal.
        if (component[bounds].any() or component[cheek].any()
                or not .08 * w * h <= area <= .85 * w * h
                or area > .15 * iw * ih):
            return None
        candidates.append(component)
    if np.count_nonzero(candidates[0] & candidates[1]) / np.count_nonzero(
            candidates[0] | candidates[1]) < .85:
        return None
    # Feather inward only: no new background pixels enter the suggestion.
    mask = candidates[0].astype(np.uint8) * 255
    return np.minimum(mask, _feather(mask, 5))


def auto_masks(image, state_dir: Any = None) -> dict[str, Any]:
    """Propose ``skin`` and ``hair`` selections for a portrait.

    Returns single-channel masks at the image's own size, plus the face box
    that produced them so the caller can say what it found. An empty result is
    an honest "no face here", not an error.
    """
    if not available():
        return {"faces": 0, "reason": "OpenCV is not installed", "masks": {}}

    rgb = np.asarray(image.convert("RGB"))
    height, width = rgb.shape[:2]
    scale = min(1.0, WORK_SIZE / max(height, width))
    small = cv2.resize(rgb, (max(1, int(width * scale)), max(1, int(height * scale))),
                       interpolation=cv2.INTER_AREA) if scale < 1 else rgb
    bgr = cv2.cvtColor(small, cv2.COLOR_RGB2BGR)
    sh, sw = bgr.shape[:2]

    # Detection runs on the full-size image and segmentation on the small one.
    # They are different jobs with opposite needs: the detector ignores a face
    # under fifty pixels across, and half of any group photograph is under
    # fifty pixels once the frame has been shrunk to 512 — while GrabCut wants
    # the small copy or it takes seconds. So detect large, then bring the box
    # down to where the segmenting happens.
    boxes = _faces(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), state_dir)
    face = _largest(boxes)
    if face is None:
        return {"faces": 0, "reason": "no face found", "masks": {}}
    fx, fy, fw, fh = (v * scale for v in face)

    # --- skin -------------------------------------------------------------
    # An ellipse inside the detector's box is definitely face; a slightly
    # larger one is probably face. Everything outside a tight frame around the
    # head is marked *definite* background rather than merely probable, which
    # is the whole difference between a mask of a face and a mask of the
    # photograph: GrabCut will happily flip "probably background" to
    # foreground across an entire frame if the colours agree, and grass and
    # skin agree more often than you would like.
    cx, cy = fx + fw / 2, fy + fh / 2
    sure_skin = _ellipse(bgr.shape, cx, cy + fh * 0.05, fw * 0.30, fh * 0.38)
    maybe_skin = _ellipse(bgr.shape, cx, cy + fh * 0.05, fw * 0.52, fh * 0.62)
    not_skin = _outside(bgr.shape, cx - fw * 0.85, cy - fh * 1.0,
                        fw * 1.70, fh * 2.0)
    skin = _bounded(_grabcut(bgr, sure_skin, maybe_skin, not_skin), maybe_skin)
    skin = _feather(skin)

    if scale < 1:
        skin = cv2.resize(skin, (width, height), interpolation=cv2.INTER_LINEAR)

    # A proposal covering almost nothing is not a proposal; saying so lets the
    # editor tell somebody to paint it rather than handing them an empty
    # selection and letting them wonder what happened.
    produced = {"skin": skin} if skin.mean() / 255.0 >= 0.0015 else {}
    hair = _hair(bgr, (fx, fy, fw, fh))
    if hair is not None:
        produced["hair"] = cv2.resize(hair, (width, height), interpolation=cv2.INTER_LINEAR) if scale < 1 else hair
    return {
        "faces": len(boxes),
        "box": [float(v) / scale for v in (fx, fy, fw, fh)],
        "masks": produced,
    }
