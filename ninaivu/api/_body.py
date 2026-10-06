"""The JSON object a request sent, or a 400 that says what was wrong."""

from __future__ import annotations

import json
from typing import Any

from flask import abort, request

#: No settings or selection a screen sends comes near this (100,000 item ids
#: are under a megabyte). A body claiming more is refused before it is read
#: into memory, rather than parsed and then found wrong. The routes that take
#: a photograph as JSON (the AI Playground) read their own bodies with their
#: own limits and do not come through here.
JSON_MAX_BYTES = 4 * 1024 * 1024

#: Bytes asked of the request stream at a time.
_PIECE = 64 * 1024


def refuse_oversized_json() -> None:
    """A 413 for a JSON body larger than :data:`JSON_MAX_BYTES`, unread."""
    if (request.content_length or 0) > JSON_MAX_BYTES:
        abort(413, description="That is too large.")


def read_at_most(limit: int, description: str = "That is too large.") -> bytes:
    """The request body, or a 413 once it is past *limit* bytes.

    Checking the declared length alone let a body sent without one (chunked)
    through whole, up to the app-wide upload ceiling, which is hundreds of
    megabytes: it was read into memory and only then found too large, or not
    found too large at all. This reads no more than *limit* + 1 bytes, so
    what is held is bounded by the route's limit whatever the body claims.
    The bytes are kept on the request, so ``request.get_data()`` and
    ``get_json()`` still see them afterwards.
    """
    if (request.content_length or 0) > limit:
        abort(413, description=description)
    cached = getattr(request, "_cached_data", None)
    if cached is not None:
        if len(cached) > limit:
            abort(413, description=description)
        return cached
    stream = request.stream
    pieces: list[bytes] = []
    held = 0
    while held <= limit:
        piece = stream.read(min(_PIECE, limit + 1 - held))
        if not piece:
            break
        pieces.append(piece)
        held += len(piece)
    if held > limit:
        abort(413, description=description)
    raw = b"".join(pieces)
    request._cached_data = raw
    return raw


def json_body(limit: int = JSON_MAX_BYTES) -> Any:
    """What ``request.get_json(silent=True)`` gave — the parsed body, or None
    for a body that is not JSON — read no further than *limit* bytes."""
    if not request.is_json:
        return None
    raw = read_at_most(limit)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None


def json_object() -> dict[str, Any]:
    """The request body as a dict — an empty one when nothing was sent.

    Endpoints read their settings with ``data.get(...)``. Read as
    ``request.get_json(silent=True) or {}``, a body of ``[1]``, ``"on"`` or
    ``5`` reached that ``.get`` and answered 500: the server's fault, by its
    own account, for what was a malformed request.
    """
    data = json_body()
    if data is None:
        return {}
    if not isinstance(data, dict):
        abort(400, description="Send the settings as a JSON object.")
    return data
