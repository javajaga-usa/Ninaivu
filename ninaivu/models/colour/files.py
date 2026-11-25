"""Where the colouring model is looked for (P16: this was in media/onnx_tools.py)."""
from __future__ import annotations

import os
from pathlib import Path

from ...media import model_catalog
from ...media.ai_editing import local_setting

#: The file names a hand-placed colourising model is looked for under, in the
#: models folder's ``onnx`` subfolder, in this order.
COLORIZE_FILES = ("ddcolor.onnx", "ddcolor_artistic.onnx")


def colorize_model_path() -> Path | None:
    """Where the colourising model is, or None: the environment or the setting
    first, then the usual names in the models folder."""
    configured = os.environ.get("NINAIVU_COLORIZE_MODEL", local_setting("colorize_model"))
    if configured:
        return Path(configured).expanduser()
    for name in COLORIZE_FILES:
        candidate = model_catalog.models_root() / "onnx" / name
        if candidate.is_file():
            return candidate
    return None
