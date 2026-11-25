"""The arithmetic around the colouring model: CIE Lab, and what colour is laid
where (P16: this was in media/onnx_tools.py, which still names everything here).

No model package is imported: only NumPy and Pillow, so the core can ask
whether a photograph looks black and white without the model being there.
"""
from __future__ import annotations

import numpy as np
from PIL import Image

#: DDColor sees the photograph's lightness as a square of this many pixels.
COLORIZE_INPUT = 512

# CIE Lab, the way the colourising model speaks it (D65, L 0–100, a and b about ±127)

_XN, _ZN = 0.950456, 1.088754


def _linear(srgb: np.ndarray) -> np.ndarray:
    return np.where(srgb <= 0.04045, srgb / 12.92, ((srgb + 0.055) / 1.055) ** 2.4)


def _encode(linear: np.ndarray) -> np.ndarray:
    linear = np.clip(linear, 0, 1)
    return np.where(linear <= 0.0031308, linear * 12.92, 1.055 * linear ** (1 / 2.4) - 0.055)


def _lab_f(t: np.ndarray) -> np.ndarray:
    return np.where(t > 0.008856, np.cbrt(t), 7.787 * t + 16 / 116)


def _lab_f_inverse(t: np.ndarray) -> np.ndarray:
    return np.where(t > 6 / 29, t ** 3, (t - 16 / 116) / 7.787)


def lightness(rgb: np.ndarray) -> np.ndarray:
    """CIE L* (0–100) of an sRGB image given as floats 0–1, shape (…, 3)."""
    linear = _linear(rgb)
    y = 0.2126 * linear[..., 0] + 0.7152 * linear[..., 1] + 0.0722 * linear[..., 2]
    return 116 * _lab_f(y) - 16


def grey_of_lightness(light: np.ndarray) -> np.ndarray:
    """The neutral sRGB grey (floats 0–1, shape (…, 3)) that has this L*."""
    y = _lab_f_inverse((light + 16) / 116)
    return np.repeat(_encode(y)[..., None], 3, axis=-1)


def lab_to_rgb(light: np.ndarray, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """sRGB floats 0–1, shape (…, 3), from L*, a*, b* planes."""
    fy = (light + 16) / 116
    x = _XN * _lab_f_inverse(fy + a / 500)
    y = _lab_f_inverse(fy)
    z = _ZN * _lab_f_inverse(fy - b / 200)
    linear = np.stack([3.2406 * x - 1.5372 * y - 0.4986 * z,
                       -0.9689 * x + 1.8758 * y + 0.0415 * z,
                       0.0557 * x - 0.2040 * y + 1.0570 * z], axis=-1)
    return _encode(linear)


# ---------------------------------------------------------------------------
# Object removal


def colour_image(image: Image.Image, run, on_progress=None) -> Image.Image:
    """DDColor: colour for a black-and-white photograph, or fresh colour for a faded one.

    The model is shown the photograph's lightness alone, as a neutral grey
    square of ``COLORIZE_INPUT`` pixels, and answers with the colour (Lab a
    and b) it believes belongs there. That colour is scaled back up and laid
    under the photograph's *own* lightness at full size, so every edge, every
    grain and every face is the original's; only the hue is new. That is also
    why a colour photograph comes out re-coloured rather than ruined: its
    light is kept and its colour is replaced.

    *run* takes the grey square (1×3×N×N floats) and returns the model's
    answer: the Playground's session here, the model host's in
    models/colour/runner.py.
    """
    image = image.convert("RGB")
    width, height = image.size
    if on_progress:
        on_progress({"stage": "running", "value": 0, "max": 3})
    rgb = np.asarray(image, dtype=np.float32) / 255.0
    light = lightness(rgb)
    small = np.asarray(image.resize((COLORIZE_INPUT, COLORIZE_INPUT), Image.Resampling.LANCZOS),
                       dtype=np.float32) / 255.0
    grey = grey_of_lightness(lightness(small)).astype(np.float32)
    if on_progress:
        on_progress({"stage": "running", "value": 1, "max": 3})
    output = run(grey.transpose(2, 0, 1)[None].copy())[0]
    if output.shape[0] != 2:
        raise RuntimeError("The colourising model did not answer with colour (two Lab planes). "
                           "Use a DDColor ONNX export.")
    if on_progress:
        on_progress({"stage": "running", "value": 2, "max": 3})
    planes = []
    for plane in output:
        back = Image.fromarray(np.ascontiguousarray(plane, dtype=np.float32))
        planes.append(np.asarray(back.resize((width, height), Image.Resampling.BILINEAR), dtype=np.float32))
    coloured = lab_to_rgb(light, planes[0], planes[1])
    if on_progress:
        on_progress({"stage": "running", "value": 3, "max": 3})
    return Image.fromarray((coloured * 255 + 0.5).astype(np.uint8))


#: How far, in YCbCr's colour planes, most of a photograph's pixels may stray
#: from its one overall tint and it still be black and white. A sepia print or
#: a blue-toned one has a tint everywhere and nothing else; a colour
#: photograph's skin, sky and grass are tens apart.
MONO_SPREAD = 6.0
#: How strong that one tint may be. Sepia is about 14 and a cyanotype's blue
#: about 24; a picture all of one strong colour is a colour picture.
MONO_TINT = 32.0


def looks_black_and_white(image: Image.Image) -> bool:
    """Whether a photograph has no colour of its own: grey, or one tint all
    over (sepia, a toned print, a scan of either). Read from a small copy, so
    the gallery's preview is enough."""
    small = image.convert("RGB")
    if max(small.size) > 256:
        small = small.copy()
        small.thumbnail((256, 256), Image.Resampling.BILINEAR)
    planes = np.asarray(small.convert("YCbCr"), dtype=np.float32)[..., 1:].reshape(-1, 2)
    if not len(planes):
        return False
    tint = np.median(planes, axis=0)
    if float(np.hypot(*(tint - 128.0))) > MONO_TINT:
        return False
    spread = np.hypot(*(planes - tint).T)
    return float(np.percentile(spread, 95)) <= MONO_SPREAD
