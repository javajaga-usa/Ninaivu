"""Local object removal via OpenCV inpainting. Never sends photographs anywhere but this machine.

Classical inpainting (Telea) rather than a generative model: it needs no download,
works the moment opencv is installed, and is well suited to the kind of small-to-medium
stray objects, blemishes, and watermarks this tool paints over — not a full scene edit.
"""
from __future__ import annotations

import io
import threading

import numpy as np
from PIL import Image

from .safe_image import open_untrusted

_MAX_PIXELS = 1600 * 1600
_slot = threading.BoundedSemaphore(1)


def available():
    try:
        import cv2  # noqa: F401
    except ImportError:
        return False
    return True


def remove(image_bytes, mask_bytes, radius=6):
    try:
        import cv2
    except ImportError as error:
        raise RuntimeError(
            'Object removal needs opencv-python-headless, already listed in requirements.txt. '
            'Install it to enable this feature.') from error
    if not _slot.acquire(blocking=False):
        raise RuntimeError('Another object-removal request is running. Try again when it finishes.')
    try:
        with Image.open(io.BytesIO(image_bytes)) as source:
            if source.width * source.height > _MAX_PIXELS:
                raise ValueError('Use a photo preview up to 1600 pixels on a side.')
            image = np.array(source.convert('RGB'))
        with open_untrusted(mask_bytes, _MAX_PIXELS) as source_mask:
            painted = source_mask.convert('L').resize((image.shape[1], image.shape[0]), Image.NEAREST)
            mask_array = np.array(painted)
        # Anything painted counts fully; a soft brush edge should not leave a half-erased halo.
        mask_array = np.where(mask_array > 24, 255, 0).astype('uint8')
        if not mask_array.any():
            raise ValueError('Paint over the object to remove before applying.')
        bgr = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
        result = cv2.inpaint(bgr, mask_array, radius, cv2.INPAINT_TELEA)
        rgb = cv2.cvtColor(result, cv2.COLOR_BGR2RGB)
        output = io.BytesIO()
        Image.fromarray(rgb).save(output, 'PNG')
        return output.getvalue()
    finally:
        _slot.release()
