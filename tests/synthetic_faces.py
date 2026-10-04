"""Faces drawn from a recipe, so a test can know exactly what is skin.

The retouching tools are judged by what they select, and the only way to say a
selection is right is to know the answer. A photograph cannot tell you where a
bindi ends; a drawing can. These are flat, noisy, unmistakably fake faces — and
that is the point: each has the parts the tools must tell apart (skin, eyes,
brows, lips, hair, a beard) and the marks they must leave alone (a bindi,
sindoor, sacred ash), placed in the face's own proportions, in skin tones from
fair to very dark, and the answer for every pixel comes back with the picture.

Measured in eye distances from the middle of the eyes — ``u`` along the eyes,
``v`` down the face — exactly as the tools see a face, but drawn here from the
recipe and not from their code.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import cv2
import numpy as np

#: Complexions, as the colours a camera records for them in good light.
SKIN = {
    "fair": (236, 192, 168),
    "wheatish": (205, 152, 118),
    "brown": (158, 104, 74),
    "dark": (104, 66, 47),
    "deep": (78, 50, 38),
}
HAIR = {
    "black": (28, 24, 22),
    "brown": (70, 48, 34),
    "grey": (150, 148, 146),
    "white": (214, 212, 208),
}


@dataclass
class Drawn:
    """A picture, where each face is, and the truth about every pixel."""

    rgb: np.ndarray
    located: list[dict] = field(default_factory=list)
    truth: list[dict[str, np.ndarray]] = field(default_factory=list)


def _blend(picture, mask, colour, alpha=1.0):
    soft = (mask.astype(np.float32) * alpha)[..., None]
    picture[:] = picture * (1.0 - soft) + np.array(colour, np.float32) * soft


def _ellipse(shape, cx, cy, rx, ry, angle=0.0):
    canvas = np.zeros(shape, np.uint8)
    cv2.ellipse(canvas, (int(round(cx)), int(round(cy))), (max(1, int(round(rx))), max(1, int(round(ry)))),
                math.degrees(angle), 0, 360, 1, -1, cv2.LINE_AA)
    return canvas.astype(np.float32)


def draw(faces: list[dict], size=(720, 560), background=(150, 150, 150), seed=3,
         blur=0.8, noise=2.0) -> Drawn:
    """One picture with a face for each entry of *faces*.

    Each entry: ``x, y`` (the middle of the eyes, pixels), ``d`` (eye distance,
    pixels), ``skin`` and ``hair`` (names from SKIN/HAIR or an RGB triple),
    ``tilt`` (radians), and any of ``bindi`` ('red' or 'black'), ``sindoor``,
    ``vibhuti``, ``beard``, ``shine``, ``circles``, ``cast`` (an RGB gain),
    ``dark`` (a gain on the whole face, for a face in shadow) and ``bald``.
    """
    width, height = size
    rng = np.random.default_rng(seed)
    picture = np.empty((height, width, 3), np.float32)
    picture[:] = background
    out = Drawn(rgb=picture)
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)

    for spec in faces:
        cx, cy, d = float(spec["x"]), float(spec["y"]), float(spec["d"])
        tilt = float(spec.get("tilt", 0.0))
        cos, sin = math.cos(tilt), math.sin(tilt)
        skin = np.array(SKIN.get(spec.get("skin", "wheatish"), spec.get("skin")), np.float32)
        hair = np.array(HAIR.get(spec.get("hair", "black"), spec.get("hair")), np.float32)
        gain = np.array(spec.get("cast", (1, 1, 1)), np.float32) * float(spec.get("dark", 1.0))

        # Each face's numbers are bound here, not read from the loop later.
        def at(u, v, cx=cx, cy=cy, d=d, cos=cos, sin=sin):
            return cx + (u * cos - v * sin) * d, cy + (u * sin + v * cos) * d

        def ell(u, v, ru, rv, extra=0.0, d=d, tilt=tilt):
            x, y = at(u, v)
            return _ellipse((height, width), x, y, ru * d, rv * d, tilt + extra)

        def line(mask_value, u0, v0, u1, v1, thickness, d=d):
            canvas = np.zeros((height, width), np.uint8)
            (x0, y0), (x1, y1) = at(u0, v0), at(u1, v1)
            cv2.line(canvas, (int(round(x0)), int(round(y0))), (int(round(x1)), int(round(y1))),
                     1, max(1, int(round(thickness * d))), cv2.LINE_AA)
            return canvas.astype(np.float32)

        face = ell(0.0, 0.34, 1.0, 1.45)
        neck = ell(0.0, 2.0, 0.6, 0.75)
        hair_mask = np.zeros((height, width), np.float32)
        if not spec.get("bald"):
            cap = ell(0.0, -0.95, 1.12, 1.0)
            sides = np.maximum(ell(-0.98, 0.7, 0.27, 1.3), ell(0.98, 0.7, 0.27, 1.3))
            hair_mask = np.maximum(cap, sides)

        # A little shape to the skin, as a light gives it: lighter in the middle.
        u, v = _coords(xx, yy, cx, cy, d, cos, sin)
        shade = 1.0 + 0.06 * (1.0 - np.clip(np.hypot(u / 1.0, (v - 0.3) / 1.45), 0, 1)) - 0.03
        side = 1.0 + float(spec.get("side", 0.0)) * np.clip(u, -1, 1)

        _blend(picture, hair_mask, hair * gain)
        _blend(picture, neck, skin * gain * 0.88)
        skin_layer = face * 1.0
        body = np.empty_like(picture)
        body[:] = skin * gain
        body *= (shade * side)[..., None]
        if spec.get("shine"):
            # A T-zone and the cheekbones: where skin is oilier and catches the light.
            glare = np.maximum.reduce([
                np.exp(-(((u - 0.0) / 0.75) ** 2 + ((v + 0.7) / 0.3) ** 2)),
                np.exp(-(((u - 0.0) / 0.2) ** 2 + ((v - 0.55) / 0.4) ** 2)),
                np.exp(-(((np.abs(u) - 0.68) / 0.14) ** 2 + ((v - 0.35) / 0.22) ** 2)),
            ]) * float(spec["shine"])
            # Oil on skin reflects the light as it is: brighter, and a good deal less coloured.
            body = body * (1.0 + 0.3 * glare[..., None])
            body = body + glare[..., None] * (np.array([255, 250, 245], np.float32) - body) * 0.45
        if spec.get("circles"):
            ring = np.maximum(ell(-0.5, 0.30, 0.38, 0.14), ell(0.5, 0.30, 0.38, 0.14))
            body = body * (1.0 - ring[..., None] * float(spec["circles"]))
        picture[:] = picture * (1.0 - skin_layer[..., None]) + body * skin_layer[..., None]

        beard = np.zeros((height, width), np.float32)
        if spec.get("beard"):
            beard = ell(0.0, 1.3, 0.92, 0.5) * face
            beard *= 1.0 - ell(0.0, 0.98, 0.3, 0.1)
            _blend(picture, beard, hair * 0.95 * gain, 0.92)

        eyes = np.maximum(ell(-0.5, 0.0, 0.2, 0.085), ell(0.5, 0.0, 0.2, 0.085))
        _blend(picture, eyes, np.array((232, 232, 228), np.float32) * gain)
        irises = np.maximum(ell(-0.5, 0.0, 0.085, 0.085), ell(0.5, 0.0, 0.085, 0.085))
        _blend(picture, irises, np.array((38, 24, 18), np.float32) * gain)
        brows = np.maximum(line(1, -0.76, -0.40, -0.30, -0.44, 0.075), line(1, 0.30, -0.44, 0.76, -0.40, 0.075))
        _blend(picture, brows, hair * 0.9)
        mouth = ell(0.0, 1.0, 0.32, 0.075)
        _blend(picture, mouth, np.array((166, 66, 78), np.float32) * gain)

        marks = np.zeros((height, width), np.float32)
        if spec.get("bindi"):
            dot = ell(0.0, -0.62, 0.06, 0.06)
            _blend(picture, dot, np.array((188, 22, 32) if spec["bindi"] == "red" else (22, 18, 18), np.float32) * gain)
            marks = np.maximum(marks, dot)
        if spec.get("sindoor"):
            parting = line(1, 0.0, -1.32, 0.0, -0.98, 0.035)
            _blend(picture, parting, np.array((205, 34, 30), np.float32) * gain)
            marks = np.maximum(marks, parting)
        if spec.get("vibhuti"):
            for level in (-0.78, -0.9, -1.02):
                band = line(1, -0.42, level, 0.42, level, 0.035)
                _blend(picture, band, np.array((236, 234, 228), np.float32) * gain, 0.95)
                marks = np.maximum(marks, band)

        skin_truth = face * (1.0 - np.maximum.reduce([eyes, brows, mouth, beard, marks, hair_mask * (v < -1.0)]))
        out.truth.append({
            "skin": (skin_truth > 0.98),
            "eyes": eyes > 0.9, "brows": brows > 0.9, "mouth": mouth > 0.9,
            "hair": (hair_mask * (1.0 - face) > 0.98) & (brows < 0.1),
            "beard": beard > 0.9,
            "marks": marks > 0.9,
        })
        ex, ey = at(-0.5, 0.0), at(0.5, 0.0)
        nose, mr, ml = at(0.0, 0.55), at(-0.32, 1.0), at(0.32, 1.0)
        out.located.append({
            "box": (cx - 1.0 * d, cy - 0.9 * d, 2.0 * d, 2.4 * d),
            "landmarks": [ex, ey, nose, mr, ml],
            "score": 0.95,
        })

    blurred = cv2.GaussianBlur(picture, (0, 0), blur) if blur else picture
    blurred = blurred + rng.normal(0.0, noise, blurred.shape).astype(np.float32)
    out.rgb = np.clip(blurred, 0, 255).astype(np.uint8)
    return out


def _coords(xx, yy, cx, cy, d, cos, sin):
    dx, dy = xx - cx, yy - cy
    return (dx * cos + dy * sin) / d, (-dx * sin + dy * cos) / d
