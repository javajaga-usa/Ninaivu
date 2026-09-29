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


def check_untrusted_file(path, max_pixels: int | None = None) -> None:
    """Refuse with ValueError a file on disk that declares too many pixels.

    For a file that arrived in a request and has already been written out — an
    upload, a phone backup — and is about to be decoded to be indexed. Only the
    header is read.

    The limit is the library's own, ``Image.MAX_IMAGE_PIXELS`` as
    :mod:`ninaivu.media.media` sets it, for every format — not the avatar cap
    :func:`open_untrusted` uses. What this stops is a decompression bomb, a
    small file declaring a vast image; a 200-megapixel HEIF from a phone or a
    large TIFF scan is a real photograph and has to be accepted, and anything
    the indexer would decode anyway is no worse for having been uploaded.

    Anything Pillow cannot open at all (a video, a document, a format whose
    plugin is not installed) is let through: it will not be decoded as a
    picture either, and whatever does handle it has its own limits.
    """
    limit = max_pixels if max_pixels is not None else Image.MAX_IMAGE_PIXELS
    # Pillow's bomb warning is caught with its error: where warnings are turned
    # into errors it is raised at open, and must not pass for a file Pillow
    # cannot read.
    try:
        with Image.open(path) as image:
            width, height = image.size
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise ValueError("That image is too large to open.") from error
    except Exception:                              # noqa: BLE001 — not a picture Pillow knows
        return
    if limit and width * height > limit:
        raise ValueError("That image is too large to open.")
