"""Optional local background segmentation (BRIA RMBG-1.4 ONNX). Never sends photographs anywhere but this machine."""
from __future__ import annotations

import io
import os
import threading
from pathlib import Path

import numpy as np
from PIL import Image

from .ai_editing import local_setting

#: Fixed square input the RMBG-1.4 ONNX graph expects.
_INPUT_SIZE = 1024
_MAX_PIXELS = 1600 * 1600

_slot = threading.BoundedSemaphore(1)
_session = None
_session_path = None
_session_lock = threading.Lock()


def model_path():
    configured = os.environ.get('NINAIVU_SEGMENT_MODEL', local_setting('segmentation_model'))
    return Path(configured) if configured else None


def available():
    """Whether background removal/blur can run right now, without loading the model."""
    path = model_path()
    if not path or not path.is_file():
        return False
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return False
    return True


def _load():
    global _session, _session_path
    path = model_path()
    if not path or not path.is_file():
        # Both of these used to name a command-line tool and a requirements
        # file. The console installs both now, and somebody who has just hit
        # this in the Playground is in the console already.
        raise RuntimeError(
            'Background removal is not installed. In the admin console, open AI '
            'models and download "Background removal (RMBG-1.4)".')
    try:
        import onnxruntime as ort
    except ImportError as error:
        raise RuntimeError(
            'Background removal needs the ONNX runtime. In the admin console, open '
            'Extras, install "Background removal and the other ONNX models", then '
            'restart Ninaivu.') from error
    with _session_lock:
        if _session is None or _session_path != path:
            from ..utils.resources import compute_threads
            options = ort.SessionOptions()
            options.intra_op_num_threads = compute_threads()
            _session = ort.InferenceSession(str(path.resolve()), sess_options=options,
                                             providers=['CPUExecutionProvider'])
            _session_path = path
        return _session


def mask(image_bytes):
    """Return a single-channel PNG mask, same dimensions as the source, where white marks the foreground subject."""
    if not _slot.acquire(blocking=False):
        raise RuntimeError('Another background operation is running. Try again when it finishes.')
    try:
        with Image.open(io.BytesIO(image_bytes)) as source:
            if source.width * source.height > _MAX_PIXELS:
                raise ValueError('Use a photo preview up to 1600 pixels on a side.')
            image = source.convert('RGB')
        session = _load()
        resized = image.resize((_INPUT_SIZE, _INPUT_SIZE), Image.BILINEAR)
        array = np.asarray(resized, dtype=np.float32) / 255.0
        array = (array - 0.5) / 1.0  # RMBG-1.4 preprocessing: zero-centered, unit scale.
        tensor = np.transpose(array, (2, 0, 1))[None, ...].astype(np.float32)
        input_name = session.get_inputs()[0].name
        outputs = session.run(None, {input_name: tensor})
        result = np.asarray(outputs[0], dtype=np.float32)
        result = result.reshape(result.shape[-2], result.shape[-1])
        low, high = result.min(), result.max()
        if high > low:
            result = (result - low) / (high - low)
        gray = Image.fromarray((result * 255).astype('uint8'), mode='L').resize(image.size, Image.BILINEAR)
        output = io.BytesIO()
        gray.save(output, 'PNG')
        return output.getvalue()
    finally:
        _slot.release()
