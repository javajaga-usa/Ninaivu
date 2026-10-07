"""Which way up a photograph goes, decided by a model that was trained to say.

The face-based detector in :mod:`ninaivu.upright` can only speak about
photographs with a person in them, and about half a family library has nobody
in the frame at all — signs, scenery, the aquarium, a plate of food. Those are
sideways just as often, and no amount of cleverness with face rectangles will
ever reach them.

This is the other half of the answer: a small EfficientNetV2-S trained on
three-quarters of a million rotated photographs to answer one question — which
of the four quarter turns puts this picture the right way up. It runs through
OpenCV's DNN module, which Ninaivu already requires, so it costs one download
and no new dependency.

**It answers in one look.** The face detector had to rotate the picture four
times and compare; this reads the picture once and returns a probability for
each of the four answers. That is both faster and far steadier — the four
scores come from one model that has seen upside-down photographs on purpose,
not from four runs of a detector that was never told the picture might be
turned.

**It is asked, not obeyed.** The verdict carries a confidence and
:mod:`ninaivu.upright` decides what to do with it. A photograph whose camera
wrote a real orientation tag is never handed to this model at all.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

try:
    import cv2
except ImportError:                                   # pragma: no cover
    cv2 = None                                        # type: ignore[assignment]

try:
    import numpy as np
except ImportError:                                   # pragma: no cover
    np = None                                         # type: ignore[assignment]

__all__ = [
    "MODEL", "model_path", "model_status", "fetch_model", "available",
    "predict", "reset",
]

#: The model, pinned by hash.
#:
#: A checksum rather than trust in the host: a truncated download or a
#: substituted file is refused rather than quietly used to turn a library of
#: photographs. Same rule the face models are held to.
MODEL: dict[str, str] = {
    "file": "orientation_efficientnetv2s_v2.onnx",
    "url": ("https://github.com/duartebarbosadev/deep-image-orientation-detection"
            "/releases/download/v2/orientation_model_v2_0.9882.onnx"),
    "sha256": "cffe911c1dff47fbfbbd90110aaab9c07134645c460d35b3ae8832079bea91ba",
    "licence": "MIT",
    "about": "EfficientNetV2-S, 98.8% on its own validation set",
}

#: What the model was trained at. Resize to a little over, then centre-crop —
#: the same transform its author used, and getting it wrong costs accuracy
#: quietly rather than loudly.
IMAGE_SIZE = 384
_RESIZE = IMAGE_SIZE + 32
_MEAN = (0.485, 0.456, 0.406)
_STD = (0.229, 0.224, 0.225)

#: Output index → the clockwise turn that puts the picture right, which is the
#: same convention ``assets.rotation`` and the EXIF table already use.
_CLASS_ROTATION = {0: 0, 1: 90, 2: 180, 3: 270}

#: One network per thread. ``cv2.dnn.Net.forward`` writes into blobs the net
#: owns, so a single shared net called from the scan pool is the same class of
#: crash the face cascades had. Loading costs a few hundred milliseconds once
#: per worker.
_local = threading.local()
_dir_lock = threading.Lock()
_model_dir: Path | None = None


def configure(models_dir: Path | str) -> None:
    """Point the module at the folder that holds the model file.

    The folder itself, not the state directory it used to be derived from:
    the orientation model now lives with every other AI model, so that a
    machine move is one folder to copy rather than models in two places.
    """
    global _model_dir
    with _dir_lock:
        _model_dir = Path(models_dir)


def model_path(models_dir: Path | str | None = None) -> Path:
    directory = Path(models_dir) if models_dir is not None else _model_dir
    if directory is None:
        raise RuntimeError("ninaivu.orientnet.configure() has not been called")
    return directory / MODEL["file"]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def model_status(models_dir: Path | str | None = None) -> dict[str, Any]:
    """What is on disk, and whether this OpenCV can run it. Downloads nothing."""
    try:
        path = model_path(models_dir)
    except RuntimeError:
        return {"present": False, "ready": False, "reason": "no state directory"}
    present = path.exists() and path.stat().st_size > 0
    support = _opencv_support()
    return {
        "file": MODEL["file"],
        "path": str(path),
        "present": present,
        "bytes": path.stat().st_size if present else 0,
        "url": MODEL["url"],
        "licence": MODEL["licence"],
        "about": MODEL["about"],
        "opencv": support,
        "ready": present and support["available"],
    }


def _opencv_support() -> dict[str, Any]:
    if cv2 is None:
        return {"available": False, "reason": "opencv is not installed"}
    if np is None:
        return {"available": False, "reason": "numpy is not installed"}
    if not hasattr(cv2, "dnn"):
        return {"available": False,
                "version": getattr(cv2, "__version__", "?"),
                "reason": "this OpenCV build has no dnn module"}
    return {"available": True, "version": getattr(cv2, "__version__", "?")}


def fetch_model(models_dir: Path | str | None = None, *, force: bool = False,
                progress: Any = None, timeout: float = 120.0) -> dict[str, Any]:
    """Download the model if it is missing. Safe to call repeatedly.

    77 MB, once, into the shared model folder beside the face models and
    the search model — so that moving Ninaivu to another machine carries
    every model in one folder.
    """
    path = model_path(models_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 0 and not force:
        return {"kept": True, "downloaded": False, "ready": True,
                "path": str(path)}

    tmp = path.with_suffix(path.suffix + ".part")
    try:
        if progress:
            progress(f"Downloading {MODEL['file']} (77 MB)…")
        request = urllib.request.Request(MODEL["url"],
                                         headers={"User-Agent": "Ninaivu"})
        with urllib.request.urlopen(request, timeout=timeout) as response, \
                open(tmp, "wb") as handle:
            shutil.copyfileobj(response, handle, 1 << 20)
        actual = _sha256(tmp)
        if actual != MODEL["sha256"]:
            tmp.unlink(missing_ok=True)
            return {"downloaded": False, "ready": False,
                    "error": f"checksum mismatch (got {actual[:12]}…)"}
        tmp.replace(path)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        tmp.unlink(missing_ok=True)
        return {"downloaded": False, "ready": False, "error": str(exc)}
    return {"downloaded": True, "kept": False, "ready": True, "path": str(path)}


def reset() -> None:
    """Drop this thread's network — used by the tests, and after a re-download."""
    _local.net = None
    _local.path = None


def _net(models_dir: Path | str | None = None):
    """This thread's network, or None when it cannot be had."""
    if not _opencv_support()["available"]:
        return None
    try:
        path = model_path(models_dir)
    except RuntimeError:
        reset()
        return None

    # A network belongs to the file it was loaded from. When _model_dir changes
    # (or a folder is passed), threads must discard stale nets from prior paths.
    if getattr(_local, "path", None) != path:
        reset()

    if not path.exists():
        return None

    cached = getattr(_local, "net", None)
    if cached is not None:
        return cached

    signature = _signature(path)
    if signature is not None and signature in _unloadable:
        return None
    try:
        net = cv2.dnn.readNetFromONNX(str(path))
    except Exception as exc:                          # noqa: BLE001
        log.warning("orientation model could not be loaded: %s", exc)
        if signature is not None:
            _unloadable.add(signature)
        return None
    _local.net = net
    _local.path = path
    return net


#: Model files (path, size, modification time) that OpenCV has refused to
#: load. The same file is not tried again, on any thread; a new download is a
#: new size or time, and is tried afresh.
_unloadable: set[tuple[str, int, float]] = set()


def _signature(path: Path) -> tuple[str, int, float] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return str(path), st.st_size, st.st_mtime


def available(models_dir: Path | str | None = None) -> bool:
    """Whether the model is here and this OpenCV can run it.

    Answered from the file, not by loading it: this is asked at the end of
    every scan, on a thread that ends with the scan, and loading the 77 MB
    network there only to throw it away cost a second and a hundred
    megabytes each time. A file OpenCV has already refused counts as absent.
    """
    if not _opencv_support()["available"]:
        return False
    try:
        path = model_path(models_dir)
    except RuntimeError:
        return False
    signature = _signature(path)
    return bool(signature and signature[1] > 0 and signature not in _unloadable)


def _blob(img):
    """The picture as the model wants it: RGB, 384², ImageNet-normalised, NCHW."""
    im = img.convert("RGB").resize((_RESIZE, _RESIZE))
    off = (_RESIZE - IMAGE_SIZE) // 2
    im = im.crop((off, off, off + IMAGE_SIZE, off + IMAGE_SIZE))
    arr = np.asarray(im, dtype=np.float32) / 255.0
    arr = (arr - np.array(_MEAN, dtype=np.float32)) / np.array(_STD, dtype=np.float32)
    return arr.transpose(2, 0, 1)[None, ...].copy()


def predict(img, models_dir: Path | str | None = None) -> tuple[int, float]:
    """``(clockwise turn, confidence)``. ``(0, 0.0)`` when the model cannot run.

    Confidence is the softmax probability of the winning answer, so 0.25 is a
    shrug and anything near 1.0 is the model being certain. It is never
    clamped or rescaled: the thresholds live with the policy, in
    :mod:`ninaivu.upright`, where they can be read next to the rule they serve.
    """
    net = _net(models_dir)
    if net is None:
        return 0, 0.0
    try:
        net.setInput(_blob(img))
        raw = net.forward().ravel()
    except Exception as exc:                          # noqa: BLE001
        log.debug("orientation model failed on one image: %s", exc)
        return 0, 0.0
    shifted = np.exp(raw - raw.max())
    probs = shifted / shifted.sum()
    index = int(probs.argmax())
    return _CLASS_ROTATION.get(index, 0), float(probs[index])
