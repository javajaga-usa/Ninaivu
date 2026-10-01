"""What this particular photograph needs, rather than what photographs need.

The Photo Studio's Auto button applied the same six numbers to everything: a
little more exposure, a little more contrast, lift the shadows. That is a
reasonable guess for an average photograph and wrong for every specific one —
it brightens a picture that was already bright, and lifts shadows in a
silhouette that was meant to be a silhouette.

This reads the photograph instead. Same statistics the scan already measures
for the quality flags, plus the per-channel means white balance needs, turned
into the slider values the editor already knows how to apply.

The numbers are in the vocabulary of the shared develop engine
(ninaivu/static/js/studio/develop.mjs and recipe.mjs), which both editors use:
exposure is in fortieths of a stop of linear light, contrast is an S-curve
about mid-grey, positive tint is magenta, and colour is lifted through
``vibrance``, which leaves skin nearly alone.

Two things it deliberately will not do. It never proposes a change larger
than a person would plausibly drag a slider to, because an automatic
adjustment that has to be undone is worse than none. And it says nothing at
all about a photograph it cannot measure — no defaults, no average-case
guess.
"""

from __future__ import annotations

import math
from typing import Any

from PIL import Image

try:
    import numpy as np
except Exception:  # pragma: no cover - numpy is a hard requirement in practice
    np = None  # type: ignore

#: Working size. Big enough for stable percentiles, small enough to be free.
SAMPLE_EDGE = 256

#: Where a well-exposed photograph's average tone sits, 0-1. Slightly below
#: the midpoint: print and screen both flatter a picture that is a shade
#: darker than neutral, and lifting is more forgiving than pulling back.
TARGET_LUMA = 0.46

#: No single slider may move further than this. The Auto button is a starting
#: point somebody then adjusts, not a filter.
MAX_MOVE = 40


def _clamp(value: float, limit: int = MAX_MOVE) -> int:
    return int(max(-limit, min(limit, round(value))))


def _luma(arr) -> Any:
    return 0.299 * arr[..., 0] + 0.587 * arr[..., 1] + 0.114 * arr[..., 2]


def _linear(v: float) -> float:
    """An encoded sRGB value, 0-1, as linear light."""
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


#: Exposure slider units in one stop: the engine's exposure is ``2 ** (value / 40)``.
STOP = 40


def suggest(img: Image.Image) -> dict[str, int]:
    """Slider values for this photograph, in the editor's own vocabulary.

    Returns an empty dict when the image cannot be measured — the caller
    should then leave the sliders alone rather than apply a guess.
    """
    if np is None:
        return {}
    try:
        small = img.convert("RGB")
        small.thumbnail((SAMPLE_EDGE, SAMPLE_EDGE), Image.Resampling.BILINEAR)
        arr = np.asarray(small, dtype=np.float64) / 255.0
    except Exception:  # noqa: BLE001 - one odd file must not break the editor
        return {}
    if arr.ndim != 3 or arr.size == 0:
        return {}

    luma = _luma(arr)
    mean = float(luma.mean())
    spread = float(luma.std())
    dark_end = float(np.percentile(luma, 2))
    light_end = float(np.percentile(luma, 98))
    shadow_clip = float((luma <= 0.02).mean())
    highlight_clip = float((luma >= 0.98).mean())

    out: dict[str, int] = {}

    # A featureless frame — a grey card, a blank scan, a lens cap — has
    # nothing to enhance, and proposing contrast for it produces noise out of
    # nowhere. Same reasoning as the blur flag's contrast floor.
    if spread < 0.02:
        return {}

    # Exposure: close the gap to the target, but only most of it. Going all
    # the way makes every photograph in a set land on the same tone, which
    # reads as processed rather than corrected. Exposure is a gain on linear
    # light, so the gap is measured in stops.
    stops = math.log2(_linear(TARGET_LUMA) / max(_linear(mean), 1e-4))
    out["exposure"] = _clamp(stops * 0.7 * STOP)

    # Contrast: a flat photograph is one whose tones never reach either end.
    # A photograph that already spans the range is left alone. The engine's
    # contrast is an S-curve that is gentle at the ends, so it takes a larger
    # number than a straight stretch did for the same effect in the midtones.
    if spread < 0.16:
        out["contrast"] = _clamp((0.16 - spread) * 300, 40)

    # Shadows and highlights: only what is genuinely lost. Lifting shadows in
    # a silhouette ruins the silhouette, so the test is clipping, not
    # darkness.
    if shadow_clip > 0.06:
        out["shadows"] = _clamp((shadow_clip - 0.06) * 90, 28)
    if highlight_clip > 0.04:
        out["highlights"] = -_clamp((highlight_clip - 0.04) * 110, 28)

    # Black and white points: pull them in when the photograph never reaches
    # either end, which is what haze and old scans look like.
    if dark_end > 0.08:
        out["blacks"] = -_clamp((dark_end - 0.08) * 150, 25)
    if light_end < 0.92:
        out["whites"] = _clamp((0.92 - light_end) * 150, 25)

    # White balance by grey world: averaged over a whole photograph, the
    # colours should cancel out. Where they do not, the light had a cast.
    # It is only a guess — a red brick wall or a field of grass will fool it
    # — so the correction is deliberately gentle.
    red, green, blue = (float(arr[..., i].mean()) for i in range(3))
    grey = (red + green + blue) / 3 or 1.0
    warmth = (blue - red) / grey
    # Positive tint is magenta: a green cast wants some.
    tint = (green - (red + blue) / 2) / grey
    if abs(warmth) > 0.02:
        out["warmth"] = _clamp(warmth * 90, 25)
    if abs(tint) > 0.02:
        out["tint"] = _clamp(tint * 90, 20)

    # Vibrance: only lift what is nearly grey already. A photograph with
    # strong colour does not need help, and pushing it produces the
    # over-saturated look people associate with automatic enhancement.
    # Vibrance rather than saturation, because it leaves skin as it is.
    colour = float((arr.max(axis=2) - arr.min(axis=2)).mean())
    if colour < 0.10:
        out["vibrance"] = _clamp((0.10 - colour) * 130, 20)

    # A one-unit nudge is not a correction, it is noise with a number on it.
    return {k: v for k, v in out.items() if abs(v) >= 2}


def describe(settings: dict[str, int]) -> str:
    """One plain sentence about what was proposed, for the interface.

    An automatic adjustment nobody can explain is one nobody trusts.
    """
    if not settings:
        return "This photograph already looks balanced."
    parts: list[str] = []
    if exposure := settings.get("exposure"):
        parts.append("brighter" if exposure > 0 else "darker")
    if settings.get("contrast"):
        parts.append("more contrast")
    if settings.get("shadows"):
        parts.append("opened-up shadows")
    if settings.get("highlights"):
        parts.append("recovered highlights")
    if warmth := settings.get("warmth"):
        parts.append("warmer" if warmth > 0 else "cooler")
    if settings.get("tint"):
        parts.append("a tint correction")
    if settings.get("vibrance"):
        parts.append("a little more colour")
    if not parts:
        return "This photograph already looks balanced."
    if len(parts) == 1:
        return f"Suggested: {parts[0]}."
    return f"Suggested: {', '.join(parts[:-1])} and {parts[-1]}."
