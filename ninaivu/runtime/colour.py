"""Colour for black-and-white photographs, in the model host.

DDColor (Kang et al., ICCV 2023; Apache-2.0), the ONNX export FaceFusion
publishes on GitHub, pinned on the AI models page as ``colour``. It sees a
photograph's lightness as a grey square of 512 pixels and answers with the
colour it believes belongs there; that colour is laid under the photograph's
own lightness at full size (media/onnx_tools.py, :func:`colour_image`), so the
grain, the edges and the faces stay the original's and only the hue is new.

The server writes the photograph, upright, to a private file and names it;
the host colours it and writes the result beside it as a JPEG. Only paths
cross between the two, never the pixels.

About a gigabyte in memory while it is loaded. Measured on four cores of a
cloud machine: six seconds a photograph. The Mac's processor is the default
(``image_providers``), as for the other image models.
"""
from __future__ import annotations

import importlib.util
import logging
import time
from pathlib import Path
from typing import Any

from ..words import said
from .host import Capability, Unavailable

log = logging.getLogger(__name__)

#: Raised when a change here should make colour copies look different.
COLOUR_VERSION = 1
#: The catalogue entry (media/model_catalog.py).
MODEL = "colour"


def model_path() -> Path | None:
    """Where the colouring model is, downloaded or placed by hand."""
    from ..media import onnx_tools                             # noqa: PLC0415
    path = onnx_tools.colorize_model_path()
    return path if path is not None and path.is_file() else None


def ready() -> str | None:
    """Why a photograph cannot be coloured on this computer, or None."""
    if importlib.util.find_spec("onnxruntime") is None:
        return said("ONNX Runtime is not installed (Background removal and the other ONNX models, under Extras on the Home & extensions page)")
    if model_path() is None:
        return said("the colouring model is not downloaded (AI models page)")
    return None


def _load(setup: dict[str, Any]) -> dict[str, Any]:
    if why := ready():
        raise Unavailable(why)
    from .devices import image_providers                       # noqa: PLC0415
    from .siglip import _session                               # noqa: PLC0415
    path = model_path()
    started = time.monotonic()
    session = _session(path, int(setup.get("threads") or 2),
                       image_providers(setup.get("image_device")))
    log.warning("colouring model loaded on %s in %.1f s", session.get_providers()[0],
                time.monotonic() - started)
    return {"session": session, "input": session.get_inputs()[0].name}


def colour(kept: dict[str, Any], *, path: str, out: str, quality: int = 92) -> dict[str, Any]:
    """Colour the picture at *path* and write it to *out* as a JPEG."""
    from PIL import Image                                      # noqa: PLC0415

    from ..media import onnx_tools                             # noqa: PLC0415
    started = time.monotonic()
    session, name = kept["session"], kept["input"]
    with Image.open(path) as image:
        coloured = onnx_tools.colour_image(image, lambda grey: session.run(None, {name: grey})[0])
    coloured.save(out, "JPEG", quality=int(quality), optimize=True)
    return {"width": coloured.width, "height": coloured.height,
            "version": COLOUR_VERSION, "took": round(time.monotonic() - started, 2)}


COLOUR = Capability("colour", COLOUR_VERSION, _load, colour)
