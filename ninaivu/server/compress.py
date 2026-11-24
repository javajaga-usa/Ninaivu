"""Compressing text answers (gzip) for a client that can take them.

Part of the web app's responses; ``ninaivu._gzip_text`` is the same function
under its old name, which the app and its tests call.
"""
from __future__ import annotations

import threading

from flask import request

#: A text answer smaller than this goes as it is: a status line gains nothing
#: from gzip that pays for its header and the time.
_GZIP_MIN_BYTES = 2048
#: No file larger than this is compressed on the fly: a large file sent as
#: text by mistake would otherwise be read whole into memory.
_GZIP_MAX_BYTES = 8 * 1024 * 1024
#: An answer already built in memory is compressed up to this size. Held to
#: the file limit, the whole gallery's layout of an administrator's 208,000
#: items (8.6 MB of JSON, 1.5 MB gzipped) went to their phone uncompressed.
_GZIP_MAX_BUILT_BYTES = 64 * 1024 * 1024
#: What is worth compressing: pictures and videos are compressed already.
_GZIP_TYPES = ("text/", "application/json", "application/javascript",
               "image/svg+xml", "application/manifest+json")
#: Static files are compressed once per version, at the best level, and kept.
_gzip_static: dict[str, tuple[str, bytes]] = {}
_gzip_static_lock = threading.Lock()
_GZIP_STATIC_LIMIT = 96


def gzip_text(response) -> None:  # noqa: ANN001
    """Compress a text answer for a client that says it can take one.

    The gallery's layout for 25,000 items is 1.2 MB of JSON and 140 KB
    gzipped, for about 13 ms of work. On the household's Wi-Fi either arrives
    quickly; on a phone reaching Ninaivu over the tailnet from outside the
    house, it is the difference between half a second a page and a twentieth
    of one. API answers are compressed as they go, at level 5 because the
    levels above it cost twice the time for a few per cent. The pages,
    scripts, styles and the Tamil strings — a few megabytes on a phone's
    first visit — are compressed once per version at level 9 and kept
    (from Ninaivu Lite).
    """
    import gzip

    mimetype = response.mimetype or ""
    if (response.status_code != 200
            or (response.is_streamed and not response.direct_passthrough)
            or "Content-Encoding" in response.headers
            or not mimetype.startswith(_GZIP_TYPES)
            or request.accept_encodings["gzip"] <= 0):
        return
    largest = _GZIP_MAX_BYTES if response.direct_passthrough else _GZIP_MAX_BUILT_BYTES
    length = response.content_length
    if length is not None and not _GZIP_MIN_BYTES <= length <= largest:
        return
    response.direct_passthrough = False
    data = response.get_data()
    if not _GZIP_MIN_BYTES <= len(data) <= largest:
        return
    static = request.path.startswith("/static/")
    etag = response.get_etag()[0] or ""
    if static and etag:
        with _gzip_static_lock:
            hit = _gzip_static.get(request.path)
        if hit and hit[0] == etag:
            packed = hit[1]
        else:
            packed = gzip.compress(data, compresslevel=9, mtime=0)
            with _gzip_static_lock:
                if len(_gzip_static) >= _GZIP_STATIC_LIMIT:
                    _gzip_static.pop(next(iter(_gzip_static)))
                _gzip_static[request.path] = (etag, packed)
    else:
        packed = gzip.compress(data, compresslevel=9 if static else 5, mtime=0)
    if len(packed) >= len(data):
        return
    response.set_data(packed)
    response.headers["Content-Encoding"] = "gzip"
    response.vary.add("Accept-Encoding")
    if etag:
        response.set_etag(f"{etag}-gz")
