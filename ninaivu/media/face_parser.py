"""An optional face-parsing network, for the faces the built-in method cannot separate.

The built-in selection of skin and hair (``portrait.py``) needs nothing but the
face detector the household already has: it reads each person's own skin from
their cheeks and forehead and finds hair from its colour and its position. It is
honest about where that stops — black hair against a black wall, brown hair
against a brown one, a turban, a face half in shadow — and says "not sure"
instead of guessing. A *face-parsing* network does better at exactly those
places, because it has been shown what a head is: it labels every pixel of a
face as skin, brow, eye, lip, ear, neck, hair or hat.

This is that network, used only when it is there. Nothing depends on it, and
without it everything works as it did:

* the model is an ordinary ONNX file, run by OpenCV, so no new Python package is
  needed;
* it is looked for in the AI models folder, as ``faceparse/face_parsing.onnx``,
  and is used if it loads and gives an answer of the right shape;
* every answer is checked against the face it is for, and a face it gets
  nonsense for is left to the built-in method rather than trusted.

Its labels are CelebAMask-HQ's nineteen, in the order BiSeNet-style networks
write them. It does not know what a bindi, sindoor or sacred ash is — they are
skin to it — and it has no beard: both stay with the built-in method, which
finds them by colour. So it replaces the choice of *where skin and hair are* and
nothing else.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
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

__all__ = ["FaceParser", "get", "model_path", "LABELS", "SKIN", "FEATURES", "HAIR", "NECK"]

#: CelebAMask-HQ's labels, by number.
LABELS = ("background", "skin", "l_brow", "r_brow", "l_eye", "r_eye", "eye_g", "l_ear", "r_ear",
          "ear_r", "nose", "mouth", "u_lip", "l_lip", "neck", "neck_l", "cloth", "hair", "hat")

#: Skin: the skin itself, the nose and the ears.
SKIN = (1, 10, 7, 8)
#: What is in a face and is not skin: brows, eyes, glasses, mouth and lips.
FEATURES = (2, 3, 4, 5, 6, 11, 12, 13)
HAIR = (17,)
NECK = (14,)

#: The size the network is shown, and what it was trained to expect of a picture.
INPUT = 512
MEAN = (0.485, 0.456, 0.406)
STD = (0.229, 0.224, 0.225)


def model_path() -> Path:
    from . import model_catalog                        # noqa: PLC0415
    return model_catalog.models_root() / "faceparse" / "face_parsing.onnx"


class FaceParser:
    """One loaded network. Thread-confined by a lock: OpenCV's networks are not safe to share."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._net = cv2.dnn.readNetFromONNX(str(self.path))

    def probabilities(self, rgb, origin=None):
        """For an RGB picture of a face, how likely each pixel is to be each of the nineteen things.

        Returns a float32 array ``(19, height, width)`` whose columns sum to one, or
        ``None`` if the network did not give an answer of that shape. *origin* says
        where the picture was cut from, which the network has no use for and a
        stand-in in a test does.
        """
        height, width = rgb.shape[:2]
        if height < 16 or width < 16:
            return None
        interpolation = cv2.INTER_AREA if max(height, width) > INPUT else cv2.INTER_LINEAR
        shown = cv2.resize(rgb, (INPUT, INPUT), interpolation=interpolation).astype(np.float32) / 255.0
        shown = (shown - np.array(MEAN, np.float32)) / np.array(STD, np.float32)
        blob = cv2.dnn.blobFromImage(shown, 1.0, (INPUT, INPUT), (0, 0, 0), swapRB=False, crop=False)
        with self._lock:
            self._net.setInput(blob)
            raw = np.asarray(self._net.forward())
        if raw.ndim == 4 and raw.shape[0] == 1:
            raw = raw[0]
        if raw.ndim != 3 or raw.shape[0] != len(LABELS):
            return None
        raw = raw - raw.max(axis=0, keepdims=True)
        exp = np.exp(raw)
        probabilities = exp / exp.sum(axis=0, keepdims=True)
        if probabilities.shape[1:] != (height, width):
            probabilities = cv2.resize(probabilities.transpose(1, 2, 0), (width, height),
                                       interpolation=cv2.INTER_LINEAR).transpose(2, 0, 1)
        return np.ascontiguousarray(probabilities, dtype=np.float32)


_cache: dict[str, Any] = {"path": None, "parser": None, "tried": False}
_cache_lock = threading.Lock()


def get() -> FaceParser | None:
    """The parser, if the model is installed and loads; ``None`` otherwise, quietly, once."""
    if cv2 is None or np is None:
        return None
    path = model_path()
    with _cache_lock:
        if _cache["path"] == str(path) and _cache["tried"]:
            return _cache["parser"]
        _cache.update(path=str(path), parser=None, tried=True)
        if not path.is_file():
            return None
        try:
            _cache["parser"] = FaceParser(path)
        except Exception as exc:                        # noqa: BLE001
            log.warning("the face-parsing model at %s did not load: %s", path, exc)
        return _cache["parser"]


def forget() -> None:
    """Look for the model again next time (it has just been installed, or removed)."""
    with _cache_lock:
        _cache.update(path=None, parser=None, tried=False)
