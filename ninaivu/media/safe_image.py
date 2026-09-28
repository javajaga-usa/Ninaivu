"""Opening an image somebody sent us, without letting its header size the server.

Pillow's decompression-bomb guard only *raises* above twice ``MAX_IMAGE_PIXELS``,
and Ninaivu sets that generously so real panoramas in the library still index.
For bytes arriving in a request that is far too generous: a 6 MB PNG can
declare 30000 x 30000 pixels and cost gigabytes the moment it is decoded. The
header is read at open, before any pixel is, so the size is checked there.
"""

from __future__ import annotations

import io

from PIL import Image

#: Enough for any photograph a phone or camera takes today.
UNTRUSTED_MAX_PIXELS = 64_000_000


def open_untrusted(data: bytes, max_pixels: int = UNTRUSTED_MAX_PIXELS) -> Image.Image:
    """``Image.open`` on *data*, refused with ValueError if it declares too many pixels."""
    try:
        image = Image.open(io.BytesIO(data))
    except Image.DecompressionBombError as error:
        raise ValueError("That image is too large to open.") from error
    width, height = image.size
    if width * height > max_pixels:
        image.close()
        raise ValueError("That image is too large to open.")
    return image
